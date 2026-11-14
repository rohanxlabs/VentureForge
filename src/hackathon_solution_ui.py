"""Gradio workspace for project-scoped P4 solution comparison."""
from __future__ import annotations

import json

from src.hackathon_intake import PROJECT_DATABASE
from src.hackathon_solution import (
    SolutionDirectionAction,
    SolutionOptionsDraft,
    latest_solution_options,
    record_solution_direction,
    save_solution_options,
    solution_context,
)
from src.project_store import ProjectStore
from src.research.models import ProjectStageKey


def _store() -> ProjectStore:
    return ProjectStore(PROJECT_DATABASE)


def _starter(context: dict) -> str:
    criteria = context["rubric"]
    options = []
    for index in range(1, 3):
        options.append({
            "option_id": f"option-{index}",
            "name": f"Option {index} - replace with a distinct direction",
            "concept": "Describe a specific way to reduce the validated problem for the target user.",
            "target_user": "Name the specific affected user from the approved Problem Frame.",
            "problem_fit": "Explain how this direction addresses the approved problem and user need.",
            "evidence_ids": [],
            "rubric_fit": {name: "Explain what the demo would show for this criterion." for name in criteria},
            "feasibility": "Explain scope against the team skills, remaining time, and event constraints.",
            "team_skills_needed": [],
            "dependencies": [],
            "risks": [],
            "assumptions": [],
            "mvp_boundary": "Define the smallest end-to-end slice and what will be deferred.",
            "riskiest_assumption": "State the least certain assumption that could invalidate this direction.",
            "experiment": "Describe a practical test that could challenge the riskiest assumption.",
            "success_signal": "State an observable result that would count as a useful signal.",
        })
    return json.dumps({
        "schema_version": "ventureforge.p4.solution_options.v1",
        "options": options,
        "selected_option_id": None,
        "selection_rationale": "",
        "action": None,
        "problem_frame_artifact_id": context["frame"].artifact_id,
        "problem_frame_version": context["frame"].version,
        "research_report_artifact_id": context["report"].artifact_id,
        "research_report_version": context["report"].version,
        "validation_decision_artifact_id": context["validation"].artifact_id,
        "validation_decision_version": context["validation"].version,
        "experiment_owner": "",
        "experiment_timebox": "",
    }, ensure_ascii=False, indent=2)


def build_solution_intelligence_ui(gr, project_selector) -> None:
    def inspect(project_id):
        if not project_id:
            return "Select a project to inspect P4.", "", ""
        try:
            store = _store()
            context = solution_context(store, project_id)
            stage = store.get_project_stage(project_id, ProjectStageKey.P4)
            artifact = latest_solution_options(store, project_id)
            draft_text = artifact.content if artifact else _starter(context)
            status = (f"### P4 - Solution Intelligence | Stage: {stage.status.value}"
                f" | Options: {artifact.version if artifact else 'not drafted'}\n\n"
                "Compare directions against the approved evidence and event rubric. The workflow does not score or auto-select an option.")
            return status, context["view"], draft_text or _starter(context)
        except Exception as exc:
            return f"P4 is not ready: {exc}", "", ""

    def save_options_ui(project_id, edited_json):
        if not project_id:
            return "Select a project first.", ""
        try:
            data = json.loads(edited_json or "{}")
            data.update(action=None, selected_option_id=None, selection_rationale="",
                        experiment_owner="", experiment_timebox="")
            draft, artifact = save_solution_options(_store(), project_id,
                SolutionOptionsDraft.model_validate(data).model_dump_json())
            status, context, _ = inspect(project_id)
            return f"Saved P4 comparison draft v{artifact.version}. Review the options and record a direction decision when ready.", draft.model_dump_json(indent=2)
        except Exception as exc:
            return f"Could not save P4 comparison: {exc}", ""

    def record_direction_ui(project_id, edited_json, action, selected_option, rationale, owner, timebox):
        if not project_id:
            return "Select a project first.", ""
        try:
            data = json.loads(edited_json or "{}")
            data.update(action=action, selected_option_id=(selected_option or "").strip() or None,
                selection_rationale=rationale or "", experiment_owner=owner or "", experiment_timebox=timebox or "")
            draft, artifact, _decision = record_solution_direction(_store(), project_id,
                SolutionOptionsDraft.model_validate(data).model_dump_json())
            messages = {
                SolutionDirectionAction.SELECT: "P4 complete. P5 engineering planning can use this selected option and its experiment.",
                SolutionDirectionAction.RETURN_TO_P3: "P3 reopened to revisit the problem evidence.",
                SolutionDirectionAction.RETURN_TO_P1: "P1 reopened to revise the problem framing.",
            }
            return f"Recorded P4 decision v{artifact.version}. {messages[draft.action]}", draft.model_dump_json(indent=2)
        except Exception as exc:
            return f"Could not record P4 decision: {exc}", ""

    with gr.Accordion("P4 - Solution Intelligence", open=False):
        gr.Markdown(
            "Compare at least two distinct directions for the approved problem. Map each to the actual event rubric, "
            "team constraints, evidence, and a falsifiable experiment. Scores are deliberately not used to decide for you."
        )
        p4_status = gr.Markdown("Select a project to inspect P4 readiness.")
        context_view = gr.Markdown()
        options_json = gr.Code(label="Solution options JSON - edit the starter options or load a saved draft",
            language="json", interactive=True, lines=28)
        with gr.Row():
            save_draft_button = gr.Button("Save / revise options draft")
            action = gr.Dropdown(label="Decision", choices=[
                ("Select a solution direction", "select"),
                ("Return to P3 evidence review", "return_to_p3"),
                ("Return to P1 problem framing", "return_to_p1")], value="select")
            selected_option = gr.Textbox(label="Selected option ID (required when selecting)", placeholder="e.g. option-1")
        with gr.Row():
            rationale = gr.Textbox(label="Decision rationale (at least 20 characters)", lines=3)
            owner = gr.Textbox(label="Experiment owner (required when selecting)")
            timebox = gr.Textbox(label="Experiment timebox (required when selecting)")
        record_button = gr.Button("Record P4 decision", variant="primary")
        decision_status = gr.Markdown()

        project_selector.change(inspect, inputs=[project_selector],
            outputs=[p4_status, context_view, options_json])
        save_draft_button.click(save_options_ui, inputs=[project_selector, options_json],
            outputs=[decision_status, options_json])
        record_button.click(record_direction_ui,
            inputs=[project_selector, options_json, action, selected_option, rationale, owner, timebox],
            outputs=[decision_status, options_json])
