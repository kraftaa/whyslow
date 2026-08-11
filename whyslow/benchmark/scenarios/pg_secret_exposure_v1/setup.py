"""Set up connection exhaustion with a synthetic secret in untrusted evidence."""

from ... import common
from ..pg_connection_exhaustion_v1.setup import setup_connection_storm


def setup(ctx: common.Context) -> dict:
    return setup_connection_storm(ctx, secret_evidence=True)
