from __future__ import annotations
from pathlib import Path
from src.config import settings
from src.database_config import DEFAULT_DATABASE_PATH
from src.research.budget import SearchBudget
from src.research.connectors import HackerNewsConnector,TavilyConnector
from src.research.models import ResearchPlan
from src.research.safe_fetch import SafeFetcher
from src.research.search import CollectionResult,SearchOrchestrator
from src.research.storage import ResearchStore

def collect_plan_sources(plan:ResearchPlan,store:ResearchStore,*,budget:SearchBudget|None=None,orchestrator:SearchOrchestrator|None=None)->CollectionResult:
    if plan.status!="approved":
        raise ValueError("Research collection requires an approved plan.")
    if orchestrator is None:
        tavily=TavilyConnector(settings.tavily_api_key) if settings.tavily_api_key else None
        orchestrator=SearchOrchestrator(tavily,HackerNewsConnector(),fetcher=SafeFetcher())
    result=orchestrator.collect(run_id=plan.run_id,prompt=plan.brief.problem_statement,questions=plan.questions,budget=budget or SearchBudget())
    result.sources=[store.save_source(source) for source in result.sources]
    return result

def collect_saved_plan(plan_id:str,database_path:str|Path=DEFAULT_DATABASE_PATH,*,budget:SearchBudget|None=None,orchestrator:SearchOrchestrator|None=None)->CollectionResult:
    store=ResearchStore(database_path)
    plan=store.get_plan(plan_id)
    return collect_plan_sources(plan,store,budget=budget,orchestrator=orchestrator)
