"""P7 evidence-linked impact, adoption, feasibility, and risk planning."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.hackathon_execution import execution_context, latest_execution_record
from src.hackathon_engineering import latest_engineering_plan
from src.hackathon_solution import latest_solution_options
from src.project_store import ProjectStore
from src.research.models import (
    ApprovalEventStatus, ArtifactCreate, DecisionCreate, ProjectStageKey,
)


class ImpactMetric(BaseModel):
    name: str = Field(min_length=3, max_length=160)
    definition: str = Field(min_length=15, max_length=2000)
    unit: str = Field(min_length=2, max_length=100)
    baseline: str = Field(min_length=5, max_length=500)
    target_or_success_signal: str = Field(min_length=5, max_length=500)
    measurement_method: str = Field(min_length=15, max_length=2000)
    measurement_window: str = Field(min_length=3, max_length=300)
    owner: str = Field(min_length=2, max_length=120)


class ImpactStakeholder(BaseModel):
    role: str = Field(min_length=2, max_length=160)
    benefit_or_interest: str = Field(min_length=10, max_length=1000)
    adoption_barrier: str = Field(min_length=5, max_length=1000)


class AdoptionStep(BaseModel):
    step: str = Field(min_length=5, max_length=500)
    owner_or_actor: str = Field(min_length=2, max_length=120)
    prerequisite: str = Field(min_length=5, max_length=1000)
    success_signal: str = Field(min_length=5, max_length=1000)


class ResourceAssumption(BaseModel):
    resource: str = Field(min_length=2, max_length=160)
    need_or_estimate: str = Field(min_length=5, max_length=1000)
    basis: Literal["known", "assumption", "not_estimated"]
    evidence_reference: str = ""


class ImpactRisk(BaseModel):
    category: str = Field(min_length=2, max_length=100)
    description: str = Field(min_length=10, max_length=1200)
    likelihood: Literal["low", "medium", "high", "unknown"]
    severity: Literal["low", "medium", "high", "unknown"]
    mitigation: str = Field(min_length=10, max_length=1200)
    owner: str = Field(min_length=2, max_length=120)
    evidence_ids: list[str] = Field(default_factory=list)


class ImpactFeasibilityBrief(BaseModel):
    schema_version: str = "ventureforge.p7.impact_feasibility.v1"
    impact_hypothesis: str = Field(min_length=30, max_length=4000)
    beneficiary_group: str = Field(min_length=5, max_length=500)
    mechanism: str = Field(min_length=15, max_length=2000)
    claim_level: Literal["hypothesis", "prototype_signal"] = "hypothesis"
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    prototype_test_ids: list[str] = Field(default_factory=list)
    primary_metric: ImpactMetric
    stakeholders: list[ImpactStakeholder] = Field(min_length=1, max_length=20)
    adoption_path: list[AdoptionStep] = Field(min_length=2, max_length=20)
    resources_and_costs: list[ResourceAssumption] = Field(min_length=1, max_length=20)
    operational_considerations: list[str] = Field(min_length=1, max_length=30)
    risks: list[ImpactRisk] = Field(min_length=1, max_length=30)
    rubric_alignment: dict[str, str] = Field(default_factory=dict)
    limitations_and_nonclaims: list[str] = Field(min_length=1, max_length=30)
    selected_option_id: str
    p1_artifact_id: str
    p1_artifact_version: int = Field(ge=1)
    p2_artifact_id: str
    p2_artifact_version: int = Field(ge=1)
    p3_artifact_id: str
    p3_artifact_version: int = Field(ge=1)
    p4_artifact_id: str
    p4_artifact_version: int = Field(ge=1)
    p5_artifact_id: str
    p5_artifact_version: int = Field(ge=1)
    p6_artifact_id: str
    p6_artifact_version: int = Field(ge=1)

    @field_validator("supporting_evidence_ids", "prototype_test_ids", "operational_considerations",
                     "limitations_and_nonclaims")
    @classmethod
    def clean_text_lists(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("List entries must contain non-blank text.")
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def require_proof_for_prototype_signal(self):
        if self.claim_level == "prototype_signal" and not self.prototype_test_ids:
            raise ValueError("A prototype signal must cite at least one P6 test ID.")
        return self


def latest_impact_brief(store: ProjectStore, project_id: str):
    items = [item for item in store.list_project_artifacts(project_id)
             if item.stage_key == ProjectStageKey.P7 and item.artifact_type == "impact_feasibility_brief"]
    return max(items, key=lambda item: (item.version, item.created_at, item.artifact_id)) if items else None


def impact_context(store: ProjectStore, project_id: str) -> dict:
    execution = execution_context(store, project_id)
    if store.get_project_stage(project_id, ProjectStageKey.P6).status.value != "completed":
        raise ValueError("P6 must be complete before P7.")
    p6 = latest_execution_record(store, project_id)
    if p6 is None or store.get_artifact_approval_status(project_id, p6.artifact_id) != "approved":
        raise ValueError("The latest P6 execution and test record must be approved before P7.")
    p6_data = json.loads(p6.content or "{}")
    if p6_data.get("p5_artifact_id") != execution["p5"].artifact_id or p6_data.get("p5_artifact_version") != execution["p5"].version:
        raise ValueError("The approved P6 record refers to an older P5 plan; update P6 before P7.")
    p4 = latest_solution_options(store, project_id)
    p5 = latest_engineering_plan(store, project_id)
    verified_evidence_ids = execution["verified_evidence_ids"]
    p6_test_ids = {item.get("test_id") for item in p6_data.get("tests", [])
                   if item.get("test_id") and item.get("outcome") not in {None, "not_run"}}
    p4_data = json.loads(p4.content or "{}")
    report_data = json.loads(execution["report"].content or "{}")
    evidence_lines = [f"- `{item.get('id')}`: {item.get('claim') or item.get('passage', '')}"
        for item in report_data.get("evidence", [])
        if item.get("id") and item.get("verification_status") == "verified"]
    test_lines = [f"- `{item.get('test_id')}` — {item.get('outcome')}: {item.get('scenario')} — {item.get('observed_result', '')}"
        for item in p6_data.get("tests", []) if item.get("outcome") not in {None, "not_run"}]
    rubric = set(next((item.get("rubric_fit", {}) for item in p4_data.get("options", [])
                       if item.get("option_id") == p4_data.get("selected_option_id")), {}))
    view = (execution["view"] + f"\n\n### P6 execution evidence | v{p6.version}\n\n"
        f"**Test environment:** {p6_data.get('test_environment', '')}\n\n"
        f"**Demo notes:** {p6_data.get('demo_notes', '')}\n\n"
        "#### Test outcomes\n" + "\n".join(
            f"- `{item.get('test_id')}` {item.get('outcome')}: {item.get('scenario')} — {item.get('observed_result', '')}"
            for item in p6_data.get("tests", [])) + "\n\n"
        "#### Recorded failures and limitations\n" + "\n".join(
            [f"- {item}" for item in p6_data.get("known_failures", []) + p6_data.get("limitations", [])]
            or ["- None recorded."]) + "\n\n"
        "Use test results as prototype signals only. Do not describe them as measured community impact unless you have separate real-world measurement.")
    view += ("\n\n#### Verified P2 evidence IDs\n"
        + ("\n".join(evidence_lines) or "No verified P2 evidence is available.")
        + "\n\nUse these IDs to link supported impact claims.\n\n#### Completed P6 test IDs\n"
        + ("\n".join(test_lines) or "No completed P6 tests are available."))
    upstream = {
        "p1_artifact_id": execution["frame"].artifact_id,
        "p1_artifact_version": execution["frame"].version,
        "p2_artifact_id": execution["report"].artifact_id,
        "p2_artifact_version": execution["report"].version,
        "p3_artifact_id": execution["validation"].artifact_id,
        "p3_artifact_version": execution["validation"].version,
        "p4_artifact_id": p4.artifact_id,
        "p4_artifact_version": p4.version,
        "p5_artifact_id": p5.artifact_id,
        "p5_artifact_version": p5.version,
        "p6_artifact_id": p6.artifact_id,
        "p6_artifact_version": p6.version,
        "selected_option_id": p4_data.get("selected_option_id"),
    }
    return {**execution, "p6": p6, "p6_data": p6_data, "upstream": upstream,
        "p4": p4, "p5": p5, "rubric": rubric, "verified_evidence_ids": verified_evidence_ids,
        "p6_test_ids": p6_test_ids, "view": view}


def _validated_brief(store: ProjectStore, project_id: str, payload: str):
    context = impact_context(store, project_id)
    brief = ImpactFeasibilityBrief.model_validate_json(payload)
    expected = {**context["upstream"]}
    for key, value in expected.items():
        if getattr(brief, key) != value:
            raise ValueError("P1-P6 changed since this P7 brief was loaded. Reload the latest approved inputs.")
    if not set(brief.supporting_evidence_ids).issubset(context["verified_evidence_ids"]):
        raise ValueError("Impact evidence references must be verified IDs from the approved P2 report.")
    if not set(brief.prototype_test_ids).issubset(context["p6_test_ids"]):
        raise ValueError("Prototype signals must cite completed test IDs from the approved P6 record.")
    for risk in brief.risks:
        if not set(risk.evidence_ids).issubset(context["verified_evidence_ids"]):
            raise ValueError("Risk evidence references must be verified IDs from the approved P2 report.")
    if not set(brief.rubric_alignment).issubset(context["rubric"]):
        raise ValueError("Rubric alignment may only use criteria from the selected direction and event brief.")
    return brief, context


def save_impact_brief(store: ProjectStore, project_id: str, payload: str):
    brief, _context = _validated_brief(store, project_id, payload)
    stage = store.get_project_stage(project_id, ProjectStageKey.P7)
    if stage.status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P7)
    elif stage.status.value == "completed":
        store.reopen_project_stage(project_id, ProjectStageKey.P7,
            note="A revised impact and feasibility brief requires a fresh review.")
    elif stage.status.value != "in_progress":
        raise ValueError(f"P7 cannot be edited while it is {stage.status.value}.")
    decision = store.create_decision(DecisionCreate(project_id=project_id, stage_key=ProjectStageKey.P7,
        title="Review impact and feasibility brief",
        description="Human review of the evidence-linked impact hypothesis, adoption plan, resources, and risks.",
        decision_type="impact_feasibility"))
    previous = latest_impact_brief(store, project_id)
    create = ArtifactCreate(project_id=project_id, stage_key=ProjectStageKey.P7,
        decision_id=decision.decision_id, artifact_type="impact_feasibility_brief",
        title="P7 Impact and Feasibility Brief",
        description="Versioned impact hypothesis, metric plan, adoption path, resources, and risks.",
        content=brief.model_dump_json(indent=2))
    artifact = store.create_artifact_version(project_id, previous.artifact_id, create) if previous else store.create_artifact(create)
    return brief, artifact, decision


def review_impact_brief(store: ProjectStore, project_id: str, artifact_id: str,
                        decision_id: str, status: ApprovalEventStatus, comment: str = ""):
    artifact = store.get_artifact(project_id, artifact_id)
    if artifact.stage_key != ProjectStageKey.P7 or artifact.artifact_type != "impact_feasibility_brief":
        raise ValueError("Artifact is not a P7 impact and feasibility brief.")
    _validated_brief(store, project_id, artifact.content or "{}")
    status = ApprovalEventStatus(status)
    if status != ApprovalEventStatus.APPROVED and not comment.strip():
        raise ValueError("Add a review note when requesting changes or rejecting the brief.")
    return store.approve_p7_impact(project_id, artifact_id, decision_id, status, comment=comment)
