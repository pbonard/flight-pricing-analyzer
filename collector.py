"""Collects Google Flights one-way economy prices from Seoul to Tokyo and Osaka into Postgres.

Searches by city code (SEL, TYO, OSA): one search covers every airport of both cities (ICN/GMP, NRT/HND, KIX/ITM),
and each result keeps its own airports.

Run every 2 hours by the CronJob in k8s/cronjob.yaml. A run is one search per (route, departure date): it starts
after a random delay, searches in random order and pauses a random while between searches, so the traffic
doesn't come at the same minute with the same rhythm every time. It stops early once Google starts blocking.

Writes to the CloudNativePG cluster of DATABASE_URL, creating the tables of schema.sql if they aren't there.
"""

import hashlib
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, fetch_flights_html
from fast_flights.parser import parse
from selectolax.lexbor import LexborHTMLParser

log = logging.getLogger("collector")

KST = ZoneInfo("Asia/Seoul")
CURRENCY = "KRW"

# what Google answers instead of results when it takes the traffic for a bot
BLOCK_MARKERS = ("unusual traffic", "/sorry/", "captcha", "detected unusual")

# what a value read from Google has to look like before it's saved, anything else is a misread
AIRPORT = re.compile(r"[A-Z]{3}")
AIRLINE_CODE = re.compile(r"[A-Z0-9]{2,3}")
FLIGHT_NUMBER = re.compile(r"[0-9]{1,4}[A-Z]?")


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


def leg_carriers(html: str) -> list[list[tuple[str, str, str] | None]]:
    """Each result's legs as (airline code, flight number, airline name), in the order parse() returns the results.
    fast-flights leaves them out, Google has them at [22] of each raw leg: ['KE', '1105', None, 'Korean Air'].
    A leg Google gives none for is None, and a page that isn't as expected gives [], it never fails the search."""
    try:
        script = LexborHTMLParser(html).css_first(r"script.ds\:1").text()
        payload = json.loads(script.split("data:", 1)[1].rsplit(",", 1)[0])
        results = []
        # the same list parse() reads, so the n-th entry here is the n-th result there
        for k in payload[3][0] or []:
            legs = []
            for raw in k[0][2]:
                try:
                    code, number, _, name = raw[22][:4]
                except (IndexError, TypeError, ValueError):
                    legs.append(None)
                    continue
                legs.append((code, number, name) if valid_carrier(code, number, name) else None)
            results.append(legs)
        return results
    except Exception:
        return []


def valid_carrier(code, number, name) -> bool:
    return (
        isinstance(code, str) and AIRLINE_CODE.fullmatch(code) is not None
        and isinstance(number, str) and FLIGHT_NUMBER.fullmatch(number) is not None
        and isinstance(name, str) and name.strip() != ""
    )


def carriers_match(f, carriers: list) -> bool:
    """The legs' airline names, each once in order, are fast-flights' airlines of the result: two readings of different
    places of Google's data agree, so [22] was read where it should be."""
    if len(carriers) != len(f.flights) or not all(carriers):
        return False
    return list(dict.fromkeys(name for _, _, name in carriers)) == list(f.airlines)


def result_problem(f, depart: date) -> str | None:
    """Why a parsed result can't be saved as it is, None when it can. The columns it fills are checked
    so a misread never lands in the table, a result failing one is skipped."""
    if not f.flights:
        return "no legs"
    if f.price is not None and not (isinstance(f.price, int) and f.price > 0):
        return f"price {f.price!r}"
    for leg in f.flights:
        for code in (leg.from_airport.code, leg.to_airport.code):
            if not (isinstance(code, str) and AIRPORT.fullmatch(code)):
                return f"airport {code!r}"
        if not (isinstance(leg.duration, int) and leg.duration > 0):
            return f"duration {leg.duration!r}"
        try:
            local_datetime(leg.departure), local_datetime(leg.arrival)
        except (TypeError, ValueError) as e:
            return f"time {e}"
    # Google lists flights leaving on the searched day, another day means the dates were read from the wrong place
    if date(*f.flights[0].departure.date) != depart:
        return f"departs {date(*f.flights[0].departure.date)}, searched {depart}"
    return None


