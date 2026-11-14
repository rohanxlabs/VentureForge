import json
import unittest

from src.hackathon_impact import save_impact_brief, review_impact_brief
from src.hackathon_impact_ui import _starter as p7_starter
from src.hackathon_pitch_prep import (
    PitchPreparationPackage,
    latest_pitch_prep,
    pitch_prep_context,
    review_pitch_prep,
    save_pitch_prep,
)
from src.hackathon_pitch_prep_ui import _starter
from src.research.models import ApprovalEventStatus
from tests import test_hackathon_impact as p7_fixture


class HackathonPitchPrepTests(unittest.TestCase):
    def setUp(self):
        p7_fixture.HackathonImpactTests.setUp(self)
        context = p7_fixture.impact_context(self.store, self.project.project_id)
        _brief, artifact, decision = save_impact_brief(self.store, self.project.project_id, p7_starter(context))
        review_impact_brief(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)

    def tearDown(self):
        p7_fixture.HackathonImpactTests.tearDown(self)

    def test_p8_requires_p7_and_starter_uses_existing_pitch_structures(self):
        context = pitch_prep_context(self.store, self.project.project_id)
        package = PitchPreparationPackage.model_validate_json(_starter(context))
        self.assertEqual({item.criterion for item in package.rubric_mapping}, {"Impact", "Feasibility"})
        self.assertEqual(len(package.judge_questions), 5)
        self.assertEqual(len(package.presentation_outline), 5)
        self.assertEqual(len(package.demo_script), 3)
        self.assertTrue(any("prototype" in claim.lower() for claim in package.claims_and_nonclaims))
        self.assertIn(context["p7"].artifact_id, context["valid_references"])

    def test_save_and_approve_p8_package_completes_stage(self):
        context = pitch_prep_context(self.store, self.project.project_id)
        package, artifact, decision = save_pitch_prep(self.store, self.project.project_id, _starter(context))
        self.assertEqual(artifact.version, 1)
        self.assertEqual(artifact.decision_id, decision.decision_id)
        with self.assertRaisesRegex(ValueError, "latest artifact is approved"):
            self.store.complete_project_stage(self.project.project_id, "P8")
        review_pitch_prep(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P8").status.value, "completed")
        self.assertEqual(latest_pitch_prep(self.store, self.project.project_id).artifact_id, artifact.artifact_id)
        self.assertEqual(package.p7_artifact_id, context["p7"].artifact_id)

    def test_rubric_must_match_event_exactly_and_unknown_proof_is_rejected(self):
        context = pitch_prep_context(self.store, self.project.project_id)
        data = json.loads(_starter(context))
        data["rubric_mapping"].pop()
        with self.assertRaisesRegex(ValueError, "Map every actual event judging criterion"):
            save_pitch_prep(self.store, self.project.project_id, json.dumps(data))
        data = json.loads(_starter(context))
        data["rubric_mapping"][0]["proof_references"] = ["foreign-artifact"]
        with self.assertRaisesRegex(ValueError, "references must use artifact IDs"):
            save_pitch_prep(self.store, self.project.project_id, json.dumps(data))

    def test_stale_upstream_p7_version_is_rejected(self):
        context = pitch_prep_context(self.store, self.project.project_id)
        data = json.loads(_starter(context))
        data["p6_artifact_version"] += 1
        with self.assertRaisesRegex(ValueError, "P1-P7 changed"):
            save_pitch_prep(self.store, self.project.project_id, json.dumps(data))

    def test_changes_requested_reopens_p8_and_requires_a_new_review(self):
        context = pitch_prep_context(self.store, self.project.project_id)
        _, artifact, decision = save_pitch_prep(self.store, self.project.project_id, _starter(context))
        review_pitch_prep(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.CHANGES_REQUESTED,
            "Add the repository URL and verify the demo duration against the official rules.")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P8").status.value, "in_progress")
        data = json.loads(artifact.content)
        data["submission_checklist"][0]["owner"] = "Team member A"
        _, revised, revised_decision = save_pitch_prep(self.store, self.project.project_id, json.dumps(data))
        self.assertEqual(revised.version, 2)
        review_pitch_prep(self.store, self.project.project_id, revised.artifact_id,
            revised_decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P8").status.value, "completed")


if __name__ == "__main__":
    unittest.main()
