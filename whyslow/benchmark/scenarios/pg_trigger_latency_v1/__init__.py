"""PostgreSQL write latency caused by an accidental sleep trigger."""

from .evaluate import evaluate
from .reset import reset
from .setup import setup

METADATA = {
    "id": "pg_trigger_latency_v1",
    "title": "PostgreSQL trigger-induced write latency",
    "summary": "Order inserts stall in a hidden debug trigger while audit behavior must survive.",
}

__all__ = ["setup", "evaluate", "reset", "METADATA"]
