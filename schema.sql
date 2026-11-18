-- applied by the collector at the start of every run, so it only ever adds: IF NOT EXISTS everywhere.

-- one Google Flights search: a route and a departure date at one moment. failed searches are kept too,
-- so a gap in prices can be told apart from a block, and how often blocks come can be seen
CREATE TABLE IF NOT EXISTS flight_search
(
    id          UUID        NOT NULL DEFAULT gen_random_uuid(),
    -- the searches of one cronjob run share it
    run_id      UUID        NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL,
    -- what was searched: city codes (SEL, TYO, OSA) cover all of a city's airports, an airport code only itself
    origin      CHAR(3)     NOT NULL,
    destination CHAR(3)     NOT NULL,
    depart_date DATE        NOT NULL,
    currency    CHAR(3)     NOT NULL,
    -- 'ok', 'not_found', 'blocked' or 'error'
    status      VARCHAR(16) NOT NULL,
    error       TEXT        NULL,
    offer_count INT         NOT NULL,
    PRIMARY KEY (id)
);

-- a route and date's searches over time, what every price history query starts from
CREATE INDEX IF NOT EXISTS flight_search_route_idx ON flight_search (origin, destination, depart_date, fetched_at);
CREATE INDEX IF NOT EXISTS flight_search_fetched_idx ON flight_search (fetched_at);

-- one result of a search, in the order Google listed it
CREATE TABLE IF NOT EXISTS flight_offer
(
    search_id        UUID      NOT NULL REFERENCES flight_search (id) ON DELETE CASCADE,
    -- position in Google's list, 0 is the top ("best") result
    rank             SMALLINT  NOT NULL,
    -- the airports it really leaves from and lands at, one of the searched cities' (ICN, GMP -> NRT, HND, KIX, ITM)
    from_airport     CHAR(3)   NOT NULL,
    to_airport       CHAR(3)   NOT NULL,
    -- in the search's currency, null when Google showed no price
    price            BIGINT    NULL,
    airlines         TEXT[]    NOT NULL,
    stops            SMALLINT  NOT NULL,
    -- local time of the departure and arrival airports, as Google shows them: no zone, KST and JST are the same anyway
    depart_at        TIMESTAMP NOT NULL,
    arrive_at        TIMESTAMP NOT NULL,
    -- time in the air, summed over the legs, layovers not counted
    flying_min       INT       NOT NULL,
    carbon_g         INT       NULL,
    typical_carbon_g INT       NULL,
    -- the same flights across searches (airlines + each leg's airports and departure), Google gives no flight numbers
    itinerary_key    CHAR(16)  NOT NULL,
    -- every leg: from, to, depart, arrive, duration_min, plane
    legs             JSONB     NOT NULL,
    PRIMARY KEY (search_id, rank)
);

-- a city pair's airport pairs compared, ICN->KIX against GMP->KIX
CREATE INDEX IF NOT EXISTS flight_offer_airports_idx ON flight_offer (from_airport, to_airport);

-- one flight's price over time
CREATE INDEX IF NOT EXISTS flight_offer_itinerary_idx ON flight_offer (itinerary_key);

-- the searches that worked with their offers, what most analysis reads.
-- origin/destination is the city pair searched, from_airport/to_airport the airports of the offer.
-- fetched_kst is when it was seen in Seoul time, days_before how far ahead of the departure it was
CREATE OR REPLACE VIEW flight_price AS
SELECT s.fetched_at,
       (s.fetched_at AT TIME ZONE 'Asia/Seoul')                         AS fetched_kst,
       s.origin,
       s.destination,
       o.from_airport,
       o.to_airport,
       s.depart_date,
       s.depart_date - (s.fetched_at AT TIME ZONE 'Asia/Seoul')::DATE AS days_before,
       s.currency,
       o.rank,
       o.price,
       o.airlines,
       o.stops,
       o.depart_at,
       o.arrive_at,
       o.flying_min,
       o.itinerary_key,
       s.run_id
FROM flight_search s
         JOIN flight_offer o ON o.search_id = s.id
WHERE s.status = 'ok';