def carbon(value) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def itinerary_key(f, carriers: list) -> str:
    """The same flights from one search to the next, with the search's departure date: the flight numbers, KE1105 or
    KE2102+JL308. When a leg has no number, a hash of the airlines and each leg's airports and departure stands in."""
    if all(carriers):
        return "+".join(f"{code}{number}" for code, number, _ in carriers)
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
    """Returns (status, offers, error): status is ok, not_found, blocked or error, offers the results that passed
    the checks with Google's rank and their legs' carriers, [(rank, result, [(code, number, name) or None per leg])].
    error says how many results were skipped when some were."""
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
        results = list(parse(html))
    except FlightsNotFound:
        return "not_found", [], None
    except Exception as e:  # Google changed the page, fast-flights needs an update
        return "error", [], f"parse: {e!r}"[:1000]

    carriers = leg_carriers(html)
    if len(carriers) != len(results):
        log.warning("leg carriers of %d results for %d results, saving without them", len(carriers), len(results))
        carriers = [[] for _ in results]

    offers, skipped, unmatched = [], [], 0
    for rank, (f, legs) in enumerate(zip(results, carriers)):
        problem = result_problem(f, depart)
        if problem:
            skipped.append(f"#{rank} {problem}")
            continue
        # carriers that don't agree with fast-flights' airlines are left out rather than saved wrong
        if not carriers_match(f, legs):
            unmatched += len(legs) > 0
            legs = [None] * len(f.flights)
        offers.append((rank, f, legs))

    if unmatched:
        log.warning("%d results' leg carriers don't match their airlines, saved without them", unmatched)
    if skipped:
        log.warning("skipped %d results: %s", len(skipped), "; ".join(skipped))
        return "ok", offers, f"skipped {len(skipped)} invalid results: {'; '.join(skipped)}"[:1000]
    return "ok", offers, None


def flight_row(search_id, rank: int, f, carriers: list) -> tuple:
    first, last = f.flights[0], f.flights[-1]
    return (
        search_id,
        rank,
        first.from_airport.code,
        last.to_airport.code,
        f.price,
        len(f.flights) - 1,
        local_datetime(first.departure),
        local_datetime(last.arrival),
        sum(leg.duration for leg in f.flights),
        carbon(f.carbon.emission) if f.carbon else None,
        carbon(f.carbon.typical_on_route) if f.carbon else None,
        itinerary_key(f, carriers),
    )


def leg_rows(search_id, rank: int, f, carriers: list) -> list[tuple]:
    return [
        (
            search_id,
            rank,
            seq,
            leg.from_airport.code,
            leg.to_airport.code,
            local_datetime(leg.departure),
            local_datetime(leg.arrival),
            leg.duration,
            leg.plane_type if isinstance(leg.plane_type, str) and leg.plane_type.strip() else None,
            carrier[0] if carrier else None,
            carrier[2] if carrier else None,
            f"{carrier[0]}{carrier[1]}" if carrier else None,
        )
        for seq, (leg, carrier) in enumerate(zip(f.flights, carriers))
    ]


def save(conn, run_id, fetched_at, origin, destination, depart, status, offers, error) -> None:
    with conn.transaction():
        search_id = conn.execute(
            """INSERT INTO search
                   (run_id, fetched_at, origin, destination, depart_date, currency, status, error, offer_count)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (run_id, fetched_at, origin, destination, depart, CURRENCY, status, error, len(offers)),
        ).fetchone()[0]
        if offers:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO flight
                           (search_id, rank, from_airport, to_airport, price, stops, depart_at, arrive_at,
                            flying_min, carbon_g, typical_carbon_g, itinerary_key)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [flight_row(search_id, rank, f, legs) for rank, f, legs in offers],
                )
                cur.executemany(
                    """INSERT INTO leg
                           (search_id, rank, seq, from_airport, to_airport, depart_at, arrive_at, duration_min,
                            plane_type, airline_code, airline, flight_number)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [
                        row
                        for rank, f, legs in offers
                        for row in leg_rows(search_id, rank, f, legs)
                    ],
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
        prices = [f.price for _, f, _ in offers if f.price is not None]
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
