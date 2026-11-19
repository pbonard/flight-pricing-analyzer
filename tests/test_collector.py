"""Tests of collector.py, run from the repo root: python -m unittest discover -s tests -t .

Google's page is faked with page(): a ds:1 script laid out the way fast-flights' parser and leg_carriers() read it,
so nothing here goes to Google. The save test needs a Postgres and runs only when TEST_DATABASE_URL is set, in a
schema of its own that it drops afterwards.
"""

import json
import os
import re
import unittest
import uuid
from datetime import date, datetime
from unittest import mock

import collector as c

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
    flight = [None] * 23
    flight[0], flight[1], flight[2] = "multi", airlines, legs
    flight[22] = [None] * 7 + list(carbon)
    return [flight, [[None, price], "booking-token"]]


def page(results) -> str:
    payload = [None] * 8
    payload[3] = [results]
    payload[7] = [None, [[], []]]
    data = json.dumps(payload)
    return (
        '<html><script class="ds:1" nonce="x">'
        f"AF_initDataCallback({{key: 'ds:1', hash: '9', data:{data}, sideChannel: {{}}}});"
        "</script></html>"
    )


NONSTOP = raw_result(["Air Premia"], [raw_leg("ICN", "NRT", ["YP", "731", None, "Air Premia"])], 187400)
CONNECTION = raw_result(
    ["Korean Air"],
    [
        raw_leg("GMP", "CJU", ["KE", "1041", None, "Korean Air"], (7, 0), (8, 10), duration=70, plane="Airbus A321"),
        raw_leg("CJU", "NRT", ["KE", "2125", None, "Korean Air"], (9, 30), (12, 0), duration=150),
    ],
    242400,
)
NOT_FOUND_PAGE = (
    '<html><script class="ds:1">AF_initDataCallback({key: \'ds:1\', data:null, errorHasStatus: true, '
    "sideChannel: {}});</script></html>"
)


def config(**env) -> c.Config:
    return c.load_config({"START_JITTER_MAX_SECONDS": "0", **env})


def run_search(html, cfg=None, depart=DEPART, return_date=None):
    with mock.patch.object(c, "fetch_flights_html", return_value=html) as fetch:
        result = c.search(cfg or config(TRIP="one-way"), "SEL", "TYO", depart, return_date)
    return result, fetch.call_args.args[0]


class LoadConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = c.load_config({})
        self.assertEqual(cfg.routes, [("SEL", "TYO"), ("SEL", "OSA")])
        self.assertEqual(cfg.trip, "round-trip")
        self.assertEqual(cfg.stay_nights, [3])
        self.assertEqual(cfg.days_ahead, [7, 14, 30, 60])
        self.assertEqual(cfg.depart_dates, [])
        self.assertEqual((cfg.seat, cfg.adults, cfg.max_stops, cfg.currency), ("economy", 1, None, "KRW"))
        self.assertFalse(cfg.dry_run)

    def test_every_setting_is_read(self):
        cfg = c.load_config({
            "ROUTES": "sel-cju, ICN-BKK", "DEPART_DATES": "2026-12-20,2027-01-02", "DAYS_AHEAD": "0,1",
            "TRIP": "round-trip", "STAY_NIGHTS": "4,2,4", "SEAT": "Business", "ADULTS": "2", "MAX_STOPS": "0",
            "CURRENCY": "usd", "PROXY": "http://proxy:8080", "START_JITTER_MAX_SECONDS": "5",
            "REQUEST_DELAY_MIN_SECONDS": "1", "REQUEST_DELAY_MAX_SECONDS": "2", "MAX_CONSECUTIVE_FAILURES": "5",
            "DRY_RUN": "true",
        })
        self.assertEqual(cfg.routes, [("SEL", "CJU"), ("ICN", "BKK")])
        self.assertEqual(cfg.depart_dates, [date(2026, 12, 20), date(2027, 1, 2)])
        self.assertEqual(cfg.days_ahead, [0, 1])
        self.assertEqual(cfg.stay_nights, [2, 4])
        self.assertEqual((cfg.seat, cfg.adults, cfg.max_stops, cfg.currency), ("business", 2, 0, "USD"))
        self.assertEqual(cfg.proxy, "http://proxy:8080")
        self.assertEqual((cfg.start_jitter_max, cfg.delay_min, cfg.delay_max), (5, 1, 2))
        self.assertEqual(cfg.max_consecutive_failures, 5)
        self.assertTrue(cfg.dry_run)

    def test_one_way_ignores_stay_nights(self):
        self.assertEqual(c.load_config({"TRIP": "one-way", "STAY_NIGHTS": "not checked"}).stay_nights, [])

    def test_every_problem_is_reported_together(self):
        with self.assertRaises(ValueError) as raised:
            c.load_config({
                "ROUTES": "SEL-TYO,icn_nrt,SEL-SEL", "DEPART_DATES": "2026-13-01", "DAYS_AHEAD": "-3,400",
                "TRIP": "return", "SEAT": "cheap", "ADULTS": "0", "MAX_STOPS": "x", "CURRENCY": "wons",
                "REQUEST_DELAY_MIN_SECONDS": "90", "REQUEST_DELAY_MAX_SECONDS": "20", "DRY_RUN": "yes",
                "MAX_CONSECUTIVE_FAILURES": "0",
            })
        message = str(raised.exception)
        for expected in (
            "'icn_nrt'", "'SEL-SEL'", "'2026-13-01'", "'-3'", "'400'", "TRIP='return'", "SEAT='cheap'",
            "ADULTS='0'", "MAX_STOPS='x'", "CURRENCY='WONS'", "REQUEST_DELAY_MAX_SECONDS", "DRY_RUN='yes'",
            "MAX_CONSECUTIVE_FAILURES='0'",
        ):
            self.assertIn(expected, message)

    def test_round_trip_needs_stay_nights(self):
        for bad in ("", "0", "31"):
            with self.subTest(STAY_NIGHTS=bad), self.assertRaises(ValueError):
                c.load_config({"STAY_NIGHTS": bad})

    def test_routes_cant_be_empty(self):
        with self.assertRaises(ValueError):
            c.load_config({"ROUTES": " , "})


class DepartureDatesTest(unittest.TestCase):
    today = date(2026, 11, 19)

    def dates(self, depart_dates, days_ahead):
        return c.departure_dates(c.load_config({"DEPART_DATES": depart_dates, "DAYS_AHEAD": days_ahead}), self.today)

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
        self.assertEqual(self.dates("2026-12-20,2026-12-21", "0,1"), [DEPART, date(2026, 12, 21), date(2026, 12, 22)])


class LegCarriersTest(unittest.TestCase):
    def test_reads_each_leg_in_result_order(self):
        self.assertEqual(
            c.leg_carriers(page([NONSTOP, CONNECTION])),
            [[("YP", "731", "Air Premia")], [("KE", "1041", "Korean Air"), ("KE", "2125", "Korean Air")]],
        )

    def test_a_malformed_carrier_is_none(self):
        bad = [
            ["YP", "73X1", None, "Air Premia"], ["yp", "731", None, "Air Premia"], ["YP", "731", None, " "],
            ["YP", None, None, "Air Premia"], ["YP"], None, "YP731",
        ]
        for carrier in bad:
            with self.subTest(carrier=carrier):
                result = raw_result(["Air Premia"], [raw_leg("ICN", "NRT", carrier)], 1)
                self.assertEqual(c.leg_carriers(page([result])), [[None]])

    def test_a_page_not_as_expected_is_empty(self):
        for html in ("<html>nothing</html>", '<script class="ds:1">no data here</script>', page([])):
            with self.subTest(html=html[:40]):
                self.assertEqual(c.leg_carriers(html), [])


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.nonstop = c.parse(page([NONSTOP]))[0]

    def test_carriers_match_fast_flights_airlines(self):
        self.assertTrue(c.carriers_match(self.nonstop, [("YP", "731", "Air Premia")]))
        self.assertFalse(c.carriers_match(self.nonstop, [("YP", "731", "Other Air")]))
        self.assertFalse(c.carriers_match(self.nonstop, [None]))
        self.assertFalse(c.carriers_match(self.nonstop, []))

    def test_a_connection_on_one_airline_matches_once(self):
        connection = c.parse(page([CONNECTION]))[0]
        self.assertTrue(c.carriers_match(connection, [("KE", "1041", "Korean Air"), ("KE", "2125", "Korean Air")]))

    def test_a_good_result_has_no_problem(self):
        self.assertIsNone(c.result_problem(self.nonstop, DEPART))

    def test_problems(self):
        cases = {
            "price": raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None)], -5),
            "airport": raw_result(["Air Premia"], [raw_leg("icn", "NRT", None)], 1),
            "duration": raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None, duration=0)], 1),
            "time": raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None, depart=(25, 0))], 1),
            "departs": raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None, day=(2026, 12, 21))], 1),
        }
        for word, result in cases.items():
            with self.subTest(problem=word):
                self.assertIn(word, c.result_problem(c.parse(page([result]))[0], DEPART))

    def test_a_missing_price_is_allowed(self):
        result = raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None)], 1)
        result[1] = []
        self.assertIsNone(c.result_problem(c.parse(page([result]))[0], DEPART))


