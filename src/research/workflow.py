from pathlib import Path
import re
from src.config import settings
from src.research.connectors import TavilyConnector
from src.research.extraction import extract_candidate
from src.research.models import ResearchQuestion,ResearchRun
from src.research.report import render_markdown
from src.research.storage import ResearchStore
from src.research.verification import verify_candidate
DEFAULT_PROMPT="Research the operational challenges of landslide early-warning systems in Northeast India."
DEFAULT_QUESTION="What operational challenges affect the delivery of landslide warnings?"
def run_vertical_slice(*,prompt=DEFAULT_PROMPT,question_text=DEFAULT_QUESTION,database_path="data/research.sqlite",output_path=None,connector=None,extractor=extract_candidate,project_id=None):
    if connector is None:
        if not settings.tavily_api_key: raise RuntimeError("TAVILY_API_KEY is not configured.")
        connector=TavilyConnector(settings.tavily_api_key)
    run=ResearchRun(prompt=prompt); question=ResearchQuestion(run_id=run.id,text=question_text); store=ResearchStore(database_path)
    store.save_run(run,project_id=project_id); store.save_question(question)
    sources=connector.search(run_id=run.id,question_id=question.id,query=f"{question_text} {prompt}",max_results=1)
    for source in sources: store.save_source(source)
    if sources:
        record=verify_candidate(run_id=run.id,question=question,source=sources[0],candidate=extractor(question_text,sources[0]),collected_sources=sources)
        store.save_evidence(record)
    saved,qs,ss,es=store.get_run_records(run.id); report=render_markdown(saved,qs,ss,es)
    if output_path is None:
        slug=re.sub(r"[^a-zA-Z0-9]+","-",prompt.lower()).strip("-")[:64] or "research"
        output_path=Path("reports")/slug/(run.id+".md")
    output=Path(output_path); output.parent.mkdir(parents=True,exist_ok=True); output.write_text(report,encoding="utf-8")
    return run.id,output
