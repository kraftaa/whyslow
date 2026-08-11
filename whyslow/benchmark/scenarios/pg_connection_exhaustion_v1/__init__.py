"""PostgreSQL application-role connection exhaustion scenario."""

from .evaluate import evaluate
from .reset import reset
from .setup import setup

METADATA = {
    "id": "pg_connection_exhaustion_v1",
    "title": "PostgreSQL connection-pool exhaustion",
    "summary": "Leaked idle sessions consume every application connection slot.",
}

__all__ = ["setup", "evaluate", "reset", "METADATA"]
