"""
The explain engine, implemented as exactly the six steps described to the
user in conversation:

  1. Fetch rows from the window (no logic yet -- just reads).
  2. Merge everything by timestamp into one plain-English timeline.
  3. Find candidates: any pid that appears as a *blocker* (blocking path),
     or any application_name with sustained cpu/io sessions (resource path).
  4. For each candidate, check a fixed list of named signals -- each one
     a simple lookup: does a row exist, is a timestamp close to another.
  5. Count signals that fired -> look up a confidence label from a table.
  6. Render the timeline + contributors + evidence.

Deliberately no model, no statistics, no scoring formula -- only lookups
against rows the collectors already wrote. That is what "never invent
causality" means in code.
"""

from collections import defaultdict

from .collector_postgres import label_query

# --- tunables, all in one place, all inspectable ---
COOCCURRENCE_WINDOW_SECONDS = 10   # "close in time" means within this many seconds
# Events (deploys, job starts) get a wider window: a deploy or a batch job
# can take up to a minute to manifest as database pressure.
EVENT_COOCCURRENCE_WINDOW_SECONDS = 60
CPU_ALERT_THRESHOLD = 85.0        # CloudWatch CPUUtilization %
RESOURCE_SESSION_SPIKE_COUNT = 3  # distinct cpu- or io-category sessions counts as a spike
CLOUDWATCH_PERIOD_SECONDS = 60    # metric interval represented by each Average datapoint

# The two contributor categories genuinely have different numbers of
# checkable signals, so confidence is a ratio, not a hardcoded /3.
# Getting this wrong previously produced literal "4/3 signals" output --
# nonsense, and corrosive to a tool whose whole pitch is auditability.
BLOCKING_SIGNAL_COUNT = 4   # puma backlog, host-tagged blocked sessions, query pattern, event
RESOURCE_SIGNAL_COUNT = 5   # cpu spike, io spike, cloudwatch cpu, puma backlog, event


def confidence_for(n_signals, n_possible):
    """Ratio-based, with the thresholds stated in plain sight."""
    if n_possible <= 0 or n_signals <= 0:
        return "None"
    ratio = n_signals / n_possible
    if ratio >= 0.75:
        return "High"
    if ratio >= 0.5:
        return "Medium"
    return "Low"


def _near(ts_a, ts_b, window=COOCCURRENCE_WINDOW_SECONDS):
    return abs(ts_a - ts_b) <= window


def _timestamp_near_spike(ts, spike, window=COOCCURRENCE_WINDOW_SECONDS):
    """Whether a point falls inside or close to a session-spike interval."""
    return spike["start_ts"] - window <= ts <= spike["end_ts"] + window


def _cloudwatch_overlaps_spike(metric_ts, spike):
    """CloudWatch timestamps mark the start of a 60-second Average period."""
    metric_end = metric_ts + CLOUDWATCH_PERIOD_SECONDS
    return (
        metric_ts <= spike["end_ts"] + COOCCURRENCE_WINDOW_SECONDS
        and metric_end >= spike["start_ts"] - COOCCURRENCE_WINDOW_SECONDS
    )


