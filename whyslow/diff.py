"""
whyslow diff -- compares two windows and reports what was different.

Deliberately does not attempt to explain *why* -- that is explain.py's
job, and only for the incident window, with named signals. diff.py
answers a narrower, safer question: what changed between a healthy
period and the incident period. Every line here is a count or a set
difference against rows the collectors already wrote -- nothing
inferred, nothing scored.
"""

from .collector_postgres import label_query


def summarize_window(store, start_ts, end_ts):
    # Sessions are aggregated in SQL rather than loaded row-by-row: on a wide
    # window (e.g. diff --last 2d) the raw table can hold ~900k rows and OOM a
    # small collector host. Counts come from per-minute/category buckets;
    # roles/apps/labels come from the distinct dimensions -- both bounded by
    # cardinality, not row count.
    aggregated = store.sessions_aggregated_in(start_ts, end_ts)
    role_values, app_values, query_values = store.session_dimensions_in(start_ts, end_ts)
    edges = store.blocking_edges_in(start_ts, end_ts)
    puma = store.puma_stats_in(start_ts, end_ts)
    cw = store.cloudwatch_metrics_in(start_ts, end_ts)

    category_counts = {}
    session_events = 0
    for _minute_bucket, category, n, _first_ts, _last_ts in aggregated:
        category_counts[category] = category_counts.get(category, 0) + n
        session_events += n

    roles = {r for r in role_values if r}
    apps = {a for a in app_values if a}
    maintenance_labels = {label for q in query_values if (label := label_query(q))}

    longest_block = None
    for row in edges:
        (
            ts,
            blocked_pid,
            blocked_app,
            blocking_pid,
            blocking_app,
            blocking_usename,
            blocking_query,
            ended_ts,
        ) = row
        if blocked_app:
            apps.add(blocked_app)
        if blocking_app:
            apps.add(blocking_app)
        if blocking_usename:
            roles.add(blocking_usename)
        label = label_query(blocking_query)
        if label:
            maintenance_labels.add(label)
        # Summarize duration as of this window, rather than leaking a later
        # resolution into an earlier historical comparison.
        held = min(ended_ts, end_ts) - ts if ended_ts else end_ts - ts
        longest_block = held if longest_block is None else max(longest_block, held)

    max_backlog = max((row[2] for row in puma), default=None)  # ts, host, backlog, ...
    cpu_values = [row[2] for row in cw if row[1] == "CPUUtilization"]
    max_cpu = max(cpu_values, default=None)

    return {
        "session_events": session_events,
        "category_counts": category_counts,
        "blocking_edges": len(edges),
        "longest_block_seconds": longest_block,
        "roles": roles,
        "apps": apps,
        "maintenance_labels": maintenance_labels,
        "max_puma_backlog": max_backlog,
        "max_cpu": max_cpu,
    }


def diff(store, baseline_start, baseline_end, incident_start, incident_end):
    baseline = summarize_window(store, baseline_start, baseline_end)
    incident = summarize_window(store, incident_start, incident_end)
    return {"baseline": baseline, "incident": incident}


def _fmt_val(v):
    return "n/a" if v is None else v


def _fmt_set_diff(before, after):
    appeared = sorted(after - before)
    disappeared = sorted(before - after)
    return appeared, disappeared


def render(result):
    b, i = result["baseline"], result["incident"]
    lines = []

    lines.append(f"Session activity:     {b['session_events']} -> {i['session_events']} events")
    for cat in ("lock", "io", "cpu", "other"):
        b_n, i_n = b["category_counts"].get(cat, 0), i["category_counts"].get(cat, 0)
        if b_n or i_n:
            lines.append(f"  {cat:<6} sessions:  {b_n} -> {i_n}")
    lines.append(
        f"Blocking edges (root cause only): {b['blocking_edges']} -> {i['blocking_edges']}"
    )
    b_long = b["longest_block_seconds"]
    i_long = i["longest_block_seconds"]
    if b_long is not None or i_long is not None:
        lines.append(
            f"Longest block held:   "
            f"{f'{b_long:.0f}s' if b_long is not None else 'n/a'} -> "
            f"{f'{i_long:.0f}s' if i_long is not None else 'n/a'}"
        )
    lines.append(
        f"Puma max backlog:     {_fmt_val(b['max_puma_backlog'])} -> {_fmt_val(i['max_puma_backlog'])}"
    )
    lines.append(f"CloudWatch max CPU:   {_fmt_val(b['max_cpu'])} -> {_fmt_val(i['max_cpu'])}")

    for label, key in (
        ("Roles", "roles"),
        ("Apps", "apps"),
        ("Maintenance patterns", "maintenance_labels"),
    ):
        appeared, disappeared = _fmt_set_diff(b[key], i[key])
        lines.append(f"{label}:")
        lines.append(f"  before: {sorted(b[key]) or '(none)'}")
        lines.append(f"  during: {sorted(i[key]) or '(none)'}")
        if appeared:
            lines.append(f"  appeared: {appeared}")
        if disappeared:
            lines.append(f"  disappeared: {disappeared}")

    return "\n".join(lines)
