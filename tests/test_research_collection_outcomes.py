import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import src.research_cli as cli
from src.research.budget import SearchBudget
from src.research.collection import collect_plan_sources
from src.research.connectors import ResearchConnectorError
from src.research.models import (
    CompetitionInput, PlanningDraft, QuestionDraft, ResearchPlan, SourceSnapshot,
)
from src.research.pipeline import CollectionUnavailableError, run_gap_research
from src.research.search import SearchOrchestrator
from src.research.storage import ResearchStore


def make_plan():
    draft = PlanningDraft(
        problem_summary="Manufacturers need fewer unexpected machine failures.",
        domain="manufacturing maintenance",
        questions=[
            QuestionDraft(text="Which affordable monitoring tools identify machine faults?", priority="high", source_categories=["community"], evidence_required="Examples of tools in use", decision_impact="Selects feasible monitoring options"),
            QuestionDraft(text="What maintenance actions reduce unexpected downtime?", priority="high", source_categories=["community"], evidence_required="Reported downtime outcomes", decision_impact="Identifies useful interventions"),
            QuestionDraft(text="What adoption barriers affect smaller manufacturers?", priority="medium", source_categories=["community"], evidence_required="First-hand adoption barriers", decision_impact="Defines practical constraints"),
        ],
    )
    plan = cli.generate_research_plan("Reduce machine downtime", CompetitionInput(), draft_provider=lambda *_: draft)
    return plan.model_copy(update={"status": "approved"})


class StaticConnector:
    cost_per_search = 0

    def __init__(self, name, *, fails=False, returns_sources=True):
        self.name = name
        self.fails = fails
        self.returns_sources = returns_sources

    def search(self, *, run_id, question_id, query, category, max_results):
        if self.fails:
            cause = requests.ConnectionError("offline")
            raise ResearchConnectorError(f"{self.name} connection failed") from cause
        if not self.returns_sources:
            return []
        return [SourceSnapshot(run_id=run_id, question_id=question_id,
            url=f"https://{self.name}.example/{question_id}", title=f"{self.name} result",
            content=f"Captured result from {self.name}.", source_categories=[category], content_origin="api_content")]


def orchestrator(*, tavily_fails=False, hn_fails=False, returns_sources=True):
    return SearchOrchestrator(
        StaticConnector("tavily", fails=tavily_fails, returns_sources=returns_sources),
        StaticConnector("hackernews", fails=hn_fails, returns_sources=returns_sources),
        fetcher=object(), max_workers=3,
    )


