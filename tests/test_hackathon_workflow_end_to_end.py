"""Sequential integration test of the Challenge Setup -> P8 project workflow.

Research providers are deterministic synthetic fixtures; this exercises project
ownership, artifacts, approvals, version references, and all stage transitions.
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.hackathon_engineering import engineering_context, review_engineering_plan, save_engineering_plan
from src.hackathon_engineering_ui import _starter as p5_starter
from src.hackathon_execution import (
    TaskExecutionStatus,
    TestOutcome,
    execution_context,
    review_execution_record,
    save_execution_update,
    starter_execution_record,
)
from src.hackathon_intake import (
    ChallengeBrief,
    JudgingCriterion,
    approve_challenge_brief,
    save_challenge_brief,
)
from src.hackathon_impact import impact_context, review_impact_brief, save_impact_brief
from src.hackathon_impact_ui import _starter as p7_starter
from src.hackathon_pitch_prep import pitch_prep_context, review_pitch_prep, save_pitch_prep
from src.hackathon_pitch_prep_ui import _starter as p8_starter
from src.hackathon_solution import (
    SolutionDirectionAction,
    SolutionOptionsDraft,
    record_solution_direction,
    solution_context,
)
from src.hackathon_solution_ui import _starter as p4_starter
from src.hackathon_validation import (
    ValidationDecision,
    ValidationOutcome,
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


class FloodWarningResearchFixture:
    def collect(self, *, run_id, prompt, questions, budget):
        sources = [SourceSnapshot(run_id=run_id, question_id=question.id,
            url=f"https://fixture-{index}.example.test/flood-warning",
            title=f"Synthetic flood preparedness source {index}",
            publisher=f"Test fixture publisher {index}",
            content=("Synthetic fixture only: flood-prone residents report needing timely, "
                     f"localized evacuation information. Research question: {question.text}"))
            for index, question in enumerate(questions)]
        return CollectionResult(sources=sources, searches_attempted=len(questions),
            searches_succeeded=len(questions), searches_failed=0)


def flood_warning_extractor(question, source):
    return CandidateExtraction(relevant=True,
        claim="Synthetic test finding: localized warning timing is relevant to resident preparedness.",
        passage=source.content, relation=SupportRelation.SUPPORTS)


class HackathonWorkflowEndToEndTests(unittest.TestCase):
    def test_flood_warning_problem_completes_sequentially_through_p8(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ProjectStore(Path(temp_dir) / "workflow-e2e.sqlite")
            project = store.create_project(ProjectCreate(name="Flood warning hackathon test"))
            project_id = project.project_id

            brief, brief_decision = save_challenge_brief(store, project_id, ChallengeBrief(
                event_name="Civic Resilience Hackathon", challenge_title="Local flood preparedness",
                challenge_type="assigned",
                official_prompt="Help residents in flood-prone neighborhoods prepare and respond to local flood warnings.",
                team_size=3, time_available_hours=24, team_skills=["Python", "UX", "community research"],
                constraints_and_eligibility=["Use synthetic data in the demo; no live emergency broadcast."],
                judging_criteria=[JudgingCriterion(name="Community impact", weight_percent=60),
                    JudgingCriterion(name="Feasibility", weight_percent=40)]))
            approve_challenge_brief(store, project_id, brief.artifact_id, brief_decision.decision_id,
                ApprovalEventStatus.APPROVED)

            frame, frame_decision = save_frame(store, project_id, create_frame(ProblemInput(
                title="Residents receive local flood warnings too late",
                description=("Residents in low-lying neighborhoods may receive fragmented or late local flood "
                    "information, leaving little time to decide where to go and what to take."),
                domain="Community emergency preparedness", geography="Low-lying neighborhoods",
                primary_users=["Residents in flood-prone neighborhoods"],
                current_process=["Wait for a local alert", "Check messages from neighbors", "Decide whether to leave"],
                workarounds=["Ask neighbors for updates"], pain_points=["Information may be fragmented or late"],
                unknowns=["Local warning lead time has not been measured"])))
            approve_frame(store, project_id, frame.artifact_id, frame_decision.decision_id,
                ApprovalEventStatus.APPROVED)
            self.assertEqual(store.get_project_stage(project_id, ProjectStageKey.P1).status.value, "completed")

            plan, plan_artifact, plan_decision = create_research_plan(store, project_id)
            store.approve_p2_artifact(project_id, plan_artifact.artifact_id, plan_decision.decision_id,
                "research_plan", ApprovalEventStatus.APPROVED)
            _, report, report_decision, workflow = execute_project_research(store, project_id,
                budget=SearchBudget(max_search_calls=8, max_pages=20, max_tavily_credits=8, max_seconds=20),
                orchestrator=FloodWarningResearchFixture(), max_rounds=1,
                extractor=flood_warning_extractor)
            self.assertEqual(workflow.status.value, "completed")
            store.approve_p2_artifact(project_id, report.artifact_id, report_decision.decision_id,
                "research_report", ApprovalEventStatus.APPROVED)
            self.assertEqual(store.get_project_stage(project_id, ProjectStageKey.P2).status.value, "completed")

            p3_context = validation_context(store, project_id)
            evidence_id = next(iter(p3_context["evidence_ids"]))
            validation = ValidationDecision(outcome=ValidationOutcome.PROCEED,
                rationale="Synthetic desk research gives enough direction to compare bounded prototype approaches.",
                supporting_evidence_ids=[evidence_id], contradicting_evidence_ids=[],
                first_hand_evidence="No real resident interviews were conducted in this synthetic integration test.",
                evidence_limitations="All passages and source URLs in this test are synthetic fixtures.",
                unresolved_questions=["How much lead time do residents need to act?"],
                confidence="Low; the test data cannot estimate real warning behavior.",
                next_action="Compare an alert relay with a preparedness checklist, then test the chosen assumption.",
                problem_frame_artifact_id=p3_context["frame"].artifact_id,
                problem_frame_version=p3_context["frame"].version,
                research_report_artifact_id=p3_context["report"].artifact_id,
                research_report_version=p3_context["report"].version)
            save_validation_decision(store, project_id, validation)
            self.assertEqual(store.get_project_stage(project_id, ProjectStageKey.P3).status.value, "completed")

            p4_context = solution_context(store, project_id)
            draft = SolutionOptionsDraft.model_validate_json(p4_starter(p4_context))
            options = [
                draft.options[0].model_copy(update={
                    "name": "Community flood alert relay",
                    "concept": "Give residents one place to review localized alerts and a clear next action.",
                    "problem_fit": "Addresses fragmented information with a simple local alert view.",
                    "rubric_fit": {"Community impact": "Shows a resident a localized alert and action.",
                                   "Feasibility": "A single alert flow fits the short event window."}}),
                draft.options[1].model_copy(update={
                    "name": "Household readiness checklist",
                    "concept": "Help households prepare a short evacuation checklist before flooding begins.",
                    "problem_fit": "Targets uncertainty about what to do when a warning arrives.",
                    "rubric_fit": {"Community impact": "Demonstrates one household readiness task.",
                                   "Feasibility": "A saved checklist needs no live emergency feed."}}),
            ]
            draft = draft.model_copy(update={"options": options})
            selected = draft.model_copy(update={"action": SolutionDirectionAction.SELECT,
                "selected_option_id": options[0].option_id,
                "selection_rationale": "The relay responds to fragmented alerts and produces a clear, demonstrable resident action.",
                "experiment_owner": "Team member A", "experiment_timebox": "First six hours"})
            _, p4_artifact, _ = record_solution_direction(store, project_id, selected.model_dump_json())
            self.assertEqual(store.get_project_stage(project_id, ProjectStageKey.P4).status.value, "completed")

            p5_context = engineering_context(store, project_id)
            _engineering_plan, p5_artifact, p5_decision = save_engineering_plan(store, project_id,
                p5_starter(p5_context))
            review_engineering_plan(store, project_id, p5_artifact.artifact_id, p5_decision.decision_id,
                ApprovalEventStatus.APPROVED)
            self.assertEqual(store.get_project_stage(project_id, ProjectStageKey.P5).status.value, "completed")

            p6_context = execution_context(store, project_id)
            execution = starter_execution_record(p6_context)
            tasks = [task.model_copy(update={"status": TaskExecutionStatus.DONE,
                "completion_evidence": f"Demonstrated the implemented task: {task.title}."})
                for task in execution.tasks]
            tests = [test.model_copy(update={"outcome": TestOutcome.PASSED,
                "observed_result": f"Observed expected result for: {test.scenario}",
                "evidence_reference": "Synthetic integration test observation"}) for test in execution.tests]
            execution = execution.model_copy(update={"tasks": tasks, "tests": tests,
                "test_environment": "Synthetic end-to-end test harness; no real application demo was run.",
                "demo_notes": "This integration test validates workflow persistence and gates, not real user outcomes."})
            _, p6_artifact, p6_decision = save_execution_update(store, project_id, execution.model_dump_json())
            review_execution_record(store, project_id, p6_artifact.artifact_id, p6_decision.decision_id,
                ApprovalEventStatus.APPROVED)

            p7_context = impact_context(store, project_id)
            _impact_brief, p7_artifact, p7_decision = save_impact_brief(store, project_id,
                p7_starter(p7_context))
            review_impact_brief(store, project_id, p7_artifact.artifact_id, p7_decision.decision_id,
                ApprovalEventStatus.APPROVED)

            p8_context = pitch_prep_context(store, project_id)
            _pitch_package, p8_artifact, p8_decision = save_pitch_prep(store, project_id,
                p8_starter(p8_context))
            review_pitch_prep(store, project_id, p8_artifact.artifact_id, p8_decision.decision_id,
                ApprovalEventStatus.APPROVED)

            self.assertEqual([store.get_project_stage(project_id, stage).status.value
                for stage in (ProjectStageKey.P1, ProjectStageKey.P2, ProjectStageKey.P3,
                              ProjectStageKey.P4, ProjectStageKey.P5, ProjectStageKey.P6,
                              ProjectStageKey.P7, ProjectStageKey.P8)], ["completed"] * 8)
            saved_p6 = json.loads(p6_artifact.content)
            self.assertEqual(saved_p6["p5_artifact_id"], p5_artifact.artifact_id)
            self.assertEqual(p4_artifact.stage_key, ProjectStageKey.P4)
            self.assertEqual(json.loads(p7_artifact.content)["p6_artifact_id"], p6_artifact.artifact_id)
            self.assertEqual(json.loads(p8_artifact.content)["p7_artifact_id"], p7_artifact.artifact_id)
            for artifact in (brief, frame, report, p5_artifact, p6_artifact, p7_artifact, p8_artifact):
                self.assertEqual(store.get_artifact_approval_status(project_id, artifact.artifact_id),
                                 ApprovalEventStatus.APPROVED.value)
            self.assertEqual(json.loads(p8_artifact.content)["p1_artifact_id"], frame.artifact_id)
            self.assertEqual(json.loads(p8_artifact.content)["p6_artifact_id"], p6_artifact.artifact_id)

            reopened = ProjectStore(Path(temp_dir) / "workflow-e2e.sqlite")
            self.assertEqual([stage.status.value for stage in reopened.list_project_stages(project_id)],
                             ["completed"] * 8)
            persisted_p8 = next(item for item in reopened.list_project_artifacts(project_id)
                                if item.artifact_id == p8_artifact.artifact_id)
            self.assertEqual(json.loads(persisted_p8.content), json.loads(p8_artifact.content))


if __name__ == "__main__":
    unittest.main()
