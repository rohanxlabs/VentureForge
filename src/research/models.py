from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, Field, field_validator


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class ProjectStageKey(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"
    P6 = "P6"
    P7 = "P7"
    P8 = "P8"


PROJECT_STAGE_NAMES = {
    ProjectStageKey.P1: "Problem Framing",
    ProjectStageKey.P2: "Research",
    ProjectStageKey.P3: "Problem Validation",
    ProjectStageKey.P4: "Solution Intelligence",
    ProjectStageKey.P5: "Solution Engineering",
    ProjectStageKey.P6: "MVP Execution",
    ProjectStageKey.P7: "Business & Impact",
    ProjectStageKey.P8: "Pitch & Judge Preparation",
}


class ProjectStageStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class ProjectStage(BaseModel):
    project_id: str
    stage_key: ProjectStageKey
    status: ProjectStageStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class ProjectStageHistory(BaseModel):
    history_id: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    stage_key: ProjectStageKey
    from_status: ProjectStageStatus
    to_status: ProjectStageStatus
    changed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: str = "user"
    note: str = ""


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    problem_domain: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Project name cannot be blank.")
        return value


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    problem_domain: str | None = None

    @field_validator("name")
    @classmethod
    def optional_name_must_not_be_blank(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Project name cannot be null.")
        value = value.strip()
        if not value:
            raise ValueError("Project name cannot be blank.")
        return value


class DecisionCreate(BaseModel):
    project_id: str
    stage_key: ProjectStageKey | None = None
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    decision_type: str = Field(default="general", min_length=1, max_length=100)
    status: str = Field(default="open", min_length=1, max_length=40)

    @field_validator("title", "decision_type", "status")
    @classmethod
    def required_text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank.")
        return value


class DecisionUpdate(BaseModel):
    stage_key: ProjectStageKey | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = None
    decision_type: str | None = Field(default=None, min_length=1, max_length=100)
    status: str | None = Field(default=None, min_length=1, max_length=40)

    @field_validator("title", "decision_type", "status")
    @classmethod
    def optional_text_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("Value cannot be blank.")
        return value


class ProjectDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    stage_key: ProjectStageKey | None = None
    title: str
    description: str = ""
    decision_type: str = "general"
    status: str = "open"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DecisionOptionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    advantages: list[str] = Field(default_factory=list)
    disadvantages: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    estimated_cost: str | None = None
    feasibility: str | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Option title cannot be blank.")
        return value


class DecisionOption(BaseModel):
    option_id: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    decision_id: str
    title: str
    description: str = ""
    advantages: list[str] = Field(default_factory=list)
    disadvantages: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    estimated_cost: str | None = None
    feasibility: str | None = None
    is_recommended: bool = False
    is_selected: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DecisionEvidenceRelationship(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXTUALIZES = "contextualizes"


class DecisionEvidenceLink(BaseModel):
    decision_id: str
    evidence_id: str
    relationship_type: DecisionEvidenceRelationship
    explanation: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApprovalEventStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    REVOKED = "revoked"


class ApprovalEvent(BaseModel):
    approval_id: str = Field(default_factory=lambda: str(uuid4()))
    event_sequence: int | None = Field(default=None, ge=1)
    project_id: str
    decision_id: str | None = None
    artifact_id: str | None = None
    status: ApprovalEventStatus
    actor: str = "user"
    comment: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ArtifactStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class ArtifactCreate(BaseModel):
    project_id: str
    stage_key: ProjectStageKey | None = None
    decision_id: str | None = None
    artifact_type: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    content: str | None = None
    storage_ref: str | None = None
    status: ArtifactStatus = ArtifactStatus.DRAFT

    @field_validator("artifact_type", "title")
    @classmethod
    def artifact_text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank.")
        return value


class ProjectArtifact(BaseModel):
    artifact_id: str = Field(default_factory=lambda: str(uuid4()))
    artifact_key: str
    project_id: str
    stage_key: ProjectStageKey | None = None
    decision_id: str | None = None
    artifact_type: str
    title: str
    description: str = ""
    content: str | None = None
    storage_ref: str | None = None
    version: int = Field(ge=1)
    status: ArtifactStatus = ArtifactStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class WorkflowRunCreate(BaseModel):
    project_id: str
    stage_key: ProjectStageKey
    workflow_type: str = Field(min_length=1, max_length=100)
    input_ref: str | None = None

    @field_validator("workflow_type")
    @classmethod
    def workflow_type_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Workflow type cannot be blank.")
        return value


class ProjectWorkflowRun(BaseModel):
    workflow_run_id: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    stage_key: ProjectStageKey
    workflow_type: str
    status: WorkflowStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_summary: str | None = None
    input_ref: str | None = None
    output_artifact_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkflowRunEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    workflow_run_id: str
    from_status: WorkflowStatus | None = None
    to_status: WorkflowStatus
    actor: str = "user"
    note: str = ""
    changed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProjectResearchRunSummary(BaseModel):
    run_id: str
    prompt: str
    created_at: datetime
    plan_status: str | None = None
    question_count: int = 0
    source_count: int = 0
    evidence_count: int = 0


class HackathonProject(BaseModel):
    project_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    problem_domain: str | None = None
    status: ProjectStatus = ProjectStatus.ACTIVE
    current_stage: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    archived_at: datetime | None = None

class VerificationStatus(StrEnum):
    VERIFIED="verified"
    SEARCH_RESULT_ONLY="search_result_only"
    UNSUPPORTED="unsupported"
    INVALID_CITATION="invalid_citation"

class SupportRelation(StrEnum):
    SUPPORTS="supports"
    WEAKENS="weakens"
    CONTEXT="context"

class ResearchRun(BaseModel):
    id:str=Field(default_factory=lambda:str(uuid4()))
    prompt:str=Field(min_length=1)
    project_id:str|None=None
    created_at:datetime=Field(default_factory=lambda:datetime.now(timezone.utc))

class CompetitionInput(BaseModel):
    competition_name:str|None=None
    rules_text:str|None=None
    judging_criteria:list[str]=Field(default_factory=list)
    team_size:int|None=Field(default=None,ge=1)
    time_limit_hours:float|None=Field(default=None,gt=0)
    required_technologies:list[str]=Field(default_factory=list)
    budget:str|None=None
    demo_requirements:list[str]=Field(default_factory=list)

class IntakeExtraction(BaseModel):
    problem_summary:str
    domain:str
    target_users:list[str]=Field(default_factory=list)
    geographic_context:str|None=None
    extracted_constraints:list[str]=Field(default_factory=list)
    inferred_assumptions:list[str]=Field(default_factory=list)
    missing_information:list[str]=Field(default_factory=list)

class ProblemBrief(BaseModel):
    problem_statement:str
    user_provided:CompetitionInput=Field(default_factory=CompetitionInput)
    extracted:IntakeExtraction

class QuestionPriority(StrEnum):
    HIGH="high"
    MEDIUM="medium"
    LOW="low"

class ResearchQuestion(BaseModel):
    id:str=Field(default_factory=lambda:str(uuid4()))
    run_id:str
    text:str=Field(min_length=1)
    priority:QuestionPriority=QuestionPriority.HIGH
    source_categories:list[str]=Field(default_factory=list)
    evidence_required:str=""
    decision_impact:str=""
    status:Literal["pending","in_progress","covered","blocked"]="pending"

class QuestionDraft(BaseModel):
    text:str=Field(min_length=8)
    priority:QuestionPriority
    source_categories:list[Literal["official","research_literature","community","technical_docs","competition_materials"]]=Field(min_length=1)
    evidence_required:str=Field(min_length=3)
    decision_impact:str=Field(min_length=3)

class PlanningDraft(BaseModel):
    problem_summary:str=Field(min_length=10)
    domain:str=Field(min_length=2)
    target_users:list[str]=Field(default_factory=list)
    geographic_context:str|None=None
    extracted_constraints:list[str]=Field(default_factory=list)
    inferred_assumptions:list[str]=Field(default_factory=list)
    missing_information:list[str]=Field(default_factory=list)
    questions:list[QuestionDraft]=Field(min_length=3,max_length=8)

class ResearchPlan(BaseModel):
    id:str=Field(default_factory=lambda:str(uuid4()))
    run_id:str
    brief:ProblemBrief
    questions:list[ResearchQuestion]=Field(min_length=3,max_length=8)
    status:Literal["draft","approved","rejected"]="draft"
    created_at:datetime=Field(default_factory=lambda:datetime.now(timezone.utc))
    approved_at:datetime|None=None

class SourceSnapshot(BaseModel):
    id:str=Field(default_factory=lambda:str(uuid4()))
    run_id:str
    question_id:str
    url:str
    title:str=""
    publisher:str=""
    published_at:str|None=None
    retrieved_at:datetime=Field(default_factory=lambda:datetime.now(timezone.utc))
    content:str=""
    search_snippet:str=""
    search_score:float|None=None
    source_categories:list[str]=Field(default_factory=list)
    content_origin:str="provider_raw_content"
    content_type:str=""
    final_url:str=""
    fetch_error:str=""

class CandidateExtraction(BaseModel):
    relevant:bool
    claim:str=""
    passage:str=""
    relation:SupportRelation=SupportRelation.CONTEXT

class EvidenceRecord(BaseModel):
    id:str=Field(default_factory=lambda:str(uuid4()))
    run_id:str
    question_id:str
    source_id:str
    claim:str=""
    passage:str=""
    relation:SupportRelation=SupportRelation.CONTEXT
    verification_status:VerificationStatus
    verification_reason:str
    created_at:datetime=Field(default_factory=lambda:datetime.now(timezone.utc))
