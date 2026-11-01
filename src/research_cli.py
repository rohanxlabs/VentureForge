import argparse
from pathlib import Path
import sys
from src.config import settings
from src.research.budget import SearchBudget
from src.research.collection import collect_plan_sources
from src.research.connectors import HackerNewsConnector,TavilyConnector
from src.research.models import CompetitionInput
from src.research.pipeline import run_gap_research
from src.research.pitch import generate_pitch_package,render_pitch_markdown
from src.research.planner import generate_research_plan,review_plan
from src.research.report import render_markdown
from src.research.solution import generate_solution_design,render_solution_markdown
from src.research.safe_fetch import SafeFetcher
from src.research.search import SearchOrchestrator
from src.research.storage import ResearchStore
from src.research.workflow import DEFAULT_PROMPT,DEFAULT_QUESTION,run_vertical_slice

def _parser():
    parser=argparse.ArgumentParser(description="VentureForge research tools.")
    commands=parser.add_subparsers(dest="command")
    run=commands.add_parser("run",help="Run the single-question evidence prototype.")
    run.add_argument("--prompt",default=DEFAULT_PROMPT)
    run.add_argument("--question",default=DEFAULT_QUESTION)
    run.add_argument("--database",default="data/research.sqlite")
    run.add_argument("--output",default=None)
    plan=commands.add_parser("plan",help="Parse a hackathon brief and review research questions.")
    plan.add_argument("--prompt",default=DEFAULT_PROMPT)
    plan.add_argument("--competition-name",default=None)
    plan.add_argument("--rules-file",default=None)
    plan.add_argument("--judging-criterion",action="append",default=[])
    plan.add_argument("--team-size",type=int,default=None)
    plan.add_argument("--time-limit-hours",type=float,default=None)
    plan.add_argument("--required-technology",action="append",default=[])
    plan.add_argument("--budget",default=None)
    plan.add_argument("--demo-requirement",action="append",default=[])
    plan.add_argument("--database",default="data/research.sqlite")
    review=commands.add_parser("plan-review",help="Resume review of a saved draft plan.")
    review.add_argument("plan_id")
    review.add_argument("--database",default="data/research.sqlite")
    collect=commands.add_parser("collect",help="Search sources for an approved plan.")
    collect.add_argument("plan_id")
    collect.add_argument("--database",default="data/research.sqlite")
    collect.add_argument("--max-searches",type=int,default=8)
    collect.add_argument("--max-tavily-credits",type=int,default=8)
    collect.add_argument("--max-pages",type=int,default=20)
    collect.add_argument("--max-seconds",type=float,default=180)
    collect.add_argument("--workers",type=int,default=4)
    research=commands.add_parser("research",help="Collect, verify, and follow up on evidence gaps for an approved plan.")
    research.add_argument("plan_id")
    research.add_argument("--database",default="data/research.sqlite")
    research.add_argument("--max-searches",type=int,default=8)
    research.add_argument("--max-tavily-credits",type=int,default=8)
    research.add_argument("--max-pages",type=int,default=20)
    research.add_argument("--max-seconds",type=float,default=180)
    research.add_argument("--max-rounds",type=int,default=2)
    research.add_argument("--workers",type=int,default=4)
    research.add_argument("--output",default=None)
    design=commands.add_parser("design",help="Compare solution options and draft an MVP execution plan.")
    design.add_argument("plan_id")
    design.add_argument("--database",default="data/research.sqlite")
    design.add_argument("--output",default=None)
    pitch=commands.add_parser("pitch",help="Map the solution to judging criteria and draft a presentation/demo package.")
    pitch.add_argument("plan_id")
    pitch.add_argument("--design-id",default=None)
    pitch.add_argument("--database",default="data/research.sqlite")
    pitch.add_argument("--output",default=None)
    return parser

