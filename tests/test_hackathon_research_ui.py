import tempfile
import unittest
from pathlib import Path

from src.hackathon_intake import ChallengeBrief, approve_challenge_brief, save_challenge_brief
from src.hackathon_research_ui import _plan_status
from src.problem_intelligence import (
    ProblemInput,
    approve_frame,
    create_frame,
    create_research_plan,
    revise_research_plan,
    save_frame,
    start_problem_framing,
)
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ProjectCreate
from src.research.project_intelligence import approve_plan


class HackathonResearchUiStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "research-ui.sqlite"
        self.store = ProjectStore(self.database)
        self.project = self.store.create_project(ProjectCreate(name="P2 workspace"))
        brief, decision = save_challenge_brief(self.store, self.project.project_id, ChallengeBrief(
            event_name="Civic Weekend", challenge_title="Flood response",
            challenge_type="assigned", official_prompt="Improve preparedness for local flooding."))
        approve_challenge_brief(self.store, self.project.project_id, brief.artifact_id,
                                decision.decision_id, ApprovalEventStatus.APPROVED)
        start_problem_framing(self.store, self.project.project_id)
        frame, frame_decision = save_frame(self.store, self.project.project_id, create_frame(ProblemInput(
            title="Delayed flood warnings",
            description="Residents in flood-prone areas may receive local warnings too late.",
            primary_users=["Residents in flood-prone areas"],
            unknowns=["Local warning lead time has not been measured"])))
        approve_frame(self.store, self.project.project_id, frame.artifact_id,
                      frame_decision.decision_id, ApprovalEventStatus.APPROVED)

    def tearDown(self):
        self.temp.cleanup()

    def test_p2_workspace_starts_empty_then_creates_and_approves_plan(self):
        header, plan_text, report_text, plan_id, report_id = _plan_status(self.store, self.project.project_id)
        self.assertIn("Stage: pending", header)
        self.assertEqual((plan_text, plan_id, report_id), ("", "", ""))
        self.assertIn("No P2 report", report_text)

        plan, artifact, decision = create_research_plan(self.store, self.project.project_id)
        self.assertGreaterEqual(len(plan.questions), 3)
        header, plan_text, _, plan_id, _ = _plan_status(self.store, self.project.project_id)
        self.assertIn("Stage: in_progress", header)
        self.assertIn(plan.id, plan_text)
        self.assertEqual(plan_id, artifact.artifact_id)

        approve_plan(self.store, self.project.project_id, artifact.artifact_id)
        self.assertEqual(self.store.get_artifact_approval_status(self.project.project_id, artifact.artifact_id), "approved")
        self.assertEqual(self.store.get_decision_approval_status(self.project.project_id, decision.decision_id), "approved")

    def test_revised_plan_is_a_new_unapproved_version(self):
        plan, artifact, _ = create_research_plan(self.store, self.project.project_id)
        approve_plan(self.store, self.project.project_id, artifact.artifact_id)
        edited = plan.model_copy(update={
            "questions": [plan.questions[0].model_copy(update={"text": "How much earlier do users need a flood warning?"}),
                          *plan.questions[1:]]
        })
        revised, revised_artifact, _ = revise_research_plan(
            self.store, self.project.project_id, edited.model_dump_json())
        self.assertEqual(revised_artifact.version, 2)
        self.assertNotEqual(revised.run_id, plan.run_id)
        self.assertEqual(self.store.get_artifact_approval_status(self.project.project_id, revised_artifact.artifact_id), "pending")
        self.assertIn("How much earlier", revised.questions[0].text)


if __name__ == "__main__":
    unittest.main()
