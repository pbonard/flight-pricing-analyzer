# pip install fast-flights   (tested API: v3.1.0)
from fast_flights import FlightQuery, Passengers, create_query, get_flights

query = create_query(
    flights=[FlightQuery(date="2026-12-20", from_airport="ICN", to_airport="NRT")],
    seat="economy",
    trip="one-way",
    passengers=Passengers(adults=1),
    language="en-US",
    currency="KRW",
)

# Optional: get_flights(query, proxy="http://user:pass@host:port")
res = get_flights(query)

for f in list(res)[:10]:
    first, last = f.flights[0], f.flights[-1]
    dep = "%02d:%02d" % first.departure.time
    arr = "%02d:%02d" % last.arrival.time
    price = f"{f.price:>10,} KRW" if f.price is not None else f"{'n/a':>14}"
    print(f"{price} | {', '.join(f.airlines):<25} | "
          f"{first.from_airport.code} {dep} -> {last.to_airport.code} {arr} | "
          f"stops: {len(f.flights) - 1}")