def main(argv=None):
    args_in=list(sys.argv[1:] if argv is None else argv)
    if not args_in: args_in=["run"]
    elif args_in[0].startswith("-") and args_in[0] not in {"-h","--help"}: args_in.insert(0,"run")
    parser=_parser(); args=parser.parse_args(args_in)
    if args.command=="run":
        run_id,output=run_vertical_slice(prompt=args.prompt,question_text=args.question,database_path=args.database,output_path=args.output)
        print(f"Research run: {run_id}\nReport: {output}")
        return
    store=ResearchStore(args.database)
    if args.command=="plan":
        rules=Path(args.rules_file).read_text(encoding="utf-8") if args.rules_file else None
        competition=CompetitionInput(competition_name=args.competition_name,rules_text=rules,
            judging_criteria=args.judging_criterion,team_size=args.team_size,time_limit_hours=args.time_limit_hours,
            required_technologies=args.required_technology,budget=args.budget,demo_requirements=args.demo_requirement)
        plan=generate_research_plan(args.prompt,competition)
        store.save_plan(plan)
        print(f"Saved draft plan: {plan.id} (run {plan.run_id})")
        try: reviewed=review_plan(plan)
        except EOFError:
            print(f"Review ended; draft remains saved: {plan.id}")
            return
        store.update_plan(reviewed)
        print(f"Plan {reviewed.status}: {reviewed.id} (run {reviewed.run_id})")
        return
    if args.command=="plan-review":
        plan=store.get_plan(args.plan_id)
        try: reviewed=review_plan(plan)
        except EOFError:
            print(f"Review ended; draft remains saved: {plan.id}")
            return
        store.update_plan(reviewed)
        print(f"Plan {reviewed.status}: {reviewed.id} (run {reviewed.run_id})")
        return
    if args.command=="collect":
        plan=store.get_plan(args.plan_id)
        budget=SearchBudget(max_search_calls=args.max_searches,max_tavily_credits=args.max_tavily_credits,
            max_pages=args.max_pages,max_seconds=args.max_seconds)
        tavily=TavilyConnector(settings.tavily_api_key) if settings.tavily_api_key else None
        orchestrator=SearchOrchestrator(tavily,
            HackerNewsConnector(),fetcher=SafeFetcher(),max_workers=args.workers)
        result=collect_plan_sources(plan,store,budget=budget,orchestrator=orchestrator)
        print(f"Collected {len(result.sources)} unique sources for run {plan.run_id}.")
        print(f"Search calls: {result.search_calls}; Tavily credits: {result.tavily_credits}; pages fetched: {result.pages_fetched}; elapsed: {result.elapsed_seconds:.1f}s")
        for failure in result.failures:
            print(f"Warning [{failure.connector}/{failure.category}]: {failure.reason}")
        return
    if args.command=="research":
        plan=store.get_plan(args.plan_id)
        budget=SearchBudget(max_search_calls=args.max_searches,max_tavily_credits=args.max_tavily_credits,
            max_pages=args.max_pages,max_seconds=args.max_seconds)
        tavily=TavilyConnector(settings.tavily_api_key) if settings.tavily_api_key else None
        orchestrator=SearchOrchestrator(tavily,HackerNewsConnector(),fetcher=SafeFetcher(),max_workers=args.workers)
        execution=run_gap_research(plan,store,budget=budget,orchestrator=orchestrator,max_rounds=args.max_rounds)
        run,questions,sources,evidence=store.get_run_records(plan.run_id)
        report=render_markdown(run,questions,sources,evidence,execution.gap_report)
        output=Path(args.output) if args.output else Path("reports")/plan.run_id/"research.md"
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(report,encoding="utf-8")
        print(f"Research rounds: {execution.rounds_run}; sources collected: {execution.sources_collected}; evidence items processed: {execution.evidence_extracted}")
        print(f"Coverage: {'sufficient by configured rule' if execution.gap_report.complete else 'gaps remain'}; report: {output}")
        for failure in execution.failures:
            print(f"Warning [{failure.connector}/{failure.category}]: {failure.reason}")
        return
    if args.command=="design":
        plan=store.get_plan(args.plan_id)
        design=generate_solution_design(plan,store)
        store.save_solution_design(design)
        output=Path(args.output) if args.output else Path("reports")/plan.run_id/"solution-design.md"
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(render_solution_markdown(design),encoding="utf-8")
        recommended=next(option.name for option in design.alternatives if option.id==design.recommended_option_id)
        print(f"Saved draft solution design: {design.id}; recommended option: {recommended}; report: {output}")
        if design.capacity_warning:
            print("Capacity warning: " + design.capacity_warning)
        return
    if args.command=="pitch":
        plan=store.get_plan(args.plan_id)
        design=store.get_solution_design(args.design_id) if args.design_id else store.get_latest_solution_design(plan.run_id)
        package=generate_pitch_package(plan,design,store)
        store.save_pitch_package(package)
        _,_,sources,evidence=store.get_run_records(plan.run_id)
        report=render_pitch_markdown(package,evidence,sources)
        output=Path(args.output) if args.output else Path("reports")/plan.run_id/"pitch-package.md"
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(report,encoding="utf-8")
        print(f"Saved draft pitch package: {package.id}; mapped {len(package.rubric_mapping)} criteria; report: {output}")
        return
    parser.error("Choose run, plan, plan-review, collect, research, design, or pitch.")

if __name__=="__main__": main()
