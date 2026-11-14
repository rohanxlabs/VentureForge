"""Competition intake and review services for the pre-P1 project setup."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from src.project_store import ProjectStore
from src.database_config import resolve_database_path
from src.research.models import (
    ApprovalEventStatus,
    ArtifactCreate,
    DecisionCreate,
)


PROJECT_DATABASE = resolve_database_path()


class JudgingCriterion(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    weight_percent: float | None = Field(default=None, ge=0, le=100)
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Every judging criterion needs a name.")
        return value


class ChallengeBrief(BaseModel):
    document_type: Literal["ventureforge.challenge_brief.v1"] = "ventureforge.challenge_brief.v1"
    event_name: str = Field(min_length=1, max_length=200)
    challenge_title: str = Field(min_length=1, max_length=240)
    challenge_type: Literal["assigned", "theme_constrained", "open"]
    official_prompt: str = Field(min_length=10, max_length=12000)
    track: str = ""
    official_url: str = ""
    rules_url: str = ""
    deadline: str = ""
    timezone: str = ""
    team_size: int | None = Field(default=None, ge=1, le=100)
    time_available_hours: float | None = Field(default=None, gt=0, le=1000)
    team_skills: list[str] = Field(default_factory=list)
    required_technologies: list[str] = Field(default_factory=list)
    allowed_tools_and_resources: list[str] = Field(default_factory=list)
    constraints_and_eligibility: list[str] = Field(default_factory=list)
    submission_requirements: list[str] = Field(default_factory=list)
    demo_format: str = ""
    judging_criteria: list[JudgingCriterion] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)

    @field_validator("event_name", "challenge_title", "official_prompt")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Required text cannot be blank.")
        return value


def split_lines(value: str | None) -> list[str]:
    return [line.strip(" \t-*") for line in (value or "").splitlines() if line.strip(" \t-*")]


def parse_criteria(value: str | None) -> list[JudgingCriterion]:
    """Parse one criterion per line: name | weight% | description (weight optional)."""
    criteria = []
    for line in split_lines(value):
        parts = [part.strip() for part in line.split("|", 2)]
        weight = None
        description = ""
        if len(parts) >= 2:
            raw_weight = parts[1].removesuffix("%").strip()
            if raw_weight:
                try:
                    weight = float(raw_weight)
                except ValueError:
                    description = " | ".join(parts[1:])
            if len(parts) == 3:
                description = parts[2]
        criteria.append(JudgingCriterion(name=parts[0], weight_percent=weight, description=description))
    supplied_weights = [item.weight_percent for item in criteria]
    if criteria and all(weight is not None for weight in supplied_weights):
        total = sum(weight for weight in supplied_weights if weight is not None)
        if abs(total - 100) > 0.01:
            raise ValueError(f"Judging criterion weights total {total:g}%; adjust them to 100% or leave weights blank.")
    return criteria


def latest_challenge_brief(store: ProjectStore, project_id: str):
    items = [item for item in store.list_project_artifacts(project_id)
             if item.stage_key is None and item.artifact_type == "challenge_brief"]
    return max(items, key=lambda item: (item.version, item.created_at, item.artifact_id)) if items else None


def save_challenge_brief(store: ProjectStore, project_id: str, brief: ChallengeBrief):
    project = store.get_project(project_id)
    if project.status.value != "active":
        raise ValueError("Challenge Setup can only be changed for an active project.")
    if store.get_project_stage(project_id, "P1").status.value != "pending":
        raise ValueError("Challenge Setup is locked after P1 begins in this initial slice; a later workflow will handle event changes and downstream review.")
    prior = latest_challenge_brief(store, project_id)
    decision = store.create_decision(DecisionCreate(
        project_id=project_id,
        title="Review Challenge Brief",
        description="Confirm the official challenge, event constraints, and judging rubric before P1.",
        decision_type="challenge_brief_review",
    ))
    artifact = ArtifactCreate(
        project_id=project_id,
        stage_key=None,
        decision_id=decision.decision_id,
        artifact_type="challenge_brief",
        title=brief.challenge_title,
        description="Pre-P1 record of the official hackathon challenge and reviewed event requirements.",
        content=brief.model_dump_json(indent=2),
    )
    saved = store.create_artifact_version(project_id, prior.artifact_id, artifact) if prior else store.create_artifact(artifact)
    return saved, decision


def approve_challenge_brief(store: ProjectStore, project_id: str, artifact_id: str,
                            decision_id: str, status: ApprovalEventStatus,
                            comment: str = ""):
    return store.approve_challenge_brief(project_id, artifact_id, decision_id, status, comment=comment)


def require_approved_challenge_brief_if_started(store: ProjectStore, project_id: str) -> None:
    """If a project uses Challenge Setup, require its latest brief approval before P1."""
    item = latest_challenge_brief(store, project_id)
    if item is None:
        return  # Preserve the pre-existing CLI path for legacy projects.
    if (not item.decision_id
            or store.get_artifact_approval_status(project_id, item.artifact_id) != "approved"
            or store.get_decision_approval_status(project_id, item.decision_id) != "approved"):
        raise ValueError("The latest Challenge Brief and its linked decision must both be approved before P1.")


def render_challenge_brief(store: ProjectStore, project_id: str) -> tuple[str, str, str, str]:
    project = store.get_project(project_id)
    stages = store.list_project_stages(project_id)
    item = latest_challenge_brief(store, project_id)
    if item is None:
        return (f"### {project.name}\n\nChallenge Setup has not been saved. P1 is available after a Challenge Brief is reviewed and approved.", "", "", "")
    brief = ChallengeBrief.model_validate_json(item.content or "{}")
    artifact_status = store.get_artifact_approval_status(project_id, item.artifact_id)
    decision_status = store.get_decision_approval_status(project_id, item.decision_id) if item.decision_id else "missing"
    p1 = next(stage for stage in stages if stage.stage_key.value == "P1")
    approved = artifact_status == decision_status == ApprovalEventStatus.APPROVED.value
    readiness = "Approved - ready to begin P1" if approved else "Needs human review before P1"
    rows = [f"### {project.name}", f"**Challenge:** {brief.challenge_title}",
            f"**Event:** {brief.event_name}", f"**Type:** {brief.challenge_type.replace('_', ' ').title()}",
            f"**Brief version:** {item.version} | **Review:** {artifact_status} | **Decision:** {decision_status}",
            f"**P1:** {p1.status.value} | **Setup:** {readiness}", "", brief.official_prompt]
    if brief.track:
        rows.extend(["", f"**Track:** {brief.track}"])
    if brief.deadline:
        rows.append(f"**Deadline:** {brief.deadline} {brief.timezone}".strip())
    if brief.official_url:
        rows.append(f"**Official prompt source:** {brief.official_url}")
    if brief.rules_url:
        rows.append(f"**Rules source:** {brief.rules_url}")
    if brief.demo_format:
        rows.append(f"**Demo:** {brief.demo_format}")
    team = []
    if brief.team_size is not None:
        team.append(f"{brief.team_size} people")
    if brief.time_available_hours is not None:
        team.append(f"{brief.time_available_hours:g} build hours")
    if team:
        rows.append(f"**Team/time:** {', '.join(team)}")
    if brief.judging_criteria:
        rows.extend(["", "#### Judging criteria"])
        for criterion in brief.judging_criteria:
            weight = f" ({criterion.weight_percent:g}%)" if criterion.weight_percent is not None else ""
            desc = f" - {criterion.description}" if criterion.description else ""
            rows.append(f"- **{criterion.name}**{weight}{desc}")
    for label, values in (("Constraints and eligibility", brief.constraints_and_eligibility),
                          ("Team skills", brief.team_skills),
                          ("Required technologies", brief.required_technologies),
                          ("Allowed tools, APIs, data, and resources", brief.allowed_tools_and_resources),
                          ("Submission requirements", brief.submission_requirements),
                          ("Unresolved questions", brief.unresolved_questions)):
        if values:
            rows.extend(["", f"#### {label}", *[f"- {value}" for value in values]])
    return "\n".join(rows), item.artifact_id, item.decision_id or "", readiness


def validate_brief_payload(**fields) -> ChallengeBrief:
    fields["team_skills"] = split_lines(fields.get("team_skills"))
    fields["required_technologies"] = split_lines(fields.get("required_technologies"))
    fields["allowed_tools_and_resources"] = split_lines(fields.get("allowed_tools_and_resources"))
    fields["constraints_and_eligibility"] = split_lines(fields.get("constraints_and_eligibility"))
    fields["submission_requirements"] = split_lines(fields.get("submission_requirements"))
    fields["unresolved_questions"] = split_lines(fields.get("unresolved_questions"))
    fields["judging_criteria"] = [item.model_dump() for item in parse_criteria(fields.get("judging_criteria"))]
    for optional in ("team_size", "time_available_hours"):
        if fields.get(optional) in (None, ""):
            fields[optional] = None
    if fields.get("team_size") is not None:
        fields["team_size"] = int(fields["team_size"])
    if fields.get("time_available_hours") is not None:
        fields["time_available_hours"] = float(fields["time_available_hours"])
    return ChallengeBrief.model_validate(fields)


def challenge_brief_form_values(store: ProjectStore, project_id: str) -> tuple:
    item = latest_challenge_brief(store, project_id)
    if item is None:
        return ("", "", "assigned", "", "", "", "", "", "", "", None, None,
                "", "", "", "", "", "", "")
    brief = ChallengeBrief.model_validate_json(item.content or "{}")
    criteria = "\n".join(
        f"{criterion.name} | {criterion.weight_percent:g} | {criterion.description}".rstrip(" | ")
        if criterion.weight_percent is not None else f"{criterion.name} |  | {criterion.description}".rstrip(" | ")
        for criterion in brief.judging_criteria)
    return (brief.event_name, brief.challenge_title, brief.challenge_type, brief.official_prompt,
            brief.track, brief.official_url, brief.rules_url, brief.deadline, brief.timezone,
            brief.demo_format, brief.team_size, brief.time_available_hours,
            "\n".join(brief.team_skills), "\n".join(brief.required_technologies),
            "\n".join(brief.allowed_tools_and_resources), "\n".join(brief.constraints_and_eligibility),
            "\n".join(brief.submission_requirements), criteria, "\n".join(brief.unresolved_questions))


def brief_to_json(store: ProjectStore, project_id: str) -> str:
    item = latest_challenge_brief(store, project_id)
    return json.dumps(json.loads(item.content), indent=2, ensure_ascii=False) if item and item.content else ""


def build_challenge_setup_ui(gr) -> None:
    """Add the pre-P1 project intake workspace to an existing Gradio Blocks app."""
    from src.research.models import ProjectCreate

    def store_for_ui() -> ProjectStore:
        return ProjectStore(PROJECT_DATABASE)

    def project_choices():
        store = store_for_ui()
        return [(f"{item.name} ({item.project_id[:8]})", item.project_id)
                for item in store.list_projects(include_archived=False)]

    def create_project_ui(name, domain, description):
        if not (name or "").strip():
            return gr.Dropdown(choices=project_choices(), value=None), "Enter a project name first."
        try:
            project = store_for_ui().create_project(ProjectCreate(
                name=name.strip(), problem_domain=(domain or "").strip() or None,
                description=(description or "").strip() or None))
            choices = project_choices()
            return gr.Dropdown(choices=choices, value=project.project_id), f"Created project {project.name}. Add and review its Challenge Brief before starting P1."
        except Exception as exc:
            return gr.Dropdown(choices=project_choices(), value=None), f"Could not create project: {exc}"

    def refresh_projects_ui(current):
        choices = project_choices()
        values = {value for _, value in choices}
        selected = current if current in values else (choices[-1][1] if choices else None)
        return gr.Dropdown(choices=choices, value=selected)

    def inspect_project_ui(project_id):
        if not project_id:
            return "Select or create a project to begin.", "", "", ""
        try:
            return render_challenge_brief(store_for_ui(), project_id)
        except Exception as exc:
            return f"Could not load project: {exc}", "", "", ""

    def load_brief_ui(project_id):
        if not project_id:
            return ("", "", "assigned", "", "", "", "", "", "", "", None, None,
                    "", "", "", "", "", "", "")
        try:
            return challenge_brief_form_values(store_for_ui(), project_id)
        except Exception:
            return ("", "", "assigned", "", "", "", "", "", "", "", None, None,
                    "", "", "", "", "", "", "")

    def save_brief_ui(project_id, event_name, challenge_title, challenge_type, official_prompt,
                      track, official_url, rules_url, deadline, timezone, team_size,
                      time_available_hours, team_skills, required_technologies,
                      allowed_tools_and_resources, constraints_and_eligibility,
                      submission_requirements, demo_format, judging_criteria,
                      unresolved_questions):
        if not project_id:
            return "Create or select a project first.", "", "", ""
        try:
            brief = validate_brief_payload(
                event_name=event_name, challenge_title=challenge_title,
                challenge_type=challenge_type, official_prompt=official_prompt,
                track=track or "", official_url=official_url or "", rules_url=rules_url or "",
                deadline=deadline or "", timezone=timezone or "", team_size=team_size,
                time_available_hours=time_available_hours, team_skills=team_skills,
                required_technologies=required_technologies,
                allowed_tools_and_resources=allowed_tools_and_resources,
                constraints_and_eligibility=constraints_and_eligibility,
                submission_requirements=submission_requirements, demo_format=demo_format or "",
                judging_criteria=judging_criteria, unresolved_questions=unresolved_questions)
            store = store_for_ui()
            artifact, decision = save_challenge_brief(store, project_id, brief)
            summary, _, _, _ = render_challenge_brief(store, project_id)
            return f"Saved Challenge Brief v{artifact.version} as a draft. Review and approve it before beginning P1.", summary, artifact.artifact_id, decision.decision_id
        except Exception as exc:
            return f"Could not save Challenge Brief: {exc}", "", "", ""

    def review_brief_ui(project_id, artifact_id, decision_id, review_status, comment):
        if not project_id or not artifact_id or not decision_id:
            return "Select a project and save a Challenge Brief before reviewing it.", ""
        try:
            store = store_for_ui()
            status = ApprovalEventStatus(review_status)
            if status != ApprovalEventStatus.APPROVED and not (comment or "").strip():
                raise ValueError("Add a review note when requesting changes or rejecting the brief.")
            approve_challenge_brief(store, project_id, artifact_id, decision_id, status, comment or "")
            summary, _, _, readiness = render_challenge_brief(store, project_id)
            return f"Recorded {status.value}. {readiness}", summary
        except Exception as exc:
            return f"Could not record review: {exc}", ""

    def begin_p1_ui(project_id):
        if not project_id:
            return "Select a project first.", ""
        try:
            store = store_for_ui()
            item = latest_challenge_brief(store, project_id)
            if item is None or not item.decision_id:
                raise ValueError("Save and approve a Challenge Brief before P1.")
            if (store.get_artifact_approval_status(project_id, item.artifact_id) != "approved"
                    or store.get_decision_approval_status(project_id, item.decision_id) != "approved"):
                raise ValueError("The latest Challenge Brief and its linked decision must both be approved before P1.")
            stage = store.get_project_stage(project_id, "P1")
            if stage.status.value == "pending":
                stage = store.start_project_stage(project_id, "P1")
            elif stage.status.value != "in_progress":
                raise ValueError(f"P1 cannot begin while it is {stage.status.value}.")
            summary, _, _, _ = render_challenge_brief(store, project_id)
            return f"P1 Problem Framing is now {stage.status.value}. The approved Challenge Brief is ready as its starting context.", summary
        except Exception as exc:
            return f"P1 is still locked: {exc}", ""

    def orchestrate_workflow_ui(project_id):
        if not project_id:
            return "Select or create a project first."
        try:
            from src.hackathon_graph import start_or_resume_hackathon_workflow
            state = start_or_resume_hackathon_workflow(project_id)
            stage_lines = [f"- **{stage}:** {status}" for stage, status in state.get("stage_statuses", {}).items()]
            return (f"### Hackathon workflow: {state.get('workflow_status', 'unknown')}\n\n"
                f"**Current stage:** {state.get('current_stage', 'unknown')}\n\n"
                f"{state.get('message', '')}\n\n"
                "#### Project stage status\n" + "\n".join(stage_lines))
        except Exception as exc:
            return f"Could not start or resume the Hackathon workflow: {exc}"

    with gr.Column():
        gr.Markdown(
            """# VentureForge Hackathon Workspace
            Start with the event brief and constraints. Review and approve this setup before moving into P1 Problem Framing.
            """)
        gr.Markdown(f"**Active project and research database:** `{PROJECT_DATABASE}`")
        with gr.Accordion("Create a project", open=True):
            with gr.Row():
                project_name = gr.Textbox(label="Project name", placeholder="e.g. Community Flood Alert")
                project_domain = gr.Textbox(label="Problem area", placeholder="e.g. disaster response")
            project_description = gr.Textbox(label="Short project context", lines=2)
            create_project_button = gr.Button("Create project", variant="primary")
            project_create_status = gr.Markdown()

        with gr.Row():
            project_selector = gr.Dropdown(label="Project", choices=project_choices(), value=None)
            refresh_button = gr.Button("Refresh projects")

        with gr.Accordion("LangGraph workflow orchestration", open=False):
            gr.Markdown("Run the separate, human-gated Challenge Setup and P1-P8 coordinator. After completing a stage in its workspace, resume here to refresh progress and reach the next stage.")
            orchestrate_button = gr.Button("Start / resume Hackathon workflow")
            orchestrate_status = gr.Markdown("Workflow not started.")

        with gr.Accordion("Challenge Setup - event, rules, and rubric", open=True):
            gr.Markdown("Record requirements from the official event materials. Use one line per item. For criteria, use `Criterion | weight% | what judges look for`; weights may be omitted, or must total 100% if all are supplied.")
            with gr.Row():
                event_name = gr.Textbox(label="Hackathon / event name")
                challenge_title = gr.Textbox(label="Challenge or track title")
                challenge_type = gr.Dropdown(label="Challenge type", choices=[
                    ("Assigned problem", "assigned"), ("Theme-constrained", "theme_constrained"),
                    ("Open challenge", "open")], value="assigned")
            official_prompt = gr.Textbox(label="Official challenge prompt", lines=5,
                placeholder="Paste the prompt as published by the organizer.")
            with gr.Row():
                track = gr.Textbox(label="Track / prize category")
                official_url = gr.Textbox(label="Official challenge URL")
                rules_url = gr.Textbox(label="Rules URL")
            with gr.Row():
                deadline = gr.Textbox(label="Submission deadline", placeholder="Date and time as stated by organizer")
                timezone = gr.Textbox(label="Deadline timezone")
                demo_format = gr.Textbox(label="Demo format and time limit")
            with gr.Row():
                team_size = gr.Number(label="Team size", precision=0, minimum=1)
                time_available_hours = gr.Number(label="Available build hours", minimum=0)
            team_skills = gr.Textbox(label="Team skills and experience (one per line)", lines=2)
            judging_criteria = gr.Textbox(label="Judging criteria (one per line)", lines=5,
                placeholder="Problem relevance |  | Addresses the selected challenge\nTechnical execution |  | Working and feasible prototype")
            with gr.Row():
                required_technologies = gr.Textbox(label="Required technologies (one per line)", lines=3)
                allowed_tools_and_resources = gr.Textbox(label="Allowed tools, APIs, data, and resources (one per line)", lines=3)
            with gr.Row():
                constraints_and_eligibility = gr.Textbox(label="Rules, eligibility, and constraints (one per line)", lines=4)
                submission_requirements = gr.Textbox(label="Required submission items (one per line)", lines=4)
            unresolved_questions = gr.Textbox(label="Unclear rules or questions to resolve (one per line)", lines=3)
            load_brief_button = gr.Button("Load latest saved brief into form")
            save_brief_button = gr.Button("Save Challenge Brief draft", variant="primary")
            brief_save_status = gr.Markdown()
            with gr.Row():
                brief_artifact_id = gr.Textbox(label="Latest brief artifact ID", interactive=False)
                brief_decision_id = gr.Textbox(label="Linked review decision ID", interactive=False)
            gr.Markdown("Review the latest saved version. Approval confirms the challenge setup is ready for P1; it does not validate the problem.")
            with gr.Row():
                review_status = gr.Dropdown(label="Review decision", choices=[
                    ("Approve", "approved"), ("Request changes", "changes_requested"),
                    ("Reject", "rejected")], value="approved")
                review_comment = gr.Textbox(label="Review note", placeholder="Required context for requested changes or rejection")
            review_brief_button = gr.Button("Record review")
            review_status_output = gr.Markdown()

            begin_p1_button = gr.Button("Begin P1: Problem Framing", variant="primary")
            begin_p1_status = gr.Markdown()

        project_summary = gr.Markdown("Select or create a project to begin.")

        create_project_button.click(create_project_ui,
            inputs=[project_name, project_domain, project_description],
            outputs=[project_selector, project_create_status])
        refresh_button.click(refresh_projects_ui, inputs=[project_selector], outputs=[project_selector])
        orchestrate_button.click(orchestrate_workflow_ui, inputs=[project_selector], outputs=[orchestrate_status])
        project_selector.change(inspect_project_ui, inputs=[project_selector],
            outputs=[project_summary, brief_artifact_id, brief_decision_id, brief_save_status])
        save_brief_button.click(save_brief_ui,
            inputs=[project_selector, event_name, challenge_title, challenge_type, official_prompt,
                    track, official_url, rules_url, deadline, timezone, team_size,
                    time_available_hours, team_skills, required_technologies,
                    allowed_tools_and_resources, constraints_and_eligibility,
                    submission_requirements, demo_format, judging_criteria, unresolved_questions],
            outputs=[brief_save_status, project_summary, brief_artifact_id, brief_decision_id])
        load_brief_button.click(load_brief_ui, inputs=[project_selector],
            outputs=[event_name, challenge_title, challenge_type, official_prompt, track,
                     official_url, rules_url, deadline, timezone, demo_format, team_size,
                     time_available_hours, team_skills, required_technologies,
                     allowed_tools_and_resources, constraints_and_eligibility,
                     submission_requirements, judging_criteria, unresolved_questions])
        review_brief_button.click(review_brief_ui,
            inputs=[project_selector, brief_artifact_id, brief_decision_id, review_status, review_comment],
            outputs=[review_status_output, project_summary])
        begin_p1_button.click(begin_p1_ui, inputs=[project_selector],
            outputs=[begin_p1_status, project_summary])
    return project_selector
