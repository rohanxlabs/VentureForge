"""Gradio workspace for P8 pitch, judge, demo, and submission preparation."""
from __future__ import annotations

import json

from src.hackathon_intake import PROJECT_DATABASE
from src.hackathon_pitch_prep import (
    DemoScriptStep,
    JudgeQuestionPrep,
    PitchPreparationPackage,
    RubricProof,
    SlideOutline,
    latest_pitch_prep,
    pitch_prep_context,
    review_pitch_prep,
    save_pitch_prep,
)
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ProjectStageKey


def _store() -> ProjectStore:
    return ProjectStore(PROJECT_DATABASE)


def _starter(context: dict) -> str:
    p1, p6, p7 = context["frame"], context["p6"], context["p7"]
    p2, p4, p5 = context["report"], context["p4"], context["p5"]
    option = context["option"]
    proof = [p1.artifact_id, p6.artifact_id]
    evidence_id = next(iter(context["evidence_ids"]), None)
    for_points = ["Approved problem framing", "Approved P6 execution and test record"]
    if evidence_id:
        proof.append(evidence_id)
        for_points.append("Verified P2 research evidence; explain the limits of desk research.")
    mappings = [RubricProof(
        criterion=criterion,
        pitch_claim=(f"The selected direction '{option['name']}' targets the approved problem and is demonstrated by the recorded prototype scope."),
        proof_points=for_points,
        evidence_ids=[evidence_id] if evidence_id else [],
        demo_moment="Show the implemented P6 flow and the observed P6 test result; update if the feature differs.",
        confidence="partial", gap="Prototype evidence does not establish realized community outcomes.",
        proof_references=proof.copy(),
        remaining_weakness="Real-world use and impact have not been established by the prototype record.",
        improvement_action="Run an appropriate, consented pilot and improve the metric using observed results.")
        for criterion in context["criteria"]]
    judge_questions = [
        JudgeQuestionPrep(question="How do you know this is a real and important problem for this user group?",
            what_judges_are_testing="Problem evidence and user understanding.",
            suggested_answer="Point to the approved P1 frame and P2 sources; distinguish desk research from direct user validation.",
            evidence_ids=[evidence_id] if evidence_id else [], proof_references=[p1.artifact_id, p2.artifact_id]),
        JudgeQuestionPrep(question="What evidence supports the chosen solution direction over current alternatives?",
            what_judges_are_testing="Problem-solution fit and evidence quality.",
            suggested_answer="Explain the P3 decision and P4 comparison, then state the assumptions that still need testing.",
            evidence_ids=[evidence_id] if evidence_id else [], proof_references=[p2.artifact_id, p4.artifact_id]),
        JudgeQuestionPrep(question="What is actually implemented, and what is simulated or deferred?",
            what_judges_are_testing="Technical completion and honesty about scope.",
            suggested_answer="Use the P5 plan and P6 task record to identify real, mocked, simulated, and deferred behavior.",
            evidence_ids=[], proof_references=[p6.artifact_id]),
        JudgeQuestionPrep(question="How would you measure whether this creates the intended impact?",
            what_judges_are_testing="Impact measurement and limitations.",
            suggested_answer="Describe the P7 metric and method. Do not present the impact hypothesis as a measured outcome.",
            evidence_ids=[], proof_references=[p7.artifact_id]),
        JudgeQuestionPrep(question="What is the biggest risk or dependency before a real pilot?",
            what_judges_are_testing="Safety, feasibility, and operational readiness.",
            suggested_answer="Name the P7 risk, mitigation, owner, and any remaining permission or integration dependency.",
            evidence_ids=[], proof_references=[p5.artifact_id, p7.artifact_id]),
    ]
    slides = [
        SlideOutline(title="The problem", key_message="State who experiences the problem and in what context.",
            talking_points=["Describe the current process", "State what remains unknown"],
            proof_references=[p1.artifact_id], duration_seconds=45),
        SlideOutline(title="What the evidence says", key_message="Separate research signals from assumptions and direct user validation.",
            talking_points=["Show a relevant source-backed finding", "Name the research limits"],
            evidence_ids=[evidence_id] if evidence_id else [], proof_references=[p2.artifact_id], duration_seconds=45),
        SlideOutline(title="The selected direction", key_message=f"Explain why the team chose {option['name']}.",
            talking_points=[option.get("concept", "Explain the selected solution concept.")],
            proof_references=[p4.artifact_id], duration_seconds=45),
        SlideOutline(title="The prototype", key_message="Show only behavior that is implemented or clearly labeled as simulated.",
            talking_points=["Follow the user journey", "Show a recorded test or known limitation"],
            proof_references=[p5.artifact_id, p6.artifact_id], duration_seconds=90),
        SlideOutline(title="Impact and next step", key_message="Present the P7 impact hypothesis and its measurement plan.",
            talking_points=["State the metric", "Name the next pilot and key risk"],
            proof_references=[p7.artifact_id], duration_seconds=45),
    ]
    demo = [
        DemoScriptStep(elapsed_seconds=0, duration_seconds=30,
            narrator="Introduce the target user and the current problem.",
            action="Open the implemented entry point and state the demo scenario.",
            expected_result="The audience understands the target user and scenario.",
            fallback="Use the approved P1 frame and a labeled screenshot if the app cannot open.",
            implementation_status="real", proof_references=[p1.artifact_id]),
        DemoScriptStep(elapsed_seconds=30, duration_seconds=60,
            narrator="Walk through the selected user flow.",
            action="Perform the actual P6-recorded workflow; clearly call out any simulated step.",
            expected_result="Show the result recorded in the P6 execution and test artifact.",
            fallback="Show a verified screenshot or recording; if none exists, explain the limitation without claiming a successful run.",
            implementation_status="real", proof_references=[p6.artifact_id]),
        DemoScriptStep(elapsed_seconds=90, duration_seconds=30,
            narrator="Close with the impact hypothesis and next measurement.",
            action="Show the P7 metric, risk, and proposed pilot.",
            expected_result="Judges can distinguish prototype evidence from real-world impact.",
            fallback="Use the P7 brief and state that no real-world impact measurement is available yet.",
            implementation_status="real", proof_references=[p7.artifact_id]),
    ]
    open_gaps = list(context["p7_data"].get("limitations_and_nonclaims", []))
    return json.dumps({
        "schema_version": "ventureforge.p8.pitch_preparation.v1",
        "pitch_summary": (f"{option['name']} explores a way to address {context['p1_data'].get('problem_statement', 'the approved problem')}. "
            "The team built a bounded prototype and recorded its tests. This package reports only the evidence in those artifacts; "
            "the impact remains a hypothesis until measured with appropriate real-world research."),
        "rubric_mapping": [item.model_dump(mode="json") for item in mappings],
        "judge_questions": [item.model_dump(mode="json") for item in judge_questions],
        "presentation_outline": [item.model_dump(mode="json") for item in slides],
        "demo_script": [item.model_dump(mode="json") for item in demo],
        "submission_checklist": [item.model_dump(mode="json") for item in context["submission_items"]],
        "open_gaps": open_gaps,
        "claims_and_nonclaims": [
            "P6 records prototype execution and test observations, not real-world emergency effectiveness.",
            "P7 impact is a hypothesis and measurement plan unless separate outcome data is supplied.",
            "Clearly label live, mocked, simulated, and deferred behavior during the demo."
        ],
        "pitch_duration_minutes": context["event"].get("demo_format") or "Confirm pitch and demo timing with event organizers.",
        **context["upstream"],
        "p7_artifact_id": p7.artifact_id, "p7_artifact_version": p7.version,
        "selected_option_id": option["option_id"],
    }, ensure_ascii=False, indent=2)


