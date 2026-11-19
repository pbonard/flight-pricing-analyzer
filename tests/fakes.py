"""The flight search data, faked for the tests: results laid out the way both collectors read them, wrapped as the plain page
the fast-flights collector gets (page) or as the answer the playwright collector's browser gets (answer).
"""

import json
from datetime import date

DEPART = date(2026, 12, 20)
RETURN = date(2026, 12, 23)


def raw_leg(frm, to, carrier, depart=(8, 45), arrive=(11, 20), day=(2026, 12, 20), duration=155, plane="Boeing 787"):
    """A leg as the search data lays it out, only the positions read are filled."""
    leg = [None] * 23
    leg[3], leg[4], leg[5], leg[6] = frm, f"{frm} Airport", f"{to} Airport", to
    leg[8], leg[10], leg[11] = list(depart), list(arrive), duration
    leg[17], leg[20], leg[21] = plane, list(day), list(day)
    leg[22] = carrier
    return leg


def raw_result(airlines, legs, price, carbon=(80000, 90000)):
    itinerary = [None] * 23
    itinerary[0], itinerary[1], itinerary[2] = "multi", airlines, legs
    itinerary[22] = [None] * 7 + list(carbon)
    return [itinerary, [[None, price], "booking-token"]]


NONSTOP = raw_result(["Air Premia"], [raw_leg("ICN", "NRT", ["YP", "731", None, "Air Premia"])], 187400)
CONNECTION = raw_result(
    ["Korean Air"],
    [
        raw_leg("GMP", "CJU", ["KE", "1041", None, "Korean Air"], (7, 0), (8, 10), duration=70, plane="Airbus A321"),
        raw_leg("CJU", "NRT", ["KE", "2125", None, "Korean Air"], (9, 30), (12,), duration=150),
    ],
    242400,
)
TOP = raw_result(["ZIPAIR Tokyo"], [raw_leg("ICN", "NRT", ["ZG", "46", None, "ZIPAIR Tokyo"], (13, 5), (15, 30))], 433489)


def payload(top=(), others=()):
    out = [None] * 8
    out[2] = [list(top)]
    out[3] = [list(others)]
    out[7] = [None, [[], []]]
    return out


def page(results) -> str:
    """The plain page the fast-flights collector gets: the payload in its ds:1 script, the other results only."""
    data = json.dumps(payload(others=results))
    return (
        '<html><script class="ds:1" nonce="x">'
        f"AF_initDataCallback({{key: 'ds:1', hash: '9', data:{data}, sideChannel: {{}}}});"
        "</script></html>"
    )


NOT_FOUND_PAGE = (
    '<html><script class="ds:1">AF_initDataCallback({key: \'ds:1\', data:null, errorHasStatus: true, '
    "sideChannel: {}});</script></html>"
)


def answer(*payloads) -> str:
    """A GetShoppingResults answer, what the playwright collector's browser gets, holding the payloads."""
    lines = [")]}'", ""]
    for p in payloads:
        line = json.dumps([["wrb.fr", None, json.dumps(p)]])
        lines += [str(len(line)), line]
    return "\n".join(lines) + "\n"


ERROR_ANSWER = (
    ")]}'\n\n270\n"
    '[["wrb.fr",null,null,null,null,[13,null,[["type.googleapis.com/travel.frontend.flights.ErrorResponse",[]]]]]]\n'
)
