"""Reference documentation for the hidden ground-truth record.

The ground truth is written by ``setup`` to ``.state/ground_truth.json`` (a
private, gitignored directory that is NEVER copied into the agent workspace).
It is consumed by ``evaluate``. This module documents the shape; it is not
imported at runtime.

Schema (all fields dynamically discovered at setup time -- no fixed pids):

    {
      "scenario":              "pg_lock_contention_v1",
      "created_at":            <float epoch>,
      "offending_application": "analytics_job",
      "blocking_pid":          <int backend pid of the lock holder>,
      "blocked": [
        {"application": "web_app", "backend_pid": <int>}, ...
      ],
      "protected": {"application": "healthcheck", "backend_pid": <int>},
      "actor_os_pids":         [<int>, ...],   # detached OS processes to reap
      "baseline_integrity":    { ... },        # see common.integrity_snapshot
      "before_snapshot":       [ ... ],        # scenario backends at setup time
      "contended_row_id":      1
    }
"""
