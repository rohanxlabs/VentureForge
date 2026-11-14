"""P6 MVP execution tracking, test evidence, and completion review."""
from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from src.hackathon_engineering import engineering_context, latest_engineering_plan
from src.project_store import ProjectStore
from src.research.models import (
    ApprovalEventStatus, ArtifactCreate, DecisionCreate, ProjectStageKey,
)


class TaskExecutionStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    DEFERRED = "deferred"


class TestOutcome(StrEnum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"


class ExecutionTask(BaseModel):
    task_id: str
    title: str = Field(min_length=3)
    status: TaskExecutionStatus = TaskExecutionStatus.TODO
    owner: str = ""
    blocker: str = ""
    completion_evidence: str = ""
    deferred_reason: str = ""

    @model_validator(mode="after")
    def explain_state(self):
        if self.status == TaskExecutionStatus.BLOCKED and not self.blocker.strip():
            raise ValueError(f"Blocked task '{self.title}' needs a blocker description.")
        if self.status == TaskExecutionStatus.DONE and len(self.completion_evidence.strip()) < 5:
            raise ValueError(f"Completed task '{self.title}' needs completion evidence or a demo note.")
        if self.status == TaskExecutionStatus.DEFERRED and len(self.deferred_reason.strip()) < 10:
            raise ValueError(f"Deferred task '{self.title}' needs a reason of at least 10 characters.")
        return self


class TestExecution(BaseModel):
    test_id: str
    scenario: str = Field(min_length=5)
    test_kind: str = "scenario"
    outcome: TestOutcome = TestOutcome.NOT_RUN
    observed_result: str = ""
    evidence_reference: str = ""
    known_failure: str = ""

    @model_validator(mode="after")
    def explain_executed_test(self):
        if self.outcome != TestOutcome.NOT_RUN and len(self.observed_result.strip()) < 5:
            raise ValueError(f"Test '{self.scenario}' needs an observed result.")
        if self.outcome == TestOutcome.FAILED and len(self.known_failure.strip()) < 5:
            raise ValueError(f"Failed test '{self.scenario}' needs a recorded failure or limitation.")
        return self


class MVPExecutionRecord(BaseModel):
    schema_version: str = "ventureforge.p6.mvp_execution.v1"
    tasks: list[ExecutionTask] = Field(min_length=1, max_length=30)
    tests: list[TestExecution] = Field(min_length=1, max_length=50)
    test_environment: str = Field(min_length=5, max_length=1000)
    demo_artifacts: list[str] = Field(default_factory=list, max_length=30)
    demo_notes: str = Field(min_length=10, max_length=4000)
    known_failures: list[str] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    p5_artifact_id: str
    p5_artifact_version: int = Field(ge=1)
    selected_option_id: str

    @field_validator("demo_artifacts", "known_failures", "limitations")
    @classmethod
    def clean_lists(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("List entries must contain non-blank text.")
        return [value.strip() for value in values]

    @field_validator("test_environment", "demo_notes")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value

    @model_validator(mode="after")
    def unique_task_and_test_ids(self):
        task_ids = [task.task_id for task in self.tasks]
        test_ids = [test.test_id for test in self.tests]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Execution task IDs must be unique.")
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("Test record IDs must be unique.")
        return self

    def completion_gaps(self) -> list[str]:
        gaps = [f"Task is still {task.status.value}: {task.title}" for task in self.tasks
                if task.status in {TaskExecutionStatus.TODO, TaskExecutionStatus.IN_PROGRESS, TaskExecutionStatus.BLOCKED}]
        gaps.extend(f"Test has not been run: {test.scenario}" for test in self.tests
                    if test.outcome == TestOutcome.NOT_RUN)
        return gaps


def latest_execution_record(store: ProjectStore, project_id: str):
    items = [a for a in store.list_project_artifacts(project_id)
             if a.stage_key == ProjectStageKey.P6 and a.artifact_type == "mvp_execution"]
    return max(items, key=lambda a: (a.version, a.created_at, a.artifact_id)) if items else None


def execution_context(store: ProjectStore, project_id: str) -> dict:
    context = engineering_context(store, project_id)
    if store.get_project_stage(project_id, ProjectStageKey.P5).status.value != "completed":
        raise ValueError("P5 must be approved before starting MVP execution.")
    p5 = latest_engineering_plan(store, project_id)
    if p5 is None or store.get_artifact_approval_status(project_id, p5.artifact_id) != "approved":
        raise ValueError("The latest P5 engineering plan must be approved before P6.")
    plan = json.loads(p5.content or "{}")
    task_lines = []
    for task in plan.get("build_tasks", []):
        task_lines.extend([f"- **{task.get('title', '')}** ({task.get('priority', 'must')}, "
            f"{task.get('estimated_person_hours', '?')} person-hours, owner: {task.get('owner', 'unassigned')}; "
            f"demo: {task.get('demo_status', 'unspecified')})",
            f"  {task.get('description', '')}",
            f"  Acceptance: {'; '.join(task.get('acceptance_criteria', []))}",
            f"  Dependencies: {', '.join(task.get('depends_on', [])) or 'none'}"])
    checks = [f"- Test: {item}" for item in plan.get("test_scenarios", [])]
    checks.extend(f"- Demo acceptance: {item}" for item in plan.get("demo_acceptance_criteria", []))
    execution_view = (context["view"] + "\n\n### Approved P5 build tasks\n\n"
        + ("\n".join(task_lines) or "No tasks are listed.")
        + "\n\n### Planned verification\n\n"
        + ("\n".join(checks) or "No test scenarios are listed."))
    return {**context, "p5": p5, "p5_data": plan, "view": execution_view}


def starter_execution_record(context: dict) -> MVPExecutionRecord:
    plan = context["p5_data"]
    tasks = [ExecutionTask(task_id=task["id"], title=task["title"], owner=task.get("owner", ""))
             for task in plan.get("build_tasks", [])]
    scenarios = [("scenario", item) for item in plan.get("test_scenarios", [])]
    scenarios.extend(("acceptance", item) for item in plan.get("demo_acceptance_criteria", []))
    tests = [TestExecution(test_id=f"test-{index}", scenario=scenario, test_kind=kind)
             for index, (kind, scenario) in enumerate(scenarios, start=1)]
    if not tasks:
        raise ValueError("Approved P5 plan has no build tasks to track.")
    return MVPExecutionRecord(tasks=tasks, tests=tests,
        test_environment="Record app version, device/browser, and relevant setup.",
        demo_artifacts=[], demo_notes="Record what is implemented and what the judge will see.",
        known_failures=[], limitations=[], p5_artifact_id=context["p5"].artifact_id,
        p5_artifact_version=context["p5"].version,
        selected_option_id=context["option"]["option_id"])


def _validate_record(store: ProjectStore, project_id: str, payload: str):
    context = execution_context(store, project_id)
    record = MVPExecutionRecord.model_validate_json(payload)
    if (record.p5_artifact_id != context["p5"].artifact_id
            or record.p5_artifact_version != context["p5"].version
            or record.selected_option_id != context["option"]["option_id"]):
        raise ValueError("P5 changed since this execution record was loaded. Reload the approved plan before continuing.")
    p5_tasks = {item["id"]: item for item in context["p5_data"].get("build_tasks", [])}
    if {item.task_id for item in record.tasks} != set(p5_tasks):
        raise ValueError("Execution tasks must match the approved P5 task IDs exactly.")
    p5_scenarios = [("scenario", item) for item in context["p5_data"].get("test_scenarios", [])]
    p5_scenarios.extend(("acceptance", item) for item in context["p5_data"].get("demo_acceptance_criteria", []))
    expected = {(kind, text) for kind, text in p5_scenarios}
    actual = {(test.test_kind, test.scenario) for test in record.tests}
    if actual != expected:
        raise ValueError("Test scenarios must match the approved P5 test and demo acceptance criteria.")
    completed = {task.task_id for task in record.tasks if task.status in {TaskExecutionStatus.DONE, TaskExecutionStatus.DEFERRED}}
    titles_by_id = {item["id"]: item["title"] for item in p5_tasks.values()}
    done_titles = {titles_by_id[task_id].casefold() for task_id in completed}
    for task in record.tasks:
        p5_task = p5_tasks[task.task_id]
        for dependency in p5_task.get("depends_on", []):
            if task.status in {TaskExecutionStatus.IN_PROGRESS, TaskExecutionStatus.DONE} and dependency.casefold() not in done_titles:
                raise ValueError(f"Task '{task.title}' cannot be in progress or done before dependency '{dependency}' is done or deferred.")
    return record, context


def save_execution_update(store: ProjectStore, project_id: str, payload: str):
    record, _context = _validate_record(store, project_id, payload)
    stage = store.get_project_stage(project_id, ProjectStageKey.P6)
    if stage.status.value == "pending":
        store.start_project_stage(project_id, ProjectStageKey.P6)
    elif stage.status.value == "completed":
        store.reopen_project_stage(project_id, ProjectStageKey.P6,
            note="A new execution record requires review.")
    elif stage.status.value != "in_progress":
        raise ValueError(f"P6 cannot be updated while it is {stage.status.value}.")
    decision = store.create_decision(DecisionCreate(project_id=project_id,
        stage_key=ProjectStageKey.P6, title="Review MVP execution record",
        description="Human review of implementation progress, test results, and limitations.",
        decision_type="mvp_execution"))
    previous = latest_execution_record(store, project_id)
    create = ArtifactCreate(project_id=project_id, stage_key=ProjectStageKey.P6,
        decision_id=decision.decision_id, artifact_type="mvp_execution",
        title="MVP execution and test record",
        description="Versioned task progress, observed tests, demo artifacts, and limitations.",
        content=record.model_dump_json(indent=2))
    artifact = store.create_artifact_version(project_id, previous.artifact_id, create) if previous else store.create_artifact(create)
    return record, artifact, decision


def review_execution_record(store: ProjectStore, project_id: str, artifact_id: str,
                            decision_id: str, status: ApprovalEventStatus, comment: str = ""):
    artifact = store.get_artifact(project_id, artifact_id)
    if artifact.stage_key != ProjectStageKey.P6 or artifact.artifact_type != "mvp_execution":
        raise ValueError("Artifact is not a P6 MVP execution record.")
    record, _context = _validate_record(store, project_id, artifact.content or "{}")
    status = ApprovalEventStatus(status)
    if status == ApprovalEventStatus.APPROVED:
        gaps = record.completion_gaps()
        if gaps:
            raise ValueError("P6 cannot complete until every task is done/deferred and every planned test has a result: " + "; ".join(gaps[:8]))
    if status != ApprovalEventStatus.APPROVED and not comment.strip():
        raise ValueError("Add a review note when requesting changes or rejecting the execution record.")
    return store.approve_p6_execution(project_id, artifact_id, decision_id, status, comment=comment)

