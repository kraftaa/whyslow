import shutil
import time

from whyslow.storage import Store
from whyslow import explain as explain_mod

now = time.time()

# ---------------------------------------------------------------
# Case 1: NO collector was running. The output must not read as
# "the database was fine" -- that is absence of evidence being
# presented as evidence of absence.
# ---------------------------------------------------------------
shutil.rmtree("/tmp/cov1", ignore_errors=True)
store = Store("/tmp/cov1/store.sqlite3")
result = explain_mod.explain(store, now - 3600, now)
out = explain_mod.render(result, now - 3600, now)
print("=== Case 1: no collector was ever running ===")
print(out)

assert not result["coverage"]["anything_ran"]
assert "NO COLLECTOR DATA" in out, "must warn loudly when nothing was watching"
assert "NOT evidence anything was" in out, "must explicitly reject the all-clear reading"
assert "none found -- no blocking edges" not in out, (
    "must NOT use the confident 'none found' phrasing for an unwatched window"
)
store.close()

# ---------------------------------------------------------------
# Case 2: collector ran the whole window, genuinely nothing happened.
# Here the confident "none found" IS appropriate.
# ---------------------------------------------------------------
shutil.rmtree("/tmp/cov2", ignore_errors=True)
store = Store("/tmp/cov2/store.sqlite3")
for minute in range(61):
    store.write_heartbeat("postgres", detail="ok", ts=now - 3600 + minute * 60)
result = explain_mod.explain(store, now - 3600, now)
out = explain_mod.render(result, now - 3600, now)
print("\n=== Case 2: collector ran throughout, nothing happened ===")
print(out)

assert result["coverage"]["db_ok"]
assert "NO COLLECTOR DATA" not in out
assert "none found -- no blocking edges" in out, (
    "with full coverage, the confident 'none found' phrasing is correct"
)
store.close()

# ---------------------------------------------------------------
# Case 3: collector was down for part of the window.
# ---------------------------------------------------------------
shutil.rmtree("/tmp/cov3", ignore_errors=True)
store = Store("/tmp/cov3/store.sqlite3")
for minute in range(20):  # only the first 20 of 60 minutes
    store.write_heartbeat("postgres", detail="ok", ts=now - 3600 + minute * 60)
result = explain_mod.explain(store, now - 3600, now)
out = explain_mod.render(result, now - 3600, now)
print("\n=== Case 3: collector down for two-thirds of the window ===")
print(out.split("Incident Summary")[0])

assert not result["coverage"]["db_ok"]
assert "INCOMPLETE COVERAGE" in out or "NO COLLECTOR DATA" in out, "must flag an incomplete window"
assert "cannot rule out" in out, "must name what cannot be concluded"
store.close()

# ---------------------------------------------------------------
# Case 4: LIVE incident -- block started 4 minutes ago, unresolved.
# This is the primary use case and previously showed no duration.
# ---------------------------------------------------------------
shutil.rmtree("/tmp/cov4", ignore_errors=True)
store = Store("/tmp/cov4/store.sqlite3")
for minute in range(11):
    store.write_heartbeat("postgres", detail="ok", ts=now - 600 + minute * 60)
store.write_blocking_edges(
    [(512, "web-3", 210, "batch", "analytics_role", "REINDEX TABLE orders")],
    ts=now - 240,
)
store.write_puma_stat(host="web-3", backlog=15, pool_capacity=0, max_threads=16,
                       running=16, rss_mb=500.0, ts=now - 235)
store.close()

store = Store("/tmp/cov4/store.sqlite3")
result = explain_mod.explain(store, now - 600, now)
out = explain_mod.render(result, now - 600, now)
print("\n=== Case 4: LIVE incident, block still active ===")
print(out)

b = [c for c in result["contributors"] if c["category"] == "blocking"][0]
assert b["still_active"] is True
assert b["held_seconds"] is not None, "a live incident must still report elapsed time"
assert 200 < b["held_seconds"] < 300, f"expected ~240s elapsed, got {b['held_seconds']}"
assert "and counting" in out, "live blocks should be visibly still running"
assert "pg_cancel_backend(210)" in out, "should offer remediation for an ACTIVE blocker"
store.close()

# ---------------------------------------------------------------
# Case 5: THE SUBTLE ONE. Puma collector alive the whole window,
# Postgres collector dead. An aggregate coverage number reads this
# as 100% covered -- and then prints a confident "no blocking edges
# found" even though nothing was ever watching for blocking.
# ---------------------------------------------------------------
shutil.rmtree("/tmp/cov5", ignore_errors=True)
store = Store("/tmp/cov5/store.sqlite3")
for minute in range(61):
    store.write_heartbeat("puma:web-3", detail="ok", ts=now - 3600 + minute * 60)
result = explain_mod.explain(store, now - 3600, now)
out = explain_mod.render(result, now - 3600, now)
print("\n=== Case 5: Puma alive, Postgres DEAD (the subtle one) ===")
print(out)

assert result["coverage"]["roles"]["puma"]["ok"], "puma was running throughout"
assert not result["coverage"]["db_ok"], "postgres was NOT running -- must not be treated as covered"
assert "none found -- no blocking edges" not in out, (
    "must NOT confidently rule out blocking when the postgres collector was dead"
)
assert "cannot rule out" in out and "blocking chains" in out, (
    "must name blocking chains specifically as unruled-out"
)
assert "puma" in out and "covered" in out, "should credit the collector that WAS running"
store.close()

# ---------------------------------------------------------------
# Case 6: one healthy Puma host must not hide a missing fleet peer.
# ---------------------------------------------------------------
shutil.rmtree("/tmp/cov6", ignore_errors=True)
store = Store("/tmp/cov6/store.sqlite3")
for minute in range(61):
    store.write_heartbeat("puma:web-3", detail="ok", ts=now - 3600 + minute * 60)
for minute in range(10):
    store.write_heartbeat("puma:web-4", detail="ok", ts=now - 3600 + minute * 60)
result = explain_mod.explain(store, now - 3600, now)
out = explain_mod.render(result, now - 3600, now)
print("\n=== Case 6: one complete Puma host, one partial host ===")
print(out.split("Incident Summary")[0])

puma = result["coverage"]["roles"]["puma"]
assert not puma["ok"], "partial coverage from one Puma host must fail the fleet role"
assert puma["collectors"] == ["puma:web-3", "puma:web-4"]
assert puma["missing_collectors"] == ["puma:web-4"]
assert "missing collectors: puma:web-4" in out
store.close()

# ---------------------------------------------------------------
# Case 7: a host first observed later was not expected in an older
# historical window.
# ---------------------------------------------------------------
shutil.rmtree("/tmp/cov7", ignore_errors=True)
store = Store("/tmp/cov7/store.sqlite3")
historical_now = now - 7200
for minute in range(61):
    store.write_heartbeat(
        "puma:web-3",
        detail="ok",
        ts=historical_now - 3600 + minute * 60,
    )
store.write_heartbeat("puma:web-4", detail="ok", ts=now)
result = explain_mod.explain(store, historical_now - 3600, historical_now)
puma = result["coverage"]["roles"]["puma"]
assert puma["ok"], "a future fleet member must not invalidate an older window"
assert puma["collectors"] == ["puma:web-3"]
store.close()

print("\nPASS: role and fleet coverage cannot be validated by an unrelated "
      "or incomplete collector")
