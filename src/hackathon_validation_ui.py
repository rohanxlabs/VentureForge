"""Gradio workspace for human-led P3 problem validation."""
from __future__ import annotations

import json

from src.hackathon_intake import PROJECT_DATABASE
from src.hackathon_validation import (
    ValidationDecision,
    ValidationOutcome,
    PrimaryResearchMethod,
    PrimaryResearchRecord,
    save_primary_research_record,
    save_validation_decision,
    validation_context,
)
from src.project_store import ProjectStore
from src.research.models import ProjectStageKey


def _store() -> ProjectStore:
    return ProjectStore(PROJECT_DATABASE)


def _latest_decision(store: ProjectStore, project_id: str):
    items = [a for a in store.list_project_artifacts(project_id)
             if a.stage_key == ProjectStageKey.P3 and a.artifact_type == "validation_decision"]
    return max(items, key=lambda item: (item.version, item.created_at, item.artifact_id)) if items else None


def build_problem_validation_ui(gr, project_selector) -> None:
    def inspect(project_id):
        if not project_id:
            return "Select a project to review P3.", "", "", "", ""
        try:
            store = _store()
            context = validation_context(store, project_id)
            stage = store.get_project_stage(project_id, ProjectStageKey.P3)
            latest = _latest_decision(store, project_id)
            previous = "No P3 decision has been recorded yet."
            if latest:
                payload = json.loads(latest.content or "{}")
                previous = (f"### Latest human decision v{latest.version}: {payload.get('outcome', 'unknown')}\n\n"
                    f"**Rationale:** {payload.get('rationale', '')}\n\n"
                    f"**Next action:** {payload.get('next_action', '')}")
            status = (f"### P3 - Problem Validation | Stage: {stage.status.value}\n\n"
                "This stage does not auto-validate the problem. Make a decision from the evidence and explain uncertainty.")
            return status, context["frame_view"], context["report_view"], context["primary_view"], previous
        except Exception as exc:
            return f"P3 is not ready: {exc}", "", "", "", ""

    def save_primary_ui(project_id, method, conducted_at, participant_context,
                        recruitment_context, questions, findings, contradictions,
                        limitations, consent, researcher):
        if not project_id:
            return "Select a project first.", ""
        try:
            context = validation_context(_store(), project_id)
            record = PrimaryResearchRecord(method=PrimaryResearchMethod(method),
                conducted_at=conducted_at or "", participant_context=participant_context or "",
                recruitment_context=recruitment_context or "",
                questions_or_task=[x.strip() for x in (questions or "").splitlines() if x.strip()],
                findings=[x.strip() for x in (findings or "").splitlines() if x.strip()],
                contradictory_findings=[x.strip() for x in (contradictions or "").splitlines() if x.strip()],
                limitations=limitations or "", consent_and_privacy=consent or "",
                researcher=researcher or "", problem_frame_artifact_id=context["frame"].artifact_id,
                problem_frame_version=context["frame"].version)
            artifact = save_primary_research_record(_store(), project_id, record)
            refreshed = validation_context(_store(), project_id)
            return f"Saved structured {method} record `{artifact.artifact_id}`.", refreshed["primary_view"]
        except Exception as exc:
            return f"Could not save primary research: {exc}", ""

    def save_decision_ui(project_id, outcome, rationale, support_ids, contradict_ids,
                         first_hand, primary_ids, limitations, unresolved, confidence, next_action,
                         method, owner, timebox):
        if not project_id:
            return "Select a project first.", ""
        try:
            context = validation_context(_store(), project_id)
            decision = ValidationDecision(
                outcome=ValidationOutcome(outcome), rationale=rationale or "",
                supporting_evidence_ids=[x.strip() for x in (support_ids or "").splitlines() if x.strip()],
                contradicting_evidence_ids=[x.strip() for x in (contradict_ids or "").splitlines() if x.strip()],
                primary_research_artifact_ids=[x.strip() for x in (primary_ids or "").splitlines() if x.strip()],
                first_hand_evidence=first_hand or "", evidence_limitations=limitations or "",
                unresolved_questions=[x.strip() for x in (unresolved or "").splitlines() if x.strip()],
                confidence=confidence or "", next_action=next_action or "",
                evidence_method=method or "", evidence_owner=owner or "", evidence_timebox=timebox or "",
                problem_frame_artifact_id=context["frame"].artifact_id,
                problem_frame_version=context["frame"].version,
                research_report_artifact_id=context["report"].artifact_id,
                research_report_version=context["report"].version)
            artifact, _linked = save_validation_decision(_store(), project_id, decision)
            status, frame, report, primary, previous = inspect(project_id)
            route = {
                ValidationOutcome.PROCEED: "P3 complete; proceed to P4 when implemented.",
                ValidationOutcome.GATHER_MORE_EVIDENCE: "P2 reopened; create a new research plan to address the stated gap.",
                ValidationOutcome.REFRAME: "P1 reopened; revise the Problem Frame, then research the new framing.",
                ValidationOutcome.STOP: "P3 complete; the recorded decision is to stop this problem direction.",
            }[decision.outcome]
            return f"Saved P3 decision v{artifact.version}. {route}", previous
        except Exception as exc:
            return f"Could not save P3 decision: {exc}", ""

    with gr.Accordion("P3 - Problem Validation", open=False):
        gr.Markdown(
            "Review the approved P1 frame and P2 report together. Decide whether this problem deserves the next hackathon step. "
            "Desk research is a signal, not proof; record first-hand observations separately and describe remaining uncertainty."
        )
        p3_status = gr.Markdown("Select a project to inspect P3 readiness.")
        with gr.Row():
            p1_review = gr.Markdown()
            p2_review = gr.Markdown()
        outcome = gr.Dropdown(label="Your decision", choices=[
            ("Proceed to solution exploration", "proceed"),
            ("Gather more evidence", "gather_more_evidence"),
            ("Reframe the problem or target user", "reframe"),
            ("Stop this problem direction", "stop")], value="proceed")
        rationale = gr.Textbox(label="Why this is your decision (minimum 20 characters)", lines=4)
        with gr.Row():
            supporting = gr.Textbox(label="Supporting P2 evidence IDs (one ID per line)", lines=3,
                placeholder="Copy IDs from the evidence shown in the P2 report")
            contradicting = gr.Textbox(label="Contradicting / weakening P2 evidence IDs (one per line)", lines=3)
        with gr.Accordion("Record first-hand research", open=False):
            gr.Markdown("Save interviews, observations, or surveys as structured project records. Use participant context only; never enter names or sensitive identifiers.")
            primary_method = gr.Dropdown(label="Method", choices=[(item.value.title(), item.value) for item in PrimaryResearchMethod], value="interview")
            with gr.Row():
                conducted_at = gr.Textbox(label="When conducted")
                researcher = gr.Textbox(label="Researcher / owner")
            participant_context = gr.Textbox(label="Participant context (non-identifying)", lines=2)
            recruitment_context = gr.Textbox(label="How participants were recruited / survey sample")
            questions = gr.Textbox(label="Interview questions, observation task, or survey questions (one per line)", lines=3)
            findings = gr.Textbox(label="Observed answers / behaviors / findings (one per line)", lines=4)
            contradictions = gr.Textbox(label="Contradictory findings (one per line)", lines=2)
            with gr.Row():
                primary_limitations = gr.Textbox(label="Bias, sample limits, and caveats", lines=2)
                consent = gr.Textbox(label="Consent and privacy handling", lines=2)
            primary_save = gr.Button("Save primary research record")
            primary_status = gr.Markdown()
            primary_records = gr.Markdown("No structured first-hand research records have been saved.")
            primary_save.click(save_primary_ui, inputs=[project_selector, primary_method, conducted_at,
                participant_context, recruitment_context, questions, findings, contradictions,
                primary_limitations, consent, researcher], outputs=[primary_status, primary_records])
        first_hand = gr.Textbox(label="Additional first-hand context (optional)", lines=3)
        primary_ids = gr.Textbox(label="Primary research record IDs used in this decision (one ID per line)", lines=2,
            placeholder="Copy IDs from the saved records above")
        limitations = gr.Textbox(label="Evidence limits and possible bias", lines=3)
        unresolved = gr.Textbox(label="Unresolved questions or assumptions (one per line)", lines=3)
        with gr.Row():
            confidence = gr.Textbox(label="Confidence and why (explain uncertainty; no score required)", lines=2)
            next_action = gr.Textbox(label="Next action / what would change your mind", lines=2)
        with gr.Row():
            evidence_method = gr.Textbox(label="If gathering evidence: method")
            evidence_owner = gr.Textbox(label="If gathering evidence: owner")
            evidence_timebox = gr.Textbox(label="If gathering evidence: timebox / due point")
        save_button = gr.Button("Record human P3 decision", variant="primary")
        save_status = gr.Markdown()
        previous_decision = gr.Markdown()

        project_selector.change(inspect, inputs=[project_selector],
            outputs=[p3_status, p1_review, p2_review, primary_records, previous_decision])
        save_button.click(save_decision_ui, inputs=[project_selector, outcome, rationale,
            supporting, contradicting, first_hand, primary_ids, limitations, unresolved, confidence,
            next_action, evidence_method, evidence_owner, evidence_timebox],
            outputs=[save_status, previous_decision])