class ItineraryKeyTest(unittest.TestCase):
    def test_flight_numbers(self):
        connection = c.parse(page([CONNECTION]))[0]
        carriers = [("KE", "1041", "Korean Air"), ("KE", "2125", "Korean Air")]
        self.assertEqual(c.itinerary_key(connection, carriers), "KE1041+KE2125")

    def test_a_hash_when_a_number_is_missing_and_the_same_every_time(self):
        nonstop = c.parse(page([NONSTOP]))[0]
        key = c.itinerary_key(nonstop, [None])
        self.assertRegex(key, r"^[0-9a-f]{16}$")
        self.assertEqual(key, c.itinerary_key(c.parse(page([NONSTOP]))[0], [None]))


class SearchTest(unittest.TestCase):
    def test_flights_with_carriers_and_google_rank(self):
        (status, flights, error), _ = run_search(page([NONSTOP, CONNECTION]))
        self.assertEqual((status, error), ("ok", None))
        self.assertEqual([(rank, legs) for rank, _, legs in flights], [
            (0, [("YP", "731", "Air Premia")]),
            (1, [("KE", "1041", "Korean Air"), ("KE", "2125", "Korean Air")]),
        ])
        self.assertEqual(flights[0][1].price, 187400)

    def test_an_invalid_result_is_skipped_and_the_rest_keep_their_rank(self):
        bad = raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None, day=(2026, 12, 21))], 1)
        with self.assertLogs(c.log, "WARNING"):
            (status, flights, error), _ = run_search(page([bad, NONSTOP]))
        self.assertEqual(status, "ok")
        self.assertEqual([rank for rank, _, _ in flights], [1])
        self.assertRegex(error, r"skipped 1 invalid results: #0 departs 2026-12-21")

    def test_carriers_not_matching_the_airlines_are_left_out(self):
        other = raw_result(["Air Premia"], [raw_leg("ICN", "NRT", ["ZG", "46", None, "ZIPAIR Tokyo"])], 200000)
        with self.assertLogs(c.log, "WARNING"):
            (status, flights, _), _ = run_search(page([other]))
        self.assertEqual(flights[0][2], [None])

    def test_carriers_that_dont_line_up_with_the_results_are_left_out(self):
        html = page([NONSTOP])
        with mock.patch.object(c, "leg_carriers", return_value=[]), self.assertLogs(c.log, "WARNING"):
            (status, flights, _), _ = run_search(html)
        self.assertEqual((status, flights[0][2]), ("ok", [None]))

    def test_statuses(self):
        cases = {
            "blocked": "<html>Our systems have detected unusual traffic from your computer</html>",
            "error": "<html>something else entirely</html>",
            "not_found": NOT_FOUND_PAGE,
        }
        for status, html in cases.items():
            with self.subTest(status=status):
                (got, flights, _), _ = run_search(html)
                self.assertEqual((got, flights), (status, []))

    def test_a_normal_page_mentioning_captcha_is_not_blocked(self):
        html = page([NONSTOP]).replace("</html>", '<a href="/recaptcha/challenge">x</a></html>')
        (status, _, _), _ = run_search(html)
        self.assertEqual(status, "ok")

    def test_a_fetch_failure_is_an_error(self):
        with mock.patch.object(c, "fetch_flights_html", side_effect=TimeoutError("slow")):
            status, flights, error = c.search(config(), "SEL", "TYO", DEPART, None)
        self.assertEqual((status, flights), ("error", []))
        self.assertIn("TimeoutError", error)

    def test_a_round_trip_query_has_the_return_and_one_ticket_only(self):
        cfg = config(SEAT="business", ADULTS="2", MAX_STOPS="0")
        _, query = run_search(page([NONSTOP]), cfg, return_date=RETURN)
        self.assertEqual(query.get_trip_type(), "round-trip")
        self.assertEqual(query.get_seat_type(), "business")
        self.assertEqual(len(query.passengers), 2)
        self.assertTrue(query.hide_separate_and_self_transfer)
        outbound, back = query.flight_data
        self.assertEqual((outbound.date, outbound.from_airport.airport, outbound.to_airport.airport),
                         ("2026-12-20", "SEL", "TYO"))
        self.assertEqual((back.date, back.from_airport.airport, back.to_airport.airport), ("2026-12-23", "TYO", "SEL"))
        self.assertEqual(outbound.max_stops, 0)

    def test_a_one_way_query_has_no_return(self):
        _, query = run_search(page([NONSTOP]), config(TRIP="one-way"))
        self.assertEqual(query.get_trip_type(), "one-way")
        self.assertEqual(len(query.flight_data), 1)
        self.assertTrue(query.hide_separate_and_self_transfer)


