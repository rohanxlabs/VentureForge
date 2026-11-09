import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import contextlib
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from pydantic import ValidationError

from src.problem_intelligence import (
    FramedClaim, ProblemFrameDraft, ProblemInput, approve_frame, create_frame,
    generate_frame, latest_problem_frame, ProblemFramingProviderError, save_frame,
)
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, DecisionCreate, ProjectCreate, ProjectStageKey


class ProblemFramingP1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.temp.name) / "p1.sqlite")
        self.project = self.store.create_project(ProjectCreate(name="P1 test"))

    def tearDown(self):
        self.temp.cleanup()

    def test_input_validation_and_claim_provenance(self):
        with self.assertRaises(ValidationError):
            ProblemInput(title="   ", description="too short")
        with self.assertRaises(ValidationError):
            FramedClaim(statement="Unverified fact", origin="model_generated", status="known")
        with self.assertRaises(ValidationError):
            FramedClaim(statement="Evidence", origin="external_evidence", status="known")
        claim = FramedClaim(statement="Potential cause", origin="user_input", status="hypothesis", source_text="Potential cause")
        self.assertEqual(claim.status, "hypothesis")

    def test_manual_frame_retains_unknowns_and_does_not_force_solution(self):
        value = ProblemInput(title="Missed follow-ups", description="Rural clinic patients miss follow-up appointments", primary_users=["patients"], unknowns=["Attendance has not been measured"])
        frame = create_frame(value)
        self.assertIn("Attendance has not been measured", frame.unknowns)
        self.assertTrue(frame.claims)
        self.assertNotIn("build an app", frame.problem_statement.casefold())
        self.assertTrue(frame.research_questions)

    def test_review_revision_and_approval_target_latest_version(self):
        first, decision1 = save_frame(self.store, self.project.project_id, create_frame(
            ProblemInput(title="Delayed warnings", description="Residents receive landslide warnings late")))
        with self.assertRaisesRegex(ValueError, "latest artifact"):
            self.store.complete_project_stage(self.project.project_id, ProjectStageKey.P1)
        approve_frame(self.store, self.project.project_id, first.artifact_id, decision1.decision_id,
                      ApprovalEventStatus.CHANGES_REQUESTED, "Clarify when it happens")
        second, decision2 = save_frame(self.store, self.project.project_id, create_frame(
            ProblemInput(title="Delayed warnings", description="Residents receive landslide warnings late", when_it_occurs="during severe rain")))
        self.assertEqual(second.version, 2)
        self.assertEqual(latest_problem_frame(self.store, self.project.project_id).artifact_id, second.artifact_id)
        with self.assertRaisesRegex(ValueError, "latest Problem Frame"):
            approve_frame(self.store, self.project.project_id, first.artifact_id, decision1.decision_id,
                          ApprovalEventStatus.APPROVED)
        approve_frame(self.store, self.project.project_id, second.artifact_id, decision2.decision_id,
                      ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P1).status.value, "completed")
        self.assertEqual(len(self.store.get_artifact_approval_history(self.project.project_id, first.artifact_id)), 1)
        requested = approve_frame(self.store, self.project.project_id, second.artifact_id, decision2.decision_id,
                                  ApprovalEventStatus.CHANGES_REQUESTED, "Recheck user scope")
        repeated = approve_frame(self.store, self.project.project_id, second.artifact_id, decision2.decision_id,
                                 ApprovalEventStatus.CHANGES_REQUESTED, "Recheck user scope")
        self.assertEqual(requested.approval_id, repeated.approval_id)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P1).status.value, "in_progress")

    def test_wrong_decision_and_archived_project_rejected(self):
        artifact, decision = save_frame(self.store, self.project.project_id, create_frame(
            ProblemInput(title="Warning delay", description="Residents receive warnings after flooding starts")))
        self.store.archive_project(self.project.project_id)
        with self.assertRaisesRegex(ValueError, "Archived projects"):
            approve_frame(self.store, self.project.project_id, artifact.artifact_id, decision.decision_id,
                          ApprovalEventStatus.APPROVED)

    def test_llm_draft_provider_is_structurally_validated(self):
        value = ProblemInput(title="Slow feedback", description="Developers wait too long to learn whether tests pass")
        draft = ProblemFrameDraft(overview="Developers wait for test results.", target_user_summary="Developers",
            workflow_summary="They run tests.", problem_statement="Developers wait for test feedback.",
            research_questions=["How frequently do developers wait for test feedback?"])
        frame = create_frame(value, draft_provider=lambda _: draft)
        self.assertEqual(frame.overview, draft.overview)
        self.assertEqual(frame.claims[0].origin, "user_input")

    def test_configured_llm_path_uses_mock_and_rejects_bad_output_or_provider_failure(self):
        from openai import OpenAIError
        value = ProblemInput(title="Slow feedback", description="Developers wait too long to learn whether tests pass")
        draft = ProblemFrameDraft(overview="Developers wait for results.", target_user_summary="Developers",
            workflow_summary="They run tests.", problem_statement="Developers wait for feedback.",
            research_questions=["How frequently do developers wait for feedback?"])
        mock_config = {"api_key": "mock-key", "base_url": "https://example.invalid/v1", "model": "mock-model"}
        with patch("src.config.Settings.get_llm_config", return_value=mock_config), \
             patch("src.llm.client.get_structured_llm", return_value=SimpleNamespace(invoke=lambda _: draft)):
            self.assertEqual(generate_frame(value).overview, draft.overview)
        with patch("src.config.Settings.get_llm_config", return_value=mock_config), \
             patch("src.llm.client.get_structured_llm", return_value=SimpleNamespace(invoke=lambda _: {"overview": "bad"})):
            with self.assertRaisesRegex(ProblemFramingProviderError, "did not match"):
                generate_frame(value)
        with patch("src.config.Settings.get_llm_config", return_value=mock_config), \
             patch("src.llm.client.get_structured_llm", side_effect=OpenAIError("mock provider failure")):
            with self.assertRaisesRegex(ProblemFramingProviderError, "provider failed"):
                generate_frame(value)
        with patch("src.config.Settings.get_llm_config", return_value={"api_key": "", "base_url": "", "model": ""}):
            with self.assertRaisesRegex(ProblemFramingProviderError, "configuration is incomplete"):
                generate_frame(value)

    def _snapshot(self, artifact_id):
        with self.store.db() as c:
            approvals = [tuple(row) for row in c.execute(
                "SELECT approval_id,event_sequence,project_id,decision_id,artifact_id,status FROM approval_events ORDER BY event_sequence")]
            stages = [tuple(row) for row in c.execute(
                "SELECT stage_key,status,started_at,completed_at FROM project_stages WHERE project_id=? ORDER BY stage_key",
                (self.project.project_id,))]
            history = [tuple(row) for row in c.execute(
                "SELECT stage_key,from_status,to_status,note FROM project_stage_history WHERE project_id=? ORDER BY rowid",
                (self.project.project_id,))]
            project = tuple(c.execute("SELECT status,current_stage FROM hackathon_projects WHERE project_id=?",
                                      (self.project.project_id,)).fetchone())
            versions = [tuple(row) for row in c.execute(
                "SELECT artifact_id,version,status FROM project_artifacts WHERE project_id=? AND artifact_type='problem_frame' ORDER BY version",
                (self.project.project_id,))]
        return approvals, stages, history, project, versions

    def test_approval_transaction_rolls_back_at_every_write_boundary_and_retries(self):
        artifact, decision = save_frame(self.store, self.project.project_id, create_frame(
            ProblemInput(title="Delayed warnings", description="Residents receive warnings after flooding begins")))
        # Seed history to prove a failed repeat preserves all prior events.
        self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id, decision.decision_id,
                                         ApprovalEventStatus.CHANGES_REQUESTED, comment="Clarify context")
        baseline = self._snapshot(artifact.artifact_id)
        failure_points = ("before_writes", "after_artifact_approval", "after_decision_approval",
                          "before_stage_completion", "after_stage_transition", "before_commit")
        for point in failure_points:
            def fail_at(where, connection, target=point):
                if where == target:
                    raise RuntimeError("injected P1 approval failure at " + target)
            with self.subTest(point=point), patch.object(self.store, "_p1_approval_checkpoint", side_effect=fail_at):
                with self.assertRaisesRegex(RuntimeError, "injected P1 approval failure"):
                    self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id,
                        decision.decision_id, ApprovalEventStatus.APPROVED)
            self.assertEqual(self._snapshot(artifact.artifact_id), baseline)

        event = self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)
        state_after_success = self._snapshot(artifact.artifact_id)
        repeat = self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(repeat.approval_id, event.approval_id)
        self.assertEqual(self._snapshot(artifact.artifact_id), state_after_success)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value, "pending")

    def test_database_failure_during_stage_project_recalculation_rolls_back_approvals(self):
        artifact, decision = save_frame(self.store, self.project.project_id, create_frame(
            ProblemInput(title="Warning delay", description="Residents receive warnings after flooding begins")))
        with self.store.db() as c:
            c.execute("""CREATE TRIGGER reject_p1_approval_pointer BEFORE UPDATE OF current_stage ON hackathon_projects
                WHEN NEW.current_stage='P2' BEGIN SELECT RAISE(ABORT,'injected stage completion failure'); END""")
        baseline = self._snapshot(artifact.artifact_id)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected stage completion failure"):
            self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id,
                decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self._snapshot(artifact.artifact_id), baseline)
        with self.store.db() as c:
            c.execute("DROP TRIGGER reject_p1_approval_pointer")
        self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P1).status.value, "completed")

    def test_deferred_constraint_failure_at_commit_rolls_back_and_can_retry(self):
        artifact, decision = save_frame(self.store, self.project.project_id, create_frame(
            ProblemInput(title="Warning delay", description="Residents receive warnings after flooding begins")))
        baseline = self._snapshot(artifact.artifact_id)
        with self.store.db() as c:
            c.execute("CREATE TABLE p1_commit_parent(id INTEGER PRIMARY KEY)")
            c.execute("CREATE TABLE p1_commit_child(parent_id INTEGER REFERENCES p1_commit_parent(id) DEFERRABLE INITIALLY DEFERRED)")
            c.execute("""CREATE TRIGGER p1_commit_failure AFTER INSERT ON approval_events
                BEGIN INSERT INTO p1_commit_child(parent_id) VALUES(999); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY constraint failed"):
            self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id,
                decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self._snapshot(artifact.artifact_id), baseline)
        with self.store.db() as c:
            c.execute("DROP TRIGGER p1_commit_failure")
            c.execute("DROP TABLE p1_commit_child")
            c.execute("DROP TABLE p1_commit_parent")
        self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P1).status.value, "completed")

    def test_concurrent_duplicate_approvals_serialize_and_are_idempotent(self):
        artifact, decision = save_frame(self.store, self.project.project_id, create_frame(
            ProblemInput(title="Warning delay", description="Residents receive warnings after flooding begins")))
        barrier = Barrier(2)
        def approve():
            barrier.wait()
            return self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id,
                decision.decision_id, ApprovalEventStatus.APPROVED)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(approve), pool.submit(approve)
            first, second = first.result(), second.result()
        self.assertEqual(first.approval_id, second.approval_id)
        self.assertEqual(len(self.store.get_artifact_approval_history(self.project.project_id, artifact.artifact_id)), 1)
        self.assertEqual(len(self.store.get_decision_approval_history(self.project.project_id, decision.decision_id)), 1)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P1).status.value, "completed")

    def test_inconsistent_legacy_partial_approval_is_reported(self):
        artifact, decision = save_frame(self.store, self.project.project_id, create_frame(
            ProblemInput(title="Warning delay", description="Residents receive warnings after flooding begins")))
        self.store.record_artifact_approval(self.project.project_id, artifact.artifact_id,
                                            ApprovalEventStatus.APPROVED)
        with self.assertRaisesRegex(RuntimeError, "inconsistent"):
            self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id,
                decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P1).status.value, "in_progress")
        self.assertEqual(len(self.store.get_artifact_approval_history(self.project.project_id, artifact.artifact_id)), 1)

    def test_cli_runs_start_frame_show_review_revise_changes_approve_status(self):
        from src import research_cli
        payload_path = Path(self.temp.name) / "problem.json"
        payload_path.write_text(json.dumps({"title": "Delayed warnings",
            "description": "Residents receive landslide warnings after flooding begins",
            "primary_users": ["residents"]}), encoding="utf-8")
        db = str(self.store.path)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertIsNone(research_cli.main(["project", "problem", "start", self.project.project_id, "--database", db]))
            self.assertIsNone(research_cli.main(["project", "problem", "frame", self.project.project_id, str(payload_path), "--database", db]))
            self.assertIsNone(research_cli.main(["project", "problem", "show-frame", self.project.project_id, "--database", db]))
            self.assertIsNone(research_cli.main(["project", "problem", "review", self.project.project_id, "--database", db]))
        first = latest_problem_frame(self.store, self.project.project_id)
        first_decision = first.decision_id
        with contextlib.redirect_stdout(output):
            self.assertIsNone(research_cli.main(["project", "problem", "approve-frame", self.project.project_id,
                first.artifact_id, first_decision, "--status", "changes_requested", "--comment", "Add context", "--database", db]))
        payload_path.write_text(json.dumps({"title": "Delayed warnings",
            "description": "Residents in low-lying villages receive landslide warnings after flooding begins",
            "primary_users": ["residents"], "when_it_occurs": "during severe rain"}), encoding="utf-8")
        with contextlib.redirect_stdout(output):
            self.assertIsNone(research_cli.main(["project", "problem", "revise", self.project.project_id, str(payload_path), "--database", db]))
        latest = latest_problem_frame(self.store, self.project.project_id)
        self.assertEqual(latest.version, 2)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value, "pending")
        with contextlib.redirect_stdout(output):
            self.assertEqual(research_cli.main(["project", "problem", "approve-frame", self.project.project_id,
                first.artifact_id, first_decision, "--status", "approved", "--database", db]), 2)
            self.assertIsNone(research_cli.main(["project", "problem", "review", self.project.project_id, "--database", db]))
            self.assertIsNone(research_cli.main(["project", "problem", "approve-frame", self.project.project_id,
                latest.artifact_id, latest.decision_id, "--status", "approved", "--database", db]))
            self.assertIsNone(research_cli.main(["project", "problem", "approve-frame", self.project.project_id,
                latest.artifact_id, latest.decision_id, "--status", "approved", "--database", db]))
            self.assertIsNone(research_cli.main(["project", "problem", "status", self.project.project_id, "--database", db]))
        self.assertIn("P1 status: in_progress", output.getvalue())
        self.assertIn("P1: completed", output.getvalue())
        self.assertEqual(len(self.store.get_artifact_approval_history(self.project.project_id, latest.artifact_id)), 1)

    def test_cli_expected_failures_are_concise_and_programming_errors_propagate(self):
        from src import research_cli
        payload = Path(self.temp.name) / "valid.json"
        payload.write_text(json.dumps({"title": "Delayed warnings",
            "description": "Residents receive warnings after flooding begins"}), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = research_cli.main(["project", "problem", "start", "missing", "--database", str(self.store.path)])
        self.assertEqual(result, 2)
        self.assertIn("Unknown project", output.getvalue())
        with patch("src.problem_intelligence.generate_frame", side_effect=TypeError("bug in frame implementation")):
            with self.assertRaisesRegex(TypeError, "bug in frame implementation"):
                research_cli.main(["project", "problem", "frame", self.project.project_id,
                    str(payload), "--ai", "--database", str(self.store.path)])

    def test_wrong_project_artifact_and_decision_are_rejected(self):
        other = self.store.create_project(ProjectCreate(name="Other project"))
        other_decision = self.store.create_decision(DecisionCreate(project_id=other.project_id,
            stage_key=ProjectStageKey.P1, title="Other project's P1 approval"))
        artifact, decision = save_frame(self.store, self.project.project_id, create_frame(
            ProblemInput(title="Delayed warnings", description="Residents receive warnings after flooding begins")))
        with self.assertRaisesRegex(KeyError, "does not belong"):
            self.store.approve_problem_frame(other.project_id, artifact.artifact_id, decision.decision_id,
                                             ApprovalEventStatus.APPROVED)
        with self.assertRaisesRegex(KeyError, "does not belong"):
            self.store.approve_problem_frame(self.project.project_id, artifact.artifact_id, other_decision.decision_id,
                                             ApprovalEventStatus.APPROVED)


if __name__ == "__main__":
    unittest.main()
