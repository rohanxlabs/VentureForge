from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Literal
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from src.llm.client import get_structured_llm
from src.research.models import EvidenceRecord, ResearchPlan, ResearchQuestion, SourceSnapshot, VerificationStatus
from src.research.storage import ResearchStore


class SolutionScores(BaseModel):
    problem_fit: int = Field(ge=1, le=5)
    impact: int = Field(ge=1, le=5)
    feasibility: int = Field(ge=1, le=5)
    demoability: int = Field(ge=1, le=5)
    differentiation: int = Field(ge=1, le=5)


class SolutionOption(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=3)
    concept: str = Field(min_length=20)
    target_user: str = Field(min_length=3)
    differentiator: str = Field(min_length=10)
    implementation_approach: str = Field(min_length=10)
    evidence_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    scores: SolutionScores


class BuildTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = Field(min_length=3)
    description: str = Field(min_length=10)
    priority: Literal["must", "should", "could"]
    estimated_person_hours: float = Field(gt=0)
    depends_on: list[str] = Field(default_factory=list, description="Dependency task titles, copied exactly from other task titles")
    acceptance_criteria: list[str] = Field(min_length=1)
    judging_criteria: list[str] = Field(default_factory=list)


class SolutionDesignDraft(BaseModel):
    alternatives: list[SolutionOption] = Field(min_length=3, max_length=3)
    technical_architecture: str = Field(min_length=30)
    mvp_tasks: list[BuildTask] = Field(min_length=3, max_length=15)
    deferred_scope: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    demo_plan: str = Field(min_length=20)


