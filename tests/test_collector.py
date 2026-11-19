"""Tests of the collector, run from the repo root: python -m unittest discover -s tests -t .

Google's answers are faked with answer(): a GetShoppingResults answer laid out the way parse.py reads it, so nothing
here goes to Google or starts a browser. The save tests need a Postgres and run only when TEST_DATABASE_URL is set,
in a schema of their own that they drop afterwards.
"""

import json
import os
import unittest
import uuid
from datetime import date, datetime
from unittest import mock

from collector import browser, config, db, main, parse, query

DEPART = date(2026, 12, 20)
RETURN = date(2026, 12, 23)


def raw_leg(frm, to, carrier, depart=(8, 45), arrive=(11, 20), day=(2026, 12, 20), duration=155, plane="Boeing 787"):
    """A leg as Google lays it out, only the positions read are filled."""
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


def payload(top=(), others=()):
    out = [None] * 8
    out[2] = [list(top)]
    out[3] = [list(others)]
    return out


def answer(*payloads) -> str:
    """A GetShoppingResults answer holding the payloads."""
    lines = [")]}'", ""]
    for p in payloads:
        line = json.dumps([["wrb.fr", None, json.dumps(p)]])
        lines += [str(len(line)), line]
    return "\n".join(lines) + "\n"


ERROR_ANSWER = (
    ")]}'\n\n270\n"
    '[["wrb.fr",null,null,null,null,[13,null,[["type.googleapis.com/travel.frontend.flights.ErrorResponse",[]]]]]]\n'
)

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


def flights_of(p, depart=DEPART):
    return parse.flights(p, depart)


class QueryTest(unittest.TestCase):
    # what fast-flights 3.1.0 encoded for the same searches, recorded before it was removed
    FAST_FLIGHTS = [
        ("round-trip", "economy", 1, None, "SEL", "TYO", "2026-12-20", "2026-12-23",
         "GhoSCjIwMjYtMTItMjBqBRIDU0VMcgUSA1RZTxoaEgoyMDI2LTEyLTIzagUSA1RZT3IFEgNTRUxCAQFIAYgBAZgBAQ=="),
        ("one-way", "business", 2, 0, "ICN", "BER", "2026-12-19", None,
         "GhwSCjIwMjYtMTItMTkoAGoFEgNJQ05yBRIDQkVSQgIBAUgDiAEBmAEC"),
        ("round-trip", "premium-economy", 9, 1, "GMP", "CJU", "2027-01-05", "2027-01-07",
         "GhwSCjIwMjctMDEtMDUoAWoFEgNHTVByBRIDQ0pVGhwSCjIwMjctMDEtMDcoAWoFEgNDSlVyBRIDR01QQgkBAQEBAQEBAQFIAogBAZgBAQ=="),
    ]

    def test_the_same_bytes_as_fast_flights(self):
        for trip, seat, adults, stops, origin, destination, out, back, expected in self.FAST_FLIGHTS:
            legs = [query.Leg(date.fromisoformat(out), origin, destination)]
            if back:
                legs.append(query.Leg(date.fromisoformat(back), destination, origin))
            with self.subTest(trip=trip, seat=seat, adults=adults, stops=stops):
                self.assertEqual(query.tfs(legs, trip, seat, adults, stops), expected)

    def test_the_url_escapes_the_search(self):
        url = query.search_url([query.Leg(DEPART, "SEL", "TYO"), query.Leg(RETURN, "TYO", "SEL")],
                               "round-trip", "economy", 1, None, "KRW", "en-US")
        self.assertTrue(url.startswith("https://www.google.com/travel/flights/search?tfs="))
        self.assertTrue(url.endswith("%3D%3D&hl=en-US&curr=KRW"))


class LoadConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = config.load_config({})
        self.assertEqual(cfg.routes, [("SEL", "TYO"), ("SEL", "OSA")])
        self.assertEqual((cfg.trip, cfg.stay_nights, cfg.days_ahead, cfg.depart_dates), ("round-trip", [3], [7, 14, 30, 60], []))
        self.assertEqual((cfg.seat, cfg.adults, cfg.max_stops, cfg.currency, cfg.dry_run), ("economy", 1, None, "KRW", False))

    def test_every_setting_is_read(self):
        cfg = config.load_config({
            "ROUTES": "sel-cju, ICN-BKK", "DEPART_DATES": "2026-12-20,2027-01-02", "DAYS_AHEAD": "0,1",
            "TRIP": "round-trip", "STAY_NIGHTS": "4,2,4", "SEAT": "Business", "ADULTS": "2", "MAX_STOPS": "0",
            "CURRENCY": "usd", "START_JITTER_MAX_SECONDS": "5", "REQUEST_DELAY_MIN_SECONDS": "1",
            "REQUEST_DELAY_MAX_SECONDS": "2", "MAX_CONSECUTIVE_FAILURES": "5", "DRY_RUN": "true",
        })
        self.assertEqual(cfg.routes, [("SEL", "CJU"), ("ICN", "BKK")])
        self.assertEqual(cfg.depart_dates, [date(2026, 12, 20), date(2027, 1, 2)])
        self.assertEqual((cfg.days_ahead, cfg.stay_nights), ([0, 1], [2, 4]))
        self.assertEqual((cfg.seat, cfg.adults, cfg.max_stops, cfg.currency), ("business", 2, 0, "USD"))
        self.assertEqual((cfg.start_jitter_max, cfg.delay_min, cfg.delay_max, cfg.max_consecutive_failures), (5, 1, 2, 5))
        self.assertTrue(cfg.dry_run)

    def test_one_way_ignores_stay_nights(self):
        self.assertEqual(config.load_config({"TRIP": "one-way", "STAY_NIGHTS": "not checked"}).stay_nights, [])

    def test_every_problem_is_reported_together(self):
        with self.assertRaises(ValueError) as raised:
            config.load_config({
                "ROUTES": "SEL-TYO,icn_nrt,SEL-SEL", "DEPART_DATES": "2026-13-01", "DAYS_AHEAD": "-3,400",
                "TRIP": "return", "SEAT": "cheap", "ADULTS": "0", "MAX_STOPS": "x", "CURRENCY": "wons",
                "REQUEST_DELAY_MIN_SECONDS": "90", "REQUEST_DELAY_MAX_SECONDS": "20", "DRY_RUN": "yes",
                "MAX_CONSECUTIVE_FAILURES": "0",
            })
        for expected in (
            "'icn_nrt'", "'SEL-SEL'", "'2026-13-01'", "'-3'", "'400'", "TRIP='return'", "SEAT='cheap'", "ADULTS='0'",
            "MAX_STOPS='x'", "CURRENCY='WONS'", "REQUEST_DELAY_MAX_SECONDS", "DRY_RUN='yes'", "MAX_CONSECUTIVE_FAILURES='0'",
        ):
            self.assertIn(expected, str(raised.exception))

    def test_round_trip_needs_stay_nights(self):
        for bad in ("", "0", "31"):
            with self.subTest(STAY_NIGHTS=bad), self.assertRaises(ValueError):
                config.load_config({"STAY_NIGHTS": bad})

    def test_page_timeout(self):
        self.assertEqual(main.page_timeout({}), 60)
        self.assertEqual(main.page_timeout({"PAGE_TIMEOUT_SECONDS": "90"}), 90)
        for bad in ("x", "5", "600"):
            with self.subTest(PAGE_TIMEOUT_SECONDS=bad), self.assertRaises(ValueError):
                main.page_timeout({"PAGE_TIMEOUT_SECONDS": bad})


class DepartureDatesTest(unittest.TestCase):
    today = date(2026, 11, 19)

    def dates(self, depart_dates, days_ahead):
        return config.departure_dates(
            config.load_config({"DEPART_DATES": depart_dates, "DAYS_AHEAD": days_ahead}), self.today)

    def test_nothing_set_is_today(self):
        self.assertEqual(self.dates("", ""), [self.today])

    def test_days_ahead_count_from_today_without_depart_dates(self):
        self.assertEqual(self.dates("", "1,7"), [date(2026, 11, 20), date(2026, 11, 26)])
        self.assertEqual(self.dates("", "0,1,7"), [self.today, date(2026, 11, 20), date(2026, 11, 26)])

    def test_days_ahead_count_from_each_depart_date(self):
        self.assertEqual(self.dates("2026-12-20", ""), [DEPART])
        self.assertEqual(self.dates("2026-12-20", "1"), [date(2026, 12, 21)])
        self.assertEqual(self.dates("2026-12-20,2027-01-10", "0,2"),
                         [DEPART, date(2026, 12, 22), date(2027, 1, 10), date(2027, 1, 12)])

    def test_passed_and_repeated_dates_are_dropped(self):
        self.assertEqual(self.dates("2026-11-01", ""), [])
        self.assertEqual(self.dates("2026-11-10", "5,9"), [self.today])


