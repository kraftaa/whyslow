"""Shared input limits for CLI and collector construction."""

import math
import re
from urllib.parse import urlsplit


MAX_COLLECTOR_INTERVAL_SECONDS = 3600
MAX_REPORT_WINDOW_SECONDS = 31 * 86400
MAX_EVENT_SOURCE_CHARS = 64
MAX_EVENT_KIND_CHARS = 256
MAX_EVENT_PAYLOAD_CHARS = 4096

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HOST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RDS_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,62}$")


def validate_interval(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("interval must be a finite number greater than zero")
    if value > MAX_COLLECTOR_INTERVAL_SECONDS:
        raise ValueError(
            f"interval must not exceed {MAX_COLLECTOR_INTERVAL_SECONDS} seconds"
        )
    return value


def validate_identifier(value, label="identifier"):
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{label} must be 1-128 characters using letters, digits, '.', '_', ':', '/', or '-'"
        )
    return value


def validate_host_name(value):
    if not _HOST_NAME.fullmatch(value):
        raise ValueError(
            "host name must be 1-128 characters using letters, digits, '.', '_', or '-'"
        )
    return value


def _validate_rds_identifier(value, label):
    if (
        not _RDS_IDENTIFIER.fullmatch(value)
        or value.endswith("-")
        or "--" in value
    ):
        raise ValueError(
            f"{label} must start with a letter, end with a letter or digit, "
            "contain only letters, digits, and single hyphens, and be at most 63 characters"
        )
    return value


def validate_rds_instance_id(value):
    return _validate_rds_identifier(value, "RDS instance identifier")


def validate_rds_cluster_id(value):
    return _validate_rds_identifier(value, "RDS cluster identifier")


def validate_http_url(value):
    if any(ord(char) <= 32 for char in value):
        raise ValueError("stats URL must not contain whitespace or control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("stats URL must be an absolute http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None or parsed.query:
        raise ValueError(
            "stats URL must not contain credentials or a query string; use WHYSLOW_PUMA_TOKEN"
        )
    if parsed.fragment:
        raise ValueError("stats URL must not contain a fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("stats URL contains an invalid port") from exc
    return value


def validate_text(value, label, max_chars, required=False):
    if value is None:
        if required:
            raise ValueError(f"{label} is required")
        return None
    if required and not value.strip():
        raise ValueError(f"{label} must not be blank")
    if len(value) > max_chars:
        raise ValueError(f"{label} must not exceed {max_chars} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} must not contain control characters")
    return value
