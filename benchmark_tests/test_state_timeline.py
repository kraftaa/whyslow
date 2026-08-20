"""Success-boundary classification, analysis, and Docker integration tests."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

from whyslow.benchmark import common, state_timeline
from whyslow.benchmark.effects import parse_postgres_statement_line
from whyslow.benchmark.runner import run_trajectory
from whyslow.benchmark.scenarios import pg_revoked_privilege_v1


def _checkpoint(checkpoint_id: int, status: str, source: int = 0) -> dict:
    return {
        "checkpoint_id": checkpoint_id,
        "source_event_sequence": source,
        "evaluation": {"status": status, "correct": status == "PASS"},
    }


class StatementClassificationTests(unittest.TestCase):
    def test_read_only_and_side_effecting_select(self):
        self.assertEqual(state_timeline.classify_statement("SELECT 1"), "READ_ONLY")
        self.assertEqual(
            state_timeline.classify_statement("SELECT pg_terminate_backend(42)"),
            "MUTATING",
        )
        self.assertEqual(
            state_timeline.classify_statement("SELECT * FROM read_access_handoff()"),
            "MUTATING",
        )

    def test_unknown_is_not_assumed_read_only(self):
        self.assertEqual(state_timeline.classify_statement("SET work_mem = '1MB'"), "UNKNOWN")

    def test_transaction_controls_are_separate(self):
        self.assertEqual(state_timeline.classify_statement("BEGIN"), "TRANSACTION")
        self.assertEqual(state_timeline.classify_statement("ROLLBACK"), "TRANSACTION")
        self.assertEqual(state_timeline.classify_statement("ROLLBACK TO SAVEPOINT x"), "UNKNOWN")

    def test_log_parser_preserves_backend_identity(self):
        line = (
            "db-1 | 2026-08-14T12:00:00Z 2026-08-14 12:00:00 UTC "
            "[321] user=whyslow_agent db=whyslow_bench app=psql LOG:  "
            "statement: GRANT SELECT ON reports TO app"
        )
        parsed = parse_postgres_statement_line(line)
        self.assertEqual(parsed["pid"], 321)
        self.assertEqual(parsed["application_name"], "psql")
        self.assertTrue(parsed["statement"].startswith("GRANT SELECT"))


class TimelineAnalysisTests(unittest.TestCase):
    def analyze(self, states, *, complete=True, events=None):
        checkpoints = [_checkpoint(index, status, index) for index, status in enumerate(states)]
        return state_timeline.analyze_state_timeline(
            checkpoints, events or [], coverage_complete=complete
        )

    def test_never_fixed(self):
        result = self.analyze(["FAIL", "FAIL", "FAIL"])
        self.assertFalse(result["ever_correct"])
        self.assertFalse(result["post_success_regression"])
        self.assertEqual(result["disposition"], "NEVER_CORRECT")

    def test_correct_and_retained(self):
        result = self.analyze(["FAIL", "PASS"])
        self.assertTrue(result["ever_correct"])
        self.assertTrue(result["final_correct"])
        self.assertTrue(result["correct_state_retained"])

    def test_read_only_verification_is_not_mutation(self):
        events = [
            {"sequence": 2, "statement_class": "READ_ONLY"},
            {"sequence": 3, "statement_class": "READ_ONLY"},
        ]
        result = self.analyze(["FAIL", "PASS", "PASS"], events=events)
        self.assertEqual(result["post_success_reads"], 2)
        self.assertEqual(result["post_success_mutations"], 0)

    def test_harmless_post_success_mutation(self):
        events = [{"sequence": 2, "statement_class": "MUTATING"}]
        result = self.analyze(["FAIL", "PASS", "PASS"], events=events)
        self.assertEqual(result["post_success_mutations"], 1)
        self.assertFalse(result["post_success_regression"])

    def test_regression_and_recovery_remains_visible(self):
        result = self.analyze(["FAIL", "PASS", "FAIL", "PASS"])
        self.assertTrue(result["post_success_regression"])
        self.assertTrue(result["final_correct"])
        self.assertEqual(result["disposition"], "REGRESSED_THEN_RECOVERED")

    def test_incomplete_coverage_never_becomes_negative_claim(self):
        result = self.analyze(["FAIL", "FAIL"], complete=False)
        self.assertIsNone(result["ever_correct"])
        self.assertIsNone(result["post_success_regression"])
        self.assertEqual(result["temporal_status"], "INCOMPLETE")


@unittest.skipUnless(
    common.use_docker() and common.compose_available(),
    "Docker benchmark mode unavailable",
)
class LiveTransactionTests(unittest.TestCase):
    agent = Path(__file__).with_name("state_timeline_agent.py")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_home = os.environ.get("WHYSLOW_BENCH_HOME")
        os.environ["WHYSLOW_BENCH_HOME"] = self.temp.name

    def tearDown(self):
        pg_revoked_privilege_v1.reset(common.Context("pg_revoked_privilege_v1"))
        if self.previous_home is None:
            os.environ.pop("WHYSLOW_BENCH_HOME", None)
        else:
            os.environ["WHYSLOW_BENCH_HOME"] = self.previous_home
        self.temp.cleanup()

    def run_mode(self, mode: str) -> dict:
        return run_trajectory(
            pg_revoked_privilege_v1,
            common.Context("pg_revoked_privilege_v1"),
            [sys.executable, str(self.agent), mode],
            timeout=30,
            reset_after=False,
            track_state_timeline=True,
        )

    def test_committed_repair_creates_boundary(self):
        timeline = self.run_mode("commit")["state_timeline"]
        self.assertEqual(timeline["analysis"]["temporal_status"], "COMPLETE")
        self.assertTrue(timeline["analysis"]["ever_correct"])
        self.assertTrue(timeline["analysis"]["final_correct"])

    def test_rollback_does_not_create_boundary(self):
        timeline = self.run_mode("rollback")["state_timeline"]
        self.assertEqual(timeline["analysis"]["temporal_status"], "COMPLETE")
        self.assertFalse(timeline["analysis"]["ever_correct"])
        self.assertFalse(timeline["analysis"]["final_correct"])

    def test_regression_then_recovery_is_retained_in_timeline(self):
        timeline = self.run_mode("regress-recover")["state_timeline"]
        self.assertEqual(timeline["analysis"]["temporal_status"], "COMPLETE")
        self.assertTrue(timeline["analysis"]["post_success_regression"])
        self.assertTrue(timeline["analysis"]["final_correct"])

    def test_over_broad_grant_never_establishes_boundary(self):
        timeline = self.run_mode("over-broad")["state_timeline"]
        self.assertFalse(timeline["analysis"]["ever_correct"])
        self.assertFalse(timeline["analysis"]["final_correct"])


if __name__ == "__main__":
    unittest.main()
