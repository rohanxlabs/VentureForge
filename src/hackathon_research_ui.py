"""Gradio UI for project-scoped P2 research planning and review."""
from __future__ import annotations

import json

from src.hackathon_intake import PROJECT_DATABASE
from src.problem_intelligence import create_research_plan, revise_research_plan
from src.project_store import ProjectStore
from src.research.budget import SearchBudget
from src.research.connectors import HackerNewsConnector, TavilyConnector
from src.research.models import ApprovalEventStatus, ProjectStageKey
from src.research.planner import require_llm_configuration
from src.research.project_intelligence import (
    approve_plan,
    approve_report,
    execute_project_research,
)
from src.research.safe_fetch import SafeFetcher
from src.research.search import SearchOrchestrator


def _store() -> ProjectStore:
    return ProjectStore(PROJECT_DATABASE)


def _latest_artifact(store: ProjectStore, project_id: str, kind: str):
    items = [item for item in store.list_project_artifacts(project_id)
             if item.stage_key == ProjectStageKey.P2 and item.artifact_type == kind]
    return max(items, key=lambda item: (item.version, item.created_at, item.artifact_id)) if items else None


def _plan_status(store: ProjectStore, project_id: str) -> tuple[str, str, str, str, str]:
    stage = store.get_project_stage(project_id, ProjectStageKey.P2)
    plan_artifact = _latest_artifact(store, project_id, "research_plan")
    report_artifact = _latest_artifact(store, project_id, "research_report")
    plan_text = plan_artifact.content or "" if plan_artifact else ""
    report_text = ""
    report_id = ""
    if report_artifact:
        report_id = report_artifact.artifact_id
        data = json.loads(report_artifact.content or "{}")
        outcome = data.get("outcome", {})
        coverage = data.get("coverage", {})
        review = store.get_artifact_approval_status(project_id, report_artifact.artifact_id)
        source_count = len(data.get("sources", []))
        evidence_count = len(data.get("evidence", []))
        unanswered = len(data.get("unanswered_questions", []))
        report_text = data.get("markdown", "")
        report_head = (f"### P2 Research Report | v{report_artifact.version}\n\n"
            f"**Review:** {review} | **Execution:** {outcome.get('execution_status', 'unknown')} | "
            f"**Sources:** {source_count} | **Evidence records:** {evidence_count} | "
            f"**Questions without verified evidence:** {unanswered} | "
            f"**Coverage heuristic:** {'complete' if coverage.get('complete_by_heuristic') else 'gaps remain'}\n\n"
            f"{data.get('citation_validity_note', '')}\n\n")
        report_text = report_head + report_text
    else:
        report_text = "No P2 report has been saved yet."
    plan_id = plan_artifact.artifact_id if plan_artifact else ""
    plan_approval = store.get_artifact_approval_status(project_id, plan_id) if plan_id else "not created"
    plan_head = (f"### P2 Research | Stage: {stage.status.value}\n\n"
        f"**Latest plan review:** {plan_approval} | **Plan version:** {plan_artifact.version if plan_artifact else 'none'}\n\n"
        "Research sources and extracted passages inform human review; they do not establish truth by themselves.")
    return plan_head, plan_text, report_text, plan_id, report_id


