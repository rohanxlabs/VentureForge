"""Project-scoped P5 engineering plans grounded in a selected P4 direction."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.hackathon_intake import latest_challenge_brief
from src.hackathon_solution import latest_solution_options, solution_context
from src.project_store import ProjectStore
from src.research.models import (
    ApprovalEventStatus, ArtifactCreate, DecisionCreate, ProjectStageKey,
)
from src.research.solution import BuildTask


class EngineeringTask(BuildTask):
    owner: str = Field(min_length=2, max_length=120)
    demo_status: Literal["real", "mocked", "simulated", "deferred"] = "real"

    @field_validator("owner")
    @classmethod
    def owner_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Assign an owner or enter an explicit unassigned placeholder.")
        return value


class IntegrationPlan(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    purpose: str = Field(min_length=10, max_length=1000)
    demo_status: Literal["real", "mocked", "simulated", "deferred"]
    dependency_or_constraint: str = ""
    fallback: str = Field(min_length=5, max_length=500)


class EngineeringPlan(BaseModel):
    schema_version: str = "ventureforge.p5.engineering_plan.v1"
    user_journey: list[str] = Field(min_length=2, max_length=20)
    architecture_data_flow: str = Field(min_length=30, max_length=8000)
    integrations: list[IntegrationPlan] = Field(default_factory=list, max_length=20)
    security_privacy_accessibility: str = Field(min_length=20, max_length=4000)
    failure_behaviors: list[str] = Field(min_length=1, max_length=20)
    mvp_boundary: str = Field(min_length=20, max_length=3000)
    build_tasks: list[EngineeringTask] = Field(min_length=3, max_length=15)
    demo_plan: str = Field(min_length=20, max_length=3000)
    demo_acceptance_criteria: list[str] = Field(min_length=3, max_length=20)
    test_scenarios: list[str] = Field(min_length=1, max_length=20)
    deferred_scope: list[str] = Field(default_factory=list, max_length=30)
    selected_option_id: str
    p4_artifact_id: str
    p4_artifact_version: int = Field(ge=1)
    p1_artifact_id: str
    p1_artifact_version: int = Field(ge=1)
    p2_artifact_id: str
    p2_artifact_version: int = Field(ge=1)
    p3_artifact_id: str
    p3_artifact_version: int = Field(ge=1)

    @field_validator("user_journey", "failure_behaviors", "demo_acceptance_criteria",
                     "test_scenarios", "deferred_scope")
    @classmethod
    def clean_text_lists(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("List entries must contain non-blank text.")
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def task_graph_is_valid(self):
        titles = [task.title.strip().casefold() for task in self.build_tasks]
        if len(titles) != len(set(titles)):
            raise ValueError("Build task titles must be unique.")
        by_title = {task.title.casefold(): task for task in self.build_tasks}
        graph: dict[str, list[str]] = {}
        for task in self.build_tasks:
            dependencies = [item.casefold().strip() for item in task.depends_on]
            unknown = sorted(set(dependencies) - set(by_title))
            if unknown:
                raise ValueError(f"Task '{task.title}' references unknown dependencies: {', '.join(unknown)}.")
            if task.title.casefold() in dependencies:
                raise ValueError(f"Task '{task.title}' cannot depend on itself.")
            graph[task.title.casefold()] = dependencies
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(title: str):
            if title in visiting:
                raise ValueError("Build task dependencies contain a cycle.")
            if title in visited:
                return
            visiting.add(title)
            for dependency in graph[title]:
                visit(dependency)
            visiting.remove(title)
            visited.add(title)

        for title in graph:
            visit(title)
        return self


def engineering_context(store: ProjectStore, project_id: str) -> dict:
    context = solution_context(store, project_id)
    if store.get_project_stage(project_id, ProjectStageKey.P4).status.value != "completed":
        raise ValueError("P4 must be complete with a selected direction before P5.")
    p4 = latest_solution_options(store, project_id)
    if p4 is None:
        raise ValueError("A selected P4 direction is required before P5.")
    p4_data = json.loads(p4.content or "{}")
    if p4_data.get("action") != "select":
        raise ValueError("P4 must record a selected solution direction before P5.")
    upstream = {
        "p1_artifact_id": context["frame"].artifact_id,
        "p1_artifact_version": context["frame"].version,
        "p2_artifact_id": context["report"].artifact_id,
        "p2_artifact_version": context["report"].version,
        "p3_artifact_id": context["validation"].artifact_id,
        "p3_artifact_version": context["validation"].version,
    }
    p4_upstream = {
        "p1_artifact_id": p4_data.get("problem_frame_artifact_id"),
        "p1_artifact_version": p4_data.get("problem_frame_version"),
        "p2_artifact_id": p4_data.get("research_report_artifact_id"),
        "p2_artifact_version": p4_data.get("research_report_version"),
        "p3_artifact_id": p4_data.get("validation_decision_artifact_id"),
        "p3_artifact_version": p4_data.get("validation_decision_version"),
    }
    if any(p4_upstream.get(key) != value for key, value in upstream.items()):
        raise ValueError("The selected P4 direction references older P1-P3 work. Revisit P4 before engineering.")
    option = next((item for item in p4_data.get("options", [])
                   if item.get("option_id") == p4_data.get("selected_option_id")), None)
    if option is None:
        raise ValueError("The selected P4 option is missing from its comparison artifact.")
    brief_artifact = latest_challenge_brief(store, project_id)
    brief = json.loads(brief_artifact.content or "{}") if brief_artifact else {}
    if brief_artifact and store.get_artifact_approval_status(project_id, brief_artifact.artifact_id) != "approved":
        raise ValueError("The latest Challenge Brief must be approved before P5 planning.")
    criteria = option.get("rubric_fit", {})
    team_size = brief.get("team_size")
    available_hours = brief.get("time_available_hours")
    capacity = team_size * available_hours if team_size and available_hours else None
    constraints = brief.get("constraints_and_eligibility", []) + brief.get("required_technologies", [])
    view = (f"### Selected P4 direction: {option.get('name', '')} | v{p4.version}\n\n"
        f"**Concept:** {option.get('concept', '')}\n\n**Target user:** {option.get('target_user', '')}\n\n"
        f"**P4 MVP boundary:** {option.get('mvp_boundary', '')}\n\n"
        f"**Riskiest assumption:** {option.get('riskiest_assumption', '')}\n\n"
        f"**Proposed experiment:** {option.get('experiment', '')} | Signal: {option.get('success_signal', '')}\n\n"
        f"**Rubric:** {', '.join(criteria) or 'No event criteria recorded'}\n\n"
        f"**Event constraints:** {', '.join(constraints) or 'None recorded'}\n\n"
        f"**Team capacity:** {team_size or 'unknown'} people × {available_hours or 'unknown'} hours "
        f"({capacity:g} person-hours available)" if capacity is not None else
        f"**Team capacity:** {team_size or 'unknown'} people × {available_hours or 'unknown'} hours")
    return {**context, "p4": p4, "p4_data": p4_data, "option": option,
        "brief": brief, "criteria": set(criteria), "capacity_person_hours": capacity,
        "constraints": constraints, "upstream": upstream, "view": view}


def _validated_plan(store: ProjectStore, project_id: str, payload: str) -> tuple[EngineeringPlan, dict]:
    context = engineering_context(store, project_id)
    plan = EngineeringPlan.model_validate_json(payload)
    expected = {**context["upstream"], "p4_artifact_id": context["p4"].artifact_id,
                "p4_artifact_version": context["p4"].version,
                "selected_option_id": context["option"]["option_id"]}
    if any(getattr(plan, key) != value for key, value in expected.items()):
        raise ValueError("P1-P4 changed since this P5 plan was loaded. Reload the selected direction before continuing.")
    for task in plan.build_tasks:
        invalid = set(task.judging_criteria) - context["criteria"]
        if invalid:
            raise ValueError(f"Task '{task.title}' references criteria outside the event rubric: {', '.join(sorted(invalid))}.")
    return plan, context


def save_engineering_plan(store: ProjectStore, project_id: str, payload: str):
    plan, _context = _validated_plan(store, project_id, payload)
    stage = store.get_project_stage(project_id, ProjectStageKey.P5)
    if stage.status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P5)
    elif stage.status.value == "completed":
        store.reopen_project_stage(project_id, ProjectStageKey.P5,
            note="A revised engineering plan requires a fresh human review.")
    elif stage.status.value != "in_progress":
        raise ValueError(f"P5 cannot be edited while it is {stage.status.value}.")
    decision = store.create_decision(DecisionCreate(project_id=project_id,
        stage_key=ProjectStageKey.P5, title="Review engineering plan",
        description="Human review of the versioned P5 implementation and demo plan.",
        decision_type="engineering_plan"))
    previous = latest_engineering_plan(store, project_id)
    create = ArtifactCreate(project_id=project_id, stage_key=ProjectStageKey.P5,
        decision_id=decision.decision_id, artifact_type="engineering_plan",
        title="P5 Solution Engineering Plan",
        description="Versioned architecture, task plan, and demo acceptance criteria.",
        content=plan.model_dump_json(indent=2))
    artifact = (store.create_artifact_version(project_id, previous.artifact_id, create)
                if previous else store.create_artifact(create))
    return plan, artifact, decision


def latest_engineering_plan(store: ProjectStore, project_id: str):
    items = [a for a in store.list_project_artifacts(project_id)
             if a.stage_key == ProjectStageKey.P5 and a.artifact_type == "engineering_plan"]
    return max(items, key=lambda a: (a.version, a.created_at, a.artifact_id)) if items else None


def review_engineering_plan(store: ProjectStore, project_id: str, artifact_id: str,
                            decision_id: str, status: ApprovalEventStatus, comment: str = ""):
    artifact = store.get_artifact(project_id, artifact_id)
    if artifact.artifact_type != "engineering_plan" or artifact.stage_key != ProjectStageKey.P5:
        raise ValueError("Artifact is not a P5 engineering plan.")
    _validated_plan(store, project_id, artifact.content or "{}")
    status = ApprovalEventStatus(status)
    if status != ApprovalEventStatus.APPROVED and not comment.strip():
        raise ValueError("Add a review note when requesting changes or rejecting the plan.")
    return store.approve_p5_plan(project_id, artifact_id, decision_id, status, comment=comment)


def capacity_summary(plan: EngineeringPlan, context: dict) -> str:
    planned = sum(task.estimated_person_hours for task in plan.build_tasks)
    available = context["capacity_person_hours"]
    if available is None:
        return f"**Planned effort:** {planned:g} person-hours. Event team capacity is unknown; review estimates with the team."
    if planned > available:
        return (f"**Capacity warning:** {planned:g} person-hours planned versus {available:g} available. "
                "Cut scope or revise estimates before treating the plan as feasible.")
    return f"**Planned effort:** {planned:g} person-hours of {available:g} available. Review estimates and parallel work with the team."
