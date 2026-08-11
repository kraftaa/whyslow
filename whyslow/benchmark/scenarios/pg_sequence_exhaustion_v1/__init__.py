"""PostgreSQL write outage caused by an exhausted sequence."""

from .evaluate import evaluate
from .reset import reset
from .setup import setup

METADATA = {
    "id": "pg_sequence_exhaustion_v1",
    "title": "PostgreSQL sequence exhaustion incident",
    "summary": "New invoice writes fail because their ID sequence reached an artificial limit.",
}

__all__ = ["setup", "evaluate", "reset", "METADATA"]
