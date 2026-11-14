"""Project-scoped P1-P3 services built on the M1 project and research stores.

These services keep user statements separate from hypotheses. Generated frames and
assessments are deterministic drafts; no model call is implied by these APIs.
"""
from __future__ import annotations

import json
from typing import Callable, Literal
from uuid import uuid4
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator

from src.project_store import ProjectStore
from src.research.models import (
    ApprovalEventStatus, ArtifactCreate, DecisionCreate, ProjectStageKey,
    ResearchPlan, ResearchQuestion, ResearchRun, QuestionPriority,
)


class ProblemInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=10)
    domain: str = ""
    environment: str | None = None
    geography: str | None = None
    hackathon_theme: str | None = None
    additional_context: str | None = None
    primary_users: list[str] = Field(default_factory=list)
    secondary_users: list[str] = Field(default_factory=list)
    stakeholders: list[str] = Field(default_factory=list)
    buyer_or_decision_maker: str | None = None
    current_process: list[str] = Field(default_factory=list)
    where_it_occurs: str | None = None
    when_it_occurs: str | None = None
    frequency: str | None = None
    existing_tools: list[str] = Field(default_factory=list)
    workarounds: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    root_cause_hypotheses: list[str] = Field(default_factory=list)
    existing_alternatives: list[str] = Field(default_factory=list)
    alternative_limitations: list[str] = Field(default_factory=list)
    measured_impacts: list[str] = Field(default_factory=list)
    qualitative_consequences: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)

    @field_validator("title", "description")
    @classmethod
    def required_text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank.")
        return value

    @field_validator("domain", mode="before")
    @classmethod
    def normalize_domain(cls, value):
        return (value or "").strip()

    @field_validator("primary_users", "secondary_users", "stakeholders", "current_process",
                     "existing_tools", "workarounds", "pain_points", "root_cause_hypotheses",
                     "existing_alternatives", "alternative_limitations", "measured_impacts",
                     "qualitative_consequences", "constraints", "assumptions", "unknowns")
    @classmethod
    def clean_list_values(cls, values: list[str]) -> list[str]:
        if any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("List values must contain non-blank text.")
        return [v.strip() for v in values]


class FramedClaim(BaseModel):
    statement: str = Field(min_length=1)
    origin: Literal["user_input", "model_generated", "external_evidence", "human_confirmed"]
    status: Literal["known", "hypothesis", "assumption", "unknown"]
    source_text: str | None = None
    source_url: str | None = None

    @model_validator(mode="after")
    def evidence_must_be_traceable(self):
        if self.origin == "external_evidence" and not (self.source_url and self.source_text):
            raise ValueError("External evidence requires both a source URL and supporting text.")
        if self.origin == "user_input" and not self.source_text:
            raise ValueError("User-input claims must retain their source text.")
        if self.origin == "model_generated" and self.status == "known":
            raise ValueError("Model-generated claims cannot be classified as known facts.")
        return self


class ProblemFrame(ProblemInput):
    overview: str = Field(min_length=1)
    target_user_summary: str = Field(min_length=1)
    workflow_summary: str = Field(min_length=1)
    pain_point_analysis: list[str] = Field(default_factory=list)
    root_cause_analysis: list[str] = Field(default_factory=list)
    alternatives_analysis: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)
    problem_statement: str = Field(min_length=1)
    claims: list[FramedClaim] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    provenance: Literal["user_input"] = "user_input"  # legacy compatibility
    research_questions: list[str] = Field(min_length=1)
    scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)

    @field_validator("problem_statement", "overview", "target_user_summary", "workflow_summary")
    @classmethod
    def generated_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Generated framing text cannot be blank.")
        return value.strip()


class ProblemFrameDraft(BaseModel):
    overview: str
    target_user_summary: str
    workflow_summary: str
    pain_point_analysis: list[str] = Field(default_factory=list)
    root_cause_analysis: list[str] = Field(default_factory=list)
    alternatives_analysis: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)
    problem_statement: str
    unknowns: list[str] = Field(default_factory=list)
    research_questions: list[str] = Field(min_length=1)
    scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class ProblemFramingProviderError(RuntimeError):
    """Expected configuration, provider, or structured-output error for P1 framing."""


