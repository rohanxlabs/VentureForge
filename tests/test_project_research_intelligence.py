"""Project P2 integration tests; all returned source content is synthetic."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from unittest.mock import patch

from src.problem_intelligence import ProblemInput, approve_frame, create_frame, create_research_plan, revise_research_plan, save_frame
from src.project_store import ProjectStore
from src.research.budget import SearchBudget
from src.research.models import (
    ApprovalEventStatus, CandidateExtraction, ProjectCreate, ProjectStageKey,
    QuestionPriority, ResearchQuestion, SourceSnapshot, SupportRelation, VerificationStatus,
)
from src.research.pipeline import CollectionUnavailableError
from src.research.project_intelligence import (
    execute_project_research, latest_approved_plan, project_research_records,
    report_document,
)
from src.research.search import CollectionFailure, CollectionResult, SearchOrchestrator
from src.research.connectors import ResearchConnectorError


class SyntheticOrchestrator:
    def collect(self, *, run_id, prompt, questions, budget):
        sources = []
        for i, question in enumerate(questions):
            sources.append(SourceSnapshot(run_id=run_id, question_id=question.id,
                url=f"https://synthetic-{i}.example.test/report", title=f"Synthetic source {i}",
                publisher=f"Synthetic publisher {i}", content=f"Synthetic evidence passage for question {i}.",
                source_categories=["official"], content_origin="synthetic_test_fixture"))
        return CollectionResult(sources=sources, searches_attempted=len(questions),
            searches_succeeded=len(questions), searches_failed=0)


class FailingOrchestrator:
    def collect(self, *, run_id, prompt, questions, budget):
        failure = CollectionFailure(questions[0].id, "official", "mock", "mock connector unavailable",
                                    ResearchConnectorError("mock connector unavailable"))
        return CollectionResult(failures=[failure], searches_attempted=1,
            searches_succeeded=0, searches_failed=1)


class PartialOrchestrator(SyntheticOrchestrator):
    def collect(self, *, run_id, prompt, questions, budget):
        result = super().collect(run_id=run_id, prompt=prompt, questions=questions[1:], budget=budget)
        result.searches_attempted = len(questions)
        result.searches_succeeded = len(questions) - 1
        result.searches_failed = 1
        result.failures = [CollectionFailure(questions[0].id, "official", "mock",
            "one synthetic connector request failed")]
        return result


class EmptyOrchestrator:
    def collect(self, *, run_id, prompt, questions, budget):
        return CollectionResult(searches_attempted=len(questions),
                                searches_succeeded=len(questions), searches_failed=0)


def synthetic_extractor(question, source):
    return CandidateExtraction(relevant=True,
        claim=f"Synthetic evidence for {question}", passage=source.content,
        relation=SupportRelation.SUPPORTS)


class ProjectResearchIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.temp.name) / "project-p2.sqlite")
        self.project = self.store.create_project(ProjectCreate(name="Synthetic P2 project"))
        frame = create_frame(ProblemInput(title="Warning delays",
            description="Residents receive landslide warnings too late to evacuate safely",
            primary_users=["residents in landslide-prone communities"], geography="Northeast India",
            unknowns=["Warning receipt time has not been measured"]))
        artifact, decision = save_frame(self.store, self.project.project_id, frame)
        approve_frame(self.store, self.project.project_id, artifact.artifact_id,
                      decision.decision_id, ApprovalEventStatus.APPROVED)

    def tearDown(self):
        self.temp.cleanup()

    def make_plan(self):
        plan, artifact, decision = create_research_plan(self.store, self.project.project_id)
        return plan, artifact, decision

    def approve_plan(self, artifact, decision):
        return self.store.approve_p2_artifact(self.project.project_id, artifact.artifact_id,
            decision.decision_id, "research_plan", ApprovalEventStatus.APPROVED)

    def test_plan_requires_approved_p1_and_contains_research_rationales(self):
        plan, artifact, _ = self.make_plan()
        self.assertEqual(artifact.stage_key, ProjectStageKey.P2)
        self.assertGreaterEqual(len(plan.objectives), 3)
        self.assertTrue(all(q.objective and q.problem_dimension and q.priority_rationale and q.evidence_required
                            for q in plan.questions))
        self.assertTrue(all(q.project_id == self.project.project_id and q.plan_id == plan.id for q in plan.questions))
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value,
                         "in_progress")

    def test_structured_planner_provider_creates_model_generated_question_draft(self):
        def provider(context):
            self.assertIn("approved_problem_frame", context)
            return [ResearchQuestion(run_id="mock-run", text=f"How often does context question {i} occur?",
                priority=QuestionPriority.HIGH if i == 0 else QuestionPriority.MEDIUM,
                source_categories=["official", "research_literature"],
                evidence_required="A dated report or dataset with the population and geography specified.",
                decision_impact="Clarifies whether the problem is widespread.") for i in range(3)]
        plan, _, _ = create_research_plan(self.store, self.project.project_id,
                                          question_provider=provider)
        self.assertEqual(plan.planning_method, "configured_llm")
        self.assertIn("model-generated drafts", " ".join(plan.known_limitations))
        self.assertIn("How often", plan.questions[0].text)

    def test_plan_approval_is_atomic_and_changes_requested_reopens_plan(self):
        plan, artifact, decision = self.make_plan()
        self.store.approve_p2_artifact(self.project.project_id, artifact.artifact_id,
            decision.decision_id, "research_plan", ApprovalEventStatus.CHANGES_REQUESTED,
            comment="Clarify measured impacts")
        self.assertEqual(self.store.get_plan(plan.id).status, "draft")
        self.assertEqual(self.store.get_artifact_approval_status(self.project.project_id, artifact.artifact_id),
                         "changes_requested")
        event = self.approve_plan(artifact, decision)
        repeated = self.approve_plan(artifact, decision)
        self.assertEqual(event.approval_id, repeated.approval_id)
        self.assertEqual(self.store.get_plan(plan.id).status, "approved")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value,
                         "in_progress")

    def test_edited_plan_is_saved_as_new_version_with_fresh_question_ownership(self):
        original, original_artifact, _ = self.make_plan()
        edited = json.loads(original.model_dump_json())
        edited["objectives"].append("Check whether the reported impact varies by season.")
        edited["questions"][0]["text"] = "How does reported event frequency vary by season in the stated geography?"
        revised, artifact, _ = revise_research_plan(self.store, self.project.project_id,
            json.dumps(edited))
        self.assertEqual(artifact.version, original_artifact.version + 1)
        self.assertNotEqual(revised.run_id, original.run_id)
        self.assertNotEqual(revised.id, original.id)
        self.assertEqual(revised.questions[0].text, edited["questions"][0]["text"])
        self.assertTrue(all(question.run_id == revised.run_id and question.plan_id == revised.id
                            and question.project_id == self.project.project_id for question in revised.questions))

    def test_plan_approval_failure_rolls_back_and_retry_succeeds(self):
        plan, artifact, decision = self.make_plan()
        def snapshot():
            with self.store.db() as c:
                return (c.execute("SELECT status,plan_json FROM research_plans WHERE plan_id=?", (plan.id,)).fetchone()[:],
                    [tuple(row) for row in c.execute("SELECT event_sequence,artifact_id,decision_id,status FROM approval_events ORDER BY event_sequence")],
                    tuple(c.execute("SELECT status,current_stage FROM hackathon_projects WHERE project_id=?",
                                    (self.project.project_id,)).fetchone()),
                    tuple(c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P2'",
                                    (self.project.project_id,)).fetchone()))
        baseline = snapshot()
        for point in ("before_writes", "after_plan_update", "after_artifact_approval",
                      "after_decision_approval", "before_commit"):
            def injected(where, connection, target=point):
                if where == target:
                    raise RuntimeError("injected P2 approval failure")
            with self.subTest(point=point), patch.object(self.store, "_p2_approval_checkpoint", side_effect=injected):
                with self.assertRaisesRegex(RuntimeError, "injected P2 approval failure"):
                    self.approve_plan(artifact, decision)
            self.assertEqual(snapshot(), baseline)
        event = self.approve_plan(artifact, decision)
        self.assertEqual(event.status, ApprovalEventStatus.APPROVED)

    def test_plan_creation_rolls_back_stage_run_questions_decision_and_artifact_together(self):
        with self.store.db() as c:
            c.execute("""CREATE TRIGGER fail_p2_plan_artifact BEFORE INSERT ON project_artifacts
                WHEN NEW.artifact_type='research_plan'
                BEGIN SELECT RAISE(ABORT,'injected plan artifact failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected plan artifact failure"):
            create_research_plan(self.store, self.project.project_id)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value,
                         "pending")
        self.assertEqual(self.store.list_project_workflows(self.project.project_id), [])
        with self.store.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM research_plans").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM decisions WHERE project_id=? AND stage_key='P2'",
                                       (self.project.project_id,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM project_artifacts WHERE project_id=? AND stage_key='P2'",
                                       (self.project.project_id,)).fetchone()[0], 0)
            c.execute("DROP TRIGGER fail_p2_plan_artifact")
        self.make_plan()
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value,
                         "in_progress")

    def test_project_run_persists_sources_evidence_gaps_report_and_workflow(self):
        plan, plan_artifact, decision = self.make_plan()
        self.approve_plan(plan_artifact, decision)
        execution, report, report_decision, workflow = execute_project_research(
            self.store, self.project.project_id, budget=SearchBudget(max_search_calls=8,
                max_pages=20, max_tavily_credits=8, max_seconds=20),
            orchestrator=SyntheticOrchestrator(), max_rounds=1, min_independent_sources=2,
            extractor=synthetic_extractor)
        self.assertEqual(workflow.status.value, "completed")
        self.assertEqual(execution.sources_collected, len(plan.questions))
        self.assertEqual(execution.evidence_extracted, len(plan.questions))
        self.assertFalse(execution.gap_report.complete)  # One synthetic source per question is below threshold.
        _, saved_questions, sources, evidence = self.store.get_run_records(plan.run_id)
        self.assertEqual(len(sources), len(plan.questions))
        self.assertTrue(all(item.verification_status == VerificationStatus.VERIFIED for item in evidence))
        self.assertTrue(all(item.source_id in {source.id for source in sources} for item in evidence))
        self.assertEqual(len(project_research_records(self.store, self.project.project_id, category="sources")), len(sources))
        artifact, data = report_document(self.store, self.project.project_id)
        self.assertEqual(artifact.artifact_id, report.artifact_id)
        self.assertIn("Synthetic evidence passage", data["markdown"])
        self.assertIn("does not establish semantic support or factual truth", data["citation_validity_note"])
        self.assertEqual(data["outcome"]["execution_status"], "complete")
        self.assertEqual(len(data["sources"]), len(sources))
        self.assertEqual(len(data["evidence"]), len(evidence))
        self.assertEqual(data["problem_context"]["problem_statement"], plan.brief.problem_statement)
        self.assertEqual(len(data["questions"]), len(plan.questions))
        with self.assertRaisesRegex(ValueError, "latest research plan must be approved"):
            # A new draft plan supersedes the approved one; research cannot run against the older version.
            self.make_plan()
            execute_project_research(self.store, self.project.project_id, orchestrator=SyntheticOrchestrator(),
                max_rounds=1, extractor=synthetic_extractor)

    def test_report_approval_failure_rolls_back_and_retry_completes_only_p2(self):
        plan, plan_artifact, plan_decision = self.make_plan()
        self.approve_plan(plan_artifact, plan_decision)
        _, report, decision, _ = execute_project_research(self.store, self.project.project_id,
            orchestrator=SyntheticOrchestrator(), max_rounds=1, extractor=synthetic_extractor)
        def snapshot():
            with self.store.db() as c:
                return ([tuple(row) for row in c.execute("SELECT event_sequence,artifact_id,decision_id,status FROM approval_events ORDER BY event_sequence")],
                    [tuple(row) for row in c.execute("SELECT stage_key,from_status,to_status,note FROM project_stage_history WHERE project_id=? ORDER BY rowid",
                                                      (self.project.project_id,))],
                    tuple(c.execute("SELECT status,current_stage FROM hackathon_projects WHERE project_id=?",
                                    (self.project.project_id,)).fetchone()),
                    tuple(c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P2'",
                                    (self.project.project_id,)).fetchone()))
        baseline = snapshot()
        for point in ("before_writes", "after_artifact_approval", "after_decision_approval",
                      "before_stage_completion", "after_stage_transition", "before_commit"):
            def injected(where, connection, target=point):
                if where == target:
                    raise RuntimeError("injected report approval failure")
            with self.subTest(point=point), patch.object(self.store, "_p2_approval_checkpoint", side_effect=injected):
                with self.assertRaisesRegex(RuntimeError, "injected report approval failure"):
                    self.store.approve_p2_artifact(self.project.project_id, report.artifact_id,
                        decision.decision_id, "research_report", ApprovalEventStatus.APPROVED)
            self.assertEqual(snapshot(), baseline)
        self.store.approve_p2_artifact(self.project.project_id, report.artifact_id,
            decision.decision_id, "research_report", ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value,
                         "completed")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P3).status.value,
                         "pending")
        self.store.approve_p2_artifact(self.project.project_id, report.artifact_id,
            decision.decision_id, "research_report", ApprovalEventStatus.APPROVED)
        self.assertEqual(len(self.store.get_artifact_approval_history(self.project.project_id, report.artifact_id)), 1)

    def test_total_collection_failure_marks_workflow_failed_without_report_or_p2_completion(self):
        plan, artifact, decision = self.make_plan()
        self.approve_plan(artifact, decision)
        with self.assertRaises(CollectionUnavailableError):
            execute_project_research(self.store, self.project.project_id,
                orchestrator=FailingOrchestrator(), max_rounds=1, extractor=synthetic_extractor)
        workflows = [item for item in self.store.list_project_workflows(self.project.project_id)
                     if item.workflow_type == "p2_research_execution"]
        self.assertEqual(len(workflows), 1)
        self.assertEqual(workflows[0].status.value, "failed")
        self.assertIsNotNone(workflows[0].error_summary)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value,
                         "in_progress")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P3).status.value,
                         "pending")

    def test_missing_connector_does_not_create_approvable_complete_report(self):
        _, artifact, decision = self.make_plan()
        self.approve_plan(artifact, decision)
        execution, report, report_decision, workflow = execute_project_research(
            self.store, self.project.project_id, orchestrator=SearchOrchestrator(None, None),
            max_rounds=1, extractor=synthetic_extractor)
        self.assertEqual(execution.searches_attempted, 0)
        self.assertEqual(workflow.status.value, "completed")
        _, data = report_document(self.store, self.project.project_id)
        self.assertEqual(data["outcome"]["execution_status"], "partial")
        with self.assertRaisesRegex(ValueError, "partial research report"):
            self.store.approve_p2_artifact(self.project.project_id, report.artifact_id,
                report_decision.decision_id, "research_report", ApprovalEventStatus.APPROVED)

    def test_partial_connector_failure_and_successful_empty_searches_are_reported_honestly(self):
        plan, artifact, decision = self.make_plan()
        self.approve_plan(artifact, decision)
        execution, report, _, workflow = execute_project_research(self.store, self.project.project_id,
            orchestrator=PartialOrchestrator(), max_rounds=1, extractor=synthetic_extractor)
        self.assertEqual(workflow.status.value, "completed")
        self.assertGreater(execution.searches_succeeded, 0)
        self.assertEqual(execution.searches_failed, 1)
        _, data = report_document(self.store, self.project.project_id)
        self.assertEqual(data["outcome"]["execution_status"], "partial")
        self.assertEqual(len(data["failures"]), 1)
        self.assertIn("one synthetic connector request failed", data["failures"][0]["reason"])
        with self.assertRaisesRegex(ValueError, "partial research report"):
            self.store.approve_p2_artifact(self.project.project_id, report.artifact_id,
                report.decision_id, "research_report", ApprovalEventStatus.APPROVED)

        # A second report is permitted after a human requests changes to the first.
        self.store.approve_p2_artifact(self.project.project_id, report.artifact_id,
            report_document(self.store, self.project.project_id)[0].decision_id,
            "research_report", ApprovalEventStatus.CHANGES_REQUESTED, comment="Need more sources")
        revised_plan, revised_artifact, revised_decision = self.make_plan()
        self.approve_plan(revised_artifact, revised_decision)
        empty, _, _, _ = execute_project_research(self.store, self.project.project_id,
            orchestrator=EmptyOrchestrator(), max_rounds=1, extractor=synthetic_extractor)
        self.assertEqual(empty.sources_collected, 0)
        self.assertEqual(empty.searches_succeeded, len(revised_plan.questions))
        latest_artifact, empty_data = report_document(self.store, self.project.project_id)
        self.assertEqual(latest_artifact.version, 2)
        self.assertIn("No evidence items were extracted", empty_data["markdown"])

    def test_plan_is_rejected_if_p1_frame_changes_after_plan_draft(self):
        _, artifact, decision = self.make_plan()
        frame = create_frame(ProblemInput(title="Warning delays",
            description="Residents receive warnings after flooding begins, limiting time to reach safety"))
        save_frame(self.store, self.project.project_id, frame)
        with self.assertRaisesRegex(ValueError, "latest approved Problem Frame"):
            self.approve_plan(artifact, decision)

    def test_cross_project_plan_and_missing_plan_rejected(self):
        with self.assertRaisesRegex(ValueError, "No P2 research plan"):
            latest_approved_plan(self.store, self.project.project_id)
        _, artifact, _ = self.make_plan()
        other = self.store.create_project(ProjectCreate(name="Other P2 project"))
        with self.assertRaises(KeyError):
            self.store.approve_p2_artifact(other.project_id, artifact.artifact_id,
                artifact.decision_id, "research_plan", ApprovalEventStatus.APPROVED)

    def test_project_p2_cli_exposes_plan_execution_review_and_approval(self):
        from src import research_cli
        output = io.StringIO()
        plan, artifact, decision = self.make_plan()
        db = str(self.store.path)
        with contextlib.redirect_stdout(output):
            self.assertIsNone(research_cli.main(["project", "research", "plan-show",
                self.project.project_id, "--database", db]))
            self.assertIsNone(research_cli.main(["project", "research", "plan-approve",
                self.project.project_id, artifact.artifact_id, "--database", db]))
        execution, report, _, _ = execute_project_research(self.store, self.project.project_id,
            orchestrator=SyntheticOrchestrator(), max_rounds=1, extractor=synthetic_extractor)
        with contextlib.redirect_stdout(output):
            for command in ("sources", "findings", "coverage", "gaps", "report", "status"):
                self.assertIsNone(research_cli.main(["project", "research", command,
                    self.project.project_id, "--database", db]))
            self.assertIsNone(research_cli.main(["project", "research", "report-approve",
                self.project.project_id, report.artifact_id, "--database", db]))
        self.assertIn(plan.id, output.getvalue())
        self.assertIn("P2 completed.", output.getvalue())
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P3).status.value,
                         "pending")


if __name__ == "__main__":
    unittest.main()
