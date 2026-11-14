"""Human-led P3 problem validation, grounded in approved P1/P2 artifacts."""
from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from src.project_store import ProjectStore
from src.research.models import ArtifactCreate, DecisionCreate, ProjectStageKey


class ValidationOutcome(StrEnum):
    PROCEED = "proceed"
    GATHER_MORE_EVIDENCE = "gather_more_evidence"
    REFRAME = "reframe"
    STOP = "stop"


class PrimaryResearchMethod(StrEnum):
    INTERVIEW = "interview"
    OBSERVATION = "observation"
    SURVEY = "survey"


class PrimaryResearchRecord(BaseModel):
    method: PrimaryResearchMethod
    conducted_at: str = Field(min_length=3, max_length=100)
    participant_context: str = Field(min_length=5, max_length=1500)
    recruitment_context: str = Field(min_length=3, max_length=1000)
    questions_or_task: list[str] = Field(min_length=1, max_length=30)
    findings: list[str] = Field(min_length=1, max_length=50)
    contradictory_findings: list[str] = Field(default_factory=list, max_length=50)
    limitations: str = Field(min_length=5, max_length=2000)
    consent_and_privacy: str = Field(min_length=5, max_length=1000)
    researcher: str = Field(min_length=2, max_length=200)
    problem_frame_artifact_id: str
    problem_frame_version: int = Field(ge=1)

    @field_validator("conducted_at", "participant_context", "recruitment_context", "limitations",
                     "consent_and_privacy", "researcher")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value

    @field_validator("questions_or_task", "findings", "contradictory_findings")
    @classmethod
    def clean_research_items(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("Research entries cannot be blank.")
        return cleaned


class ValidationDecision(BaseModel):
    outcome: ValidationOutcome
    rationale: str = Field(min_length=20, max_length=8000)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    primary_research_artifact_ids: list[str] = Field(default_factory=list)
    first_hand_evidence: str = ""
    evidence_limitations: str = ""
    unresolved_questions: list[str] = Field(default_factory=list)
    confidence: str = Field(min_length=3, max_length=1000)
    next_action: str = Field(min_length=3, max_length=2000)
    evidence_method: str = ""
    evidence_owner: str = ""
    evidence_timebox: str = ""
    problem_frame_artifact_id: str
    problem_frame_version: int = Field(ge=1)
    research_report_artifact_id: str
    research_report_version: int = Field(ge=1)

    @field_validator("rationale", "confidence", "next_action")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value

    @field_validator("supporting_evidence_ids", "contradicting_evidence_ids", "primary_research_artifact_ids", "unresolved_questions")
    @classmethod
    def clean_values(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("List entries must contain non-blank text.")
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def gather_more_has_a_research_plan(self):
        if self.outcome == ValidationOutcome.GATHER_MORE_EVIDENCE and not all(
            value.strip() for value in (self.evidence_method, self.evidence_owner, self.evidence_timebox)
        ):
            raise ValueError("Gather more evidence requires a method, owner, and timebox.")
        return self


def _latest(store: ProjectStore, project_id: str, stage: ProjectStageKey, kind: str):
    items = [item for item in store.list_project_artifacts(project_id)
             if item.stage_key == stage and item.artifact_type == kind]
    return max(items, key=lambda item: (item.version, item.created_at, item.artifact_id)) if items else None


def validation_context(store: ProjectStore, project_id: str) -> dict:
    """Return exact upstream artifacts and readable evidence for the P3 review UI."""
    store.get_project(project_id)
    frame = _latest(store, project_id, ProjectStageKey.P1, "problem_frame")
    plan = _latest(store, project_id, ProjectStageKey.P2, "research_plan")
    report = _latest(store, project_id, ProjectStageKey.P2, "research_report")
    if frame is None or store.get_artifact_approval_status(project_id, frame.artifact_id) != "approved":
        raise ValueError("P3 requires the latest Problem Frame to be approved.")
    if report is None or store.get_artifact_approval_status(project_id, report.artifact_id) != "approved":
        raise ValueError("P3 requires an approved P2 research report.")
    if plan is None or store.get_artifact_approval_status(project_id, plan.artifact_id) != "approved":
        raise ValueError("P3 requires the latest P2 research plan to be approved.")
    if store.get_project_stage(project_id, ProjectStageKey.P1).status.value != "completed":
        raise ValueError("P1 must be complete before P3 review.")
    if store.get_project_stage(project_id, ProjectStageKey.P2).status.value != "completed":
        raise ValueError("P2 must be complete before P3 review.")
    frame_data = json.loads(frame.content or "{}")
    plan_data = json.loads(plan.content or "{}")
    report_data = json.loads(report.content or "{}")
    if (plan_data.get("problem_frame_artifact_id") != frame.artifact_id
            or plan_data.get("problem_frame_version") != frame.version):
        raise ValueError("The latest P2 research plan references an older P1 version. Revise and approve P2 before validation.")
    if (report_data.get("plan_artifact_id") != plan.artifact_id
            or report_data.get("plan_id") != plan_data.get("id")):
        raise ValueError("The latest P2 research report does not match the latest approved research plan. Rerun P2 before validation.")
    evidence_lines = []
    for item in report_data.get("evidence", []):
        evidence_lines.append(
            f"- `{item.get('id', '')}` — {item.get('relation', 'context')}; "
            f"{item.get('verification_status', 'unknown')}: {item.get('claim') or item.get('passage', '')}"
        )
        if item.get("passage"):
            evidence_lines.append(f"  Passage: {item['passage']}")
        source = next((s for s in report_data.get("sources", []) if s.get("id") == item.get("source_id")), None)
        if source:
            evidence_lines.append(f"  Source: {source.get('title') or source.get('url')} — {source.get('url', '')}")
    questions = report_data.get("questions", [])
    gaps = report_data.get("unanswered_questions", [])
    report_view = [f"### P2 Research Report v{report.version}",
        f"**Execution:** {report_data.get('outcome', {}).get('execution_status', 'unknown')} | "
        f"**Sources:** {len(report_data.get('sources', []))} | **Evidence records:** {len(report_data.get('evidence', []))}",
        "", "#### Captured evidence", *(evidence_lines or ["No evidence records were captured."]),
        "", "#### Research questions", *[f"- {q.get('text', '')}" for q in questions],
        "", "#### Unanswered questions",
        *([f"- {item}" for item in gaps] if gaps else ["- None recorded by the P2 coverage check."]),
        "", "P2's coverage and exact-passage checks are descriptive. They do not establish that the problem is true or important."]
    frame_view = (f"### Approved Problem Frame v{frame.version}\n\n"
        f"**Problem:** {frame_data.get('problem_statement', '')}\n\n"
        f"**Affected users:** {frame_data.get('target_user_summary', '')}\n\n"
        f"**Current workflow:** {frame_data.get('workflow_summary', '')}\n\n"
        f"**Unknowns:** {', '.join(frame_data.get('unknowns', [])) or 'None listed'}")
    ids = {item.get("id") for item in report_data.get("evidence", []) if item.get("id")}
    primary_records = [item for item in store.list_project_artifacts(project_id)
        if item.stage_key == ProjectStageKey.P3 and item.artifact_type == "primary_research_record"]
    primary_view = "\n\n".join(
        f"### Primary research record `{item.artifact_id}` v{item.version}\n\n"
        f"{json.loads(item.content or '{}').get('method', 'unknown').title()} | "
        f"{json.loads(item.content or '{}').get('conducted_at', '')}\n\n"
        f"Participant context: {json.loads(item.content or '{}').get('participant_context', '')}\n\n"
        f"Findings: {'; '.join(json.loads(item.content or '{}').get('findings', []))}\n\n"
        f"Limitations: {json.loads(item.content or '{}').get('limitations', '')}"
        for item in primary_records)
    return {"frame": frame, "plan": plan, "report": report, "frame_view": frame_view,
            "report_view": "\n".join(report_view), "evidence_ids": ids,
            "unanswered_questions": gaps, "primary_records": primary_records,
            "primary_view": primary_view or "No structured first-hand research records have been saved.",
            "primary_record_ids": {item.artifact_id for item in primary_records}}


def save_primary_research_record(store: ProjectStore, project_id: str, record: PrimaryResearchRecord):
    """Save one structured interview, observation, or survey as a project artifact."""
    context = validation_context(store, project_id)
    frame = context["frame"]
    if (record.problem_frame_artifact_id != frame.artifact_id
            or record.problem_frame_version != frame.version):
        raise ValueError("P1 changed since this research record was prepared. Reload the current frame.")
    stage = store.get_project_stage(project_id, ProjectStageKey.P3)
    if stage.status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P3)
    elif stage.status.value != "in_progress":
        raise ValueError(f"Primary research cannot be recorded while P3 is {stage.status.value}.")
    create = ArtifactCreate(project_id=project_id, stage_key=ProjectStageKey.P3,
        artifact_type="primary_research_record", title=f"{record.method.value.title()} research record",
        description="Structured first-hand validation record; sensitive personal identifiers should not be stored.",
        content=record.model_dump_json(indent=2))
    return store.create_artifact(create)


def save_validation_decision(store: ProjectStore, project_id: str,
                             decision: ValidationDecision):
    """Save a human decision as a versioned artifact and route the chosen feedback loop."""
    context = validation_context(store, project_id)
    frame, report = context["frame"], context["report"]
    if (decision.problem_frame_artifact_id != frame.artifact_id or decision.problem_frame_version != frame.version
            or decision.research_report_artifact_id != report.artifact_id
            or decision.research_report_version != report.version):
        raise ValueError("P1/P2 changed since this review was loaded. Reload the latest evidence before deciding.")
    cited = set(decision.supporting_evidence_ids + decision.contradicting_evidence_ids)
    if not cited.issubset(context["evidence_ids"]):
        raise ValueError("Evidence references must match evidence IDs in the approved P2 report.")
    if not set(decision.primary_research_artifact_ids).issubset(context["primary_record_ids"]):
        raise ValueError("Primary research references must match saved records for this project.")
    for artifact_id in decision.primary_research_artifact_ids:
        primary = store.get_artifact(project_id, artifact_id)
        primary_data = json.loads(primary.content or "{}")
        if (primary_data.get("problem_frame_artifact_id") != frame.artifact_id
                or primary_data.get("problem_frame_version") != frame.version):
            raise ValueError("A selected primary research record references an older P1 version.")

    stage = store.get_project_stage(project_id, ProjectStageKey.P3)
    if stage.status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P3)
    elif stage.status.value == "completed":
        store.reopen_project_stage(project_id, ProjectStageKey.P3,
            note="A new human validation decision is being recorded.")
    elif stage.status.value != "in_progress":
        raise ValueError(f"P3 cannot accept a decision while it is {stage.status.value}.")

    linked = store.create_decision(DecisionCreate(project_id=project_id, stage_key=ProjectStageKey.P3,
        title=f"Problem validation: {decision.outcome.value}",
        description=decision.rationale, decision_type="problem_validation",
        status=decision.outcome.value))
    items = [a for a in store.list_project_artifacts(project_id)
             if a.stage_key == ProjectStageKey.P3 and a.artifact_type == "validation_decision"]
    artifact_key = items[-1].artifact_key if items else None
    create = ArtifactCreate(project_id=project_id, stage_key=ProjectStageKey.P3,
        decision_id=linked.decision_id, artifact_type="validation_decision",
        title=f"Problem validation decision: {decision.outcome.value}",
        description="Human-authored decision grounded in the referenced P1 and P2 versions.",
        content=decision.model_dump_json(indent=2))
    artifact = store.create_artifact(create, artifact_key=artifact_key)

    if decision.outcome in {ValidationOutcome.PROCEED, ValidationOutcome.STOP}:
        store.complete_project_stage(project_id, ProjectStageKey.P3,
            note=f"Human validation decision recorded: {decision.outcome.value}.")
    elif decision.outcome == ValidationOutcome.GATHER_MORE_EVIDENCE:
        p2 = store.get_project_stage(project_id, ProjectStageKey.P2)
        if p2.status.value == "completed":
            store.reopen_project_stage(project_id, ProjectStageKey.P2,
                note=f"P3 requested more evidence: {decision.evidence_method}; owner {decision.evidence_owner}; by {decision.evidence_timebox}.")
    else:
        p1 = store.get_project_stage(project_id, ProjectStageKey.P1)
        if p1.status.value == "completed":
            store.reopen_project_stage(project_id, ProjectStageKey.P1,
                note=f"P3 requested reframing: {decision.next_action}")
    return artifact, linked