def generate_frame(data: ProblemInput) -> ProblemFrame:
    """Generate a structured P1 draft with the configured provider; never silently falls back."""
    from openai import OpenAIError
    from pydantic import ValidationError
    from langchain_core.messages import HumanMessage, SystemMessage
    from src.config import settings
    from src.llm.client import get_structured_llm

    config = settings.get_llm_config(reasoning=True)
    missing = [label for label, value in (("API key", config.get("api_key")),
                ("base URL", config.get("base_url")), ("model", config.get("model"))) if not value]
    if missing:
        raise ProblemFramingProviderError("LLM configuration is incomplete (missing " + ", ".join(missing) + ").")
    try:
        llm = get_structured_llm(ProblemFrameDraft, temperature=0, reasoning=True)
        result = llm.invoke([
            SystemMessage(content=("Frame the supplied problem without proposing solutions. Use only supplied details. "
                "Separate measured consequences from qualitative ones, retain unknowns, label causes as hypotheses, "
                "and create a concise problem statement. Do not invent numbers, users, workflow steps, alternatives, or facts.")),
            HumanMessage(content=data.model_dump_json(indent=2)),
        ])
        draft = result if isinstance(result, ProblemFrameDraft) else ProblemFrameDraft.model_validate(result)
        return create_frame(data, draft_provider=lambda _: draft)
    except OpenAIError as exc:
        raise ProblemFramingProviderError("The configured LLM provider failed while framing the problem.") from exc
    except ValidationError as exc:
        raise ProblemFramingProviderError("The LLM response did not match the required Problem Frame schema.") from exc


class ValidationAssessment(BaseModel):
    recommendation: Literal["human_review_required"] = "human_review_required"
    confidence: Literal["not_assessed"] = "not_assessed"
    summary: str
    dimensions: dict[str, str]
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    rationale: str
    evidence_basis: Literal["deterministic_record_review"] = "deterministic_record_review"


def create_frame(data: ProblemInput, *, draft_provider: Callable[[ProblemInput], ProblemFrameDraft] | None = None) -> ProblemFrame:
    """Create an offline deterministic frame or use an injected structured LLM provider."""
    users = ", ".join(data.primary_users) if data.primary_users else "the affected users are not yet specified"
    if draft_provider:
        draft = draft_provider(data)
    else:
        geography = f" in {data.geography}" if data.geography else ""
        description = data.description
        if any(term in description.casefold() for term in ("build an app", "create an app", "ai-powered", "robot")):
            raise ValueError("Describe the underlying difficulty in the problem description, not a proposed solution.")
        unknowns = list(data.unknowns)
        if not data.frequency:
            unknowns.append("Problem frequency has not been measured")
        if not data.measured_impacts:
            unknowns.append("Measured impact is unavailable")
        draft = ProblemFrameDraft(
            overview=f"{description}{geography}",
            target_user_summary=f"Affected users: {users}. Secondary users and stakeholders: " + (", ".join(data.secondary_users + data.stakeholders) or "not specified"),
            workflow_summary="; ".join(data.current_process) if data.current_process else "The current workflow is not yet described.",
            pain_point_analysis=list(data.pain_points), root_cause_analysis=list(data.root_cause_hypotheses),
            alternatives_analysis=list(data.existing_alternatives + data.alternative_limitations + data.workarounds),
            consequences=list(data.measured_impacts + data.qualitative_consequences),
            problem_statement=f"{users} face {description}{geography}, making the desired outcome difficult; the scale and consequences require investigation.",
            unknowns=unknowns,
            research_questions=[
                f"How frequently does {data.title} occur, and in which contexts?",
                f"Which affected groups experience the greatest impact from {data.title}?",
                f"What are the measurable operational, financial, safety, or social consequences of {data.title}?",
                f"Which alternatives and workarounds are currently used, and what limitations do users report?",
                f"What evidence could verify or contradict the proposed causes of {data.title}?",
            ], scope=[x for x in [data.domain, data.environment, data.geography] if x])
    user_claims = [FramedClaim(statement=text, origin="user_input", status="known", source_text=text)
                   for text in [data.description, *data.primary_users, *data.current_process, *data.pain_points,
                                *data.existing_alternatives, *data.measured_impacts] if text]
    hypotheses = [FramedClaim(statement=text, origin="user_input", status="hypothesis", source_text=text)
                  for text in data.root_cause_hypotheses]
    assumptions = [FramedClaim(statement=text, origin="user_input", status="assumption", source_text=text)
                   for text in data.assumptions]
    generated = [FramedClaim(statement=text, origin="model_generated", status="hypothesis")
                 for text in draft.root_cause_analysis if text not in data.root_cause_hypotheses]
    values = data.model_dump()
    values.update(draft.model_dump())
    values["unknowns"] = draft.unknowns
    values.update(claims=user_claims + hypotheses + assumptions + generated, facts=[data.description])
    return ProblemFrame(**values)


