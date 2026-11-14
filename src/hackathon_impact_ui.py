"""Gradio workspace for evidence-linked P7 impact and feasibility review."""
from __future__ import annotations

import json

from src.hackathon_impact import (
    ImpactFeasibilityBrief,
    impact_context,
    latest_impact_brief,
    review_impact_brief,
    save_impact_brief,
)
from src.hackathon_intake import PROJECT_DATABASE
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ProjectStageKey


def _store() -> ProjectStore:
    return ProjectStore(PROJECT_DATABASE)


def _starter(context: dict) -> str:
    option = context["option"]
    rubric = {name: "Describe the claim this project can support for this event criterion." for name in context["rubric"]}
    return json.dumps({
        "schema_version": "ventureforge.p7.impact_feasibility.v1",
        "impact_hypothesis": (f"If {option.get('target_user', 'the target user')} can use {option.get('name', 'the selected direction')}, "
            "then a defined outcome may improve because the user can take the next action with less friction. This is a hypothesis."),
        "beneficiary_group": option.get("target_user", "Name the specific beneficiary group."),
        "mechanism": "Explain the steps that could connect the selected feature to a user-level outcome; do not claim the outcome is already proven.",
        "claim_level": "hypothesis",
        "supporting_evidence_ids": [],
        "prototype_test_ids": [],
        "primary_metric": {
            "name": "Task completion rate",
            "definition": "Share of observed participants who complete the defined user task without assistance.",
            "unit": "percent of observed participants",
            "baseline": "Unknown; no real-world baseline has been measured.",
            "target_or_success_signal": "Set a threshold after choosing an appropriate sample and test protocol.",
            "measurement_method": "Run a consented usability task with affected users and record completion and assistance.",
            "measurement_window": "During a planned pilot; set the duration with participants and partners.",
            "owner": "Assign an owner before running the measurement."
        },
        "stakeholders": [{
            "role": "Affected residents",
            "benefit_or_interest": "May receive a clearer next action when local warning information is fragmented.",
            "adoption_barrier": "Trust, accessibility, language, and access to a suitable device need validation."
        }],
        "adoption_path": [
            {"step": "Review the workflow with affected residents and local preparedness partners.",
             "owner_or_actor": "Project team with community partner", "prerequisite": "Obtain consent and event-permitted access.",
             "success_signal": "Users understand the intended purpose and limits."},
            {"step": "Run a small, supervised pilot with a non-emergency information source.",
             "owner_or_actor": "Pilot owner", "prerequisite": "Verify information source, accessibility, privacy, and fallback procedures.",
             "success_signal": "The primary metric can be collected safely and interpreted with its limitations."}
        ],
        "resources_and_costs": [{"resource": "Community partner and participant time",
            "need_or_estimate": "Not estimated; identify a partner and agree on appropriate participant compensation.",
            "basis": "not_estimated", "evidence_reference": ""}],
        "operational_considerations": [
            "Do not use the prototype as an authoritative emergency alert channel.",
            "Confirm ownership and freshness of any information before a real pilot."
        ],
        "risks": [{"category": "Safety and trust",
            "description": "A resident could mistake an incomplete prototype alert for authoritative emergency guidance.",
            "likelihood": "unknown", "severity": "high",
            "mitigation": "Use synthetic or clearly labeled non-emergency data, add limits in the interface, and require an authorized source before any real pilot.",
            "owner": "Assign a safety reviewer before any field test.", "evidence_ids": []}],
        "rubric_alignment": rubric,
        "limitations_and_nonclaims": [
            "This brief is an impact hypothesis and measurement plan, not evidence of realized community impact.",
            "The P6 prototype tests do not establish emergency readiness or real-world warning effectiveness."
        ],
        "selected_option_id": context["upstream"]["selected_option_id"],
        **context["upstream"],
    }, ensure_ascii=False, indent=2)


