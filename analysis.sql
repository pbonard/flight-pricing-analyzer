-- queries over the flight_price view (schema.sql). from a laptop:
--   kubectl -n backend port-forward svc/postgres-rw 5432 &
--   psql "$(kubectl -n backend get secret postgres-app -o jsonpath='{.data.uri}' | base64 -d | sed 's/postgres-rw.backend/localhost/')" -f analysis.sql

-- 1. is the collector healthy: searches per status per day. a run of 'blocked' means slow down or add a proxy
SELECT (fetched_at AT TIME ZONE 'Asia/Seoul')::DATE AS day_kst, status, count(*)
FROM flight_search
GROUP BY 1, 2
ORDER BY 1 DESC, 2;

-- 2. cheapest nonstop price per route and departure date, every time it was looked at
SELECT origin, destination, depart_date, fetched_kst, min(price) AS cheapest
FROM flight_price
WHERE stops = 0 AND price IS NOT NULL
GROUP BY origin, destination, depart_date, fetched_kst
ORDER BY origin, destination, depart_date, fetched_kst;

-- 3. booking curve: the cheapest nonstop by how many days before departure it was seen, over all departure dates.
-- answers "how far ahead is cheapest"
SELECT origin, destination, days_before,
       min(price)                                                AS lowest,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY price)        AS median,
       count(DISTINCT depart_date)                               AS dates
FROM flight_price
WHERE stops = 0 AND price IS NOT NULL
GROUP BY origin, destination, days_before
ORDER BY origin, destination, days_before DESC;

-- 4. which hour of the day (KST) the cheapest price of a search turned up, does checking at night pay off
WITH per_search AS (SELECT origin, destination, depart_date, fetched_kst, min(price) AS cheapest
                    FROM flight_price
                    WHERE stops = 0 AND price IS NOT NULL
                    GROUP BY 1, 2, 3, 4),
     ranked AS (SELECT *, cheapest - min(cheapest) OVER (PARTITION BY origin, destination, depart_date) AS above_low
                FROM per_search)
SELECT extract(HOUR FROM fetched_kst) AS hour_kst, avg(above_low)::INT AS avg_above_lowest, count(*)
FROM ranked
GROUP BY 1
ORDER BY 1;

-- 5. which airports are cheaper: the cheapest nonstop of each airport pair per city pair and departure date,
-- from the last day of searches. ICN vs GMP, NRT vs HND, KIX vs ITM
SELECT origin, destination, depart_date, from_airport, to_airport, min(price) AS cheapest, count(*) AS offers
FROM flight_price
WHERE stops = 0 AND price IS NOT NULL
  AND fetched_at > now() - INTERVAL '1 day'
GROUP BY origin, destination, depart_date, from_airport, to_airport
ORDER BY origin, destination, depart_date, cheapest;

-- 6. one flight's price over time, pick an itinerary_key from the query above
SELECT fetched_kst, price, airlines, depart_at, arrive_at
FROM flight_price
WHERE itinerary_key = '<itinerary_key>'
ORDER BY fetched_kst;

-- 7. cheapest by airline for a departure date, as of the latest search
SELECT DISTINCT ON (origin, destination, airlines) origin, destination, airlines, price, from_airport, to_airport,
                                                  depart_at, fetched_kst
FROM flight_price
WHERE depart_date = DATE '2026-12-20' AND stops = 0 AND price IS NOT NULL
ORDER BY origin, destination, airlines, fetched_at DESC, price;
