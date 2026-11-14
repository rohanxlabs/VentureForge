import json
import unittest

from pydantic import ValidationError

from src.hackathon_engineering import engineering_context, review_engineering_plan, save_engineering_plan
from src.hackathon_engineering_ui import _starter as p5_starter
from src.hackathon_execution import (
    MVPExecutionRecord,
    TaskExecutionStatus,
    TestExecution,
    TestOutcome,
    execution_context,
    latest_execution_record,
    review_execution_record,
    save_execution_update,
    starter_execution_record,
)
from src.hackathon_solution import SolutionDirectionAction
from src.research.models import ApprovalEventStatus
from tests import test_hackathon_engineering as p5_fixture


class HackathonExecutionTests(unittest.TestCase):
    def setUp(self):
        p5_fixture.HackathonEngineeringTests.setUp(self)
        context = engineering_context(self.store, self.project.project_id)
        _plan, artifact, decision = save_engineering_plan(self.store, self.project.project_id, p5_starter(context))
        review_engineering_plan(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)

    def tearDown(self):
        p5_fixture.HackathonEngineeringTests.tearDown(self)

    def completed_record(self):
        context = execution_context(self.store, self.project.project_id)
        record = starter_execution_record(context)
        tasks = [task.model_copy(update={"status": TaskExecutionStatus.DONE,
            "completion_evidence": f"Implemented and demonstrated {task.title}."}) for task in record.tasks]
        tests = [test.model_copy(update={"outcome": TestOutcome.PASSED,
            "observed_result": f"Observed the expected result for: {test.scenario}"}) for test in record.tests]
        return record.model_copy(update={"tasks": tasks, "tests": tests,
            "test_environment": "Local Windows environment, Chrome, current application build.",
            "demo_notes": "The main user flow was demonstrated end to end with test data."})

    def test_p6_requires_approved_p5_and_seeds_tasks_and_acceptance_checks(self):
        context = execution_context(self.store, self.project.project_id)
        record = starter_execution_record(context)
        self.assertEqual(len(record.tasks), 3)
        self.assertEqual(len(record.tests), 4)  # One P5 scenario plus three demo acceptance criteria.
        self.assertEqual(record.completion_gaps(), [f"Task is still todo: {task.title}" for task in record.tasks] +
                         [f"Test has not been run: {test.scenario}" for test in record.tests])

    def test_incomplete_board_cannot_complete_and_approved_record_completes_p6(self):
        context = execution_context(self.store, self.project.project_id)
        record = starter_execution_record(context)
        _saved, draft, draft_decision = save_execution_update(self.store, self.project.project_id,
            record.model_dump_json())
        with self.assertRaisesRegex(ValueError, "every task is done/deferred"):
            review_execution_record(self.store, self.project.project_id, draft.artifact_id,
                draft_decision.decision_id, ApprovalEventStatus.APPROVED)
        finished = self.completed_record()
        _saved, artifact, decision = save_execution_update(self.store, self.project.project_id,
            finished.model_dump_json())
        with self.assertRaisesRegex(ValueError, "latest artifact is approved"):
            self.store.complete_project_stage(self.project.project_id, "P6")
        review_execution_record(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P6").status.value, "completed")

    def test_failed_test_requires_recorded_failure_and_can_be_approved_with_limitations(self):
        record = self.completed_record()
        test = record.tests[0]
        with self.assertRaisesRegex(ValidationError, "recorded failure"):
            TestExecution.model_validate({**test.model_dump(), "outcome": TestOutcome.FAILED,
                "observed_result": "Expected warning was not displayed."})
        failed = test.model_copy(update={"outcome": TestOutcome.FAILED,
            "observed_result": "Expected warning was not displayed.",
            "known_failure": "The simulated alert feed returned no warning."})
        record = record.model_copy(update={"tests": [failed, *record.tests[1:]],
            "known_failures": ["Alert feed is simulated and returned an empty result."],
            "limitations": ["No live official alert integration is configured."]})
        _, artifact, decision = save_execution_update(self.store, self.project.project_id, record.model_dump_json())
        review_execution_record(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P6").status.value, "completed")

    def test_change_request_reopens_p6_and_next_snapshot_needs_fresh_review(self):
        record = self.completed_record()
        _, artifact, decision = save_execution_update(self.store, self.project.project_id, record.model_dump_json())
        review_execution_record(self.store, self.project.project_id, artifact.artifact_id,
            decision.decision_id, ApprovalEventStatus.CHANGES_REQUESTED, "Add a screenshot proving the saved result persisted.")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P6").status.value, "in_progress")
        revised = record.model_copy(update={"demo_artifacts": ["https://example.test/demo-proof.png"]})
        _, latest, latest_decision = save_execution_update(self.store, self.project.project_id, revised.model_dump_json())
        self.assertEqual(latest.version, 2)
        self.assertEqual(self.store.get_artifact_approval_status(self.project.project_id, latest.artifact_id), "pending")
        review_execution_record(self.store, self.project.project_id, latest.artifact_id,
            latest_decision.decision_id, ApprovalEventStatus.APPROVED)
        self.assertEqual(latest_execution_record(self.store, self.project.project_id).artifact_id, latest.artifact_id)

    def test_execution_task_ids_and_dependency_order_must_match_p5(self):
        record = self.completed_record()
        invalid = record.model_copy(update={"tasks": [record.tasks[0].model_copy(update={"task_id": "foreign-task"}), *record.tasks[1:]]})
        with self.assertRaisesRegex(ValueError, "match the approved P5 task IDs"):
            save_execution_update(self.store, self.project.project_id, invalid.model_dump_json())
        blocked = record.model_copy(update={"tasks": [record.tasks[0].model_copy(update={"status": TaskExecutionStatus.TODO,
            "completion_evidence": ""}), *record.tasks[1:]]})
        with self.assertRaisesRegex(ValueError, "cannot be in progress or done before dependency"):
            save_execution_update(self.store, self.project.project_id, blocked.model_dump_json())


if __name__ == "__main__":
    unittest.main()