class RowsTest(unittest.TestCase):
    def test_flight_and_leg_rows(self):
        (_, flights, _), _ = run_search(page([CONNECTION]))
        rank, f, legs = flights[0]
        self.assertEqual(c.flight_row("sid", rank, f, legs), (
            "sid", 0, "GMP", "NRT", 242400, 1, datetime(2026, 12, 20, 7, 0), datetime(2026, 12, 20, 12, 0),
            220, 80000, 90000, "KE1041+KE2125",
        ))
        self.assertEqual(c.leg_rows("sid", rank, f, legs), [
            ("sid", 0, 0, "GMP", "CJU", datetime(2026, 12, 20, 7, 0), datetime(2026, 12, 20, 8, 10), 70,
             "Airbus A321", "KE", "Korean Air", "KE1041"),
            ("sid", 0, 1, "CJU", "NRT", datetime(2026, 12, 20, 9, 30), datetime(2026, 12, 20, 12, 0), 150,
             "Boeing 787", "KE", "Korean Air", "KE2125"),
        ])

    def test_unusable_optional_values_are_null(self):
        result = raw_result(["Air Premia"], [raw_leg("ICN", "NRT", None, plane=" ")], 1, carbon=(-1, "x"))
        with self.assertLogs(c.log, "WARNING"):
            (_, flights, _), _ = run_search(page([result]))
        rank, f, legs = flights[0]
        self.assertEqual(c.flight_row("sid", rank, f, legs)[9:11], (None, None))
        self.assertEqual(c.leg_rows("sid", rank, f, legs)[0][8:], (None, None, None, None))


