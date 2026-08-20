"""Live, deterministic observation of committed benchmark state transitions.

Statement logs announce work before PostgreSQL necessarily commits it.  This
module therefore never equates statement order with durable state.  It waits
for the originating backend to leave its transaction, evaluates only externally
visible state, and marks coverage incomplete whenever consecutive mutations
cannot be separated reliably.
"""

from __future__ import annotations

from datetime import datetime, timezone
import queue
import re
import threading
import time
from typing import Callable

from . import common, effects


STATE_TIMELINE_SCHEMA_VERSION = "whyslow-state-timeline/1"
READ_ONLY = "READ_ONLY"
MUTATING = "MUTATING"
TRANSACTION = "TRANSACTION"
UNKNOWN = "UNKNOWN"

_READ_ONLY = re.compile(r"^\s*(?:SELECT|SHOW|VALUES|TABLE)\b", re.IGNORECASE)
_EXPLAIN = re.compile(r"^\s*EXPLAIN\b(?![\s\S]*\bANALYZE\b)", re.IGNORECASE)
_TRANSACTION = re.compile(
    r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK(?!\s+TO)|ABORT)\b",
    re.IGNORECASE,
)
_MUTATING = re.compile(
    r"^\s*(?:"
    r"ALTER|ANALYZE|CALL|CLUSTER|COMMENT|COPY|CREATE|DELETE|DO|DROP|GRANT|"
    r"INSERT|LOCK|MERGE|REFRESH|REINDEX|REVOKE|SECURITY\s+LABEL|TRUNCATE|"
    r"UPDATE|VACUUM"
    r")\b|"
    r"^\s*SELECT\s+(?:[\w\"]+\.)?(?:pg_terminate_backend|pg_cancel_backend|setval|"
    r"whyslow_restore_[a-z0-9_]*)\s*\(|"
    r"^\s*SELECT\b.*\bread_access_handoff\s*\(",
    re.IGNORECASE | re.DOTALL,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def classify_statement(sql: str) -> str:
    """Conservatively classify SQL; unknown statements require a checkpoint."""
    if _TRANSACTION.search(sql):
        return TRANSACTION
    if _MUTATING.search(sql):
        return MUTATING
    if _READ_ONLY.search(sql) or _EXPLAIN.search(sql):
        return READ_ONLY
    return UNKNOWN


def _transaction_action(sql: str) -> str | None:
    match = _TRANSACTION.search(sql)
    if not match:
        return None
    action = match.group(1).upper().replace(" ", "_")
    if action == "START_TRANSACTION":
        return "BEGIN"
    if action == "END":
        return "COMMIT"
    if action == "ABORT":
        return "ROLLBACK"
    return action


def analyze_state_timeline(
    checkpoints: list[dict],
    statement_events: list[dict],
    *,
    coverage_complete: bool,
    coverage_gaps: list[dict] | None = None,
) -> dict:
    """Derive boundary and retention facts without filling observation gaps."""
    known = [
        checkpoint
        for checkpoint in checkpoints
        if checkpoint.get("evaluation", {}).get("status") in {"PASS", "FAIL"}
    ]
    passes = [checkpoint for checkpoint in known if checkpoint["evaluation"]["status"] == "PASS"]
    final = checkpoints[-1] if checkpoints else None
    final_status = (final or {}).get("evaluation", {}).get("status", "UNKNOWN")
    first = passes[0] if passes else None
    first_id = first.get("checkpoint_id") if first else None
    first_event_sequence = first.get("source_event_sequence", 0) if first else None

    regression = None
    first_regression = None
    if first is not None:
        later_failures = [
            checkpoint
            for checkpoint in known
            if checkpoint["checkpoint_id"] > first_id
            and checkpoint["evaluation"]["status"] == "FAIL"
        ]
        if later_failures:
            regression = True
            first_regression = later_failures[0]["checkpoint_id"]
        elif coverage_complete:
            regression = False

    if first is not None:
        ever_correct = True
    elif coverage_complete:
        ever_correct = False
    else:
        ever_correct = None

    final_correct = True if final_status == "PASS" else False if final_status == "FAIL" else None
    retained = None
    if ever_correct is True and final_correct is not None:
        retained = final_correct

    post_events = (
        [
            event
            for event in statement_events
            if event.get("sequence", 0) > int(first_event_sequence or 0)
        ]
        if first is not None
        else []
    )
    post_reads = sum(event.get("statement_class") == READ_ONLY for event in post_events)
    post_mutations = sum(
        event.get("statement_class") in {MUTATING, UNKNOWN} for event in post_events
    )

    if not coverage_complete or any(
        checkpoint.get("evaluation", {}).get("status") == "UNKNOWN" for checkpoint in checkpoints
    ):
        status = "INCOMPLETE"
        disposition = "INDETERMINATE"
        # The final probe remains authoritative, but an observation gap makes
        # exact boundary, retention, regression, and post-boundary attribution
        # unknowable. A final PASS proves only that correctness was reached no
        # later than the final probe.
        ever_correct = True if final_correct is True else None
        first_id = None
        first_event_sequence = None
        retained = None
        regression = None
        first_regression = None
        post_reads = None
        post_mutations = None
    else:
        status = "COMPLETE"
        if not ever_correct:
            disposition = "NEVER_CORRECT"
        elif regression and final_correct:
            disposition = "REGRESSED_THEN_RECOVERED"
        elif regression:
            disposition = "REGRESSED_FINAL_FAILURE"
        else:
            disposition = "CORRECT_RETAINED"

    return {
        "temporal_status": status,
        "coverage_complete": coverage_complete,
        "coverage_gaps": list(coverage_gaps or []),
        "ever_correct": ever_correct,
        "success_boundary_checkpoint": first_id,
        "success_boundary_event": first_event_sequence,
        "final_correct": final_correct,
        "correct_state_retained": retained,
        "post_success_regression": regression,
        "first_regression_checkpoint": first_regression,
        "post_success_reads": post_reads,
        "post_success_mutations": post_mutations,
        "disposition": disposition,
    }


class LiveStateTimeline:
    """Follow one disposable PostgreSQL run and checkpoint stable transitions."""

    def __init__(
        self,
        scenario_module,
        ctx: common.Context,
        *,
        settle_timeout: float = 3.0,
        settle_interval: float = 0.02,
    ):
        self.scenario_module = scenario_module
        self.ctx = ctx
        self.settle_timeout = settle_timeout
        self.settle_interval = settle_interval
        self.evaluate_state: Callable = getattr(scenario_module, "evaluate_state")
        self.checkpoints: list[dict] = []
        self.statement_events: list[dict] = []
        self.coverage_gaps: list[dict] = []
        self._transactions: dict[int, list[dict]] = {}
        self._queue: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._follower = None
        self._sequence = 0

    def _evaluation(self) -> dict:
        started = time.monotonic()
        try:
            result = self.evaluate_state(self.ctx)
            correct = result.get("correct")
            status = "PASS" if correct is True else "FAIL" if correct is False else "UNKNOWN"
            return {
                "status": status,
                "correct": correct if isinstance(correct, bool) else None,
                "checks": result.get("checks", {}),
                "details": result.get("details", {}),
                "duration_seconds": round(time.monotonic() - started, 4),
            }
        except Exception as exc:
            return {
                "status": "UNKNOWN",
                "correct": None,
                "checks": {},
                "error": f"{type(exc).__name__}: {exc}",
                "duration_seconds": round(time.monotonic() - started, 4),
            }

    def _append_checkpoint(
        self,
        trigger: str,
        *,
        source_events: list[dict] | None = None,
        evaluation: dict | None = None,
    ) -> None:
        sources = source_events or []
        self.checkpoints.append(
            {
                "checkpoint_id": len(self.checkpoints),
                "timestamp": _utc_now(),
                "trigger": trigger,
                "source_event_ids": [event["event_id"] for event in sources],
                "source_event_sequence": max(
                    (event.get("sequence", 0) for event in sources), default=0
                ),
                "statement_classes": sorted({event["statement_class"] for event in sources}),
                "evaluation": evaluation or self._evaluation(),
                "_completed_monotonic": time.monotonic(),
            }
        )

    def start(self) -> None:
        self._append_checkpoint("initial")
        since = _utc_now()
        self._follower = common.compose_log_follower(since=since)
        self._reader = threading.Thread(target=self._read_logs, daemon=True)
        self._worker = threading.Thread(target=self._consume, daemon=True)
        self._reader.start()
        self._worker.start()

    def _read_logs(self) -> None:
        try:
            assert self._follower is not None and self._follower.stdout is not None
            for line in self._follower.stdout:
                parsed = effects.parse_postgres_statement_line(line)
                if parsed is None:
                    continue
                self._sequence += 1
                event = {
                    "event_id": f"db-{self._sequence}",
                    "sequence": self._sequence,
                    "observed_at": _utc_now(),
                    "observed_monotonic": time.monotonic(),
                    **parsed,
                }
                event["statement_class"] = classify_statement(event["statement"])
                self._queue.put(event)
        except Exception as exc:
            self.coverage_gaps.append(
                {"reason": f"database log follower failed: {type(exc).__name__}: {exc}"}
            )

    def _wait_for_settle(self, pid: int) -> bool:
        deadline = time.monotonic() + self.settle_timeout
        while time.monotonic() < deadline:
            try:
                if common.backend_transaction_settled(self.ctx.config, pid):
                    return True
            except Exception:
                pass
            time.sleep(self.settle_interval)
        return False

    def _record_transition(self, trigger: str, sources: list[dict]) -> None:
        pid = int(sources[-1]["pid"])
        if not self._wait_for_settle(pid):
            self.coverage_gaps.append(
                {
                    "reason": "backend transaction did not settle before checkpoint timeout",
                    "source_event_ids": [event["event_id"] for event in sources],
                }
            )
            self._append_checkpoint(
                trigger,
                source_events=sources,
                evaluation={
                    "status": "UNKNOWN",
                    "correct": None,
                    "checks": {},
                    "error": "backend did not reach an externally visible settled state",
                },
            )
            return
        evaluation = self._evaluation()
        self._append_checkpoint(trigger, source_events=sources, evaluation=evaluation)

    def _consume(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                event = item
                self.statement_events.append(event)
                statement_class = event["statement_class"]
                action = _transaction_action(event["statement"])
                pid = int(event["pid"])

                if statement_class == TRANSACTION:
                    if action == "BEGIN":
                        self._transactions[pid] = []
                    elif action in {"COMMIT", "ROLLBACK"}:
                        pending = self._transactions.pop(pid, [])
                        if pending:
                            self._record_transition(
                                (
                                    "transaction_commit"
                                    if action == "COMMIT"
                                    else "transaction_rollback"
                                ),
                                [*pending, event],
                            )
                    continue

                if statement_class == READ_ONLY:
                    continue

                if pid in self._transactions:
                    self._transactions[pid].append(event)
                    continue

                self._record_transition("database_mutation", [event])
        except Exception as exc:
            self.coverage_gaps.append(
                {"reason": f"checkpoint worker failed: {type(exc).__name__}: {exc}"}
            )

    def stop(self) -> dict:
        # Give Compose a brief chance to forward the final PostgreSQL log line.
        time.sleep(0.15)
        if self._follower is not None and self._follower.poll() is None:
            self._follower.terminate()
            try:
                self._follower.wait(timeout=2)
            except Exception:
                self._follower.kill()
        if self._reader is not None:
            self._reader.join(timeout=2)
        if self._follower is not None and self._follower.stdout is not None:
            self._follower.stdout.close()
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=30.0)
            if self._worker.is_alive():
                self.coverage_gaps.append(
                    {"reason": "checkpoint worker did not finish before the bounded timeout"}
                )

        if self._transactions:
            self.coverage_gaps.append(
                {
                    "reason": "agent exited with an observed transaction lacking COMMIT/ROLLBACK",
                    "backend_pids": sorted(self._transactions),
                }
            )

        # If another mutation was announced before a preceding checkpoint
        # completed, the probe may have observed multiple committed changes at
        # once. That run cannot support negative claims such as "never correct."
        for checkpoint in self.checkpoints[1:]:
            completed = checkpoint.get("_completed_monotonic", 0.0)
            source_ids = set(checkpoint.get("source_event_ids", []))
            source_sequence = checkpoint.get("source_event_sequence", 0)
            overlapping = [
                event
                for event in self.statement_events
                if event["statement_class"] in {MUTATING, UNKNOWN}
                and event["event_id"] not in source_ids
                and event["sequence"] > source_sequence
                and event["observed_monotonic"] <= completed
            ]
            if overlapping:
                checkpoint["evaluation"] = {
                    "status": "UNKNOWN",
                    "correct": None,
                    "checks": {},
                    "error": "a later mutation overlapped this checkpoint",
                }
                self.coverage_gaps.append(
                    {
                        "reason": "a later mutation was observed before the prior checkpoint completed",
                        "after_checkpoint": checkpoint["checkpoint_id"],
                        "source_event_ids": [event["event_id"] for event in overlapping],
                    }
                )

        self._append_checkpoint("final")
        complete = not self.coverage_gaps and all(
            checkpoint["evaluation"]["status"] != "UNKNOWN" for checkpoint in self.checkpoints
        )
        analysis = analyze_state_timeline(
            self.checkpoints,
            self.statement_events,
            coverage_complete=complete,
            coverage_gaps=self.coverage_gaps,
        )
        public_checkpoints = [
            {key: value for key, value in checkpoint.items() if not key.startswith("_")}
            for checkpoint in self.checkpoints
        ]
        return {
            "schema_version": STATE_TIMELINE_SCHEMA_VERSION,
            "supported": True,
            "scenario": self.ctx.scenario_id,
            "checkpoints": public_checkpoints,
            "statement_events": [
                {key: value for key, value in event.items() if key != "observed_monotonic"}
                for event in self.statement_events
            ],
            "analysis": analysis,
        }


def unavailable_timeline(scenario_id: str, reason: str) -> dict:
    return {
        "schema_version": STATE_TIMELINE_SCHEMA_VERSION,
        "supported": False,
        "scenario": scenario_id,
        "reason": reason,
        "checkpoints": [],
        "statement_events": [],
        "analysis": {
            "temporal_status": "UNSUPPORTED",
            "coverage_complete": False,
            "ever_correct": None,
            "final_correct": None,
            "post_success_regression": None,
        },
    }


def timeline_markdown(document: dict) -> str:
    """Render the machine-readable timeline without changing its semantics."""
    analysis = document["analysis"]
    lines = [
        f"# State timeline: {document['scenario']}",
        "",
        f"- Temporal status: **{analysis['temporal_status']}**",
        f"- Coverage complete: `{analysis.get('coverage_complete', False)}`",
        f"- Ever correct: `{analysis.get('ever_correct')}`",
        f"- Final state correct: `{analysis.get('final_correct')}`",
        f"- Correct state retained: `{analysis.get('correct_state_retained')}`",
        f"- Post-success regression: `{analysis.get('post_success_regression')}`",
        f"- Post-success reads: `{analysis.get('post_success_reads', 0)}`",
        f"- Post-success mutations: `{analysis.get('post_success_mutations', 0)}`",
        "",
        "## Committed-state checkpoints",
        "",
        "| Checkpoint | Trigger | Source events | State |",
        "|---:|---|---|---|",
    ]
    for checkpoint in document.get("checkpoints", []):
        marker = (
            " ← first correct state"
            if checkpoint["checkpoint_id"] == analysis.get("success_boundary_checkpoint")
            else ""
        )
        sources = ", ".join(checkpoint.get("source_event_ids", [])) or "—"
        lines.append(
            f"| {checkpoint['checkpoint_id']} | {checkpoint['trigger']} | {sources} | "
            f"{checkpoint['evaluation']['status']}{marker} |"
        )
    lines.extend(
        [
            "",
            "## Observed PostgreSQL statements",
            "",
            "| Event | PID | Class | Statement |",
            "|---|---:|---|---|",
        ]
    )
    for event in document.get("statement_events", []):
        statement = event["statement"].replace("|", "\\|")
        if len(statement) > 160:
            statement = statement[:157] + "..."
        lines.append(
            f"| {event['event_id']} | {event['pid']} | {event['statement_class']} | `{statement}` |"
        )
    gaps = analysis.get("coverage_gaps", [])
    if gaps:
        lines.extend(["", "## Coverage limitations", ""])
        lines.extend(f"- {gap['reason']}" for gap in gaps)
    return "\n".join(lines) + "\n"
