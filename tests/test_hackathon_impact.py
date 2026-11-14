import json
import unittest

from pydantic import ValidationError

from src.hackathon_execution import review_execution_record, save_execution_update
from src.hackathon_impact import (
    ImpactFeasibilityBrief,
    impact_context,
    latest_impact_brief,
    review_impact_brief,
    save_impact_brief,
)
from src.hackathon_impact_ui import _starter
from src.research.models import ApprovalEventStatus
from tests import test_hackathon_execution as p6_fixture


class HackathonImpactTests(unittest.TestCase):
    def setUp(self):
        p6_fixture.HackathonExecutionTests.setUp(self)
        execution = p6_fixture.HackathonExecutionTests.completed_record(self)
        _record, artifact, decision = save_execution_update(self.store, self.project.project_id,
            execution.model_dump_json())
        review_execution_record(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)

    def tearDown(self):
        p6_fixture.HackathonExecutionTests.tearDown(self)

    def test_p7_requires_approved_p6_and_starter_is_a_hypothesis(self):
        context = impact_context(self.store, self.project.project_id)
        brief = ImpactFeasibilityBrief.model_validate_json(_starter(context))
        self.assertEqual(brief.claim_level, "hypothesis")
        self.assertIn("Unknown", brief.primary_metric.baseline)
        self.assertIn("Do not describe them as measured community impact", context["view"])
        self.assertIn("Verified P2 evidence IDs", context["view"])
        self.assertTrue(context["p6_test_ids"])

    def test_save_and_approve_p7_brief_completes_stage_with_exact_lineage(self):
        context = impact_context(self.store, self.project.project_id)
        brief, artifact, decision = save_impact_brief(self.store, self.project.project_id, _starter(context))
        self.assertEqual(artifact.version, 1)
        with self.assertRaisesRegex(ValueError, "latest artifact is approved"):
            self.store.complete_project_stage(self.project.project_id, "P7")
        review_impact_brief(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P7").status.value, "completed")
        self.assertEqual(latest_impact_brief(self.store, self.project.project_id).artifact_id, artifact.artifact_id)
        self.assertEqual(brief.p6_artifact_id, context["p6"].artifact_id)

    def test_unverified_evidence_and_stale_upstream_versions_are_rejected(self):
        context = impact_context(self.store, self.project.project_id)
        data = json.loads(_starter(context))
        data["supporting_evidence_ids"] = ["evidence-from-another-project"]
        with self.assertRaisesRegex(ValueError, "must be verified IDs"):
            save_impact_brief(self.store, self.project.project_id, json.dumps(data))
        data = json.loads(_starter(context))
        data["p5_artifact_version"] += 1
        with self.assertRaisesRegex(ValueError, "P1-P6 changed"):
            save_impact_brief(self.store, self.project.project_id, json.dumps(data))

    def test_prototype_signal_requires_a_completed_p6_test_reference(self):
        context = impact_context(self.store, self.project.project_id)
        data = json.loads(_starter(context))
        data["claim_level"] = "prototype_signal"
        with self.assertRaisesRegex(ValidationError, "cite at least one P6 test ID"):
            ImpactFeasibilityBrief.model_validate(data)
        data["prototype_test_ids"] = ["fake-test"]
        with self.assertRaisesRegex(ValueError, "completed test IDs"):
            save_impact_brief(self.store, self.project.project_id, json.dumps(data))

    def test_changes_requested_keeps_p7_open_for_a_revision(self):
        context = impact_context(self.store, self.project.project_id)
        _, artifact, decision = save_impact_brief(self.store, self.project.project_id, _starter(context))
        review_impact_brief(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.CHANGES_REQUESTED,
            "Add the emergency information source dependency and owner.")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P7").status.value, "in_progress")
        data = json.loads(artifact.content)
        data["resources_and_costs"][0]["need_or_estimate"] = "Identify the authorized feed owner and validate availability before a pilot."
        _, revised, revised_decision = save_impact_brief(self.store, self.project.project_id, json.dumps(data))
        self.assertEqual(revised.version, 2)
        review_impact_brief(self.store, self.project.project_id, revised.artifact_id,
            revised_decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P7").status.value, "completed")


if __name__ == "__main__":
    unittest.main()
