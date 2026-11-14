"""Project-scoped P4 solution comparison and human selection."""
from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from src.hackathon_intake import latest_challenge_brief
from src.project_store import ProjectStore
from src.research.models import ArtifactCreate, DecisionCreate, ProjectStageKey


class SolutionDirectionAction(StrEnum):
    SELECT = "select"
    RETURN_TO_P3 = "return_to_p3"
    RETURN_TO_P1 = "return_to_p1"


class SolutionOptionDraft(BaseModel):
    option_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=3, max_length=160)
    concept: str = Field(min_length=20, max_length=3000)
    target_user: str = Field(min_length=3, max_length=500)
    problem_fit: str = Field(min_length=10, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list)
    rubric_fit: dict[str, str] = Field(default_factory=dict)
    feasibility: str = Field(min_length=10, max_length=2000)
    team_skills_needed: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    mvp_boundary: str = Field(min_length=10, max_length=2000)
    riskiest_assumption: str = Field(min_length=10, max_length=1000)
    experiment: str = Field(min_length=10, max_length=2000)
    success_signal: str = Field(min_length=5, max_length=1000)

    @field_validator("option_id", "name", "concept", "target_user", "problem_fit", "feasibility",
                     "mvp_boundary", "riskiest_assumption", "experiment", "success_signal")
    @classmethod
    def required_text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value

    @field_validator("evidence_ids", "team_skills_needed", "dependencies", "risks", "assumptions")
    @classmethod
    def clean_list(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("List entries must contain non-blank text.")
        return [value.strip() for value in values]


class SolutionOptionsDraft(BaseModel):
    schema_version: str = "ventureforge.p4.solution_options.v1"
    options: list[SolutionOptionDraft] = Field(min_length=2, max_length=5)
    selected_option_id: str | None = None
    selection_rationale: str = ""
    action: SolutionDirectionAction | None = None
    problem_frame_artifact_id: str
    problem_frame_version: int = Field(ge=1)
    research_report_artifact_id: str
    research_report_version: int = Field(ge=1)
    validation_decision_artifact_id: str
    validation_decision_version: int = Field(ge=1)
    experiment_owner: str = ""
    experiment_timebox: str = ""

    @model_validator(mode="after")
    def unique_options_and_consistent_selection(self):
        ids = [option.option_id for option in self.options]
        names = [option.name.casefold() for option in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("Solution option IDs must be unique.")
        if len(set(names)) != len(names):
            raise ValueError("Solution option names must be distinct.")
        if self.action == SolutionDirectionAction.SELECT:
            if self.selected_option_id not in set(ids):
                raise ValueError("Select one of the listed solution options.")
            if len(self.selection_rationale.strip()) < 20:
                raise ValueError("Explain the selection in at least 20 characters.")
            if not self.experiment_owner.strip() or not self.experiment_timebox.strip():
                raise ValueError("A selected direction needs an experiment owner and timebox.")
        elif self.action in {SolutionDirectionAction.RETURN_TO_P3, SolutionDirectionAction.RETURN_TO_P1}:
            if len(self.selection_rationale.strip()) < 20:
                raise ValueError("Explain why the workflow is returning to an earlier stage.")
        return self


def _latest(store: ProjectStore, project_id: str, stage: ProjectStageKey, kind: str):
    artifacts = [a for a in store.list_project_artifacts(project_id)
                 if a.stage_key == stage and a.artifact_type == kind]
    return max(artifacts, key=lambda a: (a.version, a.created_at, a.artifact_id)) if artifacts else None


def latest_solution_options(store: ProjectStore, project_id: str):
    return _latest(store, project_id, ProjectStageKey.P4, "solution_options")


def solution_context(store: ProjectStore, project_id: str) -> dict:
    store.get_project(project_id)
    frame = _latest(store, project_id, ProjectStageKey.P1, "problem_frame")
    report = _latest(store, project_id, ProjectStageKey.P2, "research_report")
    validation = _latest(store, project_id, ProjectStageKey.P3, "validation_decision")
    brief_artifact = latest_challenge_brief(store, project_id)
    if not all((frame, report, validation)):
        raise ValueError("P4 requires the approved P1/P2 work and a P3 decision to proceed.")
    if store.get_project_stage(project_id, ProjectStageKey.P1).status.value != "completed":
        raise ValueError("P1 must be complete before P4.")
    if store.get_project_stage(project_id, ProjectStageKey.P2).status.value != "completed":
        raise ValueError("P2 must be complete before P4.")
    if store.get_project_stage(project_id, ProjectStageKey.P3).status.value != "completed":
        raise ValueError("P3 must be complete before P4.")
    if store.get_artifact_approval_status(project_id, frame.artifact_id) != "approved":
        raise ValueError("The latest P1 frame must be approved.")
    if store.get_artifact_approval_status(project_id, report.artifact_id) != "approved":
        raise ValueError("The latest P2 report must be approved.")
    validation_data = json.loads(validation.content or "{}")
    if validation_data.get("outcome") != "proceed":
        raise ValueError("P4 is available only after P3 records a decision to proceed.")
    upstream_matches = (
        validation_data.get("problem_frame_artifact_id") == frame.artifact_id
        and validation_data.get("problem_frame_version") == frame.version
        and validation_data.get("research_report_artifact_id") == report.artifact_id
        and validation_data.get("research_report_version") == report.version
    )
    if not upstream_matches:
        raise ValueError("The P3 decision references older P1/P2 versions. Revisit P3 before comparing solutions.")

    frame_data = json.loads(frame.content or "{}")
    report_data = json.loads(report.content or "{}")
    p3_text = validation_data.get("rationale", "")
    p3_action = validation_data.get("next_action", "")
    rubric = []
    constraints = []
    team = "Team and capacity were not supplied in Challenge Setup."
    if brief_artifact:
        brief = json.loads(brief_artifact.content or "{}")
        rubric = [item.get("name", "") for item in brief.get("judging_criteria", []) if item.get("name")]
        constraints = brief.get("constraints_and_eligibility", []) + brief.get("required_technologies", [])
        team = (f"Team size: {brief.get('team_size') or 'unknown'}; "
            f"available time: {brief.get('time_available_hours') or 'unknown'} hours; "
            f"team skills: {', '.join(brief.get('team_skills', [])) or 'not listed'}.")
    report_evidence = report_data.get("evidence", [])
    verified_ids = {item.get("id") for item in report_evidence
                    if item.get("id") and item.get("verification_status") == "verified"}
    evidence_lines = [f"- {item.get('id')}: {item.get('claim') or item.get('passage', '')}"
        for item in report_evidence if item.get("id")]
    view = (f"### Approved inputs\n\n**P1 v{frame.version}:** {frame_data.get('problem_statement', '')}\n\n"
        f"**Target user:** {frame_data.get('target_user_summary', '')}\n\n"
        f"**P2 v{report.version}:** {len(report_data.get('sources', []))} sources, "
        f"{len(report_evidence)} evidence records. Exact passage checks do not establish truth.\n\n"
        f"**P3 v{validation.version}:** Proceed. {p3_text}\n\n**P3 next action:** {p3_action}\n\n"
        f"**Event rubric:** {', '.join(rubric) or 'No criteria recorded.'}\n\n"
        f"**Constraints:** {', '.join(constraints) or 'None recorded.'}\n\n**Team capacity:** {team}\n\n"
        "#### Evidence IDs available for option links\n" + ("\n".join(evidence_lines) or "No evidence records are available."))
    return {"frame": frame, "report": report, "validation": validation,
            "rubric": rubric, "verified_evidence_ids": verified_ids, "view": view}


def _parse_draft(store: ProjectStore, project_id: str, payload: str) -> SolutionOptionsDraft:
    context = solution_context(store, project_id)
    draft = SolutionOptionsDraft.model_validate_json(payload)
    for key, artifact in (("problem_frame_artifact_id", context["frame"]),
                          ("research_report_artifact_id", context["report"]),
                          ("validation_decision_artifact_id", context["validation"])):
        version_key = key.replace("artifact_id", "version")
        if getattr(draft, key) != artifact.artifact_id or getattr(draft, version_key) != artifact.version:
            raise ValueError("P1/P2/P3 changed since P4 was loaded. Reload upstream context before continuing.")
    rubric = set(context["rubric"])
    for option in draft.options:
        if set(option.rubric_fit) != rubric:
            raise ValueError(f"Option '{option.name}' must map every event criterion exactly: {', '.join(context['rubric']) or 'no rubric criteria'}.")
        if not set(option.evidence_ids).issubset(context["verified_evidence_ids"]):
            raise ValueError(f"Option '{option.name}' contains evidence IDs that are not verified in the approved P2 report.")
    return draft


def save_solution_options(store: ProjectStore, project_id: str, payload: str):
    draft = _parse_draft(store, project_id, payload)
    stage = store.get_project_stage(project_id, ProjectStageKey.P4)
    if stage.status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P4)
    elif stage.status.value == "completed":
        store.reopen_project_stage(project_id, ProjectStageKey.P4, note="A new solution comparison is being drafted.")
    elif stage.status.value != "in_progress":
        raise ValueError(f"P4 cannot be edited while it is {stage.status.value}.")
    create = ArtifactCreate(project_id=project_id,
        stage_key=ProjectStageKey.P4, artifact_type="solution_options", title="Solution options comparison",
        description="Human-authored alternatives linked to P1-P3 and event criteria.",
        content=draft.model_dump_json(indent=2))
    previous = latest_solution_options(store, project_id)
    artifact = store.create_artifact_version(project_id, previous.artifact_id, create) if previous else store.create_artifact(create)
    return draft, artifact


def record_solution_direction(store: ProjectStore, project_id: str, payload: str):
    draft = _parse_draft(store, project_id, payload)
    if draft.action is None:
        raise ValueError("Choose a P4 action: select a direction or return to P3/P1.")
    if draft.action == SolutionDirectionAction.SELECT:
        chosen = next(item for item in draft.options if item.option_id == draft.selected_option_id)
        title = f"Selected solution direction: {chosen.name}"
        status = "selected"
        stage_note = f"Human selected {chosen.name} and defined an experiment."
    else:
        chosen = None
        title = "Return from solution exploration"
        status = draft.action.value
        stage_note = f"Human requested feedback to {draft.action.value}."

    stage = store.get_project_stage(project_id, ProjectStageKey.P4)
    if stage.status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P4)
    elif stage.status.value == "completed":
        store.reopen_project_stage(project_id, ProjectStageKey.P4, note="A new solution direction decision is being recorded.")
    elif stage.status.value != "in_progress":
        raise ValueError(f"P4 cannot accept a decision while it is {stage.status.value}.")

    decision = store.create_decision(DecisionCreate(project_id=project_id, stage_key=ProjectStageKey.P4,
        title=title, description=draft.selection_rationale, decision_type="solution_direction", status=status))
    artifact_content = draft.model_copy(update={"action": draft.action}).model_dump_json(indent=2)
    create = ArtifactCreate(project_id=project_id, stage_key=ProjectStageKey.P4,
        decision_id=decision.decision_id, artifact_type="solution_options", title=title,
        description="Human decision linked to the approved problem and research artifacts.", content=artifact_content)
    previous = latest_solution_options(store, project_id)
    artifact = store.create_artifact_version(project_id, previous.artifact_id, create) if previous else store.create_artifact(create)
    if draft.action == SolutionDirectionAction.SELECT:
        store.complete_project_stage(project_id, ProjectStageKey.P4, note=stage_note)
    elif draft.action == SolutionDirectionAction.RETURN_TO_P3:
        if store.get_project_stage(project_id, ProjectStageKey.P3).status.value == "completed":
            store.reopen_project_stage(project_id, ProjectStageKey.P3, note=stage_note.strip())
    else:
        if store.get_project_stage(project_id, ProjectStageKey.P1).status.value == "completed":
            store.reopen_project_stage(project_id, ProjectStageKey.P1, note=stage_note.strip())
        if store.get_project_stage(project_id, ProjectStageKey.P3).status.value == "completed":
            store.reopen_project_stage(project_id, ProjectStageKey.P3,
                note="P4 returned to P1; revisit validation after the revised frame and research are ready.")
    return draft, artifact, decision
