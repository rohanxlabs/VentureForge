"""Project-scoped P2 orchestration built on existing research and M1 services."""
from __future__ import annotations

import json
from dataclasses import asdict

from src.problem_intelligence import ProblemFrame, latest_problem_frame
from src.project_store import ProjectStore
from src.research.budget import SearchBudget
from src.research.extraction import extract_candidate
from src.research.gaps import ResearchGapReport, evaluate_research_gaps
from src.research.models import (
    ApprovalEventStatus, ProjectStageKey,
    ProjectStageStatus, ResearchPlan, WorkflowRunCreate,
)
from src.research.pipeline import run_gap_research
from src.research.report import render_markdown
from src.research.search import SearchOrchestrator


def _latest_artifact(store: ProjectStore, project_id: str, artifact_type: str):
    items = [item for item in store.list_project_artifacts(project_id)
             if item.stage_key == ProjectStageKey.P2 and item.artifact_type == artifact_type]
    return max(items, key=lambda item: (item.version, item.created_at, item.artifact_id)) if items else None


def latest_approved_plan(store: ProjectStore, project_id: str):
    artifact = _latest_artifact(store, project_id, "research_plan")
    if artifact is None:
        raise ValueError("No P2 research plan exists for this project.")
    if not artifact.decision_id or store.get_artifact_approval_status(project_id, artifact.artifact_id) != "approved":
        raise ValueError("The latest research plan must be approved before research can run.")
    if store.get_decision_approval_status(project_id, artifact.decision_id) != "approved":
        raise ValueError("The linked research-plan decision is not approved.")
    plan = store.get_plan(json.loads(artifact.content or "{}").get("id", ""))
    if plan.status != "approved":
        raise ValueError("The persisted research plan is not approved.")
    store.validate_run_project(plan.run_id, project_id)
    return plan, artifact


def _validate_p2_project(store: ProjectStore, project_id: str) -> None:
    project = store.get_project(project_id)
    if project.status.value != "active":
        raise ValueError("P2 research requires an active project.")
    if store.get_project_stage(project_id, ProjectStageKey.P1).status != ProjectStageStatus.COMPLETED:
        raise ValueError("P1 must be completed before P2 research.")
    frame_artifact = latest_problem_frame(store, project_id)
    if (frame_artifact is None or frame_artifact.status.value not in {"draft", "ready"}
            or store.get_artifact_approval_status(project_id, frame_artifact.artifact_id) != "approved"
            or not frame_artifact.decision_id
            or store.get_decision_approval_status(project_id, frame_artifact.decision_id) != "approved"):
        raise ValueError("The latest Problem Frame and its linked decision must be approved.")
    ProblemFrame.model_validate_json(frame_artifact.content)


def approve_plan(store: ProjectStore, project_id: str, artifact_id: str, *,
                 status: ApprovalEventStatus = ApprovalEventStatus.APPROVED,
                 comment: str = "", actor: str = "user"):
    artifact = store.get_artifact(project_id, artifact_id)
    if not artifact.decision_id:
        raise ValueError("Research plan artifact has no linked approval decision.")
    return store.approve_p2_artifact(project_id, artifact_id, artifact.decision_id,
        "research_plan", status, comment=comment, actor=actor)