class PayloadsTest(unittest.TestCase):
    def test_reads_every_results_payload(self):
        self.assertEqual(parse.payloads(answer(payload(others=[NONSTOP]), payload(others=[CONNECTION]))),
                         [payload(others=[NONSTOP]), payload(others=[CONNECTION])])

    def test_an_error_answer_has_none(self):
        self.assertEqual(parse.payloads(ERROR_ANSWER), [])
        self.assertEqual(parse.payloads("not an answer"), [])

    def test_top_flights_first_then_the_others(self):
        results = parse.raw_results(payload(top=[TOP], others=[NONSTOP, CONNECTION]))
        self.assertEqual([top for top, _ in results], [True, False, False])
        self.assertEqual(results[0][1], TOP)

    def test_missing_lists_are_empty(self):
        empty = [None] * 8
        self.assertEqual(parse.raw_results(empty), [])
        self.assertEqual(parse.raw_results([None, None, [None], [[NONSTOP]]]), [(False, NONSTOP)])


class FlightsTest(unittest.TestCase):
    def test_flights_with_their_legs_and_google_rank(self):
        flights, skipped = flights_of(payload(top=[TOP], others=[NONSTOP, CONNECTION]))
        self.assertEqual(skipped, [])
        self.assertEqual([(f.rank, f.top, f.price, f.itinerary_key) for f in flights], [
            (0, True, 433489, "ZG46"), (1, False, 187400, "YP731"), (2, False, 242400, "KE1041+KE2125"),
        ])
        connection = flights[2]
        self.assertEqual((connection.stops, connection.flying_min, connection.carbon_g, connection.typical_carbon_g),
                         (1, 220, 80000, 90000))
        self.assertEqual(connection.legs[1], parse.Leg(
            "CJU", "NRT", datetime(2026, 12, 20, 9, 30), datetime(2026, 12, 20, 12, 0), 150, "Boeing 787",
            "KE", "Korean Air", "KE2125",
        ))

    def test_google_time_shorthand(self):
        leg = raw_leg("ICN", "NRT", ["YP", "731", None, "Air Premia"], depart=[None, 31], arrive=[8])
        (f,), _ = flights_of(payload(others=[raw_result(["Air Premia"], [leg], 1)]))
        self.assertEqual((f.legs[0].depart_at.time().isoformat(), f.legs[0].arrive_at.time().isoformat()),
                         ("00:31:00", "08:00:00"))

    def test_results_without_a_price_are_kept(self):
        cases = [raw_result(["Air Premia"], [raw_leg("ICN", "NRT", ["YP", "731", None, "Air Premia"])], 1)
                 for _ in range(4)]
        del cases[0][1]
        cases[1][1] = []
        cases[2][1] = None
        cases[3][1] = [[None]]
        flights, skipped = flights_of(payload(others=cases))
        self.assertEqual(([f.price for f in flights], skipped), ([None] * 4, []))

    def test_a_result_that_doesnt_check_out_is_skipped_with_the_reason(self):
        cases = {
            "price -5": raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None)], -5),
            "airport 'icn'": raw_result(["Air Premia"], [raw_leg("icn", "NRT", None)], 1),
            "duration 0": raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None, duration=0)], 1),
            "time": raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None, depart=(25, 0))], 1),
            "departs 2026-12-21": raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None, day=(2026, 12, 21))], 1),
            "no legs": raw_result(["Air Premia"], [], 1),
        }
        for reason, result in cases.items():
            with self.subTest(reason=reason):
                flights, skipped = flights_of(payload(others=[NONSTOP, result]))
                self.assertEqual([f.rank for f in flights], [0])
                self.assertEqual(len(skipped), 1)
                self.assertTrue(skipped[0].startswith(f"#1 {reason}"), skipped[0])

    def test_a_malformed_carrier_is_left_out(self):
        for carrier in (["YP", "73X1", None, "Air Premia"], ["yp", "731", None, "Air Premia"],
                        ["YP", "731", None, " "], ["YP"], None, "YP731"):
            with self.subTest(carrier=carrier):
                (f,), _ = flights_of(payload(others=[raw_result(["Air Premia"], [raw_leg("ICN", "NRT", carrier)], 1)]))
                self.assertEqual((f.legs[0].airline_code, f.legs[0].airline, f.legs[0].flight_number), (None, None, None))
                self.assertRegex(f.itinerary_key, r"^[0-9a-f]{16}$")

    def test_carriers_not_matching_google_airlines_are_left_out(self):
        other = raw_result(["Air Premia"], [raw_leg("ICN", "NRT", ["ZG", "46", None, "ZIPAIR Tokyo"])], 200000)
        (f,), _ = flights_of(payload(others=[other]))
        self.assertEqual(f.legs[0].flight_number, None)

    def test_the_hash_key_is_the_same_every_time(self):
        result = raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None)], 1)
        (a,), _ = flights_of(payload(others=[result]))
        (b,), _ = flights_of(payload(others=[result]))
        self.assertEqual(a.itinerary_key, b.itinerary_key)

    def test_unusable_optional_values_are_null(self):
        result = raw_result(["Air Premia"], [raw_leg("ICN", "NRT", ["YP", "731", None, "Air Premia"], plane=" ")], 1,
                            carbon=(-1, "x"))
        (f,), _ = flights_of(payload(others=[result]))
        self.assertEqual((f.carbon_g, f.typical_carbon_g, f.legs[0].plane_type), (None, None, None))


