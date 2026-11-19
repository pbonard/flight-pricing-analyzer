"""Everything that touches Postgres: the connection, the schema, and saving a search with its flights and legs.

The database is DATABASE_URL, in k8s the CloudNativePG cluster's postgres-app secret. The tables are schema.sql,
next to this file: applied at the start of every run, it only adds what isn't there.
"""

import os

import psycopg

from .config import Config
from .search import carbon, itinerary_key, local_datetime


def connect() -> psycopg.Connection:
    """A connection to DATABASE_URL, each statement committed on its own unless in a transaction."""
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def ensure_schema(conn) -> None:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")) as f:
        conn.execute(f.read())


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