def execute_project_research(store: ProjectStore, project_id: str, *,
                             budget: SearchBudget | None = None,
                             orchestrator: SearchOrchestrator | None = None,
                             max_rounds: int = 2,
                             min_independent_sources: int = 2,
                             extractor=extract_candidate):
    """Run approved P2 research and persist its report and workflow outcome."""
    _validate_p2_project(store, project_id)
    plan, plan_artifact = latest_approved_plan(store, project_id)
    stage = store.get_project_stage(project_id, ProjectStageKey.P2)
    if stage.status != ProjectStageStatus.IN_PROGRESS:
        raise ValueError(f"P2 research requires in_progress status; current status is {stage.status.value}.")
    workflow = store.start_workflow_run(WorkflowRunCreate(project_id=project_id,
        stage_key=ProjectStageKey.P2, workflow_type="p2_research_execution",
        input_ref=plan_artifact.artifact_id))
    active_budget = budget or SearchBudget(**plan.search_budget)
    try:
        execution = run_gap_research(plan, store, budget=active_budget, orchestrator=orchestrator,
            max_rounds=max_rounds, min_independent_sources=min_independent_sources, extractor=extractor)
        run, questions, sources, evidence = store.get_run_records(plan.run_id)
        markdown = render_markdown(run, questions, sources, evidence, execution.gap_report)
        frame_artifact = store.get_artifact(project_id, plan.problem_frame_artifact_id)
        report_data = {
            "schema": "ventureforge.p2.report.v1", "project_id": project_id,
            "plan_id": plan.id, "plan_artifact_id": plan_artifact.artifact_id,
            "run_id": plan.run_id, "generated_by": "existing_research_pipeline",
            "problem_context": json.loads(frame_artifact.content or "{}"),
            "research_objectives": plan.objectives,
            "methodology": "Question-driven searches through configured existing connectors; captured content is passed to the existing passage extraction and exact text verification pipeline.",
            "questions": [item.model_dump(mode="json") for item in questions],
            "sources": [{"id": item.id, "run_id": item.run_id, "question_id": item.question_id,
                "url": item.final_url or item.url, "title": item.title, "publisher": item.publisher,
                "published_at": item.published_at, "retrieved_at": item.retrieved_at.isoformat(),
                "source_categories": item.source_categories, "content_origin": item.content_origin,
                "content_type": item.content_type, "content_captured": bool(item.content),
                "content_characters": len(item.content), "fetch_error": item.fetch_error}
                for item in sources],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "known_limitations": plan.known_limitations,
            "unanswered_questions": [item.question for item in execution.gap_report.questions if not item.covered],
            "remaining_gaps": [{"question_id": item.question_id, "question": item.question,
                "gaps": item.gaps, "follow_up_query": item.follow_up_query}
                for item in execution.gap_report.uncovered],
            "citation_validity_note": "Exact passage verification establishes that a passage exists in captured text; it does not establish semantic support or factual truth.",
            "outcome": {"searches_attempted": execution.searches_attempted,
                "searches_succeeded": execution.searches_succeeded,
                "searches_failed": execution.searches_failed,
                "sources_collected": execution.sources_collected,
                "evidence_extracted": execution.evidence_extracted,
                "rounds_run": execution.rounds_run,
                "budget": {"limits": {"max_search_calls": active_budget.max_search_calls,
                    "max_pages": active_budget.max_pages,
                    "max_tavily_credits": active_budget.max_tavily_credits,
                    "max_seconds": active_budget.max_seconds},
                    "used": active_budget.snapshot()},
                "execution_status": "partial" if execution.failures or execution.searches_attempted == 0 else "complete",
                "collection_outcome": _classify_outcome(execution),
                "connector_attempts": [asdict(item) for item in execution.search_attempts]},
            "coverage": {"complete_by_heuristic": execution.gap_report.complete,
                "rounds": execution.gap_report.rounds,
                "questions": [asdict(item) for item in execution.gap_report.questions],
                "potential_disagreements": execution.gap_report.potential_disagreements},
            "failures": [{"question_id": item.question_id, "category": item.category,
                "connector": item.connector, "reason": item.reason} for item in execution.failures],
            "markdown": markdown,
        }
        saved, decision, workflow = store.persist_p2_report(project_id, workflow.workflow_run_id,
            plan.run_id, json.dumps(report_data, ensure_ascii=False, indent=2))
        return execution, saved, decision, workflow
    except Exception as exc:
        # Workflow persistence records failure while retaining the original exception.
        summary = f"{type(exc).__name__}: {str(exc)[:400]}" or type(exc).__name__
        try:
            store.fail_workflow_run(project_id, workflow.workflow_run_id, summary)
        except Exception:
            pass
        raise


def _classify_outcome(execution) -> str:
    if execution.searches_failed and execution.searches_succeeded:
        return "partial_connector_failure"
    if execution.searches_attempted == 0:
        return "no_search_attempted"
    if execution.sources_collected == 0:
        return "successful_searches_without_sources"
    if not execution.gap_report.questions or all(not item.covered for item in execution.gap_report.questions):
        return "sources_without_sufficient_coverage"
    return "sources_with_coverage"


def approve_report(store: ProjectStore, project_id: str, artifact_id: str, *,
                   status: ApprovalEventStatus = ApprovalEventStatus.APPROVED,
                   comment: str = "", actor: str = "user"):
    artifact = store.get_artifact(project_id, artifact_id)
    if not artifact.decision_id:
        raise ValueError("Research report artifact has no linked approval decision.")
    return store.approve_p2_artifact(project_id, artifact_id, artifact.decision_id,
        "research_report", status, comment=comment, actor=actor)


def project_research_records(store: ProjectStore, project_id: str, *, category: str):
    plan, _ = latest_approved_plan(store, project_id)
    store.validate_run_project(plan.run_id, project_id)
    run, questions, sources, evidence = store.get_run_records(plan.run_id)
    if category == "sources":
        return sources
    if category == "findings":
        return evidence
    report = evaluate_research_gaps(questions, sources, evidence)
    return report


def report_document(store: ProjectStore, project_id: str):
    artifact = _latest_artifact(store, project_id, "research_report")
    if artifact is None:
        raise ValueError("No P2 research report exists for this project.")
    data = json.loads(artifact.content or "{}")
    if data.get("project_id") != project_id:
        raise ValueError("Research report is not owned by this project.")
    store.validate_run_project(data.get("run_id", ""), project_id)
    return artifact, data


def p2_status(store: ProjectStore, project_id: str) -> dict:
    store.get_project(project_id)
    stage = store.get_project_stage(project_id, ProjectStageKey.P2)
    plan = _latest_artifact(store, project_id, "research_plan")
    report = _latest_artifact(store, project_id, "research_report")
    return {"project_id": project_id, "stage": stage.status.value,
        "plan": ({"artifact_id": plan.artifact_id, "version": plan.version,
                  "approval": store.get_artifact_approval_status(project_id, plan.artifact_id)} if plan else None),
        "report": ({"artifact_id": report.artifact_id, "version": report.version,
                    "approval": store.get_artifact_approval_status(project_id, report.artifact_id)} if report else None),
        "workflows": [item.model_dump(mode="json") for item in store.list_project_workflows(project_id)
                      if item.stage_key == ProjectStageKey.P2]}
