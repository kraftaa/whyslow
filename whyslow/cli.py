import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .storage import SchemaVersionError, Store
from .collector_postgres import PostgresCollector
from .collector_puma import PumaCollector
from . import explain as explain_mod
from . import diff as diff_mod
from . import status as status_mod
from . import json_output
from .validation import (
    MAX_EVENT_KIND_CHARS,
    MAX_EVENT_PAYLOAD_CHARS,
    MAX_EVENT_SOURCE_CHARS,
    MAX_REPORT_WINDOW_SECONDS,
    validate_host_name,
    validate_http_url,
    validate_identifier,
    validate_interval,
    validate_rds_cluster_id,
    validate_rds_instance_id,
    validate_text,
)


DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s):
    """Parse '15m', '2h', '90s', '1d' into seconds. Exists because computing
    exact UTC timestamps by hand during an incident is real friction."""
    s = s.strip().lower()
    unit = s[-1] if s and s[-1] in DURATION_UNITS else None
    if unit is None:
        raise SystemExit(f"could not parse duration: {s!r} (use e.g. 15m, 2h, 90s, 1d)")
    try:
        value = float(s[:-1])
    except ValueError:
        raise SystemExit(f"could not parse duration: {s!r} (use e.g. 15m, 2h, 90s, 1d)")
    if not math.isfinite(value) or value <= 0:
        raise SystemExit(f"duration must be greater than zero: {s!r}")
    seconds = value * DURATION_UNITS[unit]
    if seconds > MAX_REPORT_WINDOW_SECONDS:
        raise SystemExit("report window must not exceed 31 days")
    return seconds


def _arg_value(validator, label=None):
    def parse(value):
        try:
            return validator(value) if label is None else validator(value, label)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return parse


interval_arg = _arg_value(validate_interval)
host_name_arg = _arg_value(validate_host_name)
stats_url_arg = _arg_value(validate_http_url)
collector_name_arg = _arg_value(validate_identifier, "collector name")
rds_instance_id_arg = _arg_value(validate_rds_instance_id)
rds_cluster_id_arg = _arg_value(validate_rds_cluster_id)


def backup_keep_arg(value):
    try:
        keep = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("backup retention must be an integer") from exc
    if not 1 <= keep <= 3650:
        raise argparse.ArgumentTypeError("backup retention must be between 1 and 3650")
    return keep


def _is_clock_time(s):
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    return False


def resolve_explicit_window(from_value, to_value):
    """Parse an explicit window, treating a clock-only end before its start
    as crossing UTC midnight."""
    start_ts = parse_time(from_value)
    end_ts = parse_time(to_value)
    if end_ts <= start_ts and _is_clock_time(from_value) and _is_clock_time(to_value):
        end_ts += 86400
    if end_ts <= start_ts:
        raise SystemExit("window end must be after window start")
    if end_ts - start_ts > MAX_REPORT_WINDOW_SECONDS:
        raise SystemExit("report window must not exceed 31 days")
    return start_ts, end_ts


def resolve_window(args):
    """Resolve either --last DURATION or --from/--to into (start_ts, end_ts)."""
    if getattr(args, "last", None):
        end_ts = time.time()
        return end_ts - parse_duration(args.last), end_ts
    if not args.from_ or not args.to:
        raise SystemExit("specify either --last DURATION (e.g. --last 15m) or both --from and --to")
    return resolve_explicit_window(args.from_, args.to)


