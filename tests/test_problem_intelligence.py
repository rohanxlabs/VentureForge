"""Offline service tests; all content here is synthetic user input."""
import tempfile
import unittest
from pathlib import Path

from src.problem_intelligence import (
    ProblemInput, approve_frame, create_frame, create_research_plan, save_frame,
)
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ProjectCreate, ProjectStageKey


BENCHMARKS = {
    "landslide_northeast_india": "How do residents in landslide-prone parts of Northeast India receive warnings?",
    "healthcare_followup": "How do rural clinic patients learn about and attend follow-up appointments?",
    "developer_build_feedback": "How do developers identify which change caused a slow local test suite?",
}


class ProblemIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.temp.name) / "test.sqlite")
        self.project = self.store.create_project(ProjectCreate(name="Synthetic benchmark"))

    def tearDown(self):
        self.temp.cleanup()

    def test_frame_preserves_user_input_and_unknowns(self):
        frame = create_frame(ProblemInput(title="Warning delays",
            description=BENCHMARKS["landslide_northeast_india"],
            primary_users=["residents"], assumptions=["Cellular coverage may be unreliable"],
            unknowns=["Warning receipt time has not been measured"]))
        self.assertEqual(frame.facts, [BENCHMARKS["landslide_northeast_india"]])
        self.assertIn("Cellular coverage may be unreliable", frame.assumptions)
        self.assertIn("Warning receipt time has not been measured", frame.unknowns)
        self.assertNotIn("app", frame.problem_statement.lower())

    def test_project_frame_approval_is_append_only_and_gates_plan(self):
        frame = create_frame(ProblemInput(title="Follow-up attendance",
            description=BENCHMARKS["healthcare_followup"], unknowns=["Attendance rate is not known"]))
        artifact, decision = save_frame(self.store, self.project.project_id, frame)
        with self.assertRaisesRegex(ValueError, "P1 must be approved"):
            create_research_plan(self.store, self.project.project_id)
        approve_frame(self.store, self.project.project_id, artifact.artifact_id,
                      decision.decision_id, ApprovalEventStatus.APPROVED)
        events = self.store.get_artifact_approval_history(self.project.project_id, artifact.artifact_id)
        self.assertEqual([event.status for event in events], [ApprovalEventStatus.APPROVED])
        plan, plan_artifact, _ = create_research_plan(self.store, self.project.project_id)
        self.assertEqual(len(plan.questions), 5)
        self.assertEqual(plan_artifact.stage_key, ProjectStageKey.P2)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, ProjectStageKey.P2).status.value,
                         "in_progress")

    def test_developer_benchmark_questions_are_specific_and_numeric_claims_absent(self):
        frame = create_frame(ProblemInput(title="Slow test feedback",
            description=BENCHMARKS["developer_build_feedback"], primary_users=["software developers"]))
        self.assertTrue(any("How frequently" in question for question in frame.research_questions))
        self.assertTrue(all("%" not in question for question in frame.research_questions))


if __name__ == "__main__":
    unittest.main()
