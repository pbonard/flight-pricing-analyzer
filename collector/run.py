"""Collects Google Flights prices into Postgres, by default round trips from Seoul to Tokyo and Osaka in economy.
Run with python -m collector.

Searches by city code (SEL, TYO, OSA): one search covers every airport of both cities (ICN/GMP, NRT/HND, KIX/ITM),
and each result keeps its own airports. What to search is in config.py, how a search works in search.py.

Run every 2 hours by the CronJob in k8s/cronjob.yaml. A run is one search per (route, departure date, stay): it starts
after a random delay, searches in random order and pauses a random while between searches, so the traffic
doesn't come at the same minute with the same rhythm every time. It stops early once Google starts blocking.

Saves each search as it's done, through db.py.
"""

import logging
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import db
from .config import KST, departure_dates, load_config
from .search import search

log = logging.getLogger("collector")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    try:
        config = load_config()
    except ValueError as e:
        log.error("%s", e)
        return 2
    log.info("config: %s", config)

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
    for i, (origin, destination, depart, return_date) in enumerate(jobs):
        if i:
            time.sleep(random.uniform(config.delay_min, config.delay_max))

        fetched_at = datetime.now(timezone.utc)
        status, flights, error = search(config, origin, destination, depart, return_date)
        counts[status] = counts.get(status, 0) + 1
        prices = [f.price for _, f, _ in flights if f.price is not None]
        log.info(
            "%s->%s %s%s: %s, %d flights, cheapest %s %s",
            origin, destination, depart, f" back {return_date}" if return_date else "", status, len(flights),
            min(prices) if prices else "-", error or "",
        )

        # a new connection per save: the pauses are longer than an idle connection should be trusted to live
        if not config.dry_run:
            with db.connect() as conn:
                db.save(
                    conn, config, run_id, fetched_at, origin, destination, depart, return_date, status, flights, error
                )

        failures_in_row = failures_in_row + 1 if status in ("blocked", "error") else 0
        if failures_in_row >= config.max_consecutive_failures:
            log.error("%d failed searches in a row, stopping this run", failures_in_row)
            break

    log.info("run %s done: %s", run_id, counts)
    # a run that got nothing is a failed job, so it shows in kubectl and alerts
    return 0 if counts.get("ok", 0) + counts.get("not_found", 0) > 0 else 1