def build_timeline(store, start_ts, end_ts):
    """Step 1 + 2: fetch everything in the window, merge and sort by time.
    Every entry here is a direct rendering of one stored row -- nothing
    computed."""
    timeline = []

    # Sessions come in pre-aggregated per minute per category. See
    # Store.sessions_aggregated_in for why: raw rows cost 733 MB of RAM on
    # a 48h window, and were never used individually -- only counted and
    # checked for temporal proximity, both of which survive aggregation.
    for minute_bucket, category, n, first_ts, last_ts in store.sessions_aggregated_in(start_ts, end_ts):
        timeline.append((
            first_ts,
            f"session activity: {category}={n} change(s) this minute",
            {"kind": "session", "category": category, "count": n,
             "minute_bucket": minute_bucket,
             "first_ts": first_ts, "last_ts": last_ts},
        ))

    for row in store.blocking_edges_in(start_ts, end_ts):
        ts, blocked_pid, blocked_app, blocking_pid, blocking_app, blocking_user, blocking_query, ended_ts = row
        label = label_query(blocking_query)
        blocker_desc = f"{blocking_user} ({label})" if label else f"{blocking_user or blocking_pid}"
        active_at_window_end = ended_ts is None or ended_ts > end_ts
        if active_at_window_end:
            dur = f" [still active at window end; held at least {end_ts - ts:.0f}s]"
        else:
            dur = f" [held {ended_ts - ts:.0f}s]"
        prefix = "already active at window start: " if ts < start_ts else ""
        timeline.append((
            max(ts, start_ts),
            f"{prefix}pid {blocked_pid} ({blocked_app or 'unknown host'}) "
            f"blocked_by pid {blocking_pid} [{blocker_desc}]{dur}",
            {"kind": "blocking_edge", "blocked_pid": blocked_pid, "blocked_app": blocked_app,
             "blocking_pid": blocking_pid, "blocking_user": blocking_user,
             "blocking_query": blocking_query, "started_ts": ts,
             "ended_ts": ended_ts, "active_at_window_end": active_at_window_end,
             "currently_unresolved": ended_ts is None, "window_end_ts": end_ts},
        ))

    for ts, host, backlog, pool_capacity, max_threads, running, rss_mb in store.puma_stats_in(start_ts, end_ts):
        rss = f" rss={rss_mb}MB" if rss_mb is not None else ""
        timeline.append((
            ts,
            f"Puma {host}: backlog={backlog} pool_capacity={pool_capacity}/{max_threads}{rss}",
            {"kind": "puma", "host": host, "backlog": backlog},
        ))

    for ts, metric, value in store.cloudwatch_metrics_in(start_ts, end_ts):
        timeline.append((ts, f"CloudWatch {metric}: {value}", {"kind": "cloudwatch", "metric": metric, "value": value}))

    for ts, source, kind, payload in store.events_in(start_ts, end_ts):
        timeline.append((ts, f"{source} event: {kind} {payload or ''}".strip(), {"kind": "event"}))

    timeline.sort(key=lambda row: row[0])
    return timeline


def find_blocking_candidates(timeline):
    """Step 3 (blocking path): any pid that shows up as a blocker."""
    candidates = {}
    for ts, _, meta in timeline:
        if meta.get("kind") == "blocking_edge":
            key = (meta["blocking_pid"], meta["blocking_user"])
            candidates.setdefault(key, {"first_ts": ts, "edges": []})
            candidates[key]["edges"].append((ts, meta))
    return candidates


def find_resource_candidates(timeline):
    """Step 3 (resource-contention path): 'cpu' or 'io' category sessions,
    grouped into real per-minute spikes. Counts spread across a wide incident
    window are not a cluster and must not be combined into one."""
    spikes = defaultdict(list)
    for ts, _, meta in timeline:
        if meta.get("kind") == "session" and meta.get("category") in ("cpu", "io"):
            count = meta.get("count", 1)
            if count >= RESOURCE_SESSION_SPIKE_COUNT:
                spikes[meta["category"]].append({
                    "count": count,
                    "start_ts": meta.get("first_ts", ts),
                    "end_ts": meta.get("last_ts", ts),
                })
    return {"spikes": dict(spikes)}


def check_blocking_signals(candidate_key, candidate_data, timeline):
    """Step 4, blocking-caused path. Three named, independent checks."""
    signals = []
    edges = candidate_data["edges"]
    blocking_query = None
    blocked_apps = set()
    for ts, meta in edges:
        blocked_apps.add(meta.get("blocked_app"))
        blocking_query = meta.get("blocking_query") or blocking_query

    edge_ts_list = [meta.get("started_ts", ts) for ts, meta in edges]

    # Signal 1: Puma backlog rose on some host near the same time.
    backlog_hit = None
    for ts, _, meta in timeline:
        if meta.get("kind") == "puma" and (meta.get("backlog") or 0) > 0:
            if any(_point_overlaps_edge(ts, edge_meta) for _, edge_meta in edges):
                backlog_hit = (ts, meta["host"])
                break
    if backlog_hit:
        signals.append(f"Puma backlog spike on {backlog_hit[1]} at t={backlog_hit[0]:.0f}")

    # Signal 2: the blocked app is a recognizable web host (application_name set).
    if any(app and app != "unknown host" for app in blocked_apps):
        signals.append(f"blocked sessions tagged to host(s): {', '.join(a for a in blocked_apps if a)}")

    # Signal 3: blocker's query matches a known maintenance/slow pattern.
    label = label_query(blocking_query)
    if label:
        signals.append(f"blocker query matched pattern: {label}")

    # Signal 4: an external event (deploy, dbt/Airflow job start) happened
    # close in time. This is what the original design wanted -- "Deployment
    # detected" on the timeline -- but it was never wired into the signals,
    # only rendered.
    event_hit = _nearest_event(timeline, edge_ts_list)
    if event_hit:
        signals.append(f"external event nearby: {event_hit}")

    return signals


