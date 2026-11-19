"""Collects Google Flights prices into Postgres, by default round trips from Seoul to Tokyo and Osaka in economy.

Searches by city code (SEL, TYO, OSA): one search covers every airport of both cities (ICN/GMP, NRT/HND, KIX/ITM),
and each result keeps its own airports.

A round-trip search lists the outbound flights, each priced as a whole round-trip ticket: that outbound with the
cheapest return of the return date that can be on the same ticket. Google doesn't say which return, so only the
outbound is saved, with the round-trip price. Separate tickets (an outbound and a return sold apart, or a connection
across two tickets) are hidden, so every price is one ticket someone could buy.

What to search is set by environment variables (see load_config), in k8s from the ConfigMap in k8s/configmap.yaml,
read when each run starts. They're all checked before anything is searched, a bad one fails the run with the reason.

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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, fetch_flights_html
from fast_flights.parser import parse
from selectolax.lexbor import LexborHTMLParser

log = logging.getLogger("collector")

KST = ZoneInfo("Asia/Seoul")
# the language of Google's page: the block words are English, and airline names are saved as Google writes them
LANGUAGE = "en-US"
SEATS = ("economy", "premium-economy", "business", "first")
TRIPS = ("round-trip", "one-way")

# what Google answers instead of results when it takes the traffic for a bot
BLOCK_MARKERS = ("unusual traffic", "/sorry/", "captcha", "detected unusual")

# what a value read from Google has to look like before it's saved, anything else is a misread
AIRPORT = re.compile(r"[A-Z]{3}")
AIRLINE_CODE = re.compile(r"[A-Z0-9]{2,3}")
FLIGHT_NUMBER = re.compile(r"[0-9]{1,4}[A-Z]?")


@dataclass(frozen=True)
class Config:
    # (origin, destination) pairs, city or airport codes
    routes: list[tuple[str, str]]
    depart_dates: list[date]
    days_ahead: list[int]
    trip: str
    # nights between departure and return, a search for each. empty for one-way
    stay_nights: list[int]
    seat: str
    adults: int
    # None is any number of stops
    max_stops: int | None
    currency: str
    proxy: str | None
    start_jitter_max: float
    delay_min: float
    delay_max: float
    max_consecutive_failures: int
    # search and log, but write nothing: to try a config without touching the data
    dry_run: bool


def load_config(env=os.environ) -> Config:
    """Reads the environment. Every problem is collected and raised together, so one failed run shows them all.

    ROUTES                      SEL-TYO,SEL-OSA   origin-destination pairs, city (SEL) or airport (ICN) codes
    DEPART_DATES                (empty)           base dates, YYYY-MM-DD. empty is the day of the run (KST)
    DAYS_AHEAD                  7,14,30,60        days after each base date to search, 0 is the base date itself.
                                                  empty is 0. dates that have passed are left out
    TRIP                        round-trip        round-trip or one-way
    STAY_NIGHTS                 3                 round trip: nights between departure and return, 2,3,4 searches
                                                  each. ignored for one-way
    SEAT                        economy           economy, premium-economy, business or first
    ADULTS                      1                 1 to 9, the price is for all of them
    MAX_STOPS                   (empty)           empty for any, 0 for nonstop only
    CURRENCY                    KRW               ISO 4217
    PROXY                       (empty)           http://user:pass@host:port for Google's requests
    START_JITTER_MAX_SECONDS    1200              random wait before the first search, up to this
    REQUEST_DELAY_MIN_SECONDS   20                random pause between searches, from
    REQUEST_DELAY_MAX_SECONDS   90                to
    MAX_CONSECUTIVE_FAILURES    3                 blocked or failed searches in a row before the run stops
    DRY_RUN                     false             true searches and logs, writes nothing
    """
    problems: list[str] = []

    def get(name: str, default: str) -> str:
        return env.get(name, default).strip()

    def items(name: str, default: str) -> list[str]:
        return [v.strip() for v in get(name, default).split(",") if v.strip()]

    def number(name: str, default: str, kind=float, low=0, high=None):
        raw = get(name, default)
        try:
            value = kind(raw)
        except ValueError:
            problems.append(f"{name}={raw!r} is not a {'whole number' if kind is int else 'number'}")
            return kind(default)
        if value < low or (high is not None and value > high):
            problems.append(f"{name}={raw!r} is out of {low}..{high if high is not None else ''}")
        return value

    routes = []
    for route in items("ROUTES", "SEL-TYO,SEL-OSA"):
        parts = route.upper().split("-")
        if len(parts) == 2 and all(AIRPORT.fullmatch(p) for p in parts) and parts[0] != parts[1]:
            routes.append((parts[0], parts[1]))
        else:
            problems.append(f"ROUTES has {route!r}, not ORIGIN-DESTINATION of two different 3 letter codes")
    if not routes:
        problems.append("ROUTES is empty")

    depart_dates = []
    for d in items("DEPART_DATES", ""):
        try:
            depart_dates.append(date.fromisoformat(d))
        except ValueError:
            problems.append(f"DEPART_DATES has {d!r}, not a YYYY-MM-DD date")

    days_ahead = []
    for n in items("DAYS_AHEAD", "7,14,30,60"):
        if n.isdigit() and int(n) <= 330:
            days_ahead.append(int(n))
        else:
            # Google sells about 11 months ahead
            problems.append(f"DAYS_AHEAD has {n!r}, not a number of days from 0 to 330")

    trip = get("TRIP", "round-trip").lower()
    if trip not in TRIPS:
        problems.append(f"TRIP={trip!r} is not one of {', '.join(TRIPS)}")
    stay_nights = []
    if trip == "round-trip":
        for n in items("STAY_NIGHTS", "3"):
            if n.isdigit() and 1 <= int(n) <= 30:
                stay_nights.append(int(n))
            else:
                problems.append(f"STAY_NIGHTS has {n!r}, not a number of nights from 1 to 30")
        if not stay_nights:
            problems.append("STAY_NIGHTS is empty, a round trip needs a return")

    seat = get("SEAT", "economy").lower()
    if seat not in SEATS:
        problems.append(f"SEAT={seat!r} is not one of {', '.join(SEATS)}")

    adults = number("ADULTS", "1", int, 1, 9)
    max_stops_raw = get("MAX_STOPS", "")
    max_stops = number("MAX_STOPS", "0", int, 0, 3) if max_stops_raw else None

    currency = get("CURRENCY", "KRW").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        problems.append(f"CURRENCY={currency!r} is not a 3 letter ISO 4217 code")

    delay_min = number("REQUEST_DELAY_MIN_SECONDS", "20")
    delay_max = number("REQUEST_DELAY_MAX_SECONDS", "90")
    if delay_max < delay_min:
        problems.append(f"REQUEST_DELAY_MAX_SECONDS={delay_max} is below REQUEST_DELAY_MIN_SECONDS={delay_min}")

    dry_run_raw = get("DRY_RUN", "false").lower()
    if dry_run_raw not in ("true", "false"):
        problems.append(f"DRY_RUN={dry_run_raw!r} is not true or false")

    config = Config(
        routes=routes,
        depart_dates=depart_dates,
        days_ahead=days_ahead,
        trip=trip,
        stay_nights=sorted(set(stay_nights)),
        seat=seat,
        adults=adults,
        max_stops=max_stops,
        currency=currency,
        proxy=get("PROXY", "") or None,
        start_jitter_max=number("START_JITTER_MAX_SECONDS", "1200"),
        delay_min=delay_min,
        delay_max=delay_max,
        max_consecutive_failures=number("MAX_CONSECUTIVE_FAILURES", "3", int, 1),
        dry_run=dry_run_raw == "true",
    )
    if problems:
        raise ValueError("bad config:\n  " + "\n  ".join(problems))
    return config


def departure_dates(config: Config, today: date) -> list[date]:
    """Each base date plus each of DAYS_AHEAD. The bases are DEPART_DATES, or today when it's empty: fixed bases
    follow the same trips up to the day, today as the base shows how far out is cheapest."""
    bases = config.depart_dates or [today]
    offsets = config.days_ahead or [0]
    dates = {base + timedelta(days=n) for base in bases for n in offsets}
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


def search(config: Config, origin: str, destination: str, depart: date, return_date: date | None):
    """Returns (status, flights, error): status is ok, not_found, blocked or error, flights the results that passed
    the checks with Google's rank and their legs' carriers, [(rank, result, [(code, number, name) or None per leg])].
    error says how many results were skipped when some were."""
    flights = [FlightQuery(date=depart.isoformat(), from_airport=origin, to_airport=destination)]
    if return_date:
        flights.append(FlightQuery(date=return_date.isoformat(), from_airport=destination, to_airport=origin))
    query = create_query(
        flights=flights,
        seat=config.seat,
        trip=config.trip,
        # one ticket per price: no outbound and return sold apart, no connection across two tickets
        hide_separate_and_self_transfer=True,
        passengers=Passengers(adults=config.adults),
        language=LANGUAGE,
        currency=config.currency,
        max_stops=config.max_stops,
    )
    try:
        html = fetch_flights_html(query, proxy=config.proxy)
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

    flights, skipped, unmatched = [], [], 0
    for rank, (f, legs) in enumerate(zip(results, carriers)):
        problem = result_problem(f, depart)
        if problem:
            skipped.append(f"#{rank} {problem}")
            continue
        # carriers that don't agree with fast-flights' airlines are left out rather than saved wrong
        if not carriers_match(f, legs):
            unmatched += len(legs) > 0
            legs = [None] * len(f.flights)
        flights.append((rank, f, legs))

    if unmatched:
        log.warning("%d results' leg carriers don't match their airlines, saved without them", unmatched)
    if skipped:
        log.warning("skipped %d results: %s", len(skipped), "; ".join(skipped))
        return "ok", flights, f"skipped {len(skipped)} invalid results: {'; '.join(skipped)}"[:1000]
    return "ok", flights, None


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


def save(
    conn, config: Config, run_id, fetched_at, origin, destination, depart, return_date, status, flights, error
) -> None:
    with conn.transaction():
        search_id = conn.execute(
            """INSERT INTO search
                   (run_id, fetched_at, origin, destination, depart_date, trip, return_date, seat, adults,
                    max_stops, currency, status, error, flight_count)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                run_id, fetched_at, origin, destination, depart, config.trip, return_date, config.seat,
                config.adults, config.max_stops, config.currency, status, error, len(flights),
            ),
        ).fetchone()[0]
        if flights:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO flight
                           (search_id, rank, from_airport, to_airport, price, stops, depart_at, arrive_at,
                            flying_min, carbon_g, typical_carbon_g, itinerary_key)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [flight_row(search_id, rank, f, legs) for rank, f, legs in flights],
                )
                cur.executemany(
                    """INSERT INTO leg
                           (search_id, rank, seq, from_airport, to_airport, depart_at, arrive_at, duration_min,
                            plane_type, airline_code, airline, flight_number)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [
                        row
                        for rank, f, legs in flights
                        for row in leg_rows(search_id, rank, f, legs)
                    ],
                )


def ensure_schema(conn) -> None:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")) as f:
        conn.execute(f.read())


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
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            ensure_schema(conn)

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
            with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
                save(
                    conn, config, run_id, fetched_at, origin, destination, depart, return_date, status, flights, error
                )

        failures_in_row = failures_in_row + 1 if status in ("blocked", "error") else 0
        if failures_in_row >= config.max_consecutive_failures:
            log.error("%d failed searches in a row, stopping this run", failures_in_row)
            break

    log.info("run %s done: %s", run_id, counts)
    # a run that got nothing is a failed job, so it shows in kubectl and alerts
    return 0 if counts.get("ok", 0) + counts.get("not_found", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