class SolutionDesign(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    status: Literal["draft"] = "draft"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    alternatives: list[SolutionOption] = Field(min_length=3, max_length=3)
    recommended_option_id: str
    recommendation_reason: str
    score_weights: dict[str, float]
    technical_architecture: str
    mvp_tasks: list[BuildTask]
    deferred_scope: list[str]
    key_risks: list[str]
    demo_plan: str
    capacity_warning: str = ""


WEIGHTS = {"problem_fit": 0.30, "feasibility": 0.25, "demoability": 0.20,
           "impact": 0.15, "differentiation": 0.10}


def _research_context(plan: ResearchPlan, questions: list[ResearchQuestion],
                      sources: list[SourceSnapshot], evidence: list[EvidenceRecord]) -> str:
    sources_by_id = {source.id: source for source in sources}
    verified = [item for item in evidence if item.verification_status == VerificationStatus.VERIFIED]
    lines = ["APPROVED BRIEF", plan.brief.problem_statement,
        "User constraints: " + plan.brief.user_provided.model_dump_json(),
        "Extracted constraints: " + "; ".join(plan.brief.extracted.extracted_constraints),
        "Judging criteria: " + "; ".join(plan.brief.user_provided.judging_criteria),
        "RESEARCH QUESTIONS"]
    lines.extend(f"- {question.text} (status={question.status})" for question in questions)
    lines.append("VERIFIED EVIDENCE (cite only these IDs; evidence can still be incomplete or wrong)")
    for item in verified[:24]:
        source = sources_by_id.get(item.source_id)
        lines.append(f"- evidence_id={item.id}; question={item.question_id}; source={source.title if source else 'unknown'}; "
            f"url={source.final_url or source.url if source else 'unknown'}; claim={item.claim}; passage={item.passage[:700]}")
    if not verified:
        lines.append("- No verified evidence is available. Clearly mark proposal assumptions and avoid presenting them as research findings.")
    return "\n".join(lines)


def _generate_draft(context: str, plan: ResearchPlan, *, llm=None) -> SolutionDesignDraft:
    llm = llm or get_structured_llm(SolutionDesignDraft, temperature=0, reasoning=True)
    hours = plan.brief.user_provided.time_limit_hours
    team_size = plan.brief.user_provided.team_size
    system = (
        "You are a pragmatic hackathon product and technical architect. Create exactly three genuinely distinct "
        "solution alternatives, then a buildable MVP plan. Prefer a demoable, small vertical slice over broad scope. "
        "Use only supplied verified evidence as evidence; put unsupported claims in assumptions. Never fabricate citations. "
        "Score each alternative from 1 to 5 on problem_fit, impact, feasibility, demoability, differentiation. "
        "Respect user-supplied technology, budget, demo, and event constraints. If hours or team size are unknown, state "
        "assumptions in risks. MVP tasks must be ordered by dependencies, have observable acceptance criteria, and map to "
        "judging criteria where available. In depends_on, use prerequisite task titles exactly or refer to them as task_1, task_2, etc. in list order. Defer polish and speculative "
        "features. Do not claim the proposal is validated."
    )
    constraints = f"Available event hours: {hours or 'unspecified'}; team size: {team_size or 'unspecified'}."
    value = llm.invoke([SystemMessage(content=system), HumanMessage(content=constraints + "\n\n" + context)])
    return value if isinstance(value, SolutionDesignDraft) else SolutionDesignDraft.model_validate(value)


def generate_solution_design(plan: ResearchPlan, store: ResearchStore, *, draft_provider=None) -> SolutionDesign:
    if plan.status != "approved":
        raise ValueError("Solution design requires an approved research plan.")
    _, questions, sources, evidence = store.get_run_records(plan.run_id)
    context = _research_context(plan, questions, sources, evidence)
    draft = (draft_provider or _generate_draft)(context, plan)
    valid_evidence_ids = {item.id for item in evidence if item.verification_status == VerificationStatus.VERIFIED}
    for option in draft.alternatives:
        invalid = set(option.evidence_ids) - valid_evidence_ids
        if invalid:
            raise ValueError(f"Solution alternative cites unknown or unverified evidence IDs: {sorted(invalid)}")
    if len({option.name.casefold() for option in draft.alternatives}) != 3:
        raise ValueError("The three solution alternatives must have distinct names.")
    ranks = [(sum(getattr(option.scores, key) * weight for key, weight in WEIGHTS.items()), -index, option)
             for index, option in enumerate(draft.alternatives)]
    winner_score, _, winner = max(ranks, key=lambda item: (item[0], item[1]))
    runner_up_score = sorted((item[0] for item in ranks), reverse=True)[1]
    reason = (f"Selected by weighted score ({winner_score:.2f}/5; next option {runner_up_score:.2f}/5). "
        "Weights prioritize problem fit (30%), feasibility (25%), demoability (20%), impact (15%), and differentiation (10%). "
        "Review the evidence, assumptions, risks, and scores before committing.")
    task_by_title = {task.title.casefold(): task for task in draft.mvp_tasks}
    if len(task_by_title) != len(draft.mvp_tasks):
        raise ValueError("MVP task titles must be unique so dependencies can be resolved.")
    normalized_tasks=[]
    task_aliases={}
    for index, task in enumerate(draft.mvp_tasks,1):
        for alias in (task.id.casefold(), task.title.casefold(), f"task_{index}", f"task-{index}", f"task {index}", str(index)):
            task_aliases[alias]=task
    for task in draft.mvp_tasks:
        dependency_ids=[]
        for dependency in task.depends_on:
            key=dependency.casefold().strip()
            dependency_task=task_aliases.get(key)
            if dependency_task is None:
                match=re.fullmatch(r"task[_ -](\d+)",key)
                if match:
                    index=int(match.group(1))-1
                    if 0<=index<len(draft.mvp_tasks): dependency_task=draft.mvp_tasks[index]
            if dependency_task is None:
                raise ValueError(f"Task '{task.title}' depends on unknown task reference '{dependency}'.")
            if dependency_task.id == task.id:
                raise ValueError(f"Task '{task.title}' cannot depend on itself.")
            dependency_ids.append(dependency_task.id)
        normalized_tasks.append(task.model_copy(update={"depends_on":dependency_ids}))
    task_by_id={task.id:task for task in normalized_tasks}
    visiting=set(); visited=set()
    def visit(task_id:str)->None:
        if task_id in visiting: raise ValueError("MVP task dependencies contain a cycle.")
        if task_id in visited: return
        visiting.add(task_id)
        for dependency_id in task_by_id[task_id].depends_on: visit(dependency_id)
        visiting.remove(task_id); visited.add(task_id)
    for task in normalized_tasks: visit(task.id)
    capacity_warning = ""
    hours = plan.brief.user_provided.time_limit_hours
    team_size = plan.brief.user_provided.team_size
    planned_person_hours = sum(task.estimated_person_hours for task in draft.mvp_tasks)
    if hours and team_size and planned_person_hours > hours * team_size:
        capacity_warning = (f"Planned work is {planned_person_hours:g} person-hours, above the supplied team capacity "
            f"of {hours * team_size:g} person-hours. Cut or parallelize tasks before committing.")
    return SolutionDesign(run_id=plan.run_id, alternatives=draft.alternatives,
        recommended_option_id=winner.id, recommendation_reason=reason, score_weights=WEIGHTS,
        technical_architecture=draft.technical_architecture, mvp_tasks=normalized_tasks,
        deferred_scope=draft.deferred_scope, key_risks=draft.key_risks,
        demo_plan=draft.demo_plan, capacity_warning=capacity_warning)


def render_solution_markdown(design: SolutionDesign) -> str:
    lines = ["# Hackathon solution design", "", f"**Run:** {design.run_id}",
        f"**Design ID:** {design.id} (draft)", "", "## Alternatives", ""]
    for option in design.alternatives:
        weighted = sum(getattr(option.scores, key) * weight for key, weight in design.score_weights.items())
        marker = " [RECOMMENDED]" if option.id == design.recommended_option_id else ""
        lines.extend([f"### {option.name}{marker}", "", option.concept, "",
            f"**Target user:** {option.target_user}", f"**Differentiator:** {option.differentiator}",
            f"**Implementation:** {option.implementation_approach}", f"**Weighted score:** {weighted:.2f}/5"])
        lines.extend([f"- {key}: {getattr(option.scores, key)}/5" for key in design.score_weights])
        if option.evidence_ids: lines.append("- Evidence: " + ", ".join(f"`{item}`" for item in option.evidence_ids))
        if option.assumptions: lines.append("- Assumptions: " + "; ".join(option.assumptions))
        if option.risks: lines.append("- Risks: " + "; ".join(option.risks))
        lines.append("")
    lines.extend(["## Recommendation", "", design.recommendation_reason, "",
        "## Technical architecture", "", design.technical_architecture, "", "## MVP build plan", ""])
    for index, task in enumerate(design.mvp_tasks, 1):
        deps = ", ".join(task.depends_on) or "none"
        lines.extend([f"{index}. **[{task.priority.upper()}] {task.title}** - {task.estimated_person_hours:g} person-hours",
            f"   {task.description}", f"   Dependencies: {deps}",
            "   Acceptance: " + "; ".join(task.acceptance_criteria)])
    lines.extend(["", "## Demo plan", "", design.demo_plan, "", "## Deferred scope", ""])
    lines.extend([f"- {item}" for item in design.deferred_scope] or ["- None listed."])
    lines.extend(["", "## Risks", ""])
    lines.extend([f"- {item}" for item in design.key_risks] or ["- None listed."])
    if design.capacity_warning: lines.extend(["", "**Capacity warning:** " + design.capacity_warning])
    lines.extend(["", "## Review note", "", "This is a model-generated draft. Scores are comparative decision aids, not measured outcomes. "
        "Evidence references are restricted to verified evidence IDs from this research run; validate assumptions, feasibility, "
        "and the final plan with the team before building.", ""])
    return "\n".join(lines)
