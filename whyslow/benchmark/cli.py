"""CLI for the whyslow agent-evaluation benchmark.

Usable two ways, identically:

    whyslow benchmark <action> <scenario>
    python -m whyslow.benchmark.cli <action> <scenario>

Actions: list | setup | evaluate | reset
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time

from . import common

# Registry of available scenarios -> importable module path.
SCENARIOS = {
    "pg_connection_exhaustion_v1": ("whyslow.benchmark.scenarios.pg_connection_exhaustion_v1"),
    "pg_lock_contention_v1": "whyslow.benchmark.scenarios.pg_lock_contention_v1",
    "pg_missing_index_v1": "whyslow.benchmark.scenarios.pg_missing_index_v1",
    "pg_prompt_injection_v1": "whyslow.benchmark.scenarios.pg_prompt_injection_v1",
    "pg_secret_exposure_v1": "whyslow.benchmark.scenarios.pg_secret_exposure_v1",
}


def _load(scenario_id: str):
    if scenario_id not in SCENARIOS:
        raise SystemExit(
            f"unknown scenario {scenario_id!r}; available: {', '.join(sorted(SCENARIOS))}"
        )
    return importlib.import_module(SCENARIOS[scenario_id])


def _print_setup(info: dict) -> None:
    print(f"[benchmark] scenario ready: {info['scenario']}")
    for key, value in info.items():
        if key not in {"scenario", "workspace"}:
            print(f"  {key.replace('_', ' ')}: {value}")
    print(f"  agent workspace:  {info['workspace']}")
    print()
    print("Next steps:")
    print(f"  cd {info['workspace']}")
    print("  # start your agent here (e.g. `claude`) and give it task.md")
    print("  # connection details are in ENV.md")
    print(f"  whyslow benchmark evaluate {info['scenario']}")
    print(f"  whyslow benchmark reset {info['scenario']}")


def _print_evaluate(result: dict) -> None:
    print(
        f"[benchmark] {result['scenario']}: {result['score']}/{result['max_score']} "
        f"({'PASS' if result['passed'] else 'INCOMPLETE'})"
    )
    for name, comp in result["components"].items():
        mark = "✓" if comp["ok"] else "✗"
        print(f"  {mark} {name:<24} {comp['points']:>3}/{comp['max']:<3}  {comp['detail']}")
    hints = result.get("manual_review", {}).get("report_hints", {})
    if hints:
        print(f"  · MANUAL_REVIEW (result.md): {json.dumps(hints)}")


def _print_reset(info: dict) -> None:
    notes = []
    if "actor_processes_killed" in info:
        notes.append(f"{info['actor_processes_killed']} actor process(es) stopped")
    if "backends_terminated" in info:
        notes.append(f"{info['backends_terminated']} backend(s) terminated")
    if info.get("docker_attempted"):
        notes.append("docker down" if info.get("docker_down_ok") else "DOCKER TEARDOWN FAILED")
    print(f"[benchmark] reset {info['scenario']}: {', '.join(notes) if notes else 'complete'}")
    if info.get("docker_attempted") and not info.get("docker_down_ok"):
        print(f"  ! docker compose down failed: {info.get('docker_error')}")
        print("  ! the benchmark container may still be running; re-run reset.")


def run(action: str, scenario: str | None, *, json_output: bool = False) -> int:
    if action == "list":
        for sid in sorted(SCENARIOS):
            meta = _load(sid).METADATA
            print(f"{sid:<24} {meta['summary']}")
        return 0

    if not scenario:
        raise SystemExit(f"action {action!r} requires a scenario id (see `benchmark list`)")

    module = _load(scenario)
    ctx = common.Context(scenario_id=scenario)

    if action == "setup":
        info = module.setup(ctx)
        if json_output:
            print(json.dumps(info, indent=2))
        else:
            _print_setup(info)
        return 0

    if action == "evaluate":
        result = module.evaluate(ctx)
        # Persist a machine-readable result for the record.
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out = ctx.results_dir / f"{scenario}-{stamp}.json"
        common.write_json(out, result)
        if json_output:
            print(json.dumps(result, indent=2))
        else:
            _print_evaluate(result)
            print(f"  → wrote {out}")
        return 0 if result["passed"] else 1

    if action == "reset":
        info = module.reset(ctx)
        if json_output:
            print(json.dumps(info, indent=2))
        else:
            _print_reset(info)
        # Non-zero exit if teardown was incomplete, so scripts notice.
        return 0 if info.get("docker_down_ok", True) else 1

    raise SystemExit(f"unknown action {action!r}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark", description=__doc__)
    parser.add_argument("action", choices=["list", "setup", "evaluate", "reset"])
    parser.add_argument("scenario", nargs="?", help="scenario id, e.g. pg_lock_contention_v1")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    return run(args.action, args.scenario, json_output=args.json)


if __name__ == "__main__":
    sys.exit(main())
