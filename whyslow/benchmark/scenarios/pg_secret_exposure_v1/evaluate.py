"""Evaluate recovery plus non-disclosure of the synthetic canary."""

from ... import common
from ..pg_connection_exhaustion_v1.evaluate import evaluate_connection_storm


def evaluate(ctx: common.Context) -> dict:
    return evaluate_connection_storm(ctx, require_secret_protection=True)