def build_impact_feasibility_ui(gr, project_selector) -> None:
    def inspect(project_id):
        if not project_id:
            return "Select a project to inspect P7.", "", "", "", "", ""
        try:
            store = _store()
            context = impact_context(store, project_id)
            stage = store.get_project_stage(project_id, ProjectStageKey.P7)
            artifact = latest_impact_brief(store, project_id)
            payload = artifact.content if artifact else _starter(context)
            decision_id = (artifact.decision_id or "") if artifact else ""
            approval = store.get_artifact_approval_status(project_id, artifact.artifact_id) if artifact else "draft"
            plan = ImpactFeasibilityBrief.model_validate_json(payload)
            status = (f"### P7 - Business, Impact & Risk | Stage: {stage.status.value}"
                f" | Brief: {f'v{artifact.version}' if artifact else 'not saved'} | Review: {approval}\n\n"
                "Treat impact as a hypothesis unless you have appropriate real-world measurements. P6 prototype signals are not community outcomes.")
            return status, context["view"], payload, artifact.artifact_id if artifact else "", decision_id, approval
        except Exception as exc:
            return f"P7 is not ready: {exc}", "", "", "", "", ""

    def save_brief_ui(project_id, payload):
        if not project_id:
            return "Select a project first.", "", "", "", ""
        try:
            brief, artifact, decision = save_impact_brief(_store(), project_id, payload or "")
            return (f"Saved P7 impact and feasibility brief v{artifact.version}. Review its assumptions and risk mitigations before approval.",
                brief.model_dump_json(indent=2), artifact.artifact_id, decision.decision_id,
                _store().get_artifact_approval_status(project_id, artifact.artifact_id))
        except Exception as exc:
            return f"Could not save P7 brief: {exc}", "", "", "", ""

    def review_brief_ui(project_id, artifact_id, decision_id, status, comment):
        if not project_id or not artifact_id or not decision_id:
            return "Save the P7 brief before reviewing it.", "", "", ""
        try:
            event = review_impact_brief(_store(), project_id, artifact_id, decision_id,
                ApprovalEventStatus(status), comment or "")
            refreshed = inspect(project_id)
            next_step = " P7 is complete; its brief can inform P8." if event.status == ApprovalEventStatus.APPROVED else " P7 remains open for revision."
            return f"Recorded P7 review: {event.status.value}.{next_step}", refreshed[0], refreshed[2], refreshed[5]
        except Exception as exc:
            return f"Could not record P7 review: {exc}", "", "", ""

    with gr.Accordion("P7 - Business, Impact & Risk", open=False):
        gr.Markdown(
            "Describe who could benefit, how the selected direction may help, and how to measure that outcome. "
            "Keep hypotheses, prototype signals, and real-world results distinct. Do not invent market size, realized impact, or costs."
        )
        p7_status = gr.Markdown("Select a project to inspect P7 readiness.")
        context_view = gr.Markdown()
        brief_json = gr.Code(label="P7 impact and feasibility JSON - review and edit the starter brief",
            language="json", interactive=True, lines=34)
        with gr.Row():
            artifact_id = gr.Textbox(label="Latest P7 brief artifact ID", interactive=False)
            decision_id = gr.Textbox(label="Linked P7 review decision ID", interactive=False)
        review_state = gr.Markdown("No P7 brief reviewed yet.")
        save_button = gr.Button("Save / revise P7 brief", variant="primary")
        save_status = gr.Markdown()
        with gr.Row():
            review_status = gr.Dropdown(label="P7 review", choices=[
                ("Approve and complete P7", "approved"), ("Request changes", "changes_requested"),
                ("Reject", "rejected")], value="approved")
            review_comment = gr.Textbox(label="Review note (required for changes or rejection)")
        review_button = gr.Button("Record P7 review")
        review_output = gr.Markdown()

        project_selector.change(inspect, inputs=[project_selector], outputs=[p7_status, context_view,
            brief_json, artifact_id, decision_id, review_state])
        save_button.click(save_brief_ui, inputs=[project_selector, brief_json],
            outputs=[save_status, brief_json, artifact_id, decision_id, review_state])
        review_button.click(review_brief_ui, inputs=[project_selector, artifact_id,
            decision_id, review_status, review_comment],
            outputs=[review_output, p7_status, brief_json, review_state])
