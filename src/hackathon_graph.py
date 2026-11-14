"""LangGraph coordinator for the human-led Challenge Setup and P1-P8 workflow.

The stage services remain responsible for creating and approving domain artifacts.
This graph provides a separate, checkpointed human gate between those stages. A
small JSON routing snapshot allows the coordinator to be reconstructed after
restart; it does not persist LangGraph's interrupted execution state. The user's
project artifacts and stage history remain the source of truth.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src.database_config import REPOSITORY_ROOT, resolve_database_path
from src.hackathon_intake import latest_challenge_brief
from src.project_store import ProjectStore
from src.research.models import ProjectStageKey, ProjectStageStatus


_CHECKPOINT_PATH = Path(os.environ.get(
    "VENTUREFORGE_HACKATHON_CHECKPOINTS",
    str(REPOSITORY_ROOT / ".cache" / "hackathon_workflow_checkpoints.json"),
)).expanduser()
if not _CHECKPOINT_PATH.is_absolute():
    _CHECKPOINT_PATH = REPOSITORY_ROOT / _CHECKPOINT_PATH
_CHECKPOINT_LOCK = threading.Lock()


class HackathonWorkflowState(TypedDict, total=False):
    project_id: str
    current_stage: str
    stage_statuses: dict[str, str]
    workflow_status: str
    message: str
    updated_at: str


def _read_checkpoints() -> dict[str, Any]:
    if not _CHECKPOINT_PATH.exists():
        return {}
    try:
        payload = json.loads(_CHECKPOINT_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _persist_snapshot(state: HackathonWorkflowState) -> None:
    """Persist only workflow routing metadata, never research or user content."""
    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _CHECKPOINT_LOCK:
        records = _read_checkpoints()
        records[state["project_id"]] = {
            "schema_version": 1,
            "project_id": state["project_id"],
            "current_stage": state["current_stage"],
            "stage_statuses": state["stage_statuses"],
            "workflow_status": state["workflow_status"],
            "updated_at": state["updated_at"],
        }
        fd, temp_name = tempfile.mkstemp(prefix="hackathon-checkpoint-", suffix=".tmp",
                                         dir=_CHECKPOINT_PATH.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(records, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, _CHECKPOINT_PATH)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _load_snapshot(project_id: str) -> HackathonWorkflowState | None:
    with _CHECKPOINT_LOCK:
        record = _read_checkpoints().get(project_id)
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        return None
    return {
        "project_id": project_id,
        "current_stage": record.get("current_stage", "Challenge Setup"),
        "stage_statuses": record.get("stage_statuses", {}),
        "workflow_status": record.get("workflow_status", "running"),
        "message": "Recovered workflow state; refreshing against project artifacts.",
        "updated_at": record.get("updated_at", ""),
    }


def _project_progress(project_id: str) -> HackathonWorkflowState:
    store = ProjectStore(resolve_database_path())
    store.get_project(project_id)
    statuses = {stage.value: store.get_project_stage(project_id, stage).status.value
                for stage in ProjectStageKey}

    brief = latest_challenge_brief(store, project_id)
    brief_approved = bool(brief and brief.decision_id
        and store.get_artifact_approval_status(project_id, brief.artifact_id) == "approved"
        and store.get_decision_approval_status(project_id, brief.decision_id) == "approved")
    if not brief_approved:
        current = "Challenge Setup"
    else:
        latest_p3 = [item for item in store.list_project_artifacts(project_id)
            if item.stage_key == ProjectStageKey.P3 and item.artifact_type == "validation_decision"]
        latest_p3.sort(key=lambda item: (item.version, item.created_at, item.artifact_id))
        stopped = False
        if latest_p3:
            try:
                stopped = json.loads(latest_p3[-1].content or "{}").get("outcome") == "stop"
            except json.JSONDecodeError:
                stopped = False
        if stopped:
            current = "Stopped at P3"
        else:
            current = next((stage.value for stage in ProjectStageKey
                if statuses[stage.value] != ProjectStageStatus.COMPLETED.value), "Complete")

    workflow_status = "complete" if current == "Complete" else (
        "stopped" if current == "Stopped at P3" else "waiting_for_human")
    message = ("All stages P1-P8 are complete." if workflow_status == "complete" else
        "P3 recorded a stop decision. The project direction is closed." if workflow_status == "stopped" else
        f"Complete the {current} stage in its workspace, then resume this workflow.")
    snapshot = HackathonWorkflowState(project_id=project_id, current_stage=current,
        stage_statuses={**statuses, "Challenge Setup": "approved" if brief_approved else "pending"},
        workflow_status=workflow_status, message=message,
        updated_at=datetime.now(timezone.utc).isoformat())
    _persist_snapshot(snapshot)
    return snapshot


def _synchronize(state: HackathonWorkflowState) -> HackathonWorkflowState:
    return _project_progress(state["project_id"])


def _wait_for_human(state: HackathonWorkflowState) -> HackathonWorkflowState:
    interrupt({"project_id": state["project_id"], "stage": state["current_stage"],
               "message": state["message"], "stage_statuses": state["stage_statuses"]})
    return {"message": "Workflow resumed; checking for newly completed stage work."}


def _route_progress(state: HackathonWorkflowState) -> str:
    return END if state.get("workflow_status") in {"complete", "stopped"} else "human_gate"


def build_hackathon_graph():
    """Build an in-memory LangGraph coordinator independent of startup discovery."""
    graph = StateGraph(HackathonWorkflowState)
    graph.add_node("synchronize", _synchronize)
    graph.add_node("human_gate", _wait_for_human)
    graph.add_edge(START, "synchronize")
    graph.add_conditional_edges("synchronize", _route_progress,
                                {END: END, "human_gate": "human_gate"})
    graph.add_edge("human_gate", "synchronize")
    return graph.compile(checkpointer=MemorySaver())


HACKATHON_GRAPH = build_hackathon_graph()


def start_or_resume_hackathon_workflow(project_id: str) -> HackathonWorkflowState:
    """Start or resume the project's independent P1-P8 LangGraph thread."""
    config = {"configurable": {"thread_id": f"hackathon:{project_id}"}}
    state_snapshot = HACKATHON_GRAPH.get_state(config)
    interrupted = bool(state_snapshot and any(
        getattr(task, "interrupts", ()) for task in state_snapshot.tasks))
    if interrupted:
        result = HACKATHON_GRAPH.invoke(
            Command(resume="Check completed stage work"), config=config)
    else:
        restored = _load_snapshot(project_id) or HackathonWorkflowState(project_id=project_id)
        result = HACKATHON_GRAPH.invoke(restored, config=config)
    return HackathonWorkflowState(**result)
