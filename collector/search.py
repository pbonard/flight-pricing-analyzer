"""Searching Google Flights: one request, its page parsed by fast-flights plus what fast-flights leaves out
(each leg's airline and flight number), and every value checked before it's handed on to be saved.

A round-trip search lists the outbound flights, each priced as a whole round-trip ticket: that outbound with the
cheapest return of the return date that can be on the same ticket. Google doesn't say which return, so only the
outbound is returned, with the round-trip price. Separate tickets (an outbound and a return sold apart, or a connection
across two tickets) are hidden, so every price is one ticket someone could buy.
"""

import hashlib
import json
import logging
import re
from datetime import date, datetime

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, fetch_flights_html
from fast_flights.parser import parse_js
from selectolax.lexbor import LexborHTMLParser

from .config import AIRPORT, Config

log = logging.getLogger("collector")

# the language of Google's page: the block words are English, and airline names are saved as Google writes them
LANGUAGE = "en-US"

# what Google answers instead of results when it takes the traffic for a bot
BLOCK_MARKERS = ("unusual traffic", "/sorry/", "captcha", "detected unusual")

# what a value read from Google has to look like before it's saved, anything else is a misread
AIRLINE_CODE = re.compile(r"[A-Z0-9]{2,3}")
FLIGHT_NUMBER = re.compile(r"[0-9]{1,4}[A-Z]?")

def parse_results(html: str):
    """fast-flights' parse(), except a result without a price gets price None instead of failing the whole page:
    fast-flights 3.1.0 reads every result's price as k[1][0][1] unchecked, and Google leaves it out of some.
    Raises FlightsNotFound when Google found no flights, anything else when the page isn't as expected."""
    data = LexborHTMLParser(html).css_first(r"script.ds\:1").text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise FlightsNotFound("no flights found; received error")
    payload = json.loads(data)
    for k in (payload[3][0] if payload[3] else None) or []:
        try:
            k[1][0][1]
        except (IndexError, TypeError):
            # the shape fast-flights reads a price from, with no price in it
            k[1:2] = [[[None, None]]]
    # parse_js reads what's between "data:" and the last comma, as on the page
    return parse_js(f"data:{json.dumps(payload)},")


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
        results = list(parse_results(html))
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
