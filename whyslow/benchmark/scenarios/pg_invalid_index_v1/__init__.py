"""PostgreSQL query regression caused by an invalid index artifact."""

from .evaluate import evaluate
from .reset import reset
from .setup import setup

METADATA = {
    "id": "pg_invalid_index_v1",
    "title": "PostgreSQL invalid-index regression",
    "summary": "A failed concurrent index deployment left an unusable artifact and a slow query.",
}

__all__ = ["setup", "evaluate", "reset", "METADATA"]
