import json
import unittest

from pydantic import ValidationError

from src.hackathon_engineering import (
    EngineeringPlan,
    capacity_summary,
    engineering_context,
    latest_engineering_plan,
    review_engineering_plan,
    save_engineering_plan,
)
from src.hackathon_solution import SolutionDirectionAction, record_solution_direction
from src.hackathon_engineering_ui import _starter
from src.research.models import ApprovalEventStatus
from tests import test_hackathon_solution as p4_fixture


class HackathonEngineeringTests(unittest.TestCase):
    def setUp(self):
        p4_fixture.HackathonSolutionTests.setUp(self)
        options = p4_fixture.HackathonSolutionTests.make_options(self, action=SolutionDirectionAction.SELECT,
            selected_option_id="option-1",
            selection_rationale="This direction best connects the evidence, user need, and event constraints.",
            experiment_owner="Team member A", experiment_timebox="Before the halfway checkpoint")
        record_solution_direction(self.store, self.project.project_id, options.model_dump_json())

    def tearDown(self):
        p4_fixture.HackathonSolutionTests.tearDown(self)

    def test_p5_context_requires_selected_p4_and_starter_is_valid(self):
        context = engineering_context(self.store, self.project.project_id)
        plan = EngineeringPlan.model_validate_json(_starter(context))
        self.assertEqual(plan.selected_option_id, "option-1")
        self.assertIn("Selected P4 direction", context["view"])
        self.assertEqual(len(plan.build_tasks), 3)
        self.assertIn("person-hours", capacity_summary(plan, context))

    def test_save_plan_and_human_approval_complete_p5(self):
        context = engineering_context(self.store, self.project.project_id)
        plan, artifact, decision = save_engineering_plan(self.store, self.project.project_id, _starter(context))
        self.assertEqual(artifact.version, 1)
        self.assertEqual(artifact.decision_id, decision.decision_id)
        with self.assertRaisesRegex(ValueError, "latest artifact is approved"):
            self.store.complete_project_stage(self.project.project_id, "P5")
        review_engineering_plan(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P5").status.value, "completed")
        self.assertEqual(self.store.get_artifact_approval_status(self.project.project_id, artifact.artifact_id), "approved")

    def test_change_request_reopens_p5_and_revised_version_needs_fresh_approval(self):
        context = engineering_context(self.store, self.project.project_id)
        _, artifact, decision = save_engineering_plan(self.store, self.project.project_id, _starter(context))
        review_engineering_plan(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.CHANGES_REQUESTED, "Name an owner for the demo fallback.")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P5").status.value, "in_progress")
        revised_data = json.loads(artifact.content)
        revised_data["demo_plan"] = "The assigned presenter follows the real flow, then shows a saved fallback screenshot if the service is unavailable."
        revised_data["build_tasks"][2]["owner"] = "Team member B"
        _revised, revised, revised_decision = save_engineering_plan(self.store, self.project.project_id,
            json.dumps(revised_data))
        self.assertEqual(revised.version, 2)
        self.assertEqual(self.store.get_artifact_approval_status(self.project.project_id, revised.artifact_id), "pending")
        review_engineering_plan(self.store, self.project.project_id, revised.artifact_id,
            revised_decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(latest_engineering_plan(self.store, self.project.project_id).artifact_id, revised.artifact_id)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P5").status.value, "completed")

    def test_unknown_dependencies_and_cycles_are_rejected(self):
        context = engineering_context(self.store, self.project.project_id)
        data = json.loads(_starter(context))
        data["build_tasks"][0]["depends_on"] = ["missing task"]
        with self.assertRaises(ValidationError):
            EngineeringPlan.model_validate(data)
        data = json.loads(_starter(context))
        data["build_tasks"][0]["depends_on"] = ["Persist the core result"]
        data["build_tasks"][1]["depends_on"] = ["Build core user flow", "Verify demo and fallback"]
        data["build_tasks"][2]["depends_on"] = ["Persist the core result"]
        with self.assertRaisesRegex(ValidationError, "cycle"):
            EngineeringPlan.model_validate(data)

    def test_stale_p4_reference_is_rejected_before_save(self):
        context = engineering_context(self.store, self.project.project_id)
        data = json.loads(_starter(context))
        data["p4_artifact_version"] += 1
        with self.assertRaisesRegex(ValueError, "P1-P4 changed"):
            save_engineering_plan(self.store, self.project.project_id, json.dumps(data))


if __name__ == "__main__":
    unittest.main()
