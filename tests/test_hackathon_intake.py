import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.hackathon_intake import (
    ChallengeBrief,
    approve_challenge_brief,
    parse_criteria,
    render_challenge_brief,
    save_challenge_brief,
    validate_brief_payload,
)
from src.problem_intelligence import (
    ProblemInput,
    approve_frame,
    create_frame,
    save_frame,
    start_problem_framing,
)
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ProjectCreate


class HackathonIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.temp.name) / "challenge.sqlite")
        self.project = self.store.create_project(ProjectCreate(name="Challenge setup"))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def make_brief(title="Community Flood Alert"):
        return ChallengeBrief(
            event_name="Civic Tech Weekend",
            challenge_title=title,
            challenge_type="assigned",
            official_prompt="Help communities prepare for severe flooding.",
            official_url="https://example.org/challenge",
            rules_url="https://example.org/rules",
            team_size=3,
            time_available_hours=24,
            team_skills=["frontend", "data analysis"],
            required_technologies=["Open data API"],
            allowed_tools_and_resources=["Organizer data portal"],
            constraints_and_eligibility=["Submit before the deadline"],
            submission_requirements=["Public repository", "Demo video"],
            demo_format="Three-minute live demo",
            judging_criteria=parse_criteria("Impact | 60 | Community benefit\nFeasibility | 40 | Can be built"),
            unresolved_questions=["Is pre-event coding allowed?"],
        )

    def test_weighted_criteria_must_total_one_hundred_when_all_weights_are_set(self):
        self.assertEqual(sum(item.weight_percent for item in parse_criteria("Impact | 60\nBuild | 40")), 100)
        self.assertEqual(len(parse_criteria("Impact\nBuild")), 2)
        with self.assertRaisesRegex(ValueError, "total 90%"):
            parse_criteria("Impact | 60\nBuild | 30")

    def test_latest_brief_and_both_approvals_gate_p1(self):
        artifact, decision = save_challenge_brief(self.store, self.project.project_id, self.make_brief())
        self.assertEqual(artifact.stage_key, None)
        with self.assertRaisesRegex(ValueError, "Challenge Brief"):
            start_problem_framing(self.store, self.project.project_id)
        with self.assertRaisesRegex(ValueError, "Challenge Brief"):
            save_frame(self.store, self.project.project_id, create_frame(ProblemInput(
                title="Flood preparation", description="Residents lack timely local flood guidance.")))
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P1").status.value, "pending")

        approve_challenge_brief(self.store, self.project.project_id, artifact.artifact_id,
                                decision.decision_id, ApprovalEventStatus.APPROVED)
        stage = start_problem_framing(self.store, self.project.project_id)
        self.assertEqual(stage.status.value, "in_progress")

    def test_approved_challenge_flows_into_reviewed_problem_frame(self):
        brief, brief_decision = save_challenge_brief(self.store, self.project.project_id, self.make_brief())
        approve_challenge_brief(self.store, self.project.project_id, brief.artifact_id,
                                brief_decision.decision_id, ApprovalEventStatus.APPROVED)
        start_problem_framing(self.store, self.project.project_id)
        frame, frame_decision = save_frame(self.store, self.project.project_id, create_frame(ProblemInput(
            title="Flood preparation gaps",
            description="Residents in flood-prone neighborhoods may not receive timely local warnings.",
            primary_users=["Residents in flood-prone neighborhoods"],
            unknowns=["Warning reach and lead time have not been measured"])))
        approve_frame(self.store, self.project.project_id, frame.artifact_id,
                      frame_decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P1").status.value, "completed")

    def test_revised_brief_requires_approval_of_latest_version(self):
        first, first_decision = save_challenge_brief(self.store, self.project.project_id, self.make_brief())
        approve_challenge_brief(self.store, self.project.project_id, first.artifact_id,
                                first_decision.decision_id, ApprovalEventStatus.CHANGES_REQUESTED,
                                comment="Confirm the official track")
        second, second_decision = save_challenge_brief(self.store, self.project.project_id,
                                                       self.make_brief("Revised Flood Alert"))
        self.assertEqual(second.version, 2)
        with self.assertRaisesRegex(ValueError, "latest Challenge Brief"):
            approve_challenge_brief(self.store, self.project.project_id, first.artifact_id,
                                    first_decision.decision_id, ApprovalEventStatus.APPROVED)
        approve_challenge_brief(self.store, self.project.project_id, second.artifact_id,
                                second_decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_artifact_approval_status(self.project.project_id, second.artifact_id), "approved")
        self.assertEqual(self.store.get_decision_approval_status(self.project.project_id, second_decision.decision_id), "approved")

    def test_challenge_approval_writes_are_atomic(self):
        artifact, decision = save_challenge_brief(self.store, self.project.project_id, self.make_brief())
        with self.store.db() as connection:
            connection.execute("""CREATE TRIGGER reject_decision_review
                BEFORE INSERT ON approval_events WHEN NEW.decision_id IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'injected decision approval failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected decision approval failure"):
            approve_challenge_brief(self.store, self.project.project_id, artifact.artifact_id,
                                    decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_artifact_approval_history(self.project.project_id, artifact.artifact_id), [])
        self.assertEqual(self.store.get_decision_approval_history(self.project.project_id, decision.decision_id), [])

    def test_review_summary_exposes_materials_needed_for_human_review(self):
        artifact, decision = save_challenge_brief(self.store, self.project.project_id, self.make_brief())
        summary, artifact_id, decision_id, readiness = render_challenge_brief(self.store, self.project.project_id)
        self.assertIn("Official prompt source", summary)
        self.assertIn("Allowed tools, APIs, data, and resources", summary)
        self.assertIn("Impact", summary)
        self.assertIn("public repository", summary.casefold())
        self.assertEqual((artifact_id, decision_id), (artifact.artifact_id, decision.decision_id))
        self.assertIn("Needs human review", readiness)

    def test_payload_parser_handles_optional_numbers_and_multiline_lists(self):
        brief = validate_brief_payload(
            event_name="Event", challenge_title="Prompt", challenge_type="open",
            official_prompt="A sufficiently detailed official prompt.", team_size="",
            time_available_hours="", team_skills="Design\nEngineering",
            required_technologies="API", allowed_tools_and_resources="Dataset",
            constraints_and_eligibility="Must be eligible", submission_requirements="Demo",
            judging_criteria="Impact\nFeasibility", unresolved_questions="Clarify deadline")
        self.assertIsNone(brief.team_size)
        self.assertEqual(brief.team_skills, ["Design", "Engineering"])
        self.assertEqual(len(brief.judging_criteria), 2)
        self.assertEqual(json.loads(brief.model_dump_json())["document_type"], "ventureforge.challenge_brief.v1")


if __name__ == "__main__":
    unittest.main()