def build_pitch_preparation_ui(gr, project_selector) -> None:
    def inspect(project_id):
        if not project_id:
            return "Select a project to inspect P8.", "", "", "", "", ""
        try:
            store = _store()
            context = pitch_prep_context(store, project_id)
            stage = store.get_project_stage(project_id, ProjectStageKey.P8)
            artifact = latest_pitch_prep(store, project_id)
            payload = artifact.content if artifact else _starter(context)
            decision_id = (artifact.decision_id or "") if artifact else ""
            approval = store.get_artifact_approval_status(project_id, artifact.artifact_id) if artifact else "draft"
            status = (f"### P8 - Pitch & Judge Preparation | Stage: {stage.status.value}"
                f" | Package: {f'v{artifact.version}' if artifact else 'not saved'} | Review: {approval}\n\n"
                "The package supports honest preparation; its presence does not mean deliverables are ready or impact is proven.")
            return status, context["view"], payload, artifact.artifact_id if artifact else "", decision_id, approval
        except Exception as exc:
            return f"P8 is not ready: {exc}", "", "", "", "", ""

    def save_package_ui(project_id, payload):
        if not project_id:
            return "Select a project first.", "", "", "", ""
        try:
            package, artifact, decision = save_pitch_prep(_store(), project_id, payload or "")
            return (f"Saved P8 preparation package v{artifact.version}. Review every claim, rubric mapping, checklist item, and fallback.",
                package.model_dump_json(indent=2), artifact.artifact_id, decision.decision_id,
                _store().get_artifact_approval_status(project_id, artifact.artifact_id))
        except Exception as exc:
            return f"Could not save P8 package: {exc}", "", "", "", ""

    def review_package_ui(project_id, artifact_id, decision_id, status, comment):
        if not project_id or not artifact_id or not decision_id:
            return "Save the P8 package before reviewing it.", "", "", ""
        try:
            event = review_pitch_prep(_store(), project_id, artifact_id, decision_id,
                ApprovalEventStatus(status), comment or "")
            refreshed = inspect(project_id)
            next_step = " P8 is complete; the package has passed human review." if event.status == ApprovalEventStatus.APPROVED else " P8 remains open for revision."
            return f"Recorded P8 review: {event.status.value}.{next_step}", refreshed[0], refreshed[2], refreshed[5]
        except Exception as exc:
            return f"Could not record P8 review: {exc}", "", "", ""

    with gr.Accordion("P8 - Pitch & Judge Preparation", open=False):
        gr.Markdown(
            "Prepare the story and submission against the actual event requirements. Map each criterion to a claim, proof, "
            "demo moment, weakness, and improvement action. Rehearse honest answers and a failure fallback."
        )
        p8_status = gr.Markdown("Select a project to inspect P8 readiness.")
        context_view = gr.Markdown()
        package_json = gr.Code(label="P8 pitch and submission JSON - review and edit the starter package",
            language="json", interactive=True, lines=40)
        with gr.Row():
            artifact_id = gr.Textbox(label="Latest P8 package artifact ID", interactive=False)
            decision_id = gr.Textbox(label="Linked P8 review decision ID", interactive=False)
        review_state = gr.Markdown("No P8 package reviewed yet.")
        save_button = gr.Button("Save / revise P8 package", variant="primary")
        save_status = gr.Markdown()
        with gr.Row():
            review_status = gr.Dropdown(label="Final P8 review", choices=[
                ("Approve and complete P8", "approved"), ("Request changes", "changes_requested"),
                ("Reject", "rejected")], value="approved")
            review_comment = gr.Textbox(label="Review note (required for changes or rejection)")
        review_button = gr.Button("Record P8 review")
        review_output = gr.Markdown()

        project_selector.change(inspect, inputs=[project_selector], outputs=[p8_status, context_view,
            package_json, artifact_id, decision_id, review_state])
        save_button.click(save_package_ui, inputs=[project_selector, package_json],
            outputs=[save_status, package_json, artifact_id, decision_id, review_state])
        review_button.click(review_package_ui, inputs=[project_selector, artifact_id,
            decision_id, review_status, review_comment],
            outputs=[review_output, p8_status, package_json, review_state])
