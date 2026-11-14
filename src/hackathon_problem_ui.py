"""Gradio UI for the P1 problem-framing workflow."""
from __future__ import annotations

from src.hackathon_intake import (
    ChallengeBrief,
    PROJECT_DATABASE,
    latest_challenge_brief,
    split_lines,
)
from src.problem_intelligence import (
    ProblemFrame,
    ProblemInput,
    approve_frame,
    create_frame,
    latest_problem_frame,
    save_frame,
    start_problem_framing,
)
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus


def _store() -> ProjectStore:
    return ProjectStore(PROJECT_DATABASE)


def _latest_frame_view(store: ProjectStore, project_id: str) -> tuple[str, str, str, str]:
    project = store.get_project(project_id)
    stage = store.get_project_stage(project_id, "P1")
    artifact = latest_problem_frame(store, project_id)
    if artifact is None:
        return (f"### P1 - Problem Framing\n\n**Project:** {project.name}  \n"
                f"**Stage:** {stage.status.value}  \nNo Problem Frame has been saved yet.", "", "", "")
    frame = ProblemFrame.model_validate_json(artifact.content or "{}")
    approval = store.get_artifact_approval_status(project_id, artifact.artifact_id)
    decision_approval = store.get_decision_approval_status(project_id, artifact.decision_id) if artifact.decision_id else "missing"
    lines = [f"### P1 - Problem Framing | {project.name}",
             f"**Stage:** {stage.status.value} | **Frame:** v{artifact.version} | **Review:** {approval}",
             f"**Linked decision:** {decision_approval}", "", f"**Problem statement:** {frame.problem_statement}",
             "", f"**Affected users:** {frame.target_user_summary}",
             f"**Current workflow:** {frame.workflow_summary}", "", frame.overview]
    claim_groups = (
        ("User-provided context", [c.statement for c in frame.claims if c.origin == "user_input" and c.status == "known"]),
        ("Hypotheses to investigate", [c.statement for c in frame.claims if c.status == "hypothesis"]),
        ("Assumptions", [c.statement for c in frame.claims if c.status == "assumption"]),
        ("Unknowns", frame.unknowns),
        ("Research questions", frame.research_questions),
        ("Scope", frame.scope),
        ("Out of scope", frame.out_of_scope),
    )
    for heading, values in claim_groups:
        if values:
            lines.extend(["", f"#### {heading}", *[f"- {value}" for value in values]])
    lines.extend(["", "Framing is ready for human review. Approval accepts this frame as the basis for P2 research; it does not validate the problem."])
    return "\n".join(lines), artifact.artifact_id, artifact.decision_id or "", approval


def _frame_form_values(store: ProjectStore, project_id: str) -> tuple:
    artifact = latest_problem_frame(store, project_id)
    if artifact is None:
        return ("", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "")
    frame = ProblemFrame.model_validate_json(artifact.content or "{}")
    return (
        frame.title, frame.description, frame.domain, frame.environment or "", frame.geography or "",
        "\n".join(frame.primary_users), "\n".join(frame.secondary_users), "\n".join(frame.stakeholders),
        "\n".join(frame.current_process), frame.where_it_occurs or "", frame.when_it_occurs or "",
        frame.frequency or "", "\n".join(frame.existing_tools), "\n".join(frame.workarounds),
        "\n".join(frame.pain_points), "\n".join(frame.root_cause_hypotheses),
        "\n".join(frame.existing_alternatives), "\n".join(frame.alternative_limitations),
        "\n".join(frame.measured_impacts), "\n".join(frame.qualitative_consequences),
        "\n".join(frame.constraints), "\n".join(frame.assumptions), "\n".join(frame.unknowns),
    )


