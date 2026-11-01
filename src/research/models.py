from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, Field

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
