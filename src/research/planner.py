from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from typing import Callable
from openai import OpenAIError
from pydantic import ValidationError
from langchain_core.messages import HumanMessage, SystemMessage
from src.llm.client import get_structured_llm
from src.research.models import CompetitionInput, IntakeExtraction, PlanningDraft, ProblemBrief, QuestionDraft, ResearchPlan, ResearchQuestion, ResearchRun


class ResearchProviderError(RuntimeError):
    """An expected provider/configuration failure safe to show in the CLI."""


def require_llm_configuration(config: dict) -> None:
    missing = [name for name, value in (("API key", config.get("api_key")),
        ("base URL", config.get("base_url")), ("model", config.get("model"))) if not value]
    if missing:
        raise ResearchProviderError(
            "LLM configuration is incomplete (missing " + ", ".join(missing) +
            "). Set LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL in the environment or .env file."
        )

def _generate_draft(problem_statement:str, competition:CompetitionInput)->PlanningDraft:
    from src.config import settings
    require_llm_configuration(settings.get_llm_config(reasoning=True))
    llm=get_structured_llm(PlanningDraft,temperature=0,reasoning=True)
    system=("You are a hackathon research planner. Extract only what the supplied material supports. "
            "Keep user-provided details distinct from assumptions and missing information. "
            "Do not invent users, statistics, judging criteria, constraints, or technology requirements. "
            "Generate 3 to 8 prioritized research questions. Each question must name useful source categories, "
            "the evidence needed, and the decision it informs. Prefer primary sources for rules, technical "
            "capabilities, and official data; use first-person/community sources for user pain.")
    user=("PROBLEM STATEMENT (user provided):\n"+problem_statement+
          "\n\nCOMPETITION DETAILS (user provided JSON):\n"+competition.model_dump_json(indent=2))
    try:
        value=llm.invoke([SystemMessage(content=system),HumanMessage(content=user)])
        return value if isinstance(value,PlanningDraft) else PlanningDraft.model_validate(value)
    except OpenAIError as exc:
        raise ResearchProviderError(
            "LLM provider request failed while creating the research plan. Check network access, provider status, and configured credentials."
        ) from exc
    except ValidationError as exc:
        raise ResearchProviderError(
            "LLM provider returned a planning response that did not match the required schema. Check the selected model/provider and try again."
        ) from exc

def generate_research_plan(problem_statement:str,competition:CompetitionInput|None=None,*,draft_provider:Callable|None=None)->ResearchPlan:
    problem_statement=problem_statement.strip()
    if not problem_statement:
        raise ValueError("Problem statement must not be empty.")
    competition=competition or CompetitionInput()
    draft=(draft_provider or _generate_draft)(problem_statement,competition)
    seen=set(); questions=[]
    run=ResearchRun(prompt=problem_statement)
    for item in draft.questions:
        normalized=re.sub(r"\s+"," ",item.text).strip()
        key=normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        questions.append(ResearchQuestion(run_id=run.id,text=normalized,priority=item.priority,
            source_categories=list(item.source_categories),evidence_required=item.evidence_required,
            decision_impact=item.decision_impact))
    if len(questions)<3:
        raise ValueError("Planner returned fewer than three distinct research questions.")
    brief=ProblemBrief(problem_statement=problem_statement,user_provided=competition,
        extracted=IntakeExtraction(problem_summary=draft.problem_summary,domain=draft.domain,
            target_users=draft.target_users,geographic_context=draft.geographic_context,
            extracted_constraints=draft.extracted_constraints,
            inferred_assumptions=draft.inferred_assumptions,
            missing_information=draft.missing_information))
    return ResearchPlan(run_id=run.id,brief=brief,questions=questions)

def review_plan(plan:ResearchPlan,*,input_fn=input,output_fn=print)->ResearchPlan:
    if plan.status!="draft":
        raise ValueError("Only draft plans can be reviewed.")
    while True:
        output_fn("\nResearch plan")
        provided=plan.brief.user_provided
        output_fn("Problem statement: "+plan.brief.problem_statement)
        output_fn("Competition: "+(provided.competition_name or "unspecified"))
        output_fn("Judging criteria: "+(", ".join(provided.judging_criteria) or "not provided"))
        output_fn("Team/time: "+(str(provided.team_size) if provided.team_size is not None else "unspecified")+" / "+(str(provided.time_limit_hours)+" hours" if provided.time_limit_hours is not None else "unspecified"))
        output_fn("Required technologies: "+(", ".join(provided.required_technologies) or "not provided"))
        output_fn("Budget: "+(provided.budget or "not provided"))
        output_fn("Demo requirements: "+(", ".join(provided.demo_requirements) or "not provided"))
        if provided.rules_text:
            output_fn("Competition rules: "+provided.rules_text)
        output_fn("Extracted summary: "+plan.brief.extracted.problem_summary)
        output_fn("Domain: "+plan.brief.extracted.domain)
        output_fn("Target users: "+(", ".join(plan.brief.extracted.target_users) or "unspecified"))
        output_fn("Geography: "+(plan.brief.extracted.geographic_context or "unspecified"))
        output_fn("Constraints: "+(", ".join(plan.brief.extracted.extracted_constraints) or "none extracted"))
        output_fn("Assumptions: "+(", ".join(plan.brief.extracted.inferred_assumptions) or "none"))
        output_fn("Missing information: "+(", ".join(plan.brief.extracted.missing_information) or "none identified"))
        for index,q in enumerate(plan.questions,1):
            output_fn(f"{index}. [{q.priority.value}] {q.text}")
            output_fn(f"   Sources: {', '.join(q.source_categories)} | Evidence: {q.evidence_required} | Decision: {q.decision_impact}")
        choice=input_fn("Approve [a], edit questions [e], or reject [r]? ").strip().lower()
        if choice in {"a","approve","y","yes"}:
            return plan.model_copy(update={"status":"approved","approved_at":datetime.now(timezone.utc)})
        if choice in {"r","reject","n","no"}:
            return plan.model_copy(update={"status":"rejected","approved_at":None})
        if choice in {"e","edit"}:
            while True:
                raw=input_fn("Question number to edit, or Enter to finish edits: ").strip()
                if not raw:
                    break
                try:
                    index=int(raw)-1
                    if index<0 or index>=len(plan.questions):
                        raise ValueError
                except ValueError:
                    output_fn("Enter a valid question number.")
                    continue
                edited=input_fn("Replacement question text: ").strip()
                if edited:
                    questions=list(plan.questions)
                    questions[index]=questions[index].model_copy(update={"text":edited})
                    plan=plan.model_copy(update={"questions":questions})
            continue
        output_fn("Choose approve, edit, or reject.")
