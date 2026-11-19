"""Collects Google Flights prices into Postgres with a real browser: python -m collector.

A plain request for the search page gets about 50 results, the ones Google puts on the page before any script
runs. This opens the search in headless Chromium and reads the results the page then asks Google for, the whole
list (about 210 for Seoul to Tokyo), top flights included. See browser.py.

What to search is in config.py (in k8s the ConfigMap k8s/configmap.yaml), the tables in schema.sql. One more
setting is read here:

    PAGE_TIMEOUT_SECONDS   60   how long a search's page may take to finish

Google's page is in English (LANGUAGE): airline names are saved as Google writes them in English.
"""

import logging
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import db, parse, query
from .browser import Browser
from .config import KST, departure_dates, load_config

log = logging.getLogger("collector")

LANGUAGE = "en-US"


def legs(origin: str, destination: str, depart, return_date) -> list[query.Leg]:
    out = [query.Leg(depart, origin, destination)]
    if return_date:
        out.append(query.Leg(return_date, destination, origin))
    return out


def page_timeout(env=os.environ) -> float:
    raw = env.get("PAGE_TIMEOUT_SECONDS", "60").strip()
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"bad config:\n  PAGE_TIMEOUT_SECONDS={raw!r} is not a number") from None
    if not 10 <= value <= 300:
        raise ValueError(f"bad config:\n  PAGE_TIMEOUT_SECONDS={raw!r} is out of 10..300")
    return value


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    try:
        config = load_config()
        timeout = page_timeout()
    except ValueError as e:
        log.error("%s", e)
        return 2
    log.info("config: %s, page timeout %ss", config, timeout)

    dates = departure_dates(config, datetime.now(KST).date())
    # a search per route, departure date and, for round trips, length of stay
    returns = [timedelta(days=n) for n in config.stay_nights] or [None]
    jobs = [
        (origin, destination, d, d + stay if stay else None)
        for origin, destination in config.routes
        for d in dates
        for stay in returns
    ]
    if not jobs:
        log.warning("every departure date has passed, nothing to search")
        return 0
    random.shuffle(jobs)

    # schema first, so a bad DATABASE_URL fails the run before any request to Google
    if not config.dry_run:
        with db.connect() as conn:
            db.ensure_schema(conn)

    wait = random.uniform(0, config.start_jitter_max)
    log.info("%d searches, starting in %.0fs%s", len(jobs), wait, ", dry run" if config.dry_run else "")
    time.sleep(wait)

    run_id = uuid.uuid4()
    counts: dict[str, int] = {}
    failures_in_row = 0
    with Browser(LANGUAGE, timeout) as browser:
        for i, (origin, destination, depart, return_date) in enumerate(jobs):
            if i:
                time.sleep(random.uniform(config.delay_min, config.delay_max))

            fetched_at = datetime.now(timezone.utc)
            url = query.search_url(
                legs(origin, destination, depart, return_date), config.trip, config.seat, config.adults,
                config.max_stops, config.currency, LANGUAGE,
            )
            page = browser.load(url)
            flights, error = [], page.error
            status = page.status
            if page.status == "ok":
                flights, skipped = parse.flights(page.payload, depart)
                if skipped:
                    log.warning("skipped %d results: %s", len(skipped), "; ".join(skipped))
                    error = f"skipped {len(skipped)} invalid results: {'; '.join(skipped)}"[:1000]
                unmatched = sum(1 for f in flights if f.airlines and not any(leg.flight_number for leg in f.legs))
                if unmatched:
                    log.warning("%d results' leg carriers don't match their airlines, saved without them", unmatched)

            counts[status] = counts.get(status, 0) + 1
            prices = [f.price for f in flights if f.price is not None]
            log.info(
                "%s->%s %s%s: %s, %d flights (%d priced), cheapest %s %s",
                origin, destination, depart, f" back {return_date}" if return_date else "", status, len(flights),
                len(prices), min(prices) if prices else "-", error or "",
            )

            # a new connection per save: the pauses are longer than an idle connection should be trusted to live
            if not config.dry_run:
                with db.connect() as conn:
                    db.save(conn, config, run_id, fetched_at, origin, destination, depart, return_date, status,
                            flights, error)

            failures_in_row = failures_in_row + 1 if status in ("blocked", "error") else 0
            if failures_in_row >= config.max_consecutive_failures:
                log.error("%d failed searches in a row, stopping this run", failures_in_row)
                break

    log.info("run %s done: %s", run_id, counts)
    # a run that got nothing is a failed job, so it shows in kubectl and alerts
    return 0 if counts.get("ok", 0) + counts.get("not_found", 0) > 0 else 1