def _artifact(store: ProjectStore, project_id: str, stage: ProjectStageKey,
              kind: str, title: str, value: BaseModel, decision_id: str | None = None):
    return store.create_artifact(ArtifactCreate(project_id=project_id, stage_key=stage,
        decision_id=decision_id, artifact_type=kind, title=title,
        content=value.model_dump_json(indent=2)))


def save_frame(store: ProjectStore, project_id: str, frame: ProblemFrame):
    store.get_project(project_id)
    from src.hackathon_intake import require_approved_challenge_brief_if_started
    require_approved_challenge_brief_if_started(store, project_id)
    stage = store.get_project_stage(project_id, ProjectStageKey.P1)
    if stage.status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P1)
    elif stage.status.value == "completed":
        store.reopen_project_stage(project_id, ProjectStageKey.P1, note="A revised Problem Frame requires a new human review.")
    elif stage.status.value != "in_progress":
        raise ValueError(f"Problem Frame cannot be changed while P1 is {stage.status.value}.")
    previous = [a for a in store.list_project_artifacts(project_id) if a.stage_key == ProjectStageKey.P1
                and a.artifact_type == "problem_frame"]
    artifact_key = previous[-1].artifact_key if previous else None
    decision = store.create_decision(DecisionCreate(project_id=project_id,
        stage_key=ProjectStageKey.P1, title="Problem Framing Approval",
        description="Human review of a specific Problem Frame artifact version."))
    create = ArtifactCreate(project_id=project_id, stage_key=ProjectStageKey.P1,
        decision_id=decision.decision_id, artifact_type="problem_frame", title=frame.title,
        content=frame.model_dump_json(indent=2))
    artifact = (store.create_artifact_version(project_id, previous[-1].artifact_id, create)
                if previous else store.create_artifact(create, artifact_key=artifact_key))
    return artifact, decision


def start_problem_framing(store: ProjectStore, project_id: str):
    """Start P1 via the M1 stage API; report the current stage if already started."""
    from src.hackathon_intake import require_approved_challenge_brief_if_started
    require_approved_challenge_brief_if_started(store, project_id)
    stage = store.get_project_stage(project_id, ProjectStageKey.P1)
    if stage.status.value == "pending":
        stage = store.start_project_stage(project_id, ProjectStageKey.P1)
    elif stage.status.value != "in_progress":
        raise ValueError(f"P1 cannot be started from status {stage.status.value}.")
    return stage


def latest_problem_frame(store: ProjectStore, project_id: str):
    items = [a for a in store.list_project_artifacts(project_id)
             if a.stage_key == ProjectStageKey.P1 and a.artifact_type == "problem_frame"]
    return items[-1] if items else None


def approve_frame(store: ProjectStore, project_id: str, artifact_id: str, decision_id: str,
                  status: ApprovalEventStatus, comment: str = ""):
    return store.approve_problem_frame(project_id, artifact_id, decision_id,
                                       ApprovalEventStatus(status), comment=comment)