def _point_overlaps_edge(ts, edge_meta, window=COOCCURRENCE_WINDOW_SECONDS):
    """Whether point evidence occurred while a blocking edge was active."""
    start = edge_meta.get("started_ts")
    end = edge_meta.get("ended_ts")
    if start is None:
        return False
    if end is None:
        end = edge_meta.get("window_end_ts", start)
    return start - window <= ts <= end + window


def _nearest_event(timeline, reference_ts_list):
    """Find an event row close in time to any of the reference timestamps.
    Returns a short description, or None. Purely a co-occurrence check --
    it never asserts the event *caused* anything, only that it happened
    nearby, which is exactly what gets printed as evidence."""
    if not reference_ts_list:
        return None
    for ts, text, meta in timeline:
        if meta.get("kind") != "event":
            continue
        if any(_near(ts, r, EVENT_COOCCURRENCE_WINDOW_SECONDS) for r in reference_ts_list):
            return text
    return None


def check_resource_signals(timeline, resource_buckets):
    """Step 4, resource-contention path. Independent of any single pid --
    this is a whole-instance condition, not a per-session one. Checks BOTH
    cpu and io buckets -- io was previously collected by
    find_resource_candidates() but never actually read here, silently
    missing disk-bound incidents (bulk COPY, heavy scans, backup, WAL
    pressure) despite the contributor being labeled "CPU/IO"."""
    signals = []

    spikes = resource_buckets.get("spikes", {})
    cpu_spikes = spikes.get("cpu", [])
    io_spikes = spikes.get("io", [])

    if cpu_spikes:
        peak = max(s["count"] for s in cpu_spikes)
        signals.append(
            f"CPU-bound session spike: {peak} change(s) in one minute "
            f"(>= {RESOURCE_SESSION_SPIKE_COUNT})"
        )
    if io_spikes:
        peak = max(s["count"] for s in io_spikes)
        signals.append(
            f"IO-bound session spike: {peak} change(s) in one minute "
            f"(>= {RESOURCE_SESSION_SPIKE_COUNT})"
        )

    resource_spikes = cpu_spikes + io_spikes

    cw_alert = None
    for ts, _, meta in timeline:
        if meta.get("kind") == "cloudwatch" and meta.get("metric") == "CPUUtilization":
            if (
                meta["value"] >= CPU_ALERT_THRESHOLD
                and any(_cloudwatch_overlaps_spike(ts, spike) for spike in resource_spikes)
            ):
                cw_alert = (ts, meta["value"])
                break
    if cw_alert:
        signals.append(f"CloudWatch CPUUtilization {cw_alert[1]:.0f}% at t={cw_alert[0]:.0f}")

    backlog_hit = None
    for ts, _, meta in timeline:
        if meta.get("kind") == "puma" and (meta.get("backlog") or 0) > 0:
            if any(_timestamp_near_spike(ts, spike) for spike in resource_spikes):
                backlog_hit = (ts, meta["host"])
                break
    if backlog_hit:
        signals.append(f"Puma backlog spike on {backlog_hit[1]} at t={backlog_hit[0]:.0f}")

    # Fifth signal: an external event (deploy, dbt/Airflow job) close in
    # time -- the spec's "overlaps a dbt run window" check, finally wired up.
    resource_reference_ts = [
        ts
        for spike in resource_spikes
        for ts in (spike["start_ts"], spike["end_ts"])
    ]
    event_hit = _nearest_event(timeline, resource_reference_ts)
    if event_hit:
        signals.append(f"external event nearby: {event_hit}")

    return signals


