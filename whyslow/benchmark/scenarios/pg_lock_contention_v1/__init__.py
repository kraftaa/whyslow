"""pg_lock_contention_v1 scenario.

A realistic production PostgreSQL slowdown caused by row-lock contention: an
``analytics_job`` session holds an open transaction locking a hot row, several
``web_app`` requests hang behind it, while an unrelated ``healthcheck`` session
stays healthy. The database keeps accepting connections and CPU stays low.

Contract exposed to the benchmark CLI:
    setup(ctx)     -> dict   create the incident + hidden ground truth
    evaluate(ctx)  -> dict   deterministic system-state score
    reset(ctx)     -> dict   tear everything down (idempotent)
"""

from .setup import setup
from .evaluate import evaluate
from .reset import reset

METADATA = {
    "id": "pg_lock_contention_v1",
    "title": "PostgreSQL row-lock contention incident",
    "summary": "An open transaction holds a row lock; application requests hang.",
}

__all__ = ["setup", "evaluate", "reset", "METADATA"]