class FakePage:
    def __init__(self, content="<html></html>", url="https://www.google.com/travel/flights/search?tfs=x"):
        self._content, self.url = content, url

    def content(self):
        return self._content

    def title(self):
        return "Seoul to Tokyo | Google Flights"


class BrowserReadTest(unittest.TestCase):
    def test_the_answer_with_the_most_results_is_the_page(self):
        first = payload(others=[NONSTOP])
        full = payload(top=[TOP], others=[NONSTOP, CONNECTION])
        page = browser.Browser._read(FakePage(), [answer(first), answer(full)])
        self.assertEqual((page.status, page.payload), ("ok", full))

    def test_google_error_answer_is_an_error(self):
        page = browser.Browser._read(FakePage("<html>Oops, something went wrong.</html>"), [ERROR_ANSWER])
        self.assertEqual(page.status, "error")

    def test_a_block_page(self):
        for page in (FakePage(url="https://www.google.com/sorry/index?continue=x"),
                     FakePage("<html>Our systems have detected unusual traffic</html>")):
            with self.subTest(url=page.url):
                self.assertEqual(browser.Browser._read(page, []).status, "blocked")

    def test_no_answer_is_an_error_naming_the_page(self):
        page = browser.Browser._read(FakePage(), [])
        self.assertEqual(page.status, "error")
        self.assertIn("Seoul to Tokyo", page.error)

    def test_an_answer_without_results_or_error_is_not_found(self):
        self.assertEqual(browser.Browser._read(FakePage(), [answer(payload())]).status, "not_found")


class FakeBrowser:
    """Browser for main(): each load gives the next of pages."""

    def __init__(self, pages):
        self.pages, self.urls = list(pages), []

    def __call__(self, language, timeout):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def load(self, url):
        self.urls.append(url)
        return self.pages.pop(0)


OK = browser.Page("ok", payload(others=[NONSTOP]), None)
BLOCKED = browser.Page("blocked", None, None)
ERROR = browser.Page("error", None, "x")


