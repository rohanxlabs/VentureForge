import json
import tempfile
import unittest
from pathlib import Path

from src.hackathon_intake import ChallengeBrief, JudgingCriterion, approve_challenge_brief, save_challenge_brief
from src.hackathon_solution import (
    SolutionDirectionAction,
    SolutionOptionsDraft,
    record_solution_direction,
    save_solution_options,
    solution_context,
)
from src.hackathon_solution_ui import _starter
from src.hackathon_validation import ValidationDecision, ValidationOutcome, save_validation_decision
from src.problem_intelligence import ProblemInput, approve_frame, create_frame, create_research_plan, save_frame
from src.project_store import ProjectStore
from src.research.budget import SearchBudget
from src.research.models import ApprovalEventStatus, CandidateExtraction, ProjectCreate, SourceSnapshot, SupportRelation
from src.research.project_intelligence import execute_project_research
from src.research.search import CollectionResult


class SyntheticOrchestrator:
    def collect(self, *, run_id, prompt, questions, budget):
        return CollectionResult(sources=[SourceSnapshot(run_id=run_id, question_id=q.id,
            url=f"https://evidence-{i}.example.test", title=f"Source {i}", content=f"Evidence passage {i}.")
            for i, q in enumerate(questions)], searches_attempted=len(questions),
            searches_succeeded=len(questions), searches_failed=0)


def synthetic_extractor(question, source):
    return CandidateExtraction(relevant=True, claim=f"Support for {question}",
        passage=source.content, relation=SupportRelation.SUPPORTS)


class HackathonSolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.temp.name) / "p4.sqlite")
        self.project = self.store.create_project(ProjectCreate(name="P4 solution comparison"))
        brief, brief_decision = save_challenge_brief(self.store, self.project.project_id, ChallengeBrief(
            event_name="Civic Hack", challenge_title="Hazard alerts", challenge_type="assigned",
            official_prompt="Help residents receive useful local hazard warnings.", team_size=3,
            time_available_hours=24, team_skills=["Python", "UX"],
            judging_criteria=[JudgingCriterion(name="Impact"), JudgingCriterion(name="Feasibility")]))
        approve_challenge_brief(self.store, self.project.project_id, brief.artifact_id,
            brief_decision.decision_id, ApprovalEventStatus.APPROVED)
        frame, frame_decision = save_frame(self.store, self.project.project_id, create_frame(ProblemInput(
            title="Late hazard warnings", description="Residents receive local hazard warnings too late to act.",
            primary_users=["Residents in hazard-prone areas"], unknowns=["Local warning lead time is unknown"])))
        approve_frame(self.store, self.project.project_id, frame.artifact_id,
            frame_decision.decision_id, ApprovalEventStatus.APPROVED)
        plan, plan_artifact, plan_decision = create_research_plan(self.store, self.project.project_id)
        self.store.approve_p2_artifact(self.project.project_id, plan_artifact.artifact_id,
            plan_decision.decision_id, "research_plan", ApprovalEventStatus.APPROVED)
        _, report, report_decision, _ = execute_project_research(self.store, self.project.project_id,
            budget=SearchBudget(max_search_calls=8, max_pages=20, max_tavily_credits=8, max_seconds=20),
            orchestrator=SyntheticOrchestrator(), max_rounds=1, extractor=synthetic_extractor)
        self.store.approve_p2_artifact(self.project.project_id, report.artifact_id,
            report_decision.decision_id, "research_report", ApprovalEventStatus.APPROVED)
        context = solution_context_after_p3_setup(self.store, self.project.project_id)
        self.evidence_id = next(iter(context["verified_evidence_ids"]))

    def tearDown(self):
        self.temp.cleanup()

    def make_options(self, **updates):
        context = solution_context(self.store, self.project.project_id)
        base = dict(schema_version="ventureforge.p4.solution_options.v1", options=[
            dict(option_id="option-1", name="Local alert relay", concept="Route timely localized warnings through trusted community channels.",
                target_user="Residents in hazard-prone areas", problem_fit="Targets the warning delay in the approved problem frame.",
                evidence_ids=[self.evidence_id], rubric_fit={"Impact": "Shows earlier warning access for residents.", "Feasibility": "A bounded alert flow can be demoed."},
                feasibility="A narrow alert flow is plausible in the 24 hour team window.", team_skills_needed=["Python", "UX"],
                dependencies=["Reliable alert source"], risks=["No official feed access"], assumptions=["Residents can receive alerts digitally"],
                mvp_boundary="Demo one alert intake to resident notification flow.", riskiest_assumption="Residents can act on the alert channel in time.",
                experiment="Show a clickable warning flow to residents and ask them to interpret the alert.", success_signal="Participants can identify the action and location."),
            dict(option_id="option-2", name="Preparedness checklist", concept="Help households make a practical readiness plan before a hazard event.",
                target_user="Residents in hazard-prone areas", problem_fit="Addresses readiness when official warnings arrive late.",
                evidence_ids=[], rubric_fit={"Impact": "Demonstrates a household readiness action.", "Feasibility": "Checklist is feasible for a short demo."},
                feasibility="A checklist prototype has few integration dependencies.", team_skills_needed=["UX"], dependencies=[], risks=[], assumptions=[],
                mvp_boundary="One personalized checklist with a saved readiness status.", riskiest_assumption="Households find a checklist useful before an event.",
                experiment="Test the checklist with affected households and observe task completion.", success_signal="Users complete a key readiness step without help.")],
            selected_option_id=None, selection_rationale="", action=None,
            problem_frame_artifact_id=context["frame"].artifact_id, problem_frame_version=context["frame"].version,
            research_report_artifact_id=context["report"].artifact_id, research_report_version=context["report"].version,
            validation_decision_artifact_id=context["validation"].artifact_id, validation_decision_version=context["validation"].version,
            experiment_owner="", experiment_timebox="")
        base.update(updates)
        return SolutionOptionsDraft(**base)

    def test_p4_context_requires_p3_proceed_and_includes_rubric_and_constraints(self):
        context = solution_context(self.store, self.project.project_id)
        self.assertEqual(context["rubric"], ["Impact", "Feasibility"])
        self.assertIn("Team capacity", context["view"])
        self.assertIn("Evidence IDs available", context["view"])
        self.assertEqual(len(SolutionOptionsDraft.model_validate_json(_starter(context)).options), 2)

    def test_save_draft_then_select_direction_completes_p4_and_versions_artifact(self):
        _draft, first = save_solution_options(self.store, self.project.project_id,
            self.make_options().model_dump_json())
        selected = self.make_options(action=SolutionDirectionAction.SELECT, selected_option_id="option-1",
            selection_rationale="This direction is the clearest fit for the supported warning delay and event constraints.",
            experiment_owner="Team member A", experiment_timebox="First six hours")
        decision, final, linked = record_solution_direction(self.store, self.project.project_id,
            selected.model_dump_json())
        self.assertEqual(first.version, 1)
        self.assertEqual(final.version, 2)
        self.assertEqual(final.decision_id, linked.decision_id)
        self.assertEqual(decision.selected_option_id, "option-1")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P4").status.value, "completed")

    def test_generic_p4_completion_cannot_bypass_approved_selection(self):
        self.store.start_project_stage(self.project.project_id, "P4")
        with self.assertRaisesRegex(ValueError, "persisted selected solution direction"):
            self.store.complete_project_stage(self.project.project_id, "P4")
        with self.assertRaisesRegex(ValueError, "persisted selected solution direction"):
            self.store.transition_project_stage(self.project.project_id, "P4", "completed")

    def test_rubric_mapping_and_verified_evidence_references_are_enforced(self):
        option = self.make_options().options[0].model_copy(update={"evidence_ids": ["foreign-id"]})
        draft = self.make_options(options=[option, self.make_options().options[1]])
        with self.assertRaisesRegex(ValueError, "not verified"):
            save_solution_options(self.store, self.project.project_id, draft.model_dump_json())
        wrong_rubric = self.make_options().options[0].model_copy(update={"rubric_fit": {"Impact": "Only one criterion"}})
        draft = self.make_options(options=[wrong_rubric, self.make_options().options[1]])
        with self.assertRaisesRegex(ValueError, "map every event criterion"):
            save_solution_options(self.store, self.project.project_id, draft.model_dump_json())

    def test_return_to_p3_reopens_evidence_decision_loop(self):
        draft = self.make_options(action=SolutionDirectionAction.RETURN_TO_P3,
            selection_rationale="The solution comparison exposed an unresolved question about whether warnings arrive in time.")
        record_solution_direction(self.store, self.project.project_id, draft.model_dump_json())
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P3").status.value, "in_progress")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P4").status.value, "in_progress")

    def test_return_to_p1_reopens_problem_framing(self):
        draft = self.make_options(action=SolutionDirectionAction.RETURN_TO_P1,
            selection_rationale="The target user is too broad to compare solution directions responsibly.")
        record_solution_direction(self.store, self.project.project_id, draft.model_dump_json())
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P1").status.value, "in_progress")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P3").status.value, "in_progress")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P4").status.value, "in_progress")


def solution_context_after_p3_setup(store, project_id):
    from src.hackathon_validation import ValidationDecision, ValidationOutcome, save_validation_decision
    from src.hackathon_validation import validation_context
    context = validation_context(store, project_id)
    evidence_id = next(iter(context["evidence_ids"]))
    decision = ValidationDecision(outcome=ValidationOutcome.PROCEED,
        rationale="Research signals justify comparing a small set of solution directions.",
        supporting_evidence_ids=[evidence_id], contradicting_evidence_ids=[], first_hand_evidence="No interviews yet.",
        evidence_limitations="Research is desk-based and does not measure local behavior.",
        unresolved_questions=["Can residents act on warnings in time?"], confidence="Low; local behavior needs direct testing.",
        next_action="Compare bounded directions and test their riskiest assumptions.",
        problem_frame_artifact_id=context["frame"].artifact_id, problem_frame_version=context["frame"].version,
        research_report_artifact_id=context["report"].artifact_id, research_report_version=context["report"].version)
    save_validation_decision(store, project_id, decision)
    return solution_context(store, project_id)


if __name__ == "__main__":
    unittest.main()