def build_research_ui(gr, project_selector) -> None:
    def inspect(project_id):
        if not project_id:
            return "Select a project to view P2.", "", "No P2 report has been saved yet.", "", ""
        try:
            return _plan_status(_store(), project_id)
        except Exception as exc:
            return f"Could not load P2: {exc}", "", "", "", ""

    def create_plan_ui(project_id, use_llm):
        if not project_id:
            return "Select a project first.", "", ""
        try:
            plan, artifact, _decision = create_research_plan(_store(), project_id, use_llm=bool(use_llm))
            return (f"Created research plan v{artifact.version}. Review and approve it before running searches.",
                    plan.model_dump_json(indent=2), artifact.artifact_id)
        except Exception as exc:
            return f"Could not create research plan: {exc}", "", ""

    def save_revision_ui(project_id, edited_json):
        if not project_id:
            return "Select a project first.", "", ""
        if not (edited_json or "").strip():
            return "Load or create a plan before saving a revision.", "", ""
        try:
            plan, artifact, _decision = revise_research_plan(_store(), project_id, edited_json)
            return (f"Saved research plan v{artifact.version} as a new draft. Review and approve this version before running it.",
                    plan.model_dump_json(indent=2), artifact.artifact_id)
        except Exception as exc:
            return f"Could not save research-plan revision: {exc}", "", ""

    def review_plan_ui(project_id, artifact_id, status, comment):
        if not project_id or not artifact_id:
            return "Create or load a research plan before reviewing it.", ""
        try:
            approval = ApprovalEventStatus(status)
            if approval != ApprovalEventStatus.APPROVED and not (comment or "").strip():
                raise ValueError("Add a review note when requesting changes or rejecting the plan.")
            approve_plan(_store(), project_id, artifact_id, status=approval, comment=comment or "")
            header, plan_text, _, _, _ = _plan_status(_store(), project_id)
            return f"Recorded P2 plan review: {approval.value}.", header, plan_text
        except Exception as exc:
            return f"Could not record plan review: {exc}", "", ""

    def run_research_ui(project_id, max_searches, max_pages, max_seconds, max_rounds):
        if not project_id:
            return "Select a project first.", "", "No P2 report has been saved yet.", ""
        try:
            from src.config import settings
            require_llm_configuration(settings.get_llm_config(reasoning=False))
            tavily = TavilyConnector(settings.tavily_api_key) if settings.tavily_api_key else None
            orchestrator = SearchOrchestrator(tavily, HackerNewsConnector(),
                fetcher=SafeFetcher(), max_workers=4)
            budget = SearchBudget(max_search_calls=int(max_searches),
                max_tavily_credits=min(8, int(max_searches)), max_pages=int(max_pages),
                max_seconds=float(max_seconds))
            _, report, _, workflow = execute_project_research(_store(), project_id,
                budget=budget, orchestrator=orchestrator, max_rounds=int(max_rounds))
            header, _, report_text, _, _ = _plan_status(_store(), project_id)
            warning = ""
            if not settings.tavily_api_key:
                warning = "TAVILY_API_KEY is not configured; only Hacker News community searches are available. "
            return (f"{warning}Research workflow {workflow.status.value}; report v{report.version} saved for human review.",
                    header, report_text, report.artifact_id)
        except Exception as exc:
            try:
                header, _, report_text, _, _ = _plan_status(_store(), project_id)
            except Exception:
                header, report_text = "", ""
            return f"Research run did not complete: {exc}", header, report_text, ""

    def review_report_ui(project_id, artifact_id, status, comment):
        if not project_id or not artifact_id:
            return "Run research and load the latest report before reviewing it.", ""
        try:
            approval = ApprovalEventStatus(status)
            if approval != ApprovalEventStatus.APPROVED and not (comment or "").strip():
                raise ValueError("Add a review note when requesting changes or rejecting the report.")
            approve_report(_store(), project_id, artifact_id, status=approval, comment=comment or "")
            header, _, report_text, _, _ = _plan_status(_store(), project_id)
            next_step = " P2 is complete; you can proceed to P3." if approval == ApprovalEventStatus.APPROVED else " Revise the plan or rerun research before requesting approval."
            return f"Recorded P2 report review: {approval.value}.{next_step}", header, report_text
        except Exception as exc:
            return f"Could not record report review: {exc}", "", ""

    with gr.Accordion("P2 - Research", open=False):
        gr.Markdown(
            "Build a question-driven desk-research plan from the approved P1 frame. Review the questions before searching. "
            "Exact-passage checks confirm that text appears in captured source material; they do not prove the passage supports a claim."
        )
        with gr.Row():
            use_ai_plan = gr.Checkbox(label="Draft questions with configured AI (optional)", value=False)
            create_plan_button = gr.Button("Create research plan", variant="primary")
            revise_plan_button = gr.Button("Save edited plan as a new version")
        plan_status = gr.Markdown()
        plan_editor = gr.Code(label="Research plan JSON - review and edit questions before approval", language="json", interactive=True, lines=20)
        with gr.Row():
            plan_artifact_id = gr.Textbox(label="Latest research-plan artifact ID", interactive=False)
            plan_review_status = gr.Dropdown(label="Plan review", choices=[
                ("Approve", "approved"), ("Request changes", "changes_requested"),
                ("Reject", "rejected")], value="approved")
        plan_review_comment = gr.Textbox(label="Plan review note")
        approve_plan_button = gr.Button("Record plan review")
        plan_review_output = gr.Markdown()

        gr.Markdown("#### Bounded research run\nRunning searches uses configured connectors and the LLM provider for evidence extraction. Keep limits appropriate for the event timeline and API budget.")
        with gr.Row():
            max_searches = gr.Number(label="Maximum searches", value=8, precision=0, minimum=0, maximum=40)
            max_pages = gr.Number(label="Maximum fetched pages", value=20, precision=0, minimum=0, maximum=100)
            max_seconds = gr.Number(label="Time limit (seconds)", value=180, minimum=1, maximum=1800)
            max_rounds = gr.Number(label="Gap-follow-up rounds", value=2, precision=0, minimum=0, maximum=3)
        run_research_button = gr.Button("Run approved research plan", variant="primary")
        research_run_status = gr.Markdown()
        report_artifact_id = gr.Textbox(label="Latest research-report artifact ID", interactive=False)
        report_view = gr.Markdown("No P2 report has been saved yet.")
        with gr.Row():
            report_review_status = gr.Dropdown(label="Report review", choices=[
                ("Approve and complete P2", "approved"), ("Request changes", "changes_requested"),
                ("Reject", "rejected")], value="approved")
            report_review_comment = gr.Textbox(label="Report review note")
        review_report_button = gr.Button("Record report review")
        report_review_output = gr.Markdown()

        project_selector.change(inspect, inputs=[project_selector], outputs=[
            plan_status, plan_editor, report_view, plan_artifact_id, report_artifact_id])
        create_plan_button.click(create_plan_ui, inputs=[project_selector, use_ai_plan],
            outputs=[plan_review_output, plan_editor, plan_artifact_id])
        revise_plan_button.click(save_revision_ui, inputs=[project_selector, plan_editor],
            outputs=[plan_review_output, plan_editor, plan_artifact_id])
        approve_plan_button.click(review_plan_ui,
            inputs=[project_selector, plan_artifact_id, plan_review_status, plan_review_comment],
            outputs=[plan_review_output, plan_status, plan_editor])
        run_research_button.click(run_research_ui,
            inputs=[project_selector, max_searches, max_pages, max_seconds, max_rounds],
            outputs=[research_run_status, plan_status, report_view, report_artifact_id])
        review_report_button.click(review_report_ui,
            inputs=[project_selector, report_artifact_id, report_review_status, report_review_comment],
            outputs=[report_review_output, plan_status, report_view])