def create_research_plan(store: ProjectStore, project_id: str, *, use_llm: bool = False,
                         question_provider: Callable[[str], list[ResearchQuestion]] | None = None) -> tuple[ResearchPlan, object, object]:
    project = store.get_project(project_id)
    if project.status.value != "active":
        raise ValueError("Research planning requires an active project.")
    if store.get_project_stage(project_id, ProjectStageKey.P1).status.value != "completed":
        raise ValueError("P1 must be approved before creating a P2 plan.")
    frame_artifact = latest_problem_frame(store, project_id)
    if (frame_artifact is None or frame_artifact.stage_key != ProjectStageKey.P1
            or store.get_artifact_approval_status(project_id, frame_artifact.artifact_id) != "approved"
            or not frame_artifact.decision_id
            or store.get_decision_approval_status(project_id, frame_artifact.decision_id) != "approved"):
        raise ValueError("No approved problem frame was found.")
    frame = ProblemFrame.model_validate_json(frame_artifact.content)
    if frame_artifact.status.value not in {"draft", "ready"}:
        raise ValueError("Latest Problem Frame is not eligible for research planning.")
    run = ResearchRun(prompt=frame.problem_statement, project_id=project_id)
    generated_questions = None
    if use_llm or question_provider is not None:
        if question_provider is None:
            from src.research.planner import generate_research_plan
            question_provider = lambda context: generate_research_plan(context).questions
        planning_context = json.dumps({"instruction": "Generate research questions only. Treat every supplied detail as user-provided context, not as independently verified evidence. Do not invent statistics, sources, or conclusions. Include questions that could find contradictory evidence.",
            "approved_problem_frame": frame.model_dump(mode="json")}, ensure_ascii=False)
        generated_questions = question_provider(planning_context)
        if not 3 <= len(generated_questions) <= 8:
            raise ValueError("Research planner must produce between three and eight questions.")
    dimensions = ("frequency", "user_characteristics", "impact", "alternatives", "root_cause")
    rationales = (
        "Frequency and context determine how broadly the reported problem occurs.",
        "Identifying the most affected users defines the scope for later validation.",
        "Measured consequences indicate why the problem matters; no magnitude is assumed.",
        "Current alternatives show what people already use and where gaps may remain.",
        "The proposed causes are hypotheses and require evidence before acceptance.",
    )
    question_texts = generated_questions or frame.research_questions
    questions = [ResearchQuestion(run_id=run.id, text=(item.text if isinstance(item, ResearchQuestion) else item),
        priority=(item.priority if isinstance(item, ResearchQuestion) else
                  QuestionPriority.HIGH if i < 3 else QuestionPriority.MEDIUM),
        source_categories=(list(item.source_categories) if isinstance(item, ResearchQuestion) and item.source_categories
                           else ["official", "research_literature", "community"]),
        evidence_required=(item.evidence_required if isinstance(item, ResearchQuestion) and item.evidence_required else
            "Source-backed passages relevant to the affected users and context; include contradictory evidence where available."),
        decision_impact=(item.decision_impact if isinstance(item, ResearchQuestion) and item.decision_impact else
            "Inform later human review of the problem definition."),
        plan_id="pending", project_id=project_id,
        objective=("Establish frequency and context" if i == 0 else
                   "Identify affected groups" if i == 1 else
                   "Assess reported consequences" if i == 2 else
                   "Understand current alternatives" if i == 3 else "Examine suspected contributing causes"),
        problem_dimension=dimensions[min(i, len(dimensions)-1)],
        priority_rationale=rationales[min(i, len(rationales)-1)],
        geographic_scope=frame.geography,
        expected_answer_type="quantitative_and_qualitative" if i in {0, 2} else "qualitative",
        limitations=["Search findings do not establish factual truth; human review is required."])
        for i, item in enumerate(question_texts)]
    plan = ResearchPlan(run_id=run.id, brief={"problem_statement": frame.problem_statement,
        "extracted": {"problem_summary": frame.description, "domain": frame.domain or "unspecified",
        "target_users": frame.primary_users, "geographic_context": frame.geography,
        "inferred_assumptions": frame.assumptions, "missing_information": frame.unknowns}}, questions=questions,
        objectives=["Determine how often and where the reported problem occurs.",
            "Identify which affected groups and contexts are most relevant.",
            "Find source-backed information about consequences without inventing impact measures.",
            "Document current alternatives and test supplied root-cause hypotheses."],
        research_scope=list(frame.scope),
        expected_outputs=["Question-linked sources and captured passages", "Coverage and unresolved research gaps",
            "A traceable report for human review"], unresolved_assumptions=list(frame.assumptions),
        problem_frame_artifact_id=frame_artifact.artifact_id,
        problem_frame_version=frame_artifact.version,
        planning_method="configured_llm" if generated_questions is not None else "deterministic_from_problem_frame",
        known_limitations=["P1 inputs are not independent evidence.",
            "Exact passage verification establishes citation validity only, not semantic support or factual truth."] +
            (["Research questions are model-generated drafts and require human review."] if generated_questions is not None else []))
    questions = [q.model_copy(update={"plan_id": plan.id}) for q in questions]
    plan = plan.model_copy(update={"questions": questions})
    return store.create_p2_research_plan(plan)