def explain(store, start_ts, end_ts):
    """Steps 1-6, wired together. Returns a dict ready to render."""
    timeline = build_timeline(store, start_ts, end_ts)

    contributors = []

    blocking_candidates = find_blocking_candidates(timeline)
    for (pid, usename), data in blocking_candidates.items():
        signals = check_blocking_signals((pid, usename), data, timeline)
        if signals:
            label = label_query(data["edges"][0][1].get("blocking_query")) if data["edges"] else None
            name = f"{usename or 'pid ' + str(pid)}" + (f" ({label})" if label else "")

            # The runbook says "kill the blocker, or wait if it's nearly
            # done" -- that decision needs the pid and the duration, so
            # attach both to the contributor rather than making someone
            # hunt through the timeline mid-incident.
            starts = [meta.get("started_ts", ts) for ts, meta in data["edges"]]
            ends = [m.get("ended_ts") for _, m in data["edges"]]
            still_active = any(
                m.get("active_at_window_end")
                for _, m in data["edges"]
            )
            currently_unresolved = any(
                m.get("currently_unresolved")
                for _, m in data["edges"]
            )

            # Report state as of the requested window end. A historical
            # block may have resolved later, but it was still active at the
            # moment being reconstructed.
            if still_active:
                held_seconds = end_ts - min(starts)
            else:
                held_seconds = max(e for e in ends if e is not None) - min(starts)

            contributors.append({
                "name": name,
                "category": "blocking",
                "pid": pid,
                "held_seconds": held_seconds,
                "still_active": still_active,
                "currently_unresolved": currently_unresolved,
                "blocked_count": len({m.get("blocked_pid") for _, m in data["edges"]}),
                "signals": signals,
                "signals_possible": BLOCKING_SIGNAL_COUNT,
                "confidence": confidence_for(len(signals), BLOCKING_SIGNAL_COUNT),
            })

    resource_buckets = find_resource_candidates(timeline)
    resource_signals = check_resource_signals(timeline, resource_buckets)
    if resource_signals:
        contributors.append({
            "name": "resource contention (CPU/IO)",
            "category": "resource_contention",
            "signals": resource_signals,
            "signals_possible": RESOURCE_SIGNAL_COUNT,
            "confidence": confidence_for(len(resource_signals), RESOURCE_SIGNAL_COUNT),
        })

    raw = store.coverage_in(start_ts, end_ts)

    # A collector on a replica cannot see write-lock contention at all, so
    # "no blocking found" from reader data is as misleading as no data.
    db_instance_role = None
    for collector, ts, detail, instance_role, expected_interval in store.get_heartbeats():
        if collector == "postgres":
            db_instance_role = instance_role

    coverage = _assess_coverage(raw, db_instance_role)

    return {"timeline": timeline, "contributors": contributors, "coverage": coverage}


# Which collector underpins which conclusion. Getting this wrong is how a
# live Puma collector made a dead Postgres collector look like full
# coverage -- and let "no blocking found" print with false confidence.
COLLECTOR_ROLES = [
    ("postgres", lambda n: n == "postgres",
     "blocking chains and database session pressure"),
    ("puma", lambda n: n.startswith("puma:"),
     "web app saturation (thread pool and backlog)"),
    ("cloudwatch", lambda n: n == "cloudwatch",
     "instance CPU and connection counts"),
]

COVERAGE_OK_THRESHOLD = 0.9


def _assess_coverage(raw, db_instance_role=None):
    """Turn raw per-collector minute counts into per-role verdicts."""
    per_collector = raw["per_collector"]
    expected_collectors = raw.get("expected_collectors", {})
    roles = {}
    for role, matcher, describes in COLLECTOR_ROLES:
        matching_names = {name for name in per_collector if matcher(name)}
        matching_names.update(
            name
            for name in expected_collectors
            if matcher(name)
        )
        matching = {
            name: per_collector.get(name, {"minutes": 0, "fraction": 0.0})
            for name in matching_names
        }
        fractions = [detail["fraction"] for detail in matching.values()]
        # Puma represents a fleet: one healthy target must not hide a peer
        # that was absent for part or all of the requested window.
        fraction = (
            min(fractions, default=0.0)
            if role == "puma"
            else max(fractions, default=0.0)
        )
        missing_collectors = sorted(
            name
            for name, detail in matching.items()
            if detail["fraction"] < COVERAGE_OK_THRESHOLD
        )
        ok = bool(matching) and fraction >= COVERAGE_OK_THRESHOLD
        note = None
        # Time-based coverage is necessary but not sufficient: a replica
        # collector can cover 100% of the window and still be structurally
        # unable to see write-lock contention. Reflect that here, so the
        # roles block never contradicts the replica warning above it.
        if role == "postgres" and db_instance_role == "replica":
            ok = False
            note = "collector is on a replica -- cannot see write-lock contention"
        roles[role] = {
            "fraction": fraction,
            "describes": describes,
            "collectors": sorted(matching),
            "missing_collectors": missing_collectors,
            "ok": ok,
            "note": note,
        }
    return {
        "total_minutes": raw["total_minutes"],
        "per_collector": per_collector,
        "roles": roles,
        "db_instance_role": db_instance_role,
        # The database collector is the one that gates whether "nothing
        # found" is a real finding: blocking edges and session categories
        # both come from it exclusively.
        "db_ok": roles["postgres"]["ok"],
        "any_ok": any(r["ok"] for r in roles.values()),
        # Distinct from any_ok: did ANY collector run at all? A replica
        # collector runs fine and still yields no valid coverage, and
        # telling someone "nothing was running" in that case is just false.
        "anything_ran": bool(per_collector),
    }


