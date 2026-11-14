"""Project-scoped P8 pitch, rubric, submission, and judge preparation."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.hackathon_impact import impact_context, latest_impact_brief
from src.hackathon_intake import latest_challenge_brief
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ArtifactCreate, DecisionCreate, ProjectStageKey
from src.research.pitch import DemoStep, JudgeQuestion, PresentationSlide, RubricMapping


class RubricProof(RubricMapping):
    proof_references: list[str] = Field(default_factory=list)
    remaining_weakness: str = Field(min_length=5, max_length=1000)
    improvement_action: str = Field(min_length=5, max_length=1000)


class JudgeQuestionPrep(JudgeQuestion):
    proof_references: list[str] = Field(default_factory=list)


class SlideOutline(PresentationSlide):
    proof_references: list[str] = Field(default_factory=list)
    duration_seconds: int = Field(gt=0, le=600)


class DemoScriptStep(DemoStep):
    implementation_status: Literal["real", "mocked", "simulated"]
    proof_references: list[str] = Field(default_factory=list)


class SubmissionChecklistItem(BaseModel):
    requirement: str = Field(min_length=3, max_length=500)
    required_by_event: bool = True
    status: Literal["not_started", "in_progress", "complete", "not_applicable"] = "not_started"
    owner: str = ""
    proof_or_location: str = ""
    note: str = ""

    @model_validator(mode="after")
    def status_needs_evidence_or_explanation(self):
        if self.status == "complete" and len(self.proof_or_location.strip()) < 3:
            raise ValueError(f"Completed submission item '{self.requirement}' needs a link or proof location.")
        if self.status == "not_applicable" and len(self.note.strip()) < 5:
            raise ValueError(f"Not-applicable item '{self.requirement}' needs an explanation.")
        return self


class PitchPreparationPackage(BaseModel):
    schema_version: str = "ventureforge.p8.pitch_preparation.v1"
    pitch_summary: str = Field(min_length=40, max_length=6000)
    rubric_mapping: list[RubricProof] = Field(default_factory=list, max_length=15)
    judge_questions: list[JudgeQuestionPrep] = Field(min_length=3, max_length=12)
    presentation_outline: list[SlideOutline] = Field(min_length=3, max_length=12)
    demo_script: list[DemoScriptStep] = Field(min_length=3, max_length=15)
    submission_checklist: list[SubmissionChecklistItem] = Field(min_length=1, max_length=40)
    open_gaps: list[str] = Field(default_factory=list, max_length=40)
    claims_and_nonclaims: list[str] = Field(min_length=1, max_length=30)
    pitch_duration_minutes: str = Field(min_length=3, max_length=200)
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
    p7_artifact_id: str
    p7_artifact_version: int = Field(ge=1)
    selected_option_id: str

    @field_validator("open_gaps", "claims_and_nonclaims")
    @classmethod
    def clean_lists(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("List entries must contain non-blank text.")
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def rubric_has_one_mapping_per_criterion_and_demo_is_ordered(self):
        criteria = [item.criterion.casefold().strip() for item in self.rubric_mapping]
        if len(criteria) != len(set(criteria)):
            raise ValueError("Each judging criterion must be mapped exactly once.")
        previous_end = -1
        for step in self.demo_script:
            if step.elapsed_seconds < previous_end:
                raise ValueError("Demo script steps must be ordered by elapsed time without overlap.")
            previous_end = step.elapsed_seconds + step.duration_seconds
        return self


def pitch_prep_context(store: ProjectStore, project_id: str) -> dict:
    impact = impact_context(store, project_id)
    if store.get_project_stage(project_id, ProjectStageKey.P7).status.value != "completed":
        raise ValueError("P7 must be approved before P8.")
    p7 = latest_impact_brief(store, project_id)
    if p7 is None or store.get_artifact_approval_status(project_id, p7.artifact_id) != "approved":
        raise ValueError("The latest P7 impact and feasibility brief must be approved before P8.")
    p7_data = json.loads(p7.content or "{}")
    upstream = dict(impact["upstream"])
    if any(p7_data.get(key) != value for key, value in upstream.items()):
        raise ValueError("P7 references older P1-P6 work. Revisit P7 before preparing the final pitch.")
    brief_artifact = latest_challenge_brief(store, project_id)
    event = json.loads(brief_artifact.content or "{}") if brief_artifact else {}
    report_data = json.loads(impact["report"].content or "{}")
    p1 = json.loads(impact["frame"].content or "{}")
    p4 = impact["p4_data"]
    option = next(item for item in p4["options"] if item["option_id"] == p4["selected_option_id"])
    artifacts = {
        "P1": impact["frame"], "P2": impact["report"], "P3": impact["validation"],
        "P4": impact["p4"], "P5": impact["p5"], "P6": impact["p6"], "P7": p7,
    }
    artifact_ids = {item.artifact_id for item in artifacts.values()}
    evidence_ids = set(impact["verified_evidence_ids"])
    test_ids = set(impact["p6_test_ids"])
    valid_references = artifact_ids | evidence_ids | test_ids
    source_by_id = {item.get("id"): item for item in report_data.get("sources", [])}
    evidence_lines = []
    for item in report_data.get("evidence", []):
        if item.get("id") not in evidence_ids:
            continue
        source = source_by_id.get(item.get("source_id"), {})
        evidence_lines.append(f"- `{item.get('id')}` {item.get('claim') or item.get('passage', '')} "
            f"— {source.get('title') or source.get('url', '')} {source.get('url', '')}")
    artifact_lines = [f"- **{stage}** `{artifact.artifact_id}` v{artifact.version}: {artifact.title}"
        for stage, artifact in artifacts.items()]
    criteria = list(dict.fromkeys(item.strip() for item in impact["rubric"] if item.strip()))
    submission = [SubmissionChecklistItem(requirement=item, required_by_event=True,
        owner="Assign an owner", note="Add the final link or proof after submission is prepared.")
        for item in event.get("submission_requirements", []) if item.strip()]
    demo_format = event.get("demo_format", "").strip()
    if demo_format:
        submission.append(SubmissionChecklistItem(requirement=f"Prepare demo in the required format: {demo_format}",
            required_by_event=True, owner="Assign a demo owner",
            note="Record the final demo link or location when ready."))
    if not submission:
        submission.append(SubmissionChecklistItem(requirement="Confirm submission requirements with the event organizer.",
            required_by_event=False, owner="Project lead", note="Challenge Setup contains no required submission items."))
    return {**impact, "p7": p7, "p7_data": p7_data, "event": event,
        "option": option, "p1_data": p1, "criteria": criteria,
        "artifacts": artifacts, "artifact_ids": artifact_ids, "evidence_ids": evidence_ids,
        "test_ids": test_ids, "valid_references": valid_references,
        "evidence_lines": evidence_lines, "artifact_lines": artifact_lines,
        "submission_items": submission}


def _validate_package(store: ProjectStore, project_id: str, payload: str):
    context = pitch_prep_context(store, project_id)
    package = PitchPreparationPackage.model_validate_json(payload)
    expected = {**context["upstream"], "p7_artifact_id": context["p7"].artifact_id,
        "p7_artifact_version": context["p7"].version,
        "selected_option_id": context["option"]["option_id"]}
    for key, value in expected.items():
        if getattr(package, key) != value:
            raise ValueError("P1-P7 changed since P8 was loaded. Reload the current approved project work.")
    mapped = [item.criterion.casefold().strip() for item in package.rubric_mapping]
    criteria = {item.casefold().strip() for item in context["criteria"]}
    if set(mapped) != criteria or len(mapped) != len(criteria):
        if criteria:
            raise ValueError("Map every actual event judging criterion exactly once; do not add generic criteria.")
        if mapped:
            raise ValueError("No event rubric was recorded. Do not present generic criteria as event requirements.")
    refs = []
    for group in package.rubric_mapping:
        refs.extend(group.proof_references)
        refs.extend(group.evidence_ids)
    for question in package.judge_questions:
        refs.extend(question.proof_references)
        refs.extend(question.evidence_ids)
    for slide in package.presentation_outline:
        refs.extend(slide.proof_references)
        refs.extend(slide.evidence_ids)
    for step in package.demo_script:
        refs.extend(step.proof_references)
    unknown = set(refs) - context["valid_references"]
    if unknown:
        raise ValueError(f"P8 references must use artifact IDs, verified P2 evidence IDs, or completed P6 test IDs: {sorted(unknown)}")
    official_requirements = {item.get("requirement") for item in context["submission_items"]
                             if item.required_by_event}
    supplied_requirements = {item.requirement for item in package.submission_checklist if item.required_by_event}
    if supplied_requirements != official_requirements:
        raise ValueError("Keep every event-required submission item from Challenge Setup in the P8 checklist.")
    return package, context


def latest_pitch_prep(store: ProjectStore, project_id: str):
    items = [item for item in store.list_project_artifacts(project_id)
             if item.stage_key == ProjectStageKey.P8 and item.artifact_type == "pitch_preparation_package"]
    return max(items, key=lambda item: (item.version, item.created_at, item.artifact_id)) if items else None


def save_pitch_prep(store: ProjectStore, project_id: str, payload: str):
    package, _context = _validate_package(store, project_id, payload)
    stage = store.get_project_stage(project_id, ProjectStageKey.P8)
    if stage.status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P8)
    elif stage.status.value == "completed":
        store.reopen_project_stage(project_id, ProjectStageKey.P8,
            note="A revised pitch preparation package needs a fresh review.")
    elif stage.status.value != "in_progress":
        raise ValueError(f"P8 cannot be edited while it is {stage.status.value}.")
    decision = store.create_decision(DecisionCreate(project_id=project_id, stage_key=ProjectStageKey.P8,
        title="Review final pitch and submission package",
        description="Human review of the rubric mapping, pitch outline, demo, judge answers, and deliverables.",
        decision_type="pitch_preparation"))
    previous = latest_pitch_prep(store, project_id)
    create = ArtifactCreate(project_id=project_id, stage_key=ProjectStageKey.P8,
        decision_id=decision.decision_id, artifact_type="pitch_preparation_package",
        title="P8 Pitch and Judge Preparation Package",
        description="Rubric-mapped pitch, demo, submission checklist, and judge preparation.",
        content=package.model_dump_json(indent=2))
    artifact = store.create_artifact_version(project_id, previous.artifact_id, create) if previous else store.create_artifact(create)
    return package, artifact, decision


def review_pitch_prep(store: ProjectStore, project_id: str, artifact_id: str,
                      decision_id: str, status: ApprovalEventStatus, comment: str = ""):
    artifact = store.get_artifact(project_id, artifact_id)
    if artifact.stage_key != ProjectStageKey.P8 or artifact.artifact_type != "pitch_preparation_package":
        raise ValueError("Artifact is not a P8 pitch preparation package.")
    _validate_package(store, project_id, artifact.content or "{}")
    status = ApprovalEventStatus(status)
    if status != ApprovalEventStatus.APPROVED and not comment.strip():
        raise ValueError("Add a review note when requesting changes or rejecting the package.")
    return store.approve_p8_pitch(project_id, artifact_id, decision_id, status, comment=comment)