class CollectionOutcomeTests(unittest.TestCase):
    def _collect(self, plan, store, search):
        budget = SearchBudget(max_search_calls=20, max_pages=0, max_seconds=5)
        if store is not None:
            return collect_plan_sources(plan, store, budget=budget, orchestrator=search)
        return search.collect(run_id=plan.run_id, prompt=plan.brief.problem_statement,
            questions=plan.questions, budget=budget)

    def test_tavily_failure_hacker_news_success_retains_sources(self):
        plan = make_plan()
        with tempfile.TemporaryDirectory() as temp:
            store = ResearchStore(Path(temp) / "research.sqlite")
            store.save_plan(plan)
            result = self._collect(plan, store, orchestrator(tavily_fails=True))
            self.assertEqual((result.searches_attempted, result.searches_succeeded, result.searches_failed), (6, 3, 3))
            self.assertEqual({failure.connector for failure in result.failures}, {"tavily"})
            self.assertEqual(len(result.sources), 3)
            self.assertEqual(len(store.get_run_records(plan.run_id)[2]), 3)

    def test_hacker_news_failure_tavily_success_retains_sources(self):
        plan = make_plan()
        with tempfile.TemporaryDirectory() as temp:
            store = ResearchStore(Path(temp) / "research.sqlite")
            store.save_plan(plan)
            result = self._collect(plan, store, orchestrator(hn_fails=True))
            self.assertEqual((result.searches_attempted, result.searches_succeeded, result.searches_failed), (6, 3, 3))
            self.assertEqual({failure.connector for failure in result.failures}, {"hackernews"})
            self.assertEqual(len(result.sources), 3)

    def test_both_connectors_succeed_with_sources(self):
        plan = make_plan()
        with tempfile.TemporaryDirectory() as temp:
            store = ResearchStore(Path(temp) / "research.sqlite")
            store.save_plan(plan)
            result = self._collect(plan, store, orchestrator())
            self.assertEqual((result.searches_attempted, result.searches_succeeded, result.searches_failed), (6, 6, 0))
            self.assertEqual(len(result.sources), 6)
            self.assertFalse(result.failures)

    def test_both_connectors_succeed_with_zero_sources(self):
        plan = make_plan()
        with tempfile.TemporaryDirectory() as temp:
            store = ResearchStore(Path(temp) / "research.sqlite")
            store.save_plan(plan)
            result = self._collect(plan, store, orchestrator(returns_sources=False))
            self.assertEqual((result.searches_attempted, result.searches_succeeded, result.searches_failed), (6, 6, 0))
            self.assertEqual(result.sources, [])
            self.assertEqual(result.failures, [])

    def test_collect_cli_labels_successful_zero_result_search(self):
        plan = make_plan()
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "research.sqlite"
            store = ResearchStore(database)
            store.save_plan(plan)
            output = io.StringIO()
            with patch.object(cli.settings, "tavily_api_key", "test-tavily-key"), \
                 patch.object(cli, "TavilyConnector", return_value=StaticConnector("tavily", returns_sources=False)), \
                 patch.object(cli, "HackerNewsConnector", return_value=StaticConnector("hackernews", returns_sources=False)), \
                 contextlib.redirect_stdout(output):
                status = cli.main(["collect", plan.id, "--database", str(database)])
            self.assertIsNone(status)
            self.assertIn("6 attempted, 6 succeeded, 0 failed", output.getvalue())
            self.assertIn("searches succeeded, but no relevant sources were found", output.getvalue())

    def test_research_cli_reports_successful_zero_result_without_findings(self):
        plan = make_plan()
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "research.sqlite"
            report_path = Path(temp) / "research.md"
            ResearchStore(database).save_plan(plan)
            output = io.StringIO()
            with patch.object(type(cli.settings), "get_llm_config", return_value={"api_key": "test", "base_url": "https://llm.example/v1", "model": "test"}), \
                 patch.object(cli.settings, "tavily_api_key", "test-tavily-key"), \
                 patch.object(cli, "TavilyConnector", return_value=object()), \
                 patch.object(cli, "SearchOrchestrator", return_value=orchestrator(returns_sources=False)), \
                 contextlib.redirect_stdout(output):
                status = cli.main(["research", plan.id, "--database", str(database), "--output", str(report_path), "--max-rounds", "1"])
            self.assertIsNone(status)
            self.assertIn("6 attempted, 6 succeeded, 0 failed", output.getvalue())
            self.assertIn("searches succeeded, but no relevant sources were found", output.getvalue())
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("No evidence items were extracted", report)
            self.assertNotIn("### 1.", report)

    def test_both_connectors_failing_aborts_before_report_generation(self):
        plan = make_plan()
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "research.sqlite"
            output_path = Path(temp) / "research.md"
            store = ResearchStore(database)
            store.save_plan(plan)
            failed_search = orchestrator(tavily_fails=True, hn_fails=True)
            with self.assertRaises(CollectionUnavailableError) as caught:
                run_gap_research(plan, store, budget=SearchBudget(max_search_calls=20, max_pages=0, max_seconds=5),
                    orchestrator=failed_search, max_rounds=1)
            self.assertEqual(caught.exception.searches_attempted, 6)
            self.assertEqual(len(caught.exception.failures), 6)
            self.assertIsInstance(caught.exception.__cause__, ResearchConnectorError)
            self.assertIsInstance(caught.exception.__cause__.__cause__, requests.ConnectionError)

            output = io.StringIO()
            errors = io.StringIO()
            with patch.object(type(cli.settings), "get_llm_config", return_value={"api_key": "test", "base_url": "https://llm.example/v1", "model": "test"}), \
                 patch.object(cli.settings, "tavily_api_key", "test-tavily-key"), \
                 patch.object(cli, "TavilyConnector", return_value=object()), \
                 patch.object(cli, "SearchOrchestrator", return_value=orchestrator(tavily_fails=True, hn_fails=True)), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = cli.main(["research", plan.id, "--database", str(database), "--output", str(output_path), "--max-rounds", "1"])
            self.assertEqual(status, 2)
            self.assertIn("All 6 attempted search requests failed", errors.getvalue())
            self.assertFalse(output_path.exists())

    def test_unexpected_connector_programming_error_still_propagates(self):
        class BrokenConnector:
            name = "broken"
            cost_per_search = 0
            def search(self, **kwargs):
                raise RuntimeError("programming defect")

        plan = make_plan()
        broken = SearchOrchestrator(None, BrokenConnector(), fetcher=object())
        with self.assertRaisesRegex(RuntimeError, "programming defect"):
            self._collect(plan, None, broken)


if __name__ == "__main__":
    unittest.main()
