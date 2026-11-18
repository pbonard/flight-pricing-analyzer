"""Collects Google Flights one-way economy prices from Seoul to Tokyo and Osaka into Postgres.

Searches by city code (SEL, TYO, OSA): one search covers every airport of both cities (ICN/GMP, NRT/HND, KIX/ITM),
and each result keeps its own airports.

Run every 2 hours by the CronJob in k8s/cronjob.yaml. A run is one search per (route, departure date): it starts
after a random delay, searches in random order and pauses a random while between searches, so the traffic
doesn't come at the same minute with the same rhythm every time. It stops early once Google starts blocking.

Writes to the CloudNativePG cluster of DATABASE_URL, creating the tables of schema.sql if they aren't there.
"""

import hashlib
import logging
import os
import random
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, fetch_flights_html
from fast_flights.parser import parse
from psycopg.types.json import Jsonb

log = logging.getLogger("collector")

KST = ZoneInfo("Asia/Seoul")
CURRENCY = "KRW"

# what Google answers instead of results when it takes the traffic for a bot
BLOCK_MARKERS = ("unusual traffic", "/sorry/", "captcha", "detected unusual")


def env_list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def departure_dates(today: date) -> list[date]:
    """DEPART_DATES are fixed dates to follow, DAYS_AHEAD are dates a number of days from today.
    Fixed dates show how one trip's price moves up to the day, days ahead show how far out is cheapest."""
    dates = {date.fromisoformat(d) for d in env_list("DEPART_DATES", "")}
    dates |= {today + timedelta(days=int(n)) for n in env_list("DAYS_AHEAD", "")}
    # a fixed date that has passed has nothing left to sell
    return sorted(d for d in dates if d >= today)


def itinerary_key(f) -> str:
    """The same flights from one search to the next. Google gives no flight numbers, the airlines and each leg's
    airports and departure time stand in for them."""
    legs = "|".join(
        f"{leg.from_airport.code}{leg.to_airport.code}{date(*leg.departure.date).isoformat()}"
        f"T{leg.departure.time[0]:02d}{leg.departure.time[1]:02d}"
        for leg in f.flights
    )
    return hashlib.sha1(f"{','.join(f.airlines)}#{legs}".encode()).hexdigest()[:16]


def local_datetime(d) -> datetime:
    """A leg's departure or arrival, in the airport's local time as Google shows it, without a zone."""
    return datetime(*d.date, *d.time)


def search(origin: str, destination: str, depart: date, proxy: str | None):
    """Returns (status, offers, error): status is ok, not_found, blocked or error."""
    query = create_query(
        flights=[FlightQuery(date=depart.isoformat(), from_airport=origin, to_airport=destination)],
        seat="economy",
        trip="one-way",
        passengers=Passengers(adults=1),
        language="en-US",
        currency=CURRENCY,
    )
    try:
        html = fetch_flights_html(query, proxy=proxy)
    except Exception as e:  # network errors, timeouts
        return "error", [], f"fetch: {e!r}"[:1000]

    # the results come in the ds:1 script. a normal page mentions captcha too (a /recaptcha/ route list),
    # so the block words are only looked for on a page that has no results script
    if "ds:1" not in html:
        lowered = html.lower()
        if any(m in lowered for m in BLOCK_MARKERS):
            return "blocked", [], None
        return "error", [], f"no results script in a {len(html)} character page"

    try:
        return "ok", list(parse(html)), None
    except FlightsNotFound:
        return "not_found", [], None
    except Exception as e:  # Google changed the page, fast-flights needs an update
        return "error", [], f"parse: {e!r}"[:1000]


def offer_row(search_id, rank: int, f) -> tuple:
    first, last = f.flights[0], f.flights[-1]
    legs = [
        {
            "from": leg.from_airport.code,
            "to": leg.to_airport.code,
            "depart": local_datetime(leg.departure).isoformat(),
            "arrive": local_datetime(leg.arrival).isoformat(),
            "duration_min": leg.duration,
            "plane": leg.plane_type,
        }
        for leg in f.flights
    ]
    return (
        search_id,
        rank,
        first.from_airport.code,
        last.to_airport.code,
        f.price,
        f.airlines,
        len(f.flights) - 1,
        local_datetime(first.departure),
        local_datetime(last.arrival),
        sum(leg.duration for leg in f.flights),
        f.carbon.emission if f.carbon else None,
        f.carbon.typical_on_route if f.carbon else None,
        itinerary_key(f),
        Jsonb(legs),
    )


def save(conn, run_id, fetched_at, origin, destination, depart, status, offers, error) -> None:
    with conn.transaction():
        search_id = conn.execute(
            """INSERT INTO flight_search
                   (run_id, fetched_at, origin, destination, depart_date, currency, status, error, offer_count)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (run_id, fetched_at, origin, destination, depart, CURRENCY, status, error, len(offers)),
        ).fetchone()[0]
        if offers:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO flight_offer
                           (search_id, rank, from_airport, to_airport, price, airlines, stops, depart_at, arrive_at,
                            flying_min, carbon_g, typical_carbon_g, itinerary_key, legs)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [offer_row(search_id, rank, f) for rank, f in enumerate(offers)],
                )


def ensure_schema(conn) -> None:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")) as f:
        conn.execute(f.read())


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    origins = env_list("ORIGINS", "SEL")
    destinations = env_list("DESTINATIONS", "TYO,OSA")
    proxy = os.environ.get("PROXY") or None
    start_jitter = env_float("START_JITTER_MAX_SECONDS", 1200)
    delay_min = env_float("REQUEST_DELAY_MIN_SECONDS", 20)
    delay_max = env_float("REQUEST_DELAY_MAX_SECONDS", 90)
    # blocks come in a row once they start, more requests only make the block last longer
    max_blocked = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", 3))

    dates = departure_dates(datetime.now(KST).date())
    jobs = [(o, d, dt) for o in origins for d in destinations for dt in dates]
    if not jobs:
        log.warning("no departure dates to search, set DEPART_DATES or DAYS_AHEAD")
        return 0
    random.shuffle(jobs)

    # schema first, so a bad DATABASE_URL fails the run before any request to Google
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        ensure_schema(conn)

    wait = random.uniform(0, start_jitter)
    log.info("%d searches, starting in %.0fs", len(jobs), wait)
    time.sleep(wait)

    run_id = uuid.uuid4()
    counts: dict[str, int] = {}
    failures_in_row = 0
    for i, (origin, destination, depart) in enumerate(jobs):
        if i:
            time.sleep(random.uniform(delay_min, delay_max))

        fetched_at = datetime.now(timezone.utc)
        status, offers, error = search(origin, destination, depart, proxy)
        counts[status] = counts.get(status, 0) + 1
        prices = [f.price for f in offers if f.price is not None]
        log.info(
            "%s->%s %s: %s, %d offers, cheapest %s %s",
            origin, destination, depart, status, len(offers), min(prices) if prices else "-", error or "",
        )

        # a new connection per save: the pauses are longer than an idle connection should be trusted to live
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            save(conn, run_id, fetched_at, origin, destination, depart, status, offers, error)

        failures_in_row = failures_in_row + 1 if status in ("blocked", "error") else 0
        if failures_in_row >= max_blocked:
            log.error("%d failed searches in a row, stopping this run", failures_in_row)
            break

    log.info("run %s done: %s", run_id, counts)
    # a run that got nothing is a failed job, so it shows in kubectl and alerts
    return 0 if counts.get("ok", 0) + counts.get("not_found", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