def parse_time(s):
    """Accepts ISO-8601, HH:MM, HH:MM:SS, or raw epoch seconds.
    Clock-only values are always
    interpreted as UTC -- collectors store time.time() (UTC epoch), and an
    engineer reading a timestamp off any dashboard (CloudWatch, logs) is
    reading UTC too. Using local system time here was a real, silent bug:
    the exact same "23:30" typed on a laptop in a different timezone than
    the collector would resolve to a different epoch with no error at all,
    returning the wrong window during an incident."""
    try:
        epoch = float(s)
        if not math.isfinite(epoch):
            raise SystemExit(f"timestamp must be finite: {s!r}")
        try:
            datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise SystemExit(f"timestamp is outside the supported date range: {s!r}")
        return epoch
    except (ValueError, OverflowError, OSError):
        pass

    # ISO-8601 is the unambiguous form for historical and cross-date
    # incidents. A missing offset is interpreted as UTC, matching the
    # clock-only behavior and the collector's epoch timestamps.
    normalized = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        pass

    today = datetime.now(timezone.utc).date()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt).time()
            return datetime.combine(today, t, tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    raise SystemExit(
        f"could not parse time: {s!r} -- interpreted as UTC; "
        f"use ISO-8601, HH:MM, HH:MM:SS, or epoch seconds"
    )


def cmd_collect_pg(args):
    if not args.dsn:
        raise SystemExit("Postgres DSN required: set WHYSLOW_PG_DSN or pass --dsn")
    store = Store(args.db)
    collector = PostgresCollector(args.dsn, store, interval=args.interval)
    collector.run_forever()


def cmd_collect_puma(args):
    store = Store(args.db)
    collector = PumaCollector(
        args.host_name, args.stats_url, store, interval=args.interval, auth_token=args.token
    )
    collector.run_forever()


def cmd_collect_cw(args):
    from .collector_cloudwatch import CloudWatchCollector

    store = Store(args.db)
    collector = CloudWatchCollector(
        args.db_instance_id,
        store,
        interval=args.interval,
        region=args.region,
        db_cluster_id=args.db_cluster_id,
    )
    collector.run_forever()


def cmd_explain(args):
    start_ts, end_ts = resolve_window(args)
    store = Store(args.db)
    result = explain_mod.explain(store, start_ts, end_ts)
    if args.json:
        print(json_output.dumps(json_output.explain_document(result, start_ts, end_ts)))
    else:
        print(explain_mod.render(result, start_ts, end_ts))


def cmd_event(args):
    ts = parse_time(args.at) if args.at else time.time()
    store = Store(args.db)
    store.write_event(args.source, args.kind, args.payload, ts=ts)
    print(f"[whyslow] recorded event: source={args.source} kind={args.kind} at {ts:.0f}")


def cmd_status(args):
    store = Store(args.db)
    result = status_mod.status(store)
    healthy = status_mod.is_healthy(result)
    if args.json:
        print(json_output.dumps(json_output.status_document(result, healthy)))
    else:
        print(status_mod.render(result))
    # Non-zero exit if any collector is stale, so this is usable as a
    # monitoring check (cron, Nagios, whatever) rather than only by eye.
    if not healthy:
        sys.exit(1)


def cmd_prune(args):
    store = Store(args.db)
    deleted = store.prune()
    store.close()
    summary = ", ".join(f"{table}={count}" for table, count in sorted(deleted.items()))
    print(f"[whyslow] pruned old rows: {summary}")


def cmd_backup(args):
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = output_dir / f"whyslow-{timestamp}.sqlite3"

    store = Store(args.db)
    try:
        store.backup(destination)
    finally:
        store.close()

    backups = sorted(output_dir.glob("whyslow-*.sqlite3"))
    expired = backups[: -args.keep]
    for old_backup in expired:
        old_backup.unlink()
    print(
        f"[whyslow] backup ready: {destination} "
        f"(retained={len(backups) - len(expired)}, removed={len(expired)})"
    )


def cmd_retire(args):
    ts = parse_time(args.at) if args.at else time.time()
    if ts > time.time():
        raise SystemExit("collector retirement time cannot be in the future")
    store = Store(args.db)
    if not store.retire_collector(args.collector, ts=ts):
        store.close()
        raise SystemExit(f"unknown collector: {args.collector}")
    store.close()
    print(f"[whyslow] retired collector: {args.collector} at {ts:.0f}")


def cmd_doctor(args):
    from . import doctor as doctor_mod

    store = Store(args.db)
    db_cluster_id = args.db_cluster_id
    db_instance_id = args.db_instance_id
    if not db_cluster_id and not db_instance_id:
        db_cluster_id = os.environ.get("WHYSLOW_DB_CLUSTER_ID")
        if not db_cluster_id:
            db_instance_id = os.environ.get("WHYSLOW_DB_INSTANCE_ID")
    result = doctor_mod.doctor(
        store,
        dsn=os.environ.get("WHYSLOW_PG_DSN"),
        puma_url=os.environ.get("WHYSLOW_PUMA_STATS_URL"),
        puma_token=os.environ.get("WHYSLOW_PUMA_TOKEN"),
        db_cluster_id=db_cluster_id,
        db_instance_id=db_instance_id,
        region=args.region,
    )
    store.close()
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else doctor_mod.render(result))
    if not result["ok"]:
        sys.exit(1)