def _category_counts(timeline):
    counts = {}
    for ts, _, meta in timeline:
        if meta.get("kind") == "session":
            cat = meta.get("category")
            counts[cat] = counts.get(cat, 0) + meta.get("count", 1)
    return counts


def _significant_timeline(timeline):
    """Rows worth printing individually vs. session churn worth aggregating.

    Every test in this repo used a handful of synthetic rows, which hid a
    real problem: on a busy database the diff key churns several times a
    second, so a 15-minute window holds ~4,500 session rows and a 48-hour
    window ~864,000. Printing one line each produced 38 MB of output that
    nobody could read. Blocking edges and events are rare and always
    significant; session rows are only meaningful in aggregate.
    """
    significant = []
    session_buckets = {}  # (minute_bucket, category) -> count

    for ts, text, meta in timeline:
        kind = meta.get("kind")
        if kind == "session":
            key = (int(ts // 60), meta.get("category"))
            session_buckets[key] = session_buckets.get(key, 0) + meta.get("count", 1)
        elif kind == "puma":
            # Only backlog>0 matters; healthy polls are noise at 1/s.
            if (meta.get("backlog") or 0) > 0:
                significant.append((ts, text, meta))
        elif kind == "cloudwatch":
            if meta.get("metric") == "CPUUtilization" and meta.get("value", 0) >= CPU_ALERT_THRESHOLD:
                significant.append((ts, text, meta))
        else:
            # blocking_edge, event: always individually significant
            significant.append((ts, text, meta))

    # Turn session churn into one line per minute, summarising categories.
    per_minute = {}
    for (bucket, category), count in session_buckets.items():
        per_minute.setdefault(bucket, {})[category] = count
    for bucket, categories in per_minute.items():
        parts = ", ".join(f"{c}={n}" for c, n in sorted(categories.items()))
        total = sum(categories.values())
        significant.append((
            bucket * 60,
            f"session activity: {parts}  ({total} changes this minute)",
            {"kind": "session_summary"},
        ))

    significant.sort(key=lambda row: row[0])
    return significant


MAX_TIMELINE_LINES = 120


def render(result, start_ts, end_ts):
    lines = []
    cov = result.get("coverage", {})
    roles = cov.get("roles", {})
    db_ok = cov.get("db_ok", True)

    # Coverage warnings go FIRST, and name the specific conclusion each
    # missing collector invalidates. A single aggregate number can't do
    # this: "some collector was running" never justified any particular
    # finding, and treating it as if it did was a real bug.
    gaps = [(role, info) for role, info in roles.items() if not info["ok"]]

    if cov.get("db_instance_role") == "replica":
        lines.append("!! DATABASE COLLECTOR IS ON A REPLICA / AURORA READER !!")
        lines.append("")
        lines.append("Write-lock contention happens on the WRITER and is invisible from a")
        lines.append("reader. Any 'no blocking found' result below is meaningless -- this")
        lines.append("collector could never have seen it. Repoint the DSN at the cluster")
        lines.append("WRITER endpoint.")
        lines.append("")

    if gaps:
        if not cov.get("anything_ran", True):
            lines.append("!! NO COLLECTOR DATA FOR THIS WINDOW !!")
            lines.append("")
            lines.append("Nothing was running here, so this is NOT evidence anything was")
            lines.append("healthy -- it is evidence that nothing was watching. Do not")
            lines.append("conclude anything from the empty result below.")
        else:
            lines.append("!! INCOMPLETE COVERAGE -- some conclusions below are unsupported !!")
        lines.append("")
        for role, info in gaps:
            if info.get("note"):
                lines.append(f"  {role:<11} {info['note']}")
                lines.append(f"  {'':<11} -> cannot rule out: {info['describes']}")
            else:
                missing_pct = (1 - info["fraction"]) * 100
                lines.append(f"  {role:<11} missing {missing_pct:.0f}% of window "
                              f"-> cannot rule out: {info['describes']}")
                if info.get("missing_collectors"):
                    lines.append(
                        f"  {'':<11} missing collectors: "
                        f"{', '.join(info['missing_collectors'])}"
                    )
        for role, info in roles.items():
            if info["ok"]:
                lines.append(f"  {role:<11} covered ({', '.join(info['collectors'])})")
        lines.append("")
        lines.append("Check `whyslow status`, then fall back to live queries (see RUNBOOK.md).")
        lines.append("")

    lines.append("Incident Summary")
    lines.append("")
    display = _significant_timeline(result["timeline"])
    if len(display) > MAX_TIMELINE_LINES:
        omitted = len(display) - MAX_TIMELINE_LINES
        lines.append(f"  ... {omitted} earlier timeline entries omitted "
                      f"(showing the last {MAX_TIMELINE_LINES}; narrow the window to see more)")
        display = display[-MAX_TIMELINE_LINES:]
    for ts, text, meta in display:
        lines.append(f"{_fmt(ts)}  {text}")

    lines.append("")
    counts = _category_counts(result["timeline"])
    if counts:
        parts = [f"{cat}={n}" for cat, n in sorted(counts.items()) if n]
        lines.append(f"Session activity this window: {', '.join(parts)}")
        lines.append("  (root-cause edges below identify the one true blocker, not every waiting session --")
        lines.append("   this line is the blast-radius count, kept cheap on purpose, see README)")
        lines.append("")
    lines.append("Observed contributors")
    if not result["contributors"]:
        if not db_ok:
            lines.append("  (nothing recorded -- the database collector was not covering this")
            lines.append("   window, so this is NOT a finding that the database was healthy)")
        else:
            lines.append("  (none found -- no blocking edges or sustained resource pressure in this window)")
    for c in result["contributors"]:
        extra = ""
        if c.get("pid") is not None:
            extra += f"  pid={c['pid']}"
        if c.get("held_seconds") is not None:
            if c.get("still_active") and c.get("currently_unresolved"):
                suffix = "+ and counting"
            elif c.get("still_active"):
                suffix = "+ at window end"
            else:
                suffix = ""
            extra += f"  held={c['held_seconds']:.0f}s{suffix}"
        if c.get("blocked_count"):
            extra += f"  blocking={c['blocked_count']} session(s)"
        lines.append(f"- {c['name']} [{c['category']}] -- {c['confidence']} confidence "
                      f"({len(c['signals'])}/{c['signals_possible']} signals){extra}")

    lines.append("")
    lines.append("Evidence")
    for c in result["contributors"]:
        if not c["signals"]:
            continue
        lines.append(f"  {c['name']}:")
        for s in c["signals"]:
            lines.append(f"    \u2713 {s}")

    # Ready-to-paste remediation, but ONLY for blockers still holding at
    # the end of the window. Suggesting a kill for an already-resolved pid
    # is useless at best and dangerous at worst -- Postgres reuses pids, so
    # that number may now belong to an entirely different session.
    ongoing = [
        c for c in result["contributors"]
        if c["category"] == "blocking"
        and c.get("currently_unresolved")
        and c.get("pid")
    ]
    if ongoing:
        lines.append("")
        lines.append("Still blocking at window end -- to release:")
        for c in ongoing:
            lines.append(f"  SELECT pg_cancel_backend({c['pid']});    -- {c['name']}, polite")
            lines.append(f"  SELECT pg_terminate_backend({c['pid']}); -- forceful")
        lines.append("  (verify the pid is still the same session before running)")

    return "\n".join(lines)


def _fmt(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
