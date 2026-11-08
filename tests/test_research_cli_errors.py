import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from openai import APIConnectionError
import httpx

import src.research_cli as cli
from src.research.connectors import HackerNewsConnector, ResearchConnectorError, TavilyConnector
from src.research.models import CompetitionInput, PlanningDraft, QuestionDraft, ResearchQuestion
from src.research.planner import ResearchProviderError, _generate_draft, generate_research_plan
from src.research.storage import ResearchStore
from src.research.models import ProjectCreate
from src.project_store import ProjectStore


def sample_draft():
    return PlanningDraft(
        problem_summary="Manufacturers need to reduce unplanned machine downtime.",
        domain="manufacturing maintenance",
        target_users=["small manufacturing businesses"],
        questions=[
            QuestionDraft(text="What low-cost sensors are used for predictive maintenance?", priority="high", source_categories=["community"], evidence_required="Documented low-cost sensor deployments", decision_impact="Establishes feasible inputs"),
            QuestionDraft(text="Which maintenance practices reduce unexpected machine failures?", priority="high", source_categories=["community"], evidence_required="Measured failure or downtime changes", decision_impact="Identifies effective interventions"),
            QuestionDraft(text="What prevents small manufacturers adopting predictive maintenance?", priority="medium", source_categories=["community"], evidence_required="Reported adoption barriers", decision_impact="Defines implementation constraints"),
        ],
    )


def approved_plan():
    return generate_research_plan(
        "Reduce machine downtime", draft_provider=lambda *_: sample_draft()
    ).model_copy(update={"status": "approved"})


class FailingSession:
    def get(self, *args, **kwargs):
        raise requests.ConnectionError("offline; secret=do-not-display")

    def post(self, *args, **kwargs):
        raise requests.ConnectionError("offline; secret=do-not-display")


class MemoryCache:
    def get(self, key, default=None):
        return default

    def set(self, *args, **kwargs):
        pass


