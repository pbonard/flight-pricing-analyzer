"""Google's results, as the browser gets them, into flights and legs.

The page asks Google for its results with GetShoppingResults. The answer is Google's batchexecute format: a
)]}' line, then lines of length and JSON. The results are in the ["wrb.fr", ...] lines, as a JSON string holding
a payload laid out like the page's ds:1 script:

    payload[2][0]   the top flights Google shows first
    payload[3][0]   every other flight
    result[0]       the itinerary: [1] airline names, [2] legs, [22][7] / [22][8] carbon and typical carbon (g)
    result[1][0][1] the price, missing when Google has none ("Total price is unavailable")
    leg             [3] / [6] from / to, [8] / [10] departure / arrival time, [20] / [21] their dates,
                    [11] minutes in the air, [17] plane, [22] carrier: [code, number, None, name]

Every value is checked before it's handed on: a result whose flight data is wrong is skipped with the reason,
a leg's carrier that doesn't look right is left out (null), so a misread never lands in a table.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime

AIRPORT = re.compile(r"[A-Z]{3}")
AIRLINE_CODE = re.compile(r"[A-Z0-9]{2,3}")
FLIGHT_NUMBER = re.compile(r"[0-9]{1,4}[A-Z]?")


@dataclass(frozen=True)
class Leg:
    from_airport: str
    to_airport: str
    # local time of each airport, as Google shows it
    depart_at: datetime
    arrive_at: datetime
    duration_min: int
    plane_type: str | None
    # all three or none: a carrier that doesn't check out is left out whole
    airline_code: str | None
    airline: str | None
    flight_number: str | None


@dataclass(frozen=True)
class Flight:
    # position on Google's page, top flights first, 0 is the first
    rank: int
    # one of the top flights Google shows first
    top: bool
    price: int | None
    airlines: list[str]
    legs: list[Leg]
    carbon_g: int | None
    typical_carbon_g: int | None

    @property
    def stops(self) -> int:
        return len(self.legs) - 1

    @property
    def flying_min(self) -> int:
        return sum(leg.duration_min for leg in self.legs)

    @property
    def itinerary_key(self) -> str:
        """The same flights from one search to the next, with the search's departure date: the flight numbers,
        KE1105 or KE2102+JL308. When a leg has no number, a hash of the airlines and each leg's airports and
        departure stands in, the same as the first collector's."""
        if all(leg.flight_number for leg in self.legs):
            return "+".join(leg.flight_number for leg in self.legs)
        legs = "|".join(
            f"{leg.from_airport}{leg.to_airport}{leg.depart_at.date().isoformat()}T{leg.depart_at:%H%M}"
            for leg in self.legs
        )
        return hashlib.sha1(f"{','.join(self.airlines)}#{legs}".encode()).hexdigest()[:16]


def payloads(body: str) -> list:
    """Every results payload in a GetShoppingResults answer. An error answer has none."""
    found = []
    for line in body.split("\n"):
        if not line.startswith('[["wrb.fr"'):
            continue
        try:
            frame = json.loads(line)
            if isinstance(frame[0][2], str):
                found.append(json.loads(frame[0][2]))
        except (ValueError, IndexError, TypeError):
            continue
    return found


def raw_results(payload) -> list[tuple[bool, list]]:
    """(top, result) in page order, top flights first."""
    out = []
    for index, top in ((2, True), (3, False)):
        try:
            block = payload[index][0]
        except (IndexError, TypeError):
            continue
        out.extend((top, k) for k in block or [] if isinstance(k, list))
    return out


def _time(value) -> tuple[int, int]:
    """Google drops trailing zero parts and writes a leading one as null: [8] is 08:00, [None, 31] is 00:31."""
    padded = [*(value or []), None, None]
    return padded[0] or 0, padded[1] or 0


def _datetime(day, time) -> datetime:
    return datetime(*day, *_time(time))


def _text(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _grams(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _carrier(raw) -> tuple[str, str, str] | None:
    try:
        code, number, _, name = raw[:4]
    except (TypeError, ValueError):
        return None
    if (
        isinstance(code, str) and AIRLINE_CODE.fullmatch(code)
        and isinstance(number, str) and FLIGHT_NUMBER.fullmatch(number)
        and isinstance(name, str) and name.strip()
    ):
        return code, number, name
    return None


def _leg(raw) -> Leg:
    """Raises ValueError with the reason when the leg can't be saved as it is."""
    for code in (raw[3], raw[6]):
        if not (isinstance(code, str) and AIRPORT.fullmatch(code)):
            raise ValueError(f"airport {code!r}")
    if not (isinstance(raw[11], int) and not isinstance(raw[11], bool) and raw[11] > 0):
        raise ValueError(f"duration {raw[11]!r}")
    try:
        depart_at, arrive_at = _datetime(raw[20], raw[8]), _datetime(raw[21], raw[10])
    except (TypeError, ValueError) as e:
        raise ValueError(f"time {e}") from None
    carrier = _carrier(raw[22]) if len(raw) > 22 else None
    return Leg(
        from_airport=raw[3],
        to_airport=raw[6],
        depart_at=depart_at,
        arrive_at=arrive_at,
        duration_min=raw[11],
        plane_type=_text(raw[17]),
        airline_code=carrier[0] if carrier else None,
        airline=carrier[2] if carrier else None,
        flight_number=f"{carrier[0]}{carrier[1]}" if carrier else None,
    )


def flight(rank: int, top: bool, raw, depart: date) -> Flight:
    """One result. Raises ValueError with the reason when it can't be saved as it is."""
    try:
        itinerary, airlines, raw_legs = raw[0], raw[0][1], raw[0][2]
    except (IndexError, TypeError):
        raise ValueError("not a result") from None
    if not isinstance(raw_legs, list) or not raw_legs:
        raise ValueError("no legs")
    try:
        legs = [_leg(leg) for leg in raw_legs]
    except (IndexError, TypeError):
        raise ValueError("leg not as expected") from None

    try:
        price = raw[1][0][1]
    except (IndexError, TypeError):
        price = None
    if price is not None and not (isinstance(price, int) and not isinstance(price, bool) and price > 0):
        raise ValueError(f"price {price!r}")
    # Google lists flights leaving on the searched day, another day means the dates were read from the wrong place
    if legs[0].depart_at.date() != depart:
        raise ValueError(f"departs {legs[0].depart_at.date()}, searched {depart}")

    airlines = [a for a in airlines if isinstance(a, str)] if isinstance(airlines, list) else []
    # the legs' airline names, each once in order, are Google's airlines of the result: two places of Google's data
    # agree, so the carriers were read where they should be. otherwise none of them is kept
    if list(dict.fromkeys(leg.airline for leg in legs)) != airlines:
        legs = [Leg(**{**leg.__dict__, "airline_code": None, "airline": None, "flight_number": None}) for leg in legs]

    extras = itinerary[22] if len(itinerary) > 22 and isinstance(itinerary[22], list) else []
    return Flight(
        rank=rank,
        top=top,
        price=price,
        airlines=airlines,
        legs=legs,
        carbon_g=_grams(extras[7]) if len(extras) > 7 else None,
        typical_carbon_g=_grams(extras[8]) if len(extras) > 8 else None,
    )


def flights(payload, depart: date) -> tuple[list[Flight], list[str]]:
    """The results of a payload that check out, and why each other one was skipped (#rank reason)."""
    found, skipped = [], []
    for rank, (top, raw) in enumerate(raw_results(payload)):
        try:
            found.append(flight(rank, top, raw, depart))
        except ValueError as e:
            skipped.append(f"#{rank} {e}")
    return found, skipped
