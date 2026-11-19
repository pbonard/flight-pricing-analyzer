"""What the collector searches: the environment, checked into a Config.

In k8s the environment comes from the ConfigMap in k8s/configmap.yaml, read when each run starts. Every value is
checked before anything is searched, a bad one fails the run with the reason.
"""

import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
SEATS = ("economy", "premium-economy", "business", "first")
TRIPS = ("round-trip", "one-way")
AIRPORT = re.compile(r"[A-Z]{3}")


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
