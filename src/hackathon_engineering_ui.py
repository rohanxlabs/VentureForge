"""Gradio workspace for the P5 engineering plan and human review gate."""
from __future__ import annotations

import json

from src.hackathon_engineering import (
    EngineeringPlan,
    capacity_summary,
    engineering_context,
    latest_engineering_plan,
    review_engineering_plan,
    save_engineering_plan,
)
from src.hackathon_intake import PROJECT_DATABASE
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ProjectStageKey


def _store() -> ProjectStore:
    return ProjectStore(PROJECT_DATABASE)


def _starter(context: dict) -> str:
    criterion = next(iter(context["criteria"]), "")
    selected = context["option"]
    return json.dumps({
        "schema_version": "ventureforge.p5.engineering_plan.v1",
        "user_journey": ["Describe the first user action.", "Describe the result the user sees."],
        "architecture_data_flow": "Describe the UI, services, storage, data flow, and which parts are real or mocked.",
        "integrations": [],
        "security_privacy_accessibility": "Privacy: ... Security: ... Accessibility: ... State what applies and what is out of scope.",
        "failure_behaviors": ["Describe what the user sees if the main dependency is unavailable."],
        "mvp_boundary": selected.get("mvp_boundary", "Define the smallest end-to-end slice and deferred scope."),
        "build_tasks": [
            {"id": "task-1", "title": "Build core user flow", "description": "Implement the minimum UI and service flow for the selected direction.",
             "priority": "must", "estimated_person_hours": 3, "depends_on": [],
             "acceptance_criteria": ["A user can complete the first journey step."],
             "judging_criteria": [criterion] if criterion else [], "owner": "Unassigned - name an owner",
             "demo_status": "real"},
            {"id": "task-2", "title": "Persist the core result", "description": "Save and reload the result needed to demonstrate the user value.",
             "priority": "must", "estimated_person_hours": 3, "depends_on": ["Build core user flow"],
             "acceptance_criteria": ["The saved result can be reloaded."],
             "judging_criteria": [criterion] if criterion else [], "owner": "Unassigned - name an owner",
             "demo_status": "real"},
            {"id": "task-3", "title": "Verify demo and fallback", "description": "Exercise the main journey and document a fallback for unavailable dependencies.",
             "priority": "must", "estimated_person_hours": 2, "depends_on": ["Persist the core result"],
             "acceptance_criteria": ["The happy path and one failure path are demonstrated."],
             "judging_criteria": [criterion] if criterion else [], "owner": "Unassigned - name an owner",
             "demo_status": "real"},
        ],
        "demo_plan": "Describe the demo path and clearly call out simulated or mocked behavior.",
        "demo_acceptance_criteria": ["The selected user journey can be completed.",
            "The main result is visible and persisted.", "A dependency failure has a clear fallback."],
        "test_scenarios": ["Describe one realistic user scenario and its expected outcome."],
        "deferred_scope": [],
        "selected_option_id": selected["option_id"],
        "p4_artifact_id": context["p4"].artifact_id,
        "p4_artifact_version": context["p4"].version,
        **context["upstream"],
    }, ensure_ascii=False, indent=2)


def build_solution_engineering_ui(gr, project_selector) -> None:
    def inspect(project_id):
        if not project_id:
            return "Select a project to inspect P5.", "", "", "", "", ""
        try:
            store = _store()
            context = engineering_context(store, project_id)
            stage = store.get_project_stage(project_id, ProjectStageKey.P5)
            artifact = latest_engineering_plan(store, project_id)
            payload = artifact.content if artifact else _starter(context)
            decision_id = artifact.decision_id or "" if artifact else ""
            approval = store.get_artifact_approval_status(project_id, artifact.artifact_id) if artifact else "draft"
            status = (f"### P5 - Solution Engineering | Stage: {stage.status.value}"
                f" | Plan: {f'v{artifact.version}' if artifact else 'not saved'} | Review: {approval}\n\n"
                "Review architecture, implementation ownership, estimates, failure behavior, and demo boundaries before P6.")
            plan = EngineeringPlan.model_validate_json(payload)
            status += "\n\n" + capacity_summary(plan, context)
            return status, context["view"], payload, artifact.artifact_id if artifact else "", decision_id, approval
        except Exception as exc:
            return f"P5 is not ready: {exc}", "", "", "", "", ""

    def save_plan_ui(project_id, edited_json):
        if not project_id:
            return "Select a project first.", "", "", "", ""
        try:
            _plan, artifact, decision = save_engineering_plan(_store(), project_id, edited_json or "")
            status, _context, payload, artifact_id, decision_id, approval = inspect(project_id)
            return f"Saved P5 engineering plan v{artifact.version} as a draft. Review it before approving P5.", payload, artifact_id, decision.decision_id, approval
        except Exception as exc:
            return f"Could not save P5 plan: {exc}", "", "", "", ""

    def review_plan_ui(project_id, artifact_id, decision_id, status, comment):
        if not project_id or not artifact_id or not decision_id:
            return "Save an engineering plan before reviewing it.", "", "", ""
        try:
            event = review_engineering_plan(_store(), project_id, artifact_id, decision_id,
                ApprovalEventStatus(status), comment or "")
            summary, _context, payload, new_artifact_id, new_decision_id, approval = inspect(project_id)
            next_step = " P5 is complete; the approved plan can guide P6." if event.status == ApprovalEventStatus.APPROVED else " P5 remains open for revision."
            return f"Recorded P5 review: {event.status.value}.{next_step}", summary, payload, approval
        except Exception as exc:
            return f"Could not record P5 review: {exc}", "", "", ""

    with gr.Accordion("P5 - Solution Engineering", open=False):
        gr.Markdown(
            "Turn the selected P4 direction into a bounded implementation plan. Mark integrations and tasks as real, "
            "mocked, simulated, or deferred. Estimates are planning inputs; review capacity with the team."
        )
        p5_status = gr.Markdown("Select a project to inspect P5 readiness.")
        context_view = gr.Markdown()
        plan_json = gr.Code(label="P5 engineering plan JSON - review and edit the starter plan",
            language="json", interactive=True, lines=32)
        with gr.Row():
            plan_artifact_id = gr.Textbox(label="Latest P5 plan artifact ID", interactive=False)
            plan_decision_id = gr.Textbox(label="Linked review decision ID", interactive=False)
        plan_review_status = gr.Markdown("No plan reviewed yet.")
        save_button = gr.Button("Save / revise engineering plan draft", variant="primary")
        save_status = gr.Markdown()
        with gr.Row():
            review_status = gr.Dropdown(label="P5 review", choices=[
                ("Approve and complete P5", "approved"),
                ("Request changes", "changes_requested"), ("Reject", "rejected")], value="approved")
            review_comment = gr.Textbox(label="Review note (required for changes or rejection)")
        review_button = gr.Button("Record P5 review")
        review_output = gr.Markdown()

        project_selector.change(inspect, inputs=[project_selector], outputs=[p5_status, context_view,
            plan_json, plan_artifact_id, plan_decision_id, plan_review_status])
        save_button.click(save_plan_ui, inputs=[project_selector, plan_json],
            outputs=[save_status, plan_json, plan_artifact_id, plan_decision_id, plan_review_status])
        review_button.click(review_plan_ui, inputs=[project_selector, plan_artifact_id,
            plan_decision_id, review_status, review_comment],
            outputs=[review_output, p5_status, plan_json, plan_review_status])
