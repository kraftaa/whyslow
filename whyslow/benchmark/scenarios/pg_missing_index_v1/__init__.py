"""PostgreSQL query regression caused by a missing composite index."""

from .evaluate import evaluate
from .reset import reset
from .setup import setup

METADATA = {
    "id": "pg_missing_index_v1",
    "title": "PostgreSQL missing-index query regression",
    "summary": "A high-volume tenant query scans and sorts the entire events table.",
}

__all__ = ["setup", "evaluate", "reset", "METADATA"]
