"""Gradio workspace for tracking MVP execution and test results in P6."""
from __future__ import annotations

import json

from src.hackathon_execution import (
    MVPExecutionRecord,
    execution_context,
    latest_execution_record,
    review_execution_record,
    save_execution_update,
    starter_execution_record,
)
from src.hackathon_intake import PROJECT_DATABASE
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ProjectStageKey


def _store() -> ProjectStore:
    return ProjectStore(PROJECT_DATABASE)


def build_mvp_execution_ui(gr, project_selector) -> None:
    def inspect(project_id):
        if not project_id:
            return "Select a project to inspect P6.", "", "", "", "", ""
        try:
            store = _store()
            context = execution_context(store, project_id)
            stage = store.get_project_stage(project_id, ProjectStageKey.P6)
            artifact = latest_execution_record(store, project_id)
            record = (MVPExecutionRecord.model_validate_json(artifact.content or "{}") if artifact
                      else starter_execution_record(context))
            approval = store.get_artifact_approval_status(project_id, artifact.artifact_id) if artifact else "draft"
            status = (f"### P6 - MVP Execution & Testing | Stage: {stage.status.value}"
                f" | Record: {f'v{artifact.version}' if artifact else 'not saved'} | Review: {approval}\n\n"
                f"Tasks: {len(record.tasks)} | Tests/acceptance checks: {len(record.tests)} | "
                f"Open completion gaps: {len(record.completion_gaps())}")
            return status, context["view"], record.model_dump_json(indent=2), artifact.artifact_id if artifact else "", artifact.decision_id or "" if artifact else "", approval
        except Exception as exc:
            return f"P6 is not ready: {exc}", "", "", "", "", ""

    def save_update_ui(project_id, payload):
        if not project_id:
            return "Select a project first.", "", "", "", ""
        try:
            record, artifact, decision = save_execution_update(_store(), project_id, payload or "")
            return (f"Saved P6 execution record v{artifact.version}. Record actual progress and save again before review.",
                record.model_dump_json(indent=2), artifact.artifact_id, decision.decision_id,
                _store().get_artifact_approval_status(project_id, artifact.artifact_id))
        except Exception as exc:
            return f"Could not save P6 progress: {exc}", "", "", "", ""

    def review_update_ui(project_id, artifact_id, decision_id, status, comment):
        if not project_id or not artifact_id or not decision_id:
            return "Save the current execution record before reviewing it.", "", "", ""
        try:
            event = review_execution_record(_store(), project_id, artifact_id, decision_id,
                ApprovalEventStatus(status), comment or "")
            refreshed = inspect(project_id)
            route = (" P6 is complete; the project has a reviewed MVP execution and test record."
                     if event.status == ApprovalEventStatus.APPROVED else " P6 remains open for updates.")
            return f"Recorded P6 review: {event.status.value}.{route}", refreshed[0], refreshed[2], refreshed[5]
        except Exception as exc:
            return f"Could not record P6 review: {exc}", "", "", ""

    with gr.Accordion("P6 - MVP Execution & Testing", open=False):
        gr.Markdown(
            "Track real implementation progress from the approved P5 plan. Record observed outcomes for every planned test "
            "and demo criterion, including failures and limitations. This board records work; it does not build the product for you."
        )
        p6_status = gr.Markdown("Select a project to inspect P6 readiness.")
        context_view = gr.Markdown()
        execution_json = gr.Code(label="P6 execution record JSON - update task status and test observations",
            language="json", interactive=True, lines=34)
        with gr.Row():
            artifact_id = gr.Textbox(label="Latest P6 record artifact ID", interactive=False)
            decision_id = gr.Textbox(label="Linked P6 review decision ID", interactive=False)
        review_state = gr.Markdown("No P6 execution record saved yet.")
        save_button = gr.Button("Save execution progress", variant="primary")
        save_status = gr.Markdown()
        with gr.Row():
            review_status = gr.Dropdown(label="P6 completion review", choices=[
                ("Approve and complete P6", "approved"), ("Request changes", "changes_requested"),
                ("Reject", "rejected")], value="approved")
            review_comment = gr.Textbox(label="Review note (required for changes or rejection)")
        review_button = gr.Button("Record P6 review")
        review_output = gr.Markdown()

        project_selector.change(inspect, inputs=[project_selector], outputs=[p6_status, context_view,
            execution_json, artifact_id, decision_id, review_state])
        save_button.click(save_update_ui, inputs=[project_selector, execution_json],
            outputs=[save_status, execution_json, artifact_id, decision_id, review_state])
        review_button.click(review_update_ui, inputs=[project_selector, artifact_id,
            decision_id, review_status, review_comment],
            outputs=[review_output, p6_status, execution_json, review_state])
