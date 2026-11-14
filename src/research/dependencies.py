"""Exact artifact dependencies shared by persistence and workflow services."""
from __future__ import annotations

from typing import Any


# Keys map a persisted artifact type to (upstream artifact type, content ID key,
# content version key). IDs are unique per artifact version; versions are kept
# alongside IDs so consumers can detect malformed or legacy lineage explicitly.
_DEPENDENCY_FIELDS: dict[str, tuple[tuple[str, str, str | None], ...]] = {
    "research_plan": (("problem_frame", "problem_frame_artifact_id", "problem_frame_version"),),
    "research_report": (("research_plan", "plan_artifact_id", "plan_artifact_version"),),
    "validation_decision": (
        ("problem_frame", "problem_frame_artifact_id", "problem_frame_version"),
        ("research_report", "research_report_artifact_id", "research_report_version"),
        # Primary research records are referenced as exact, version-specific IDs
        # in ValidationDecision.primary_research_artifact_ids.
        ("primary_research_record", "primary_research_artifact_ids", None),
    ),
    "primary_research_record": (("problem_frame", "problem_frame_artifact_id", "problem_frame_version"),),
    "solution_options": (
        ("problem_frame", "problem_frame_artifact_id", "problem_frame_version"),
        ("research_report", "research_report_artifact_id", "research_report_version"),
        ("validation_decision", "validation_decision_artifact_id", "validation_decision_version"),
    ),
    "engineering_plan": (
        ("solution_options", "p4_artifact_id", "p4_artifact_version"),
        ("problem_frame", "p1_artifact_id", "p1_artifact_version"),
        ("research_report", "p2_artifact_id", "p2_artifact_version"),
        ("validation_decision", "p3_artifact_id", "p3_artifact_version"),
    ),
    "mvp_execution": (("engineering_plan", "p5_artifact_id", "p5_artifact_version"),),
    "impact_feasibility_brief": (
        ("problem_frame", "p1_artifact_id", "p1_artifact_version"),
        ("research_report", "p2_artifact_id", "p2_artifact_version"),
        ("validation_decision", "p3_artifact_id", "p3_artifact_version"),
        ("solution_options", "p4_artifact_id", "p4_artifact_version"),
        ("engineering_plan", "p5_artifact_id", "p5_artifact_version"),
        ("mvp_execution", "p6_artifact_id", "p6_artifact_version"),
    ),
    "pitch_preparation_package": (
        ("problem_frame", "p1_artifact_id", "p1_artifact_version"),
        ("research_report", "p2_artifact_id", "p2_artifact_version"),
        ("validation_decision", "p3_artifact_id", "p3_artifact_version"),
        ("solution_options", "p4_artifact_id", "p4_artifact_version"),
        ("engineering_plan", "p5_artifact_id", "p5_artifact_version"),
        ("mvp_execution", "p6_artifact_id", "p6_artifact_version"),
        ("impact_feasibility_brief", "p7_artifact_id", "p7_artifact_version"),
    ),
}


def artifact_dependencies(artifact_type: str, content: str | dict[str, Any] | None) -> list[tuple[str, str, int | None]]:
    """Extract (type, artifact_id, version) references from an artifact payload."""
    import json

    if isinstance(content, str):
        try:
            payload = json.loads(content or "{}")
        except json.JSONDecodeError:
            return []
    else:
        payload = content or {}
    if not isinstance(payload, dict):
        return []
    result = []
    for upstream_type, id_key, version_key in _DEPENDENCY_FIELDS.get(artifact_type, ()):
        artifact_ids = payload.get(id_key)
        if version_key is None:
            if not isinstance(artifact_ids, list):
                continue
            for artifact_id in artifact_ids:
                if isinstance(artifact_id, str) and artifact_id:
                    result.append((upstream_type, artifact_id, None))
            continue
        if not isinstance(artifact_ids, str) or not artifact_ids:
            continue
        version = payload.get(version_key)
        result.append((upstream_type, artifact_ids, version if isinstance(version, int) else None))
    return result


def depends_on_exact_artifact(artifact_type: str, content: str | dict[str, Any] | None,
                              upstream_type: str, artifact_id: str, version: int) -> bool:
    return any(ref_type == upstream_type and ref_id == artifact_id
               and ref_version == version
               for ref_type, ref_id, ref_version in artifact_dependencies(artifact_type, content))