class MainTest(unittest.TestCase):
    ENV = {
        "ROUTES": "SEL-TYO,SEL-OSA", "DEPART_DATES": "2026-12-20", "DAYS_AHEAD": "", "STAY_NIGHTS": "3,4",
        "START_JITTER_MAX_SECONDS": "0", "DRY_RUN": "true",
    }

    def run_main(self, pages, **env):
        fake = FakeBrowser(pages)
        with mock.patch.dict(os.environ, {**self.ENV, **env}, clear=True), \
                mock.patch.object(main, "Browser", fake), \
                mock.patch.object(main.time, "sleep"), \
                mock.patch.object(main, "datetime", wraps=datetime) as fake_datetime, \
                mock.patch.object(main.db, "connect") as connect, \
                self.assertLogs("collector", "INFO"):
            fake_datetime.now.side_effect = lambda tz=None: datetime(2026, 11, 19, 9, 0, tzinfo=tz)
            code = main.main()
        return code, fake.urls, connect

    def test_a_search_per_route_date_and_stay_and_nothing_written_in_a_dry_run(self):
        code, urls, connect = self.run_main([OK] * 4)
        self.assertEqual((code, len(urls)), (0, 4))
        expected = {
            query.search_url(main.legs(o, d, DEPART, back), "round-trip", "economy", 1, None, "KRW", "en-US")
            for o, d in (("SEL", "TYO"), ("SEL", "OSA")) for back in (RETURN, date(2026, 12, 24))
        }
        self.assertEqual(set(urls), expected)
        connect.assert_not_called()

    def test_one_way_has_no_return(self):
        _, urls, _ = self.run_main([OK] * 2, TRIP="one-way")
        self.assertEqual(set(urls), {
            query.search_url([query.Leg(DEPART, "SEL", d)], "one-way", "economy", 1, None, "KRW", "en-US")
            for d in ("TYO", "OSA")
        })

    def test_stops_after_failures_in_a_row_and_fails_the_job(self):
        code, urls, _ = self.run_main([BLOCKED] * 4, MAX_CONSECUTIVE_FAILURES="2")
        self.assertEqual((code, len(urls)), (1, 2))

    def test_a_success_resets_the_failure_count(self):
        code, urls, _ = self.run_main([BLOCKED, OK, BLOCKED, ERROR], MAX_CONSECUTIVE_FAILURES="2")
        self.assertEqual((code, len(urls)), (0, 4))

    def test_a_bad_config_exits_2_before_searching(self):
        for env in ({"SEAT": "cheap"}, {"PAGE_TIMEOUT_SECONDS": "x"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(main, "Browser") as browser_class, self.assertLogs("collector", "ERROR"):
                self.assertEqual(main.main(), 2)
                browser_class.assert_not_called()

    def test_all_dates_passed_is_nothing_to_do(self):
        code, urls, _ = self.run_main([], DEPART_DATES="2026-11-01")
        self.assertEqual((code, urls), (0, []))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class SaveTest(unittest.TestCase):
    def setUp(self):
        self.schema = f"test_{uuid.uuid4().hex[:8]}"
        self.conn = db.psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.conn.execute(f"CREATE SCHEMA {self.schema}")
        self.conn.execute(f"SET search_path TO {self.schema}")
        db.ensure_schema(self.conn)
        # applying it again changes nothing, it runs at the start of every run
        db.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.execute(f"DROP SCHEMA {self.schema} CASCADE")
        self.conn.close()

    def test_a_round_trip_search_its_flights_and_legs(self):
        flights, _ = flights_of(payload(top=[TOP], others=[NONSTOP, CONNECTION]))
        run_id = uuid.uuid4()
        db.save(self.conn, config.load_config({}), run_id, datetime.now().astimezone(), "SEL", "TYO", DEPART, RETURN,
                "ok", flights, None)
        self.assertEqual(
            self.conn.execute("SELECT run_id, trip, return_date, seat, adults, max_stops, currency, status, flight_count "
                              "FROM search").fetchall(),
            [(run_id, "round-trip", RETURN, "economy", 1, None, "KRW", "ok", 3)],
        )
        self.assertEqual(
            self.conn.execute("SELECT rank, itinerary_key, price, stops FROM flight ORDER BY rank").fetchall(),
            [(0, "ZG46", 433489, 0), (1, "YP731", 187400, 0), (2, "KE1041+KE2125", 242400, 1)],
        )
        self.assertEqual(
            self.conn.execute("SELECT rank, seq, flight_number FROM leg ORDER BY rank, seq").fetchall(),
            [(0, 0, "ZG46"), (1, 0, "YP731"), (2, 0, "KE1041"), (2, 1, "KE2125")],
        )

    def test_a_failed_search_is_kept_without_flights(self):
        db.save(self.conn, config.load_config({"TRIP": "one-way"}), uuid.uuid4(), datetime.now().astimezone(), "SEL",
                "TYO", DEPART, None, "blocked", [], None)
        self.assertEqual(self.conn.execute("SELECT trip, status, flight_count FROM search").fetchall(),
                         [("one-way", "blocked", 0)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM flight").fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()
