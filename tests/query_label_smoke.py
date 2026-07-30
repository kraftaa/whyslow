from whyslow.collector_postgres import label_query

cases = [
    ("REINDEX TABLE orders", "reindex"),
    ("/*application:MyApp,controller:orders*/ REINDEX TABLE orders", "reindex"),
    ("-- annotation: some/tool tag\nVACUUM FULL orders", "vacuum full"),
    ("/*app*/ -- line comment too\nCOPY orders TO STDOUT", "bulk load"),
    ("   \n  /*app*/  CREATE INDEX idx ON orders(id)", "index build"),
    ("CREATE INDEX CONCURRENTLY idx ON orders(id)", None),  # CONCURRENTLY excluded on purpose
    ("SELECT * FROM orders", None),
    (None, None),
    ("", None),
]

for query, expected in cases:
    got = label_query(query)
    status = "OK" if got == expected else "FAIL"
    print(f"{status}: {query!r} -> {got!r} (expected {expected!r})")
    assert got == expected, f"label_query({query!r}) == {got!r}, expected {expected!r}"

print("\nPASS: maintenance-pattern matching survives leading comments (Rails/ActiveRecord query tags)")