class ResearchCLIErrorTests(unittest.TestCase):
    def test_llm_connection_failure_during_planning_is_actionable_and_chained(self):
        class BrokenLLM:
            def invoke(self, messages):
                request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
                raise APIConnectionError(message="offline", request=request)

        with patch.object(type(cli.settings), "get_llm_config", return_value={"api_key": "test-key", "base_url": "https://llm.example/v1", "model": "test-model"}), \
             patch("src.research.planner.get_structured_llm", return_value=BrokenLLM()):
            with self.assertRaises(ResearchProviderError) as caught:
                _generate_draft("A benchmark problem", CompetitionInput())
        self.assertIsInstance(caught.exception.__cause__, APIConnectionError)
        self.assertIn("Check network access", str(caught.exception))

        with tempfile.TemporaryDirectory() as temp, \
             patch.object(type(cli.settings), "get_llm_config", return_value={"api_key": "test-key", "base_url": "https://llm.example/v1", "model": "test-model"}), \
             patch.object(cli, "generate_research_plan", side_effect=caught.exception):
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                result = cli.main(["plan", "--prompt", "benchmark", "--database", str(Path(temp) / "test.sqlite")])
        self.assertEqual(result, 2)
        self.assertIn("Error: LLM provider request failed", error.getvalue())
        self.assertNotIn("do-not-display", error.getvalue())

    def test_tavily_connection_failure_is_reported_during_collection(self):
        self._assert_collection_connector_failure("tavily")

    def test_hacker_news_connection_failure_is_reported_during_collection(self):
        self._assert_collection_connector_failure("hackernews")

    def _assert_collection_connector_failure(self, connector_name):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "research.sqlite"
            store = ResearchStore(database)
            plan = approved_plan()
            store.save_plan(plan)

            tavily = lambda *_: TavilyConnector("test-key", session=FailingSession(), cache=MemoryCache(), retries=0)
            hackernews = lambda *_: HackerNewsConnector(session=FailingSession(), cache=MemoryCache(), retries=0)
            output = io.StringIO()
            with patch.object(cli.settings, "tavily_api_key", "test-key"), \
                 patch.object(cli, "TavilyConnector", side_effect=tavily), \
                 patch.object(cli, "HackerNewsConnector", side_effect=hackernews), \
                 contextlib.redirect_stdout(output):
                result = cli.main(["collect", plan.id, "--database", str(database)])
            rendered = output.getvalue()
            self.assertIsNone(result)
            self.assertIn(f"Warning [{connector_name}/community]", rendered)
            self.assertNotIn("do-not-display", rendered)

            connector = (TavilyConnector("test-key", session=FailingSession(), cache=MemoryCache(), retries=0)
                         if connector_name == "tavily" else
                         HackerNewsConnector(session=FailingSession(), cache=MemoryCache(), retries=0))
            with self.assertRaises(ResearchConnectorError) as caught:
                connector.search(run_id="r", question_id="q", query="test", category="community", max_results=1)
            self.assertIsInstance(caught.exception.__cause__, requests.ConnectionError)
            self.assertNotIn("do-not-display", str(caught.exception))

    def test_missing_llm_configuration_is_clear_and_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "missing.sqlite"
            output = io.StringIO()
            with patch.object(type(cli.settings), "get_llm_config", return_value={"api_key": "", "base_url": "https://llm.example/v1", "model": "test-model"}), \
                 contextlib.redirect_stderr(output):
                result = cli.main(["plan", "--database", str(database)])
            self.assertEqual(result, 2)
            self.assertIn("LLM configuration is incomplete (missing API key)", output.getvalue())
            self.assertFalse(database.exists())

    def test_successful_mocked_plan_execution_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "research.sqlite"
            approved = approved_plan()
            output = io.StringIO()
            with patch.object(type(cli.settings), "get_llm_config", return_value={"api_key": "test-key", "base_url": "https://llm.example/v1", "model": "test-model"}), \
                 patch.object(cli, "generate_research_plan", return_value=approved.model_copy(update={"status": "draft"})), \
                 patch.object(cli, "review_plan", return_value=approved), \
                 contextlib.redirect_stdout(output):
                result = cli.main(["plan", "--prompt", "Reduce machine downtime", "--database", str(database)])
            self.assertIsNone(result)
            self.assertIn(f"Plan approved: {approved.id}", output.getvalue())
            self.assertEqual(ResearchStore(database).get_plan(approved.id).status, "approved")

    def test_unexpected_connector_programming_error_is_not_swallowed(self):
        class BrokenConnector:
            name = "broken"
            cost_per_search = 0
            def search(self, **kwargs):
                raise RuntimeError("programming defect")

        from src.research.budget import SearchBudget
        from src.research.search import SearchOrchestrator
        question = ResearchQuestion(run_id="run", text="Question?", source_categories=["community"])
        with self.assertRaisesRegex(RuntimeError, "programming defect"):
            SearchOrchestrator(None, BrokenConnector(), fetcher=object()).collect(
                run_id="run", prompt="", questions=[question], budget=SearchBudget()
            )

    def test_project_scoped_research_errors_are_concise_and_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            database=Path(temp)/"scope.sqlite"
            store=ProjectStore(database)
            owner=store.create_project(ProjectCreate(name="Owner"))
            other=store.create_project(ProjectCreate(name="Other"))
            plan=approved_plan()
            store.save_plan(plan,project_id=owner.project_id)
            output=io.StringIO()
            with patch.object(cli,"require_llm_configuration"), contextlib.redirect_stderr(output):
                result=cli.main(["research",plan.id,"--project-id",other.project_id,"--database",str(database)])
            self.assertEqual(result,2)
            self.assertIn("Research run is not associated with the supplied project",output.getvalue())
            self.assertNotIn("Traceback",output.getvalue())

    def test_missing_project_is_reported_before_plan_provider_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            database=Path(temp)/"missing-project.sqlite"
            output=io.StringIO()
            with patch.object(cli,"require_llm_configuration"), \
                 patch.object(cli,"generate_research_plan",side_effect=AssertionError("provider must not run")), \
                 contextlib.redirect_stderr(output):
                result=cli.main(["plan","--project-id","missing-project","--database",str(database)])
            self.assertEqual(result,2)
            self.assertIn("Unknown project: missing-project",output.getvalue())
            self.assertNotIn("Traceback",output.getvalue())

    def test_run_missing_project_is_concise_and_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            database=Path(temp)/"run-scope.sqlite"
            output=io.StringIO()
            with patch.object(cli.settings,"tavily_api_key","test-key"), \
                 patch.object(cli,"require_llm_configuration"), \
                 patch.object(cli,"run_vertical_slice",side_effect=AssertionError("run must not start")), \
                 contextlib.redirect_stderr(output):
                result=cli.main(["run","--project-id","missing-project","--database",str(database)])
            self.assertEqual(result,2)
            self.assertIn("Unknown project: missing-project",output.getvalue())
            self.assertNotIn("Traceback",output.getvalue())

    def test_unexpected_run_programming_error_still_propagates_through_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            database=Path(temp)/"run-programming-error.sqlite"
            project=ProjectStore(database).create_project(ProjectCreate(name="Active"))
            with patch.object(cli.settings,"tavily_api_key","test-key"), \
                 patch.object(cli,"require_llm_configuration"), \
                 patch.object(cli,"run_vertical_slice",side_effect=RuntimeError("unexpected defect")):
                with self.assertRaisesRegex(RuntimeError,"unexpected defect"):
                    cli.main(["run","--project-id",project.project_id,"--database",str(database)])

    def test_design_and_pitch_project_mismatch_errors_are_concise_and_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            database=Path(temp)/"design-pitch-scope.sqlite"
            store=ProjectStore(database)
            owner=store.create_project(ProjectCreate(name="Owner"))
            other=store.create_project(ProjectCreate(name="Other"))
            plan=approved_plan()
            store.save_plan(plan,project_id=owner.project_id)
            for command in ("design","pitch"):
                with self.subTest(command=command):
                    output=io.StringIO()
                    with patch.object(cli,"require_llm_configuration"), contextlib.redirect_stderr(output):
                        result=cli.main([command,plan.id,"--project-id",other.project_id,"--database",str(database)])
                    self.assertEqual(result,2)
                    self.assertIn("Research run is not associated with the supplied project",output.getvalue())
                    self.assertNotIn("Traceback",output.getvalue())


if __name__ == "__main__":
    unittest.main()