def revise_research_plan(store: ProjectStore, project_id: str, edited_json: str):
    """Persist a human-edited P2 plan as a new draft version with fresh run/question IDs."""
    from src.research.project_intelligence import _latest_artifact, _validate_p2_project

    _validate_p2_project(store, project_id)
    previous_artifact = _latest_artifact(store, project_id, "research_plan")
    if previous_artifact is None:
        raise ValueError("Create a P2 research plan before revising it.")
    edited = ResearchPlan.model_validate_json(edited_json)
    frame_artifact = latest_problem_frame(store, project_id)
    frame = ProblemFrame.model_validate_json(frame_artifact.content)
    run = ResearchRun(prompt=frame.problem_statement, project_id=project_id)
    plan_id = str(uuid4())
    questions = [question.model_copy(update={"id": str(uuid4()), "run_id": run.id,
        "plan_id": plan_id, "project_id": project_id, "status": "pending"})
        for question in edited.questions]
    plan = edited.model_copy(update={"id": plan_id, "run_id": run.id, "questions": questions,
        "status": "draft", "created_at": datetime.now(timezone.utc), "approved_at": None,
        "problem_frame_artifact_id": frame_artifact.artifact_id,
        "problem_frame_version": frame_artifact.version})
    return store.create_p2_research_plan(plan)


def assess_problem(store: ProjectStore, project_id: str, run_id: str) -> ValidationAssessment:
    store.validate_run_project(run_id, project_id)
    run, questions, sources, evidence = store.get_run_records(run_id)
    valid_sources = {s.id for s in sources}
    verified = [e for e in evidence if e.source_id in valid_sources and e.run_id == run_id
                and e.verification_status.value == "verified" and bool(e.passage)]
    supporting = [e.id for e in verified if e.relation.value == "supports"]
    contradicting = [e.id for e in verified if e.relation.value == "weakens"]
    unknowns = [q.text for q in questions if not any(e.question_id == q.id for e in verified)]
    return ValidationAssessment(
        summary=f"Review of research run {run_id}: {len(verified)} exact-passage verified record(s).",
        dimensions={"problem_existence": "supported" if verified else "not established",
            "target_user": "requires human review", "frequency": "not established without direct measurements",
            "severity": "not established", "impact": "not established",
            "root_cause": "hypotheses remain unverified", "alternatives": "see research findings",
            "consistency": "review supporting and contradicting records",
            "coverage": f"{len(questions)-len(unknowns)}/{len(questions)} questions have verified evidence",
            "uncertainty": "material unknowns remain" if unknowns else "review source limits"},
        supporting_evidence_ids=supporting, contradicting_evidence_ids=contradicting,
        unknowns=unknowns, rationale="Descriptive record summary only. Exact-passage verification and question coverage do not establish semantic support, factual truth, or user need.")


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
    raise ValueError("The legacy P3 assessment cannot validate or complete the problem stage. "
        "Use the P3 Problem Validation workspace to record a human decision with rationale and evidence references.")