class MainTest(unittest.TestCase):
    ENV = {
        "ROUTES": "SEL-TYO,SEL-OSA", "DEPART_DATES": "2026-12-20", "DAYS_AHEAD": "", "STAY_NIGHTS": "3,4",
        "START_JITTER_MAX_SECONDS": "0", "DRY_RUN": "true",
    }

    def run_main(self, results, **env):
        """main() with search() answering each call with the next of results, no sleeping and no database."""
        calls = []

        def fake_search(cfg, origin, destination, depart, return_date):
            calls.append((origin, destination, depart, return_date))
            return results[len(calls) - 1]

        with mock.patch.dict(os.environ, {**self.ENV, **env}, clear=True), \
                mock.patch.object(c, "search", side_effect=fake_search), \
                mock.patch.object(c.time, "sleep"), \
                mock.patch.object(c, "datetime", wraps=datetime) as fake_datetime, \
                mock.patch.object(c.psycopg, "connect") as connect, \
                self.assertLogs(c.log, "INFO"):
            fake_datetime.now.side_effect = lambda tz=None: datetime(2026, 11, 19, 9, 0, tzinfo=tz)
            code = c.main()
        return code, calls, connect

    def test_a_search_per_route_date_and_stay_and_nothing_written_in_a_dry_run(self):
        code, calls, connect = self.run_main([("ok", [], None)] * 4)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(calls), [
            ("SEL", "OSA", DEPART, RETURN), ("SEL", "OSA", DEPART, date(2026, 12, 24)),
            ("SEL", "TYO", DEPART, RETURN), ("SEL", "TYO", DEPART, date(2026, 12, 24)),
        ])
        connect.assert_not_called()

    def test_one_way_has_no_return(self):
        _, calls, _ = self.run_main([("ok", [], None)] * 2, TRIP="one-way")
        self.assertEqual(sorted(calls), [("SEL", "OSA", DEPART, None), ("SEL", "TYO", DEPART, None)])

    def test_stops_after_failures_in_a_row_and_fails_the_job(self):
        code, calls, _ = self.run_main([("blocked", [], None)] * 4, MAX_CONSECUTIVE_FAILURES="2")
        self.assertEqual((code, len(calls)), (1, 2))

    def test_a_success_resets_the_failure_count(self):
        results = [("blocked", [], None), ("ok", [], None), ("blocked", [], None), ("error", [], "x")]
        code, calls, _ = self.run_main(results, MAX_CONSECUTIVE_FAILURES="2")
        self.assertEqual((code, len(calls)), (0, 4))

    def test_a_bad_config_exits_2_before_searching(self):
        with mock.patch.dict(os.environ, {"SEAT": "cheap"}, clear=True), \
                mock.patch.object(c, "search") as search, self.assertLogs(c.log, "ERROR"):
            self.assertEqual(c.main(), 2)
        search.assert_not_called()

    def test_all_dates_passed_is_nothing_to_do(self):
        code, calls, _ = self.run_main([], DEPART_DATES="2026-11-01")
        self.assertEqual((code, calls), (0, []))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class SaveTest(unittest.TestCase):
    def setUp(self):
        self.schema = f"test_{uuid.uuid4().hex[:8]}"
        self.conn = c.psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.conn.execute(f"CREATE SCHEMA {self.schema}")
        self.conn.execute(f"SET search_path TO {self.schema}")
        c.ensure_schema(self.conn)
        # applying it again changes nothing, it runs at the start of every run
        c.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.execute(f"DROP SCHEMA {self.schema} CASCADE")
        self.conn.close()

    def test_a_round_trip_search_its_flights_and_legs(self):
        (status, flights, error), _ = run_search(page([NONSTOP, CONNECTION]))
        cfg = config(SEAT="economy")
        run_id = uuid.uuid4()
        c.save(self.conn, cfg, run_id, datetime.now().astimezone(), "SEL", "TYO", DEPART, RETURN, status, flights, error)

        search = self.conn.execute(
            "SELECT run_id, trip, return_date, seat, adults, max_stops, currency, status, flight_count FROM search"
        ).fetchall()
        self.assertEqual(search, [(run_id, "round-trip", RETURN, "economy", 1, None, "KRW", "ok", 2)])
        self.assertEqual(
            self.conn.execute("SELECT rank, itinerary_key, price, stops FROM flight ORDER BY rank").fetchall(),
            [(0, "YP731", 187400, 0), (1, "KE1041+KE2125", 242400, 1)],
        )
        self.assertEqual(
            self.conn.execute("SELECT rank, seq, flight_number FROM leg ORDER BY rank, seq").fetchall(),
            [(0, 0, "YP731"), (1, 0, "KE1041"), (1, 1, "KE2125")],
        )

    def test_a_failed_search_is_kept_without_flights(self):
        c.save(self.conn, config(TRIP="one-way"), uuid.uuid4(), datetime.now().astimezone(), "SEL", "TYO", DEPART,
               None, "blocked", [], None)
        self.assertEqual(self.conn.execute("SELECT trip, status, flight_count FROM search").fetchall(),
                         [("one-way", "blocked", 0)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM flight").fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()