def cmd_diff(args):
    incident_start, incident_end = resolve_window(args)
    if args.baseline_last:
        # Baseline = the same-length window immediately before the incident
        # window, unless explicitly given. Sensible default: compare against
        # "just before this started".
        baseline_len = parse_duration(args.baseline_last)
        baseline_end = incident_start
        baseline_start = baseline_end - baseline_len
    else:
        if not args.baseline_from or not args.baseline_to:
            raise SystemExit(
                "specify either --baseline-last DURATION or both --baseline-from and --baseline-to"
            )
        baseline_start, baseline_end = resolve_explicit_window(
            args.baseline_from,
            args.baseline_to,
        )

    store = Store(args.db)

    result = diff_mod.diff(store, baseline_start, baseline_end, incident_start, incident_end)
    if args.json:
        print(
            json_output.dumps(
                json_output.diff_document(
                    result,
                    baseline_start,
                    baseline_end,
                    incident_start,
                    incident_end,
                )
            )
        )
    else:
        print(diff_mod.render(result))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="whyslow",
        description="Why is it slow? Deterministic, evidence-first incident reconstruction.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=False)

    p = sub.add_parser("collect-pg", help="poll pg_stat_activity + blocking chains")
    p.add_argument(
        "--dsn",
        default=os.environ.get("WHYSLOW_PG_DSN"),
        help="postgres connection string (default: WHYSLOW_PG_DSN)",
    )
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    p.add_argument("--interval", type=interval_arg, default=1.0)
    p.set_defaults(func=cmd_collect_pg)

    p = sub.add_parser("collect-puma", help="poll a Puma control-app /stats endpoint")
    p.add_argument("--host-name", type=host_name_arg, required=True)
    p.add_argument("--stats-url", type=stats_url_arg, required=True)
    p.add_argument(
        "--token",
        default=os.environ.get("WHYSLOW_PUMA_TOKEN"),
        help="Puma bearer token (default: WHYSLOW_PUMA_TOKEN)",
    )
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    p.add_argument("--interval", type=interval_arg, default=1.0)
    p.set_defaults(func=cmd_collect_puma)

    p = sub.add_parser("collect-cw", help="poll CloudWatch CPU/connections (requires boto3)")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--db-cluster-id",
        type=rds_cluster_id_arg,
        help="Aurora cluster identifier; automatically follows its current writer",
    )
    target.add_argument(
        "--db-instance-id",
        type=rds_instance_id_arg,
        help="fixed RDS instance identifier (standalone/compatibility mode)",
    )
    p.add_argument("--region", default=None)
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    p.add_argument("--interval", type=interval_arg, default=60.0)
    p.set_defaults(func=cmd_collect_cw)

    p = sub.add_parser("explain", help="reconstruct a timeline + evidence for a window")
    p.add_argument(
        "--last",
        metavar="DURATION",
        help="window ending now, e.g. 15m, 2h, 90s (alternative to --from/--to)",
    )
    p.add_argument(
        "--from",
        dest="from_",
        help="window start (ISO-8601, HH:MM UTC, or epoch)",
    )
    p.add_argument("--to", help="window end (ISO-8601, HH:MM UTC, or epoch)")
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    p.add_argument("--json", action="store_true", help="emit versioned machine-readable JSON")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("status", help="is anything actually being collected right now?")
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    p.add_argument("--json", action="store_true", help="emit versioned machine-readable JSON")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("prune", help="delete rows past their retention windows")
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("backup", help="create a validated online SQLite backup")
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    p.add_argument(
        "--output-dir",
        default=".whyslow/backups",
        help="private backup directory (default: .whyslow/backups)",
    )
    p.add_argument(
        "--keep",
        type=backup_keep_arg,
        default=7,
        help="number of newest backups to retain (default: 7)",
    )
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser(
        "retire",
        help="retire a collector without deleting its historical evidence",
    )
    p.add_argument(
        "collector",
        type=collector_name_arg,
        help="collector name shown by status, e.g. puma:web-4",
    )
    p.add_argument(
        "--at",
        help="retirement timestamp (ISO-8601, HH:MM UTC, or epoch); defaults to now",
    )
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    p.set_defaults(func=cmd_retire)

    p = sub.add_parser("doctor", help="check deployment and collector readiness")
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    target = p.add_mutually_exclusive_group()
    target.add_argument(
        "--db-cluster-id",
        type=rds_cluster_id_arg,
        help="Aurora cluster identifier (default: WHYSLOW_DB_CLUSTER_ID)",
    )
    target.add_argument(
        "--db-instance-id",
        type=rds_instance_id_arg,
        help="RDS instance identifier (default: WHYSLOW_DB_INSTANCE_ID)",
    )
    p.add_argument("--region", default=None)
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("diff", help="compare a healthy baseline window against an incident window")
    p.add_argument(
        "--last",
        metavar="DURATION",
        help="incident window ending now, e.g. 15m (alternative to --from/--to)",
    )
    p.add_argument(
        "--baseline-last",
        metavar="DURATION",
        help="baseline of this length immediately before the incident window",
    )
    p.add_argument("--baseline-from")
    p.add_argument("--baseline-to")
    p.add_argument("--from", dest="from_")
    p.add_argument("--to")
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    p.add_argument("--json", action="store_true", help="emit versioned machine-readable JSON")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("event", help="record a deploy/job marker (one line in your CI/CD pipeline)")
    p.add_argument(
        "--source",
        type=_arg_value(
            lambda value: validate_identifier(
                validate_text(
                    value,
                    "event source",
                    MAX_EVENT_SOURCE_CHARS,
                    required=True,
                ),
                "event source",
            )
        ),
        required=True,
        help="e.g. deploy, dbt, airflow, manual",
    )
    p.add_argument(
        "--kind",
        type=_arg_value(
            lambda value: validate_text(value, "event kind", MAX_EVENT_KIND_CHARS, required=True)
        ),
        help="e.g. 'v1.2.3 released', 'nightly_rollup started'",
    )
    p.add_argument(
        "--payload",
        type=_arg_value(
            lambda value: validate_text(value, "event payload", MAX_EVENT_PAYLOAD_CHARS)
        ),
        help="optional extra detail (sha, job id, ...)",
    )
    p.add_argument("--at", help="timestamp (ISO-8601, HH:MM UTC, or epoch); defaults to now")
    p.add_argument("--db", default=".whyslow/store.sqlite3")
    p.set_defaults(func=cmd_event)

    # `explain` is the default action, so the primary command reads as
    # `whyslow --last 15m` rather than `whyslow explain --last 15m`.
    # Insert it only when the first argument isn't a known subcommand and
    # isn't a bare -h/--help, so `whyslow status` etc. still work.
    argv_list = list(sys.argv[1:] if argv is None else argv)
    known = set(sub.choices)
    if (
        argv_list
        and argv_list[0] not in known
        and argv_list[0] not in ("-h", "--help", "--version")
    ):
        argv_list = ["explain"] + argv_list
    elif not argv_list:
        parser.print_help()
        return

    args = parser.parse_args(argv_list)
    try:
        args.func(args)
    except SchemaVersionError as exc:
        parser.exit(2, f"whyslow: {exc}\n")


if __name__ == "__main__":
    main()
