"""Project-scoped P1-P3 services built on the M1 project and research stores.

These services keep user statements separate from hypotheses. Generated frames and
assessments are deterministic drafts; no model call is implied by these APIs.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.project_store import ProjectStore
from src.research.models import (
    ApprovalEventStatus, ArtifactCreate, DecisionCreate, ProjectStageKey,
    ResearchPlan, ResearchQuestion, ResearchRun, QuestionPriority,
)


class ProblemInput(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=10)
    domain: str = ""
    geography: str | None = None
    primary_users: list[str] = Field(default_factory=list)
    stakeholders: list[str] = Field(default_factory=list)
    current_process: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    root_cause_hypotheses: list[str] = Field(default_factory=list)
    existing_alternatives: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class ProblemFrame(ProblemInput):
    problem_statement: str
    provenance: Literal["user_input"] = "user_input"
    facts: list[str] = Field(default_factory=list)
    research_questions: list[str] = Field(min_length=1)
    scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class ValidationAssessment(BaseModel):
    recommendation: Literal["validated", "partially_validated", "insufficiently_validated", "rejected"]
    confidence: Literal["high", "medium", "low"]
    summary: str
    dimensions: dict[str, str]
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    rationale: str
    evidence_basis: Literal["deterministic_record_review"] = "deterministic_record_review"


def create_frame(data: ProblemInput) -> ProblemFrame:
    users = ", ".join(data.primary_users) if data.primary_users else "the affected users are not yet specified"
    context = f" in {data.geography}" if data.geography else ""
    statement = f"{users} experience {data.description.strip()}{context}; the scale and consequences require evidence."
    questions = [
        f"What evidence documents whether {data.title} occurs for {users}?",
        f"How frequently does {data.title} occur and in which contexts{context}?",
        f"What consequences and measurable operational or financial effects are associated with {data.title}?",
        f"Which existing alternatives are used for {data.title}, and what limitations are documented?",
        f"What evidence supports or challenges the proposed causes of {data.title}?",
    ]
    return ProblemFrame(**data.model_dump(), problem_statement=statement,
        facts=[data.description], research_questions=questions,
        scope=[data.domain] if data.domain else [], out_of_scope=[])


def _artifact(store: ProjectStore, project_id: str, stage: ProjectStageKey,
              kind: str, title: str, value: BaseModel, decision_id: str | None = None):
    return store.create_artifact(ArtifactCreate(project_id=project_id, stage_key=stage,
        decision_id=decision_id, artifact_type=kind, title=title,
        content=value.model_dump_json(indent=2)))


def save_frame(store: ProjectStore, project_id: str, frame: ProblemFrame):
    store.get_project(project_id)
    stage = store.get_project_stage(project_id, ProjectStageKey.P1)
    if stage.status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P1)
    decision = store.create_decision(DecisionCreate(project_id=project_id,
        stage_key=ProjectStageKey.P1, title="Accept problem frame",
        description="Human review required before project research."))
    artifact = _artifact(store, project_id, ProjectStageKey.P1, "problem_frame", frame.title, frame, decision.decision_id)
    return artifact, decision


def approve_frame(store: ProjectStore, project_id: str, artifact_id: str, decision_id: str,
                  status: ApprovalEventStatus, comment: str = ""):
    artifact = store.get_artifact(project_id, artifact_id)
    if artifact.stage_key != ProjectStageKey.P1 or artifact.artifact_type != "problem_frame":
        raise ValueError("Artifact is not a P1 problem frame.")
    store.get_decision(project_id, decision_id)
    store.record_artifact_approval(project_id, artifact_id, status, comment=comment)
    event = store.record_decision_approval(project_id, decision_id, status, comment=comment)
    if status == ApprovalEventStatus.APPROVED:
        store.complete_project_stage(project_id, ProjectStageKey.P1, note="Problem frame approved by human.")
    return event


def create_research_plan(store: ProjectStore, project_id: str) -> tuple[ResearchPlan, object, object]:
    if store.get_project_stage(project_id, ProjectStageKey.P1).status.value != "completed":
        raise ValueError("P1 must be approved before creating a P2 plan.")
    frames = [a for a in store.list_project_artifacts(project_id) if a.artifact_type == "problem_frame"
              and store.get_artifact_approval_status(project_id, a.artifact_id) == "approved"]
    if not frames:
        raise ValueError("No approved problem frame was found.")
    frame = ProblemFrame.model_validate_json(frames[-1].content)
    run = ResearchRun(prompt=frame.problem_statement, project_id=project_id)
    questions = [ResearchQuestion(run_id=run.id, text=q,
        priority=QuestionPriority.HIGH if i < 3 else QuestionPriority.MEDIUM,
        source_categories=["official", "research_literature", "community"],
        evidence_required="Source-backed passages relevant to this question.",
        decision_impact="Inform problem validation.") for i, q in enumerate(frame.research_questions)]
    plan = ResearchPlan(run_id=run.id, brief={"problem_statement": frame.problem_statement,
        "extracted": {"problem_summary": frame.description, "domain": frame.domain or "unspecified",
        "target_users": frame.primary_users, "geographic_context": frame.geography,
        "inferred_assumptions": frame.assumptions, "missing_information": frame.unknowns}}, questions=questions)
    store.save_plan(plan, project_id=project_id)
    if store.get_project_stage(project_id, ProjectStageKey.P2).status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P2)
    decision = store.create_decision(DecisionCreate(project_id=project_id, stage_key=ProjectStageKey.P2,
        title="Approve research plan"))
    artifact = _artifact(store, project_id, ProjectStageKey.P2, "research_plan", "Research plan", plan, decision.decision_id)
    return plan, artifact, decision


def assess_problem(store: ProjectStore, project_id: str, run_id: str) -> ValidationAssessment:
    store.validate_run_project(run_id, project_id)
    run, questions, sources, evidence = store.get_run_records(run_id)
    valid_sources = {s.id for s in sources}
    verified = [e for e in evidence if e.source_id in valid_sources and e.run_id == run_id
                and e.verification_status.value == "verified" and bool(e.passage)]
    supporting = [e.id for e in verified if e.relation.value == "supports"]
    contradicting = [e.id for e in verified if e.relation.value == "weakens"]
    unknowns = [q.text for q in questions if not any(e.question_id == q.id for e in verified)]
    enough = len(verified) >= 2 and bool(questions) and not unknowns
    recommendation = "partially_validated" if verified else "insufficiently_validated"
    if enough and not contradicting:
        recommendation = "validated"
    elif contradicting and not supporting:
        recommendation = "insufficiently_validated"
    return ValidationAssessment(recommendation=recommendation,
        confidence="low" if unknowns or not verified else "medium",
        summary=f"Review of research run {run_id}: {len(verified)} exact-passage verified record(s).",
        dimensions={"problem_existence": "supported" if verified else "not established",
            "target_user": "requires human review", "frequency": "not established without direct measurements",
            "severity": "not established", "impact": "not established",
            "root_cause": "hypotheses remain unverified", "alternatives": "see research findings",
            "consistency": "review supporting and contradicting records",
            "coverage": f"{len(questions)-len(unknowns)}/{len(questions)} questions have verified evidence",
            "uncertainty": "material unknowns remain" if unknowns else "review source limits"},
        supporting_evidence_ids=supporting, contradicting_evidence_ids=contradicting,
        unknowns=unknowns, rationale="This deterministic assessment checks record validity and coverage only. Exact passage verification does not establish semantic support or factual truth.")


def save_assessment(store: ProjectStore, project_id: str, assessment: ValidationAssessment):
    if store.get_project_stage(project_id, ProjectStageKey.P2).status.value != "completed":
        raise ValueError("P2 must be reviewed and completed before P3 validation.")
    if store.get_project_stage(project_id, ProjectStageKey.P3).status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P3)
    decision = store.create_decision(DecisionCreate(project_id=project_id,
        stage_key=ProjectStageKey.P3, title="Review problem validation assessment"))
    artifact = _artifact(store, project_id, ProjectStageKey.P3, "validation_assessment",
        "Problem validation assessment", assessment, decision.decision_id)
    return artifact, decision


def approve_assessment(store: ProjectStore, project_id: str, artifact_id: str,
                       decision_id: str, status: ApprovalEventStatus, comment: str = ""):
    artifact = store.get_artifact(project_id, artifact_id)
    if artifact.stage_key != ProjectStageKey.P3 or artifact.artifact_type != "validation_assessment":
        raise ValueError("Artifact is not a P3 validation assessment.")
    store.record_artifact_approval(project_id, artifact_id, status, comment=comment)
    event = store.record_decision_approval(project_id, decision_id, status, comment=comment)
    if status == ApprovalEventStatus.APPROVED:
        store.complete_project_stage(project_id, ProjectStageKey.P3, note="Assessment accepted by human.")
    return event
