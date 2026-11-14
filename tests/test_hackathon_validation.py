import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.hackathon_validation import (
    PrimaryResearchMethod,
    PrimaryResearchRecord,
    ValidationDecision,
    ValidationOutcome,
    save_primary_research_record,
    save_validation_decision,
    validation_context,
)
from src.problem_intelligence import ProblemInput, approve_frame, create_frame, create_research_plan, save_frame
from src.project_store import ProjectStore
from src.research.budget import SearchBudget
from src.research.models import (
    ApprovalEventStatus,
    CandidateExtraction,
    ProjectCreate,
    ProjectStageKey,
    SourceSnapshot,
    SupportRelation,
)
from src.research.project_intelligence import execute_project_research
from src.research.search import CollectionResult


class SyntheticOrchestrator:
    def collect(self, *, run_id, prompt, questions, budget):
        return CollectionResult(sources=[SourceSnapshot(run_id=run_id,
            question_id=question.id, url=f"https://source-{index}.example.test/evidence",
            title=f"Synthetic source {index}", content=f"Synthetic passage {index}.")
            for index, question in enumerate(questions)], searches_attempted=len(questions),
            searches_succeeded=len(questions), searches_failed=0)


def synthetic_extractor(question, source):
    return CandidateExtraction(relevant=True, claim=f"Finding for {question}",
        passage=source.content, relation=SupportRelation.SUPPORTS)


class HackathonValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.temp.name) / "validation.sqlite")
        self.project = self.store.create_project(ProjectCreate(name="P3 validation"))
        frame = create_frame(ProblemInput(title="Delayed local warnings",
            description="Residents receive local hazard warnings too late to reach safety.",
            primary_users=["Residents in hazard-prone areas"],
            unknowns=["Warning lead time has not been measured"]))
        self.frame, frame_decision = save_frame(self.store, self.project.project_id, frame)
        approve_frame(self.store, self.project.project_id, self.frame.artifact_id,
            frame_decision.decision_id, ApprovalEventStatus.APPROVED)
        plan, plan_artifact, plan_decision = create_research_plan(self.store, self.project.project_id)
        self.store.approve_p2_artifact(self.project.project_id, plan_artifact.artifact_id,
            plan_decision.decision_id, "research_plan", ApprovalEventStatus.APPROVED)
        _, self.report, report_decision, _ = execute_project_research(self.store, self.project.project_id,
            budget=SearchBudget(max_search_calls=8, max_pages=20, max_tavily_credits=8, max_seconds=20),
            orchestrator=SyntheticOrchestrator(), max_rounds=1, extractor=synthetic_extractor)
        self.store.approve_p2_artifact(self.project.project_id, self.report.artifact_id,
            report_decision.decision_id, "research_report", ApprovalEventStatus.APPROVED)
        context = validation_context(self.store, self.project.project_id)
        self.evidence_id = next(iter(context["evidence_ids"]))

    def tearDown(self):
        self.temp.cleanup()

    def decision(self, outcome=ValidationOutcome.PROCEED, **updates):
        context = validation_context(self.store, self.project.project_id)
        values = dict(outcome=outcome, rationale="The evidence supports a focused next step, with important uncertainty remaining.",
            supporting_evidence_ids=[self.evidence_id], contradicting_evidence_ids=[],
            first_hand_evidence="No direct user interviews have been conducted yet.",
            evidence_limitations="Synthetic test data; the real project must review source quality.",
            unresolved_questions=["How early must the warning arrive?"],
            confidence="Low to moderate; desk evidence does not measure local behavior.",
            next_action="Test the framing with affected residents before selecting a solution.",
            evidence_method="", evidence_owner="", evidence_timebox="",
            problem_frame_artifact_id=context["frame"].artifact_id,
            problem_frame_version=context["frame"].version,
            research_report_artifact_id=context["report"].artifact_id,
            research_report_version=context["report"].version)
        values.update(updates)
        return ValidationDecision(**values)

    def test_context_shows_upstream_versions_sources_evidence_and_gaps(self):
        context = validation_context(self.store, self.project.project_id)
        self.assertIn("Problem Frame v1", context["frame_view"])
        self.assertIn("Research Report v1", context["report_view"])
        self.assertIn(self.evidence_id, context["report_view"])
        self.assertIn("Unanswered questions", context["report_view"])

    def test_proceed_records_human_decision_and_completes_p3(self):
        artifact, linked = save_validation_decision(self.store, self.project.project_id, self.decision())
        self.assertEqual(artifact.artifact_type, "validation_decision")
        self.assertEqual(artifact.decision_id, linked.decision_id)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P3).status.value,
                         "completed")

    def test_generic_p3_completion_cannot_bypass_persisted_decision(self):
        self.store.start_project_stage(self.project.project_id, ProjectStageKey.P3)
        with self.assertRaisesRegex(ValueError, "persisted validation decision"):
            self.store.complete_project_stage(self.project.project_id, ProjectStageKey.P3)
        with self.assertRaisesRegex(ValueError, "persisted validation decision"):
            self.store.transition_project_stage(self.project.project_id, ProjectStageKey.P3, "completed")

    def test_stop_decision_completes_but_rejected_decision_cannot_complete(self):
        artifact, linked = save_validation_decision(self.store, self.project.project_id,
            self.decision(ValidationOutcome.STOP, next_action="Stop this problem direction."))
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P3).status.value,
                         "completed")
        self.store.record_artifact_approval(self.project.project_id, artifact.artifact_id,
            ApprovalEventStatus.REJECTED, actor="reviewer", comment="Decision needs reconsideration")
        self.store.record_decision_approval(self.project.project_id, linked.decision_id,
            ApprovalEventStatus.REJECTED, actor="reviewer", comment="Decision needs reconsideration")
        self.store.reopen_project_stage(self.project.project_id, ProjectStageKey.P3)
        with self.assertRaisesRegex(ValueError, "rejected or changes-requested"):
            self.store.complete_project_stage(self.project.project_id, ProjectStageKey.P3)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value,
                         "completed")

    def test_gather_more_evidence_reopens_p2_and_requires_owner_method_timebox(self):
        with self.assertRaises(ValidationError):
            self.decision(ValidationOutcome.GATHER_MORE_EVIDENCE)
        decision = self.decision(ValidationOutcome.GATHER_MORE_EVIDENCE,
            evidence_method="Interview warning recipients", evidence_owner="Team member A",
            evidence_timebox="Before the next review")
        save_validation_decision(self.store, self.project.project_id, decision)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value,
                         "in_progress")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P3).status.value,
                         "in_progress")

    def test_reframe_reopens_p1_and_rejects_stale_upstream_ids(self):
        stale = self.decision(ValidationOutcome.REFRAME,
            next_action="Narrow the target user to volunteer responders.")
        stale.problem_frame_version = 2
        with self.assertRaisesRegex(ValueError, "P1/P2 changed"):
            save_validation_decision(self.store, self.project.project_id, stale)
        current = self.decision(ValidationOutcome.REFRAME,
            next_action="Narrow the target user to volunteer responders.")
        save_validation_decision(self.store, self.project.project_id, current)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P1).status.value,
                         "in_progress")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P3).status.value,
                         "in_progress")

    def test_evidence_ids_must_belong_to_this_projects_approved_report(self):
        decision = self.decision(supporting_evidence_ids=["other-project-evidence"])
        with self.assertRaisesRegex(ValueError, "must match evidence IDs"):
            save_validation_decision(self.store, self.project.project_id, decision)

    def test_structured_primary_research_is_saved_and_linked_to_p3_decision(self):
        context = validation_context(self.store, self.project.project_id)
        record = PrimaryResearchRecord(method=PrimaryResearchMethod.INTERVIEW,
            conducted_at="2026-11-14", participant_context="Three local warning recipients; no names recorded.",
            recruitment_context="Invited through a community preparedness group.",
            questions_or_task=["Describe the last warning you received."],
            findings=["Two participants did not know which route to take."],
            contradictory_findings=["One participant reported receiving a timely warning."],
            limitations="Small convenience sample; recall bias is possible.",
            consent_and_privacy="Participants agreed to anonymous notes; no recordings retained.",
            researcher="Team member A", problem_frame_artifact_id=context["frame"].artifact_id,
            problem_frame_version=context["frame"].version)
        artifact = save_primary_research_record(self.store, self.project.project_id, record)
        self.assertEqual(artifact.artifact_type, "primary_research_record")
        refreshed = validation_context(self.store, self.project.project_id)
        self.assertIn(artifact.artifact_id, refreshed["primary_view"])

        decision = self.decision(primary_research_artifact_ids=[artifact.artifact_id])
        saved, _linked = save_validation_decision(self.store, self.project.project_id, decision)
        self.assertEqual(json.loads(saved.content)["primary_research_artifact_ids"],
                         [artifact.artifact_id])

    def test_p3_rejects_an_approved_p2_plan_linked_to_an_older_p1_frame(self):
        revised = create_frame(ProblemInput(title="Warning delays",
            description="Residents in remote areas receive local warnings too late.",
            primary_users=["Residents in remote hazard-prone areas"]))
        frame, decision = save_frame(self.store, self.project.project_id, revised)
        approve_frame(self.store, self.project.project_id, frame.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)
        with self.assertRaisesRegex(ValueError, "approved P2 research report|older P1 version"):
            validation_context(self.store, self.project.project_id)

    def test_p3_rejects_a_report_invalidated_by_a_newer_p2_plan(self):
        _plan, plan_artifact, plan_decision = create_research_plan(self.store, self.project.project_id)
        self.store.approve_p2_artifact(self.project.project_id, plan_artifact.artifact_id,
            plan_decision.decision_id, "research_plan", ApprovalEventStatus.APPROVED)
        with self.assertRaisesRegex(ValueError, "approved P2 research report"):
            validation_context(self.store, self.project.project_id)


if __name__ == "__main__":
    unittest.main()