def build_problem_framing_ui(gr, project_selector) -> None:
    def inspect(project_id):
        if not project_id:
            return "Select a project to begin P1.", "", "", ""
        try:
            return _latest_frame_view(_store(), project_id)
        except Exception as exc:
            return f"Could not load P1: {exc}", "", "", ""

    def load_frame(project_id):
        if not project_id:
            return ("", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "")
        try:
            return _frame_form_values(_store(), project_id)
        except Exception:
            return ("", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "")

    def save_frame_ui(project_id, title, description, domain, environment, geography,
                      primary_users, secondary_users, stakeholders, current_process,
                      where_it_occurs, when_it_occurs, frequency, existing_tools, workarounds,
                      pain_points, root_cause_hypotheses, existing_alternatives,
                      alternative_limitations, measured_impacts, qualitative_consequences,
                      constraints, assumptions, unknowns):
        if not project_id:
            return "Select a project first.", "", "", ""
        try:
            store = _store()
            brief_artifact = latest_challenge_brief(store, project_id)
            brief = ChallengeBrief.model_validate_json(brief_artifact.content or "{}") if brief_artifact else None
            context_parts = []
            if brief:
                context_parts.extend([
                    f"Official event: {brief.event_name}",
                    f"Challenge: {brief.challenge_title}",
                    f"Organizer prompt (context only, not independent user evidence): {brief.official_prompt}",
                ])
                if brief.track:
                    context_parts.append(f"Track: {brief.track}")
                context_parts.extend(f"Event constraint: {item}" for item in brief.constraints_and_eligibility)
            problem = ProblemInput(
                title=title or "", description=description or "", domain=domain or "",
                environment=environment or None, geography=geography or None,
                hackathon_theme=brief.challenge_title if brief else None,
                additional_context="\n".join(context_parts) or None,
                primary_users=split_lines(primary_users), secondary_users=split_lines(secondary_users),
                stakeholders=split_lines(stakeholders), current_process=split_lines(current_process),
                where_it_occurs=where_it_occurs or None, when_it_occurs=when_it_occurs or None,
                frequency=frequency or None, existing_tools=split_lines(existing_tools),
                workarounds=split_lines(workarounds), pain_points=split_lines(pain_points),
                root_cause_hypotheses=split_lines(root_cause_hypotheses),
                existing_alternatives=split_lines(existing_alternatives),
                alternative_limitations=split_lines(alternative_limitations),
                measured_impacts=split_lines(measured_impacts),
                qualitative_consequences=split_lines(qualitative_consequences),
                constraints=list(dict.fromkeys(split_lines(constraints) + (brief.constraints_and_eligibility if brief else []))),
                assumptions=split_lines(assumptions), unknowns=split_lines(unknowns),
            )
            stage = store.get_project_stage(project_id, "P1")
            if stage.status.value in {"pending", "in_progress"}:
                start_problem_framing(store, project_id)
            elif stage.status.value != "completed":
                raise ValueError(f"Problem framing cannot be edited while P1 is {stage.status.value}.")
            artifact, decision = save_frame(store, project_id, create_frame(problem))
            view, _, _, status = _latest_frame_view(store, project_id)
            return f"Saved Problem Frame v{artifact.version}. Review it below; approval completes P1.", view, artifact.artifact_id, decision.decision_id
        except Exception as exc:
            return f"Could not create Problem Frame: {exc}", "", "", ""

    def review_frame_ui(project_id, artifact_id, decision_id, status, comment):
        if not project_id or not artifact_id or not decision_id:
            return "Save a Problem Frame before reviewing it.", ""
        try:
            approval = ApprovalEventStatus(status)
            if approval != ApprovalEventStatus.APPROVED and not (comment or "").strip():
                raise ValueError("Add a review note when requesting changes or rejecting the frame.")
            approve_frame(_store(), project_id, artifact_id, decision_id, approval, comment or "")
            view, _, _, _ = _latest_frame_view(_store(), project_id)
            return f"Recorded P1 review: {approval.value}.", view
        except Exception as exc:
            return f"Could not record P1 review: {exc}", ""

    with gr.Accordion("P1 - Problem Framing", open=True):
        gr.Markdown(
            "Use the event prompt as context, then describe the underlying difficulty and affected people. "
            "Keep observations, assumptions, and unknowns distinct. The frame generator is deterministic; "
            "it does not collect external evidence or validate the problem."
        )
        with gr.Row():
            frame_title = gr.Textbox(label="Problem title", placeholder="Short name for the difficulty")
            frame_domain = gr.Textbox(label="Domain")
            frame_geography = gr.Textbox(label="Geography")
        frame_description = gr.Textbox(label="Underlying problem (do not propose a solution)", lines=3)
        with gr.Row():
            frame_environment = gr.Textbox(label="Environment / setting")
            frame_frequency = gr.Textbox(label="Frequency (only if known)")
        with gr.Row():
            primary_users = gr.Textbox(label="Primary affected users (one per line)", lines=3)
            secondary_users = gr.Textbox(label="Secondary users (one per line)", lines=3)
            stakeholders = gr.Textbox(label="Other stakeholders (one per line)", lines=3)
        current_process = gr.Textbox(label="What people do today (one step per line)", lines=3)
        with gr.Row():
            where_it_occurs = gr.Textbox(label="Where it occurs")
            when_it_occurs = gr.Textbox(label="When it occurs")
        with gr.Row():
            pain_points = gr.Textbox(label="Reported pain points (one per line)", lines=3)
            workarounds = gr.Textbox(label="Workarounds (one per line)", lines=3)
            existing_tools = gr.Textbox(label="Tools currently used (one per line)", lines=3)
        with gr.Row():
            alternatives = gr.Textbox(label="Existing alternatives (one per line)", lines=3)
            alternative_limitations = gr.Textbox(label="Limitations of alternatives (one per line)", lines=3)
        root_cause_hypotheses = gr.Textbox(label="Possible causes - hypotheses, not facts (one per line)", lines=3)
        with gr.Row():
            measured_impacts = gr.Textbox(label="Measured impacts (only observed measurements)", lines=3)
            qualitative_consequences = gr.Textbox(label="Qualitative consequences (one per line)", lines=3)
        with gr.Row():
            constraints = gr.Textbox(label="Problem / context constraints (one per line)", lines=3)
            assumptions = gr.Textbox(label="Assumptions (one per line)", lines=3)
            unknowns = gr.Textbox(label="Unknowns to investigate (one per line)", lines=3)
        with gr.Row():
            load_frame_button = gr.Button("Load latest frame into form")
            save_frame_button = gr.Button("Create / revise Problem Frame", variant="primary")
        frame_save_status = gr.Markdown()
        with gr.Row():
            frame_artifact_id = gr.Textbox(label="Latest frame artifact ID", interactive=False)
            frame_decision_id = gr.Textbox(label="Linked review decision ID", interactive=False)
        frame_review = gr.Markdown("No frame loaded.")
        with gr.Row():
            frame_review_status = gr.Dropdown(label="Review decision", choices=[
                ("Approve", "approved"), ("Request changes", "changes_requested"),
                ("Reject", "rejected")], value="approved")
            frame_review_comment = gr.Textbox(label="Review note")
        review_frame_button = gr.Button("Record P1 review")
        frame_review_output = gr.Markdown()

        form_components = [frame_title, frame_description, frame_domain, frame_environment,
            frame_geography, primary_users, secondary_users, stakeholders, current_process,
            where_it_occurs, when_it_occurs, frame_frequency, existing_tools, workarounds,
            pain_points, root_cause_hypotheses, alternatives, alternative_limitations,
            measured_impacts, qualitative_consequences, constraints, assumptions, unknowns]
        project_selector.change(inspect, inputs=[project_selector],
            outputs=[frame_review, frame_artifact_id, frame_decision_id, frame_save_status])
        load_frame_button.click(load_frame, inputs=[project_selector], outputs=form_components)
        save_frame_button.click(save_frame_ui,
            inputs=[project_selector, frame_title, frame_description, frame_domain, frame_environment,
                frame_geography, primary_users, secondary_users, stakeholders, current_process,
                where_it_occurs, when_it_occurs, frame_frequency, existing_tools, workarounds,
                pain_points, root_cause_hypotheses, alternatives, alternative_limitations,
                measured_impacts, qualitative_consequences, constraints, assumptions, unknowns],
            outputs=[frame_save_status, frame_review, frame_artifact_id, frame_decision_id])
        review_frame_button.click(review_frame_ui,
            inputs=[project_selector, frame_artifact_id, frame_decision_id, frame_review_status, frame_review_comment],
            outputs=[frame_review_output, frame_review])
