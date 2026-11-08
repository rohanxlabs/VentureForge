import argparse
import json
from pathlib import Path
import sqlite3
import sys
from pydantic import ValidationError
from src.config import settings
from src.research.budget import SearchBudget
from src.research.collection import collect_plan_sources
from src.research.connectors import HackerNewsConnector,ResearchConnectorError,TavilyConnector
from src.research.models import CompetitionInput
from src.project_store import ProjectStore
from src.research.models import (
    PROJECT_STAGE_NAMES, ApprovalEventStatus, ArtifactCreate, DecisionCreate,
    DecisionEvidenceRelationship, DecisionOptionCreate, DecisionUpdate,
    ProjectCreate, ProjectStageKey, ProjectUpdate, WorkflowRunCreate,
)
from src.research.pipeline import CollectionUnavailableError,run_gap_research
from src.research.pitch import generate_pitch_package,render_pitch_markdown
from src.research.planner import ResearchProviderError,generate_research_plan,require_llm_configuration,review_plan
from src.research.report import render_markdown
from src.research.solution import generate_solution_design,render_solution_markdown
from src.research.safe_fetch import SafeFetcher
from src.research.search import SearchOrchestrator
from src.research.storage import ProjectAssociationError, ProjectNotFoundError, ResearchStore
from src.research.workflow import DEFAULT_PROMPT,DEFAULT_QUESTION,run_vertical_slice

def _parser():
    parser=argparse.ArgumentParser(description="VentureForge research tools.")
    commands=parser.add_subparsers(dest="command")
    run=commands.add_parser("run",help="Run the single-question evidence prototype.")
    run.add_argument("--prompt",default=DEFAULT_PROMPT)
    run.add_argument("--question",default=DEFAULT_QUESTION)
    run.add_argument("--database",default="data/research.sqlite")
    run.add_argument("--output",default=None)
    run.add_argument("--project-id",default=None)
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
    plan.add_argument("--project-id",default=None)
    review=commands.add_parser("plan-review",help="Resume review of a saved draft plan.")
    review.add_argument("plan_id")
    review.add_argument("--database",default="data/research.sqlite")
    review.add_argument("--project-id",default=None)
    collect=commands.add_parser("collect",help="Search sources for an approved plan.")
    collect.add_argument("plan_id")
    collect.add_argument("--database",default="data/research.sqlite")
    collect.add_argument("--project-id",default=None)
    collect.add_argument("--max-searches",type=int,default=8)
    collect.add_argument("--max-tavily-credits",type=int,default=8)
    collect.add_argument("--max-pages",type=int,default=20)
    collect.add_argument("--max-seconds",type=float,default=180)
    collect.add_argument("--workers",type=int,default=4)
    research=commands.add_parser("research",help="Collect, verify, and follow up on evidence gaps for an approved plan.")
    research.add_argument("plan_id")
    research.add_argument("--database",default="data/research.sqlite")
    research.add_argument("--project-id",default=None)
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
    design.add_argument("--project-id",default=None)
    design.add_argument("--output",default=None)
    pitch=commands.add_parser("pitch",help="Map the solution to judging criteria and draft a presentation/demo package.")
    pitch.add_argument("plan_id")
    pitch.add_argument("--design-id",default=None)
    pitch.add_argument("--database",default="data/research.sqlite")
    pitch.add_argument("--project-id",default=None)
    pitch.add_argument("--output",default=None)
    project=commands.add_parser("project",help="Manage persistent hackathon projects.")
    project_commands=project.add_subparsers(dest="project_command",required=True)
    problem=project_commands.add_parser("problem",help="Manage project problem intelligence (P1-P3).")
    problem_commands=problem.add_subparsers(dest="problem_command",required=True)
    frame=problem_commands.add_parser("frame",help="Create a P1 frame from a JSON file of structured user input.")
    frame.add_argument("project_id"); frame.add_argument("input_json"); frame.add_argument("--database",default="data/research.sqlite")
    frame_show=problem_commands.add_parser("show-frame",help="Show the latest problem frame.")
    frame_show.add_argument("project_id"); frame_show.add_argument("--database",default="data/research.sqlite")
    frame_approve=problem_commands.add_parser("approve-frame",help="Record human approval or request changes for a P1 frame.")
    frame_approve.add_argument("project_id"); frame_approve.add_argument("artifact_id"); frame_approve.add_argument("decision_id")
    frame_approve.add_argument("--status",choices=["approved","changes_requested","rejected"],required=True)
    frame_approve.add_argument("--comment",default=""); frame_approve.add_argument("--database",default="data/research.sqlite")
    pi_plan=problem_commands.add_parser("plan",help="Create a structured research plan from the approved P1 frame.")
    pi_plan.add_argument("project_id"); pi_plan.add_argument("--database",default="data/research.sqlite")
    plan_approve=problem_commands.add_parser("approve-plan",help="Approve an existing P2 research plan.")
    plan_approve.add_argument("project_id"); plan_approve.add_argument("artifact_id"); plan_approve.add_argument("decision_id")
    plan_approve.add_argument("--database",default="data/research.sqlite")
    report_approve=problem_commands.add_parser("approve-report",help="Approve a saved P2 research report and complete P2.")
    report_approve.add_argument("project_id"); report_approve.add_argument("artifact_id"); report_approve.add_argument("decision_id")
    report_approve.add_argument("--database",default="data/research.sqlite")
    pi_assess=problem_commands.add_parser("assess",help="Create a conservative P3 assessment from a project research run.")
    pi_assess.add_argument("project_id"); pi_assess.add_argument("run_id"); pi_assess.add_argument("--database",default="data/research.sqlite")
    assessment_approve=problem_commands.add_parser("approve-assessment",help="Accept a P3 assessment and complete P3.")
    assessment_approve.add_argument("project_id"); assessment_approve.add_argument("artifact_id"); assessment_approve.add_argument("decision_id")
    assessment_approve.add_argument("--status",choices=["approved","changes_requested","rejected"],default="approved")
    assessment_approve.add_argument("--comment",default=""); assessment_approve.add_argument("--database",default="data/research.sqlite")
    create=project_commands.add_parser("create",help="Create a project.")
    create.add_argument("--name",required=True)
    create.add_argument("--description",default=None)
    create.add_argument("--problem-domain",default=None)
    create.add_argument("--database",default="data/research.sqlite")
    listing=project_commands.add_parser("list",help="List projects.")
    listing.add_argument("--active-only",action="store_true",help="Hide archived projects.")
    listing.add_argument("--database",default="data/research.sqlite")
    show=project_commands.add_parser("show",help="Show a project.")
    show.add_argument("project_id")
    show.add_argument("--database",default="data/research.sqlite")
    update=project_commands.add_parser("update",help="Update project metadata.")
    update.add_argument("project_id")
    update.add_argument("--name",default=None)
    update.add_argument("--description",default=None)
    update.add_argument("--problem-domain",default=None)
    update.add_argument("--database",default="data/research.sqlite")
    archive=project_commands.add_parser("archive",help="Archive a project.")
    archive.add_argument("project_id")
    archive.add_argument("--database",default="data/research.sqlite")
    stage=project_commands.add_parser("stage",help="Manage P1-P8 project stages.")
    stage_commands=stage.add_subparsers(dest="stage_command",required=True)
    stage_list=stage_commands.add_parser("list",help="List a project's stages.")
    stage_list.add_argument("project_id")
    stage_list.add_argument("--database",default="data/research.sqlite")
    for action, help_text in (("start", "Start a pending stage."), ("resume", "Resume a blocked stage."),
                              ("complete", "Complete an in-progress stage."), ("block", "Block an in-progress stage."),
                              ("skip", "Skip a stage with a reason."), ("reopen", "Reopen a completed or skipped stage.")):
        action_parser=stage_commands.add_parser(action,help=help_text)
        action_parser.add_argument("project_id")
        action_parser.add_argument("stage_key")
        action_parser.add_argument("--actor",default="user")
        action_parser.add_argument("--database",default="data/research.sqlite")
        if action in {"block", "reopen"}:
            action_parser.add_argument("--note",default=None)
        if action == "skip":
            action_parser.add_argument("--reason",required=True)
    history=stage_commands.add_parser("history",help="Show stage transition history.")
    history.add_argument("project_id")
    history.add_argument("stage_key",nargs="?")
    history.add_argument("--database",default="data/research.sqlite")
    decision=project_commands.add_parser("decision",help="Manage project decisions and approvals.")
    decision_commands=decision.add_subparsers(dest="decision_command",required=True)
    decision_create=decision_commands.add_parser("create",help="Create a decision.")
    decision_create.add_argument("project_id")
    decision_create.add_argument("--title",required=True)
    decision_create.add_argument("--description",default="")
    decision_create.add_argument("--type",dest="decision_type",default="general")
    decision_create.add_argument("--stage",choices=[stage.value for stage in ProjectStageKey],default=None)
    decision_create.add_argument("--database",default="data/research.sqlite")
    decision_list=decision_commands.add_parser("list",help="List decisions for a project.")
    decision_list.add_argument("project_id")
    decision_list.add_argument("--database",default="data/research.sqlite")
    decision_show=decision_commands.add_parser("show",help="Show a decision and its current approval state.")
    decision_show.add_argument("project_id")
    decision_show.add_argument("decision_id")
    decision_show.add_argument("--database",default="data/research.sqlite")
    decision_update=decision_commands.add_parser("update",help="Update an unapproved decision.")
    decision_update.add_argument("project_id")
    decision_update.add_argument("decision_id")
    decision_update.add_argument("--title",default=None)
    decision_update.add_argument("--description",default=None)
    decision_update.add_argument("--type",dest="decision_type",default=None)
    decision_update.add_argument("--stage",choices=[stage.value for stage in ProjectStageKey],default=None)
    decision_update.add_argument("--database",default="data/research.sqlite")
    option=decision_commands.add_parser("option",help="Add, list, recommend or select decision options.")
    option_commands=option.add_subparsers(dest="option_command",required=True)
    option_add=option_commands.add_parser("add",help="Add an option.")
    option_add.add_argument("project_id")
    option_add.add_argument("decision_id")
    option_add.add_argument("--title",required=True)
    option_add.add_argument("--description",default="")
    option_add.add_argument("--advantage",action="append",default=[])
    option_add.add_argument("--disadvantage",action="append",default=[])
    option_add.add_argument("--assumption",action="append",default=[])
    option_add.add_argument("--estimated-cost",default=None)
    option_add.add_argument("--feasibility",default=None)
    option_add.add_argument("--database",default="data/research.sqlite")
    option_list=option_commands.add_parser("list",help="List options.")
    option_list.add_argument("project_id")
    option_list.add_argument("decision_id")
    option_list.add_argument("--database",default="data/research.sqlite")
    for choice in ("recommend", "select"):
        choose=option_commands.add_parser(choice,help=f"{choice.title()} an option.")
        choose.add_argument("project_id")
        choose.add_argument("decision_id")
        choose.add_argument("option_id")
        choose.add_argument("--database",default="data/research.sqlite")
    evidence=decision_commands.add_parser("evidence",help="Link project-owned evidence to a decision.")
    evidence_commands=evidence.add_subparsers(dest="evidence_command",required=True)
    evidence_link=evidence_commands.add_parser("link",help="Link evidence.")
    evidence_link.add_argument("project_id")
    evidence_link.add_argument("decision_id")
    evidence_link.add_argument("evidence_id")
    evidence_link.add_argument("--relationship",choices=[item.value for item in DecisionEvidenceRelationship],required=True)
    evidence_link.add_argument("--explanation",default="")
    evidence_link.add_argument("--database",default="data/research.sqlite")
    evidence_list=evidence_commands.add_parser("list",help="List linked evidence.")
    evidence_list.add_argument("project_id")
    evidence_list.add_argument("decision_id")
    evidence_list.add_argument("--database",default="data/research.sqlite")
    for action, status in (("approve",ApprovalEventStatus.APPROVED), ("reject",ApprovalEventStatus.REJECTED),
                           ("request-changes",ApprovalEventStatus.CHANGES_REQUESTED),
                           ("revoke",ApprovalEventStatus.REVOKED)):
        approve=decision_commands.add_parser(action,help=f"Record a {status.value} decision event.")
        approve.add_argument("project_id")
        approve.add_argument("decision_id")
        approve.add_argument("--comment",default="")
        approve.add_argument("--actor",default="user")
        approve.add_argument("--database",default="data/research.sqlite")
    decision_history=decision_commands.add_parser("history",help="Show approval event history.")
    decision_history.add_argument("project_id")
    decision_history.add_argument("decision_id")
    decision_history.add_argument("--database",default="data/research.sqlite")
    artifact=project_commands.add_parser("artifact",help="Manage project artifacts and versions.")
    artifact_commands=artifact.add_subparsers(dest="artifact_command",required=True)
    artifact_create=artifact_commands.add_parser("create")
    artifact_create.add_argument("project_id"); artifact_create.add_argument("--stage",choices=[s.value for s in ProjectStageKey])
    artifact_create.add_argument("--decision-id",default=None)
    artifact_create.add_argument("--type",dest="artifact_type",required=True); artifact_create.add_argument("--title",required=True)
    artifact_create.add_argument("--description",default=""); artifact_create.add_argument("--content",default=None)
    artifact_create.add_argument("--storage-ref",default=None); artifact_create.add_argument("--database",default="data/research.sqlite")
    artifact_list=artifact_commands.add_parser("list"); artifact_list.add_argument("project_id"); artifact_list.add_argument("--database",default="data/research.sqlite")
    artifact_show=artifact_commands.add_parser("show"); artifact_show.add_argument("project_id"); artifact_show.add_argument("artifact_id"); artifact_show.add_argument("--database",default="data/research.sqlite")
    artifact_approve=artifact_commands.add_parser("approve"); artifact_approve.add_argument("project_id"); artifact_approve.add_argument("artifact_id"); artifact_approve.add_argument("--comment",default=""); artifact_approve.add_argument("--actor",default="user"); artifact_approve.add_argument("--database",default="data/research.sqlite")
    workflow=project_commands.add_parser("workflow",help="Inspect project workflow records.")
    workflow_commands=workflow.add_subparsers(dest="workflow_command",required=True)
    workflow_start=workflow_commands.add_parser("start"); workflow_start.add_argument("project_id"); workflow_start.add_argument("stage",choices=[s.value for s in ProjectStageKey]); workflow_start.add_argument("--type",dest="workflow_type",required=True); workflow_start.add_argument("--input-ref",default=None); workflow_start.add_argument("--database",default="data/research.sqlite")
    workflow_list=workflow_commands.add_parser("list"); workflow_list.add_argument("project_id"); workflow_list.add_argument("--database",default="data/research.sqlite")
    workflow_show=workflow_commands.add_parser("show"); workflow_show.add_argument("project_id"); workflow_show.add_argument("workflow_run_id"); workflow_show.add_argument("--database",default="data/research.sqlite")
    for lifecycle in ("complete","fail","interrupt"):
        action=workflow_commands.add_parser(lifecycle); action.add_argument("project_id"); action.add_argument("workflow_run_id"); action.add_argument("--database",default="data/research.sqlite")
        if lifecycle=="fail": action.add_argument("--summary",required=True)
    project_research=project_commands.add_parser("research",help="Inspect or explicitly associate project research runs.")
    project_research_commands=project_research.add_subparsers(dest="project_research_command",required=True)
    research_list=project_research_commands.add_parser("list"); research_list.add_argument("project_id"); research_list.add_argument("--database",default="data/research.sqlite")
    research_attach=project_research_commands.add_parser("attach"); research_attach.add_argument("project_id"); research_attach.add_argument("run_id"); research_attach.add_argument("--database",default="data/research.sqlite")
    return parser

def main(argv=None):
    args_in=list(sys.argv[1:] if argv is None else argv)
    if not args_in: args_in=["run"]
    elif args_in[0].startswith("-") and args_in[0] not in {"-h","--help"}: args_in.insert(0,"run")
    parser=_parser(); args=parser.parse_args(args_in)
    try:
        _dispatch(args)
    except ResearchProviderError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except ResearchConnectorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except CollectionUnavailableError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors())
        print(f"Error: invalid input: {details}", file=sys.stderr)
        return 2
    except (ProjectNotFoundError, ProjectAssociationError) as exc:
        print(f"Error: {exc.args[0] if isinstance(exc, KeyError) else exc}", file=sys.stderr)
        return 2
    except (KeyError, ValueError) as exc:
        if args.command != "project":
            raise
        message = exc.args[0] if isinstance(exc, KeyError) else str(exc)
        print(f"Error: {message}", file=sys.stderr)
        return 2
    except sqlite3.DatabaseError as exc:
        if args.command != "project":
            raise
        print(f"Error: project storage operation failed ({exc.__class__.__name__}). Check the database path and project data.", file=sys.stderr)
        return 2


def _print_project(project):
    print(f"Project: {project.project_id}")
    print(f"Name: {project.name}")
    print(f"Status: {project.status.value}")
    print(f"Problem domain: {project.problem_domain or '(not set)'}")
    print(f"Description: {project.description or '(not set)'}")
    print(f"Current stage: {project.current_stage or '(not initialized)'}")
    print(f"Created: {project.created_at.isoformat()}")
    print(f"Updated: {project.updated_at.isoformat()}")
    if project.archived_at:
        print(f"Archived: {project.archived_at.isoformat()}")

def _print_collection_outcome(*,attempted:int,succeeded:int,failed:int,sources:int)->None:
    print(f"Search requests: {attempted} attempted, {succeeded} succeeded, {failed} failed.")
    if attempted and failed == attempted and succeeded == 0:
        print("Collection outcome: all attempted searches failed; no sources were collected.")
    elif succeeded and sources == 0:
        print("Collection outcome: searches succeeded, but no relevant sources were found.")
    elif failed and succeeded:
        print("Collection outcome: partial connector failure; successful search results were retained.")
    elif succeeded:
        print("Collection outcome: search completed successfully.")
    else:
        print("Collection outcome: no searches were attempted.")

def _dispatch(args):
    if args.command=="project":
        store=ProjectStore(args.database)
        if args.project_command=="problem":
            from src.problem_intelligence import (ProblemInput, ProblemFrame, create_frame, save_frame,
                create_research_plan, assess_problem, save_assessment, approve_frame, approve_assessment)
            if args.problem_command=="frame":
                payload=json.loads(Path(args.input_json).read_text(encoding="utf-8"))
                artifact,decision=save_frame(store,args.project_id,create_frame(ProblemInput.model_validate(payload)))
                print(f"Saved P1 frame draft: {artifact.artifact_id}; decision: {decision.decision_id}")
                return
            if args.problem_command=="show-frame":
                items=[a for a in store.list_project_artifacts(args.project_id) if a.artifact_type=="problem_frame"]
                if not items: raise ValueError("No problem frame found.")
                print(json.dumps(json.loads(items[-1].content),indent=2,ensure_ascii=False))
                print(f"Artifact: {items[-1].artifact_id} v{items[-1].version}; approval: {store.get_artifact_approval_status(args.project_id,items[-1].artifact_id)}")
                return
            if args.problem_command=="approve-frame":
                result=approve_frame(store,args.project_id,args.artifact_id,args.decision_id,
                    ApprovalEventStatus(args.status),args.comment)
                print(f"Recorded P1 {result.status.value} event: {result.approval_id}")
                return
            if args.problem_command=="plan":
                plan,artifact,decision=create_research_plan(store,args.project_id)
                print(f"Saved P2 research plan draft: {artifact.artifact_id}; run: {plan.run_id}; decision: {decision.decision_id}")
                for i,q in enumerate(plan.questions,1): print(f"{i}. [{q.priority.value}] {q.text}")
                return
            if args.problem_command=="approve-plan":
                from datetime import datetime, timezone
                artifact=store.get_artifact(args.project_id,args.artifact_id)
                if artifact.artifact_type!="research_plan" or artifact.stage_key!=ProjectStageKey.P2: raise ValueError("Artifact is not a P2 research plan.")
                plan=store.get_plan(json.loads(artifact.content)["id"])
                store.update_plan(plan.model_copy(update={"status":"approved","approved_at":datetime.now(timezone.utc)}))
                store.record_artifact_approval(args.project_id,args.artifact_id,ApprovalEventStatus.APPROVED)
                store.record_decision_approval(args.project_id,args.decision_id,ApprovalEventStatus.APPROVED)
                print(f"Approved research plan {plan.id}; research run: {plan.run_id}")
                return
            if args.problem_command=="approve-report":
                artifact=store.get_artifact(args.project_id,args.artifact_id)
                if artifact.artifact_type!="research_report" or artifact.stage_key!=ProjectStageKey.P2: raise ValueError("Artifact is not a P2 research report.")
                store.record_artifact_approval(args.project_id,args.artifact_id,ApprovalEventStatus.APPROVED)
                store.record_decision_approval(args.project_id,args.decision_id,ApprovalEventStatus.APPROVED)
                store.complete_project_stage(args.project_id,ProjectStageKey.P2,note="Research report approved by human.")
                print(f"Approved research report {artifact.artifact_id}; P2 completed.")
                return
            if args.problem_command=="assess":
                artifact,decision=save_assessment(store,args.project_id,assess_problem(store,args.project_id,args.run_id))
                print(f"Saved P3 assessment draft: {artifact.artifact_id}; decision: {decision.decision_id}")
                print(artifact.content)
                return
            if args.problem_command=="approve-assessment":
                result=approve_assessment(store,args.project_id,args.artifact_id,args.decision_id,
                    ApprovalEventStatus(args.status),args.comment)
                print(f"Recorded P3 {result.status.value} event: {result.approval_id}")
                return
        if args.project_command=="decision":
            if args.decision_command=="create":
                created=store.create_decision(DecisionCreate(project_id=args.project_id,
                    stage_key=ProjectStageKey(args.stage) if args.stage else None,title=args.title,
                    description=args.description,decision_type=args.decision_type))
                print(f"Created decision: {created.decision_id} ({created.title})")
                return
            if args.decision_command=="list":
                decisions=store.list_project_decisions(args.project_id)
                if not decisions:
                    print("No decisions found.")
                    return
                for item in decisions:
                    print(f"{item.decision_id}\t{item.stage_key.value if item.stage_key else '-'}\t{item.status}\t{item.title}")
                return
            if args.decision_command=="show":
                item=store.get_decision(args.project_id,args.decision_id)
                print(f"Decision: {item.decision_id}\nTitle: {item.title}\nProject: {item.project_id}")
                print(f"Stage: {item.stage_key.value if item.stage_key else '(not set)'}\nType: {item.decision_type}")
                print(f"Status: {item.status}\nApproval: {store.get_decision_approval_status(args.project_id,item.decision_id)}")
                print(f"Description: {item.description or '(not set)'}")
                for option_item in store.list_decision_options(args.project_id,item.decision_id):
                    labels=" ".join(label for label,flag in (("recommended",option_item.is_recommended),("selected",option_item.is_selected)) if flag)
                    print(f"Option {option_item.option_id}: {option_item.title}" + (f" [{labels}]" if labels else ""))
                return
            if args.decision_command=="update":
                fields={"title":args.title,"description":args.description,"decision_type":args.decision_type}
                if args.stage is not None:
                    fields["stage_key"]=ProjectStageKey(args.stage)
                fields={key:value for key,value in fields.items() if value is not None}
                if not fields:
                    raise ValueError("Provide at least one decision field to update.")
                item=store.update_decision(args.project_id,args.decision_id,DecisionUpdate.model_validate(fields))
                print(f"Updated decision: {item.decision_id} ({item.title})")
                return
            if args.decision_command=="option":
                if args.option_command=="add":
                    item=store.add_decision_option(args.project_id,args.decision_id,DecisionOptionCreate(
                        title=args.title,description=args.description,advantages=args.advantage,
                        disadvantages=args.disadvantage,assumptions=args.assumption,
                        estimated_cost=args.estimated_cost,feasibility=args.feasibility))
                    print(f"Added option: {item.option_id} ({item.title})")
                    return
                if args.option_command=="list":
                    for item in store.list_decision_options(args.project_id,args.decision_id):
                        labels=" ".join(label for label,flag in (("recommended",item.is_recommended),("selected",item.is_selected)) if flag)
                        print(f"{item.option_id}\t{item.title}" + (f" [{labels}]" if labels else ""))
                    return
                method=store.recommend_decision_option if args.option_command=="recommend" else store.select_decision_option
                item=method(args.project_id,args.decision_id,args.option_id)
                verb="Recommended" if args.option_command=="recommend" else "Selected"
                print(f"{verb} option: {item.option_id} ({item.title})")
                return
            if args.decision_command=="evidence":
                if args.evidence_command=="link":
                    item=store.link_decision_evidence(args.project_id,args.decision_id,args.evidence_id,
                        DecisionEvidenceRelationship(args.relationship),args.explanation)
                    print(f"Linked evidence {item.evidence_id} as {item.relationship_type.value}.")
                    return
                items=store.list_decision_evidence(args.project_id,args.decision_id)
                for item in items:
                    print(f"{item.evidence_id}\t{item.relationship_type.value}\t{item.explanation}")
                if not items:
                    print("No evidence links found.")
                return
            if args.decision_command in {"approve","reject","request-changes","revoke"}:
                status={"approve":ApprovalEventStatus.APPROVED,"reject":ApprovalEventStatus.REJECTED,
                        "request-changes":ApprovalEventStatus.CHANGES_REQUESTED,"revoke":ApprovalEventStatus.REVOKED}[args.decision_command]
                item=store.record_decision_approval(args.project_id,args.decision_id,status,
                    actor=args.actor,comment=args.comment)
                print(f"Recorded decision approval event: {item.status.value} ({item.approval_id})")
                return
            if args.decision_command=="history":
                items=store.get_decision_approval_history(args.project_id,args.decision_id)
                if not items:
                    print("No approval events; decision is pending approval.")
                    return
                for item in items:
                    print(f"{item.created_at.isoformat()}\t{item.status.value}\t{item.actor}\t{item.comment}")
                return
        if args.project_command=="artifact":
            if args.artifact_command=="create":
                item=store.create_artifact(ArtifactCreate(project_id=args.project_id,
                    stage_key=ProjectStageKey(args.stage) if args.stage else None,decision_id=args.decision_id,artifact_type=args.artifact_type,
                    title=args.title,description=args.description,content=args.content,storage_ref=args.storage_ref))
                print(f"Created artifact: {item.artifact_id} (version {item.version})")
                return
            if args.artifact_command=="list":
                for item in store.list_project_artifacts(args.project_id): print(f"{item.artifact_id}\tv{item.version}\t{item.status.value}\t{item.title}")
                return
            if args.artifact_command=="show":
                item=store.get_artifact(args.project_id,args.artifact_id)
                print(f"Artifact: {item.artifact_id}\nVersion: {item.version}\nTitle: {item.title}\nType: {item.artifact_type}\nDecision: {item.decision_id or '(not linked)'}\nApproval: {store.get_artifact_approval_status(args.project_id,item.artifact_id)}")
                if item.content is not None: print(item.content)
                elif item.storage_ref: print(f"Storage reference: {item.storage_ref}")
                return
            if args.artifact_command=="approve":
                item=store.record_artifact_approval(args.project_id,args.artifact_id,ApprovalEventStatus.APPROVED,actor=args.actor,comment=args.comment)
                print(f"Recorded artifact approval: {item.approval_id}")
                return
        if args.project_command=="workflow":
            if args.workflow_command=="start":
                item=store.start_workflow_run(WorkflowRunCreate(project_id=args.project_id,stage_key=ProjectStageKey(args.stage),workflow_type=args.workflow_type,input_ref=args.input_ref))
            elif args.workflow_command=="list":
                for item in store.list_project_workflows(args.project_id): print(f"{item.workflow_run_id}\t{item.stage_key.value}\t{item.status.value}\t{item.workflow_type}")
                return
            elif args.workflow_command=="show":
                item=store.get_workflow_run(args.project_id,args.workflow_run_id)
                print(f"Workflow: {item.workflow_run_id}\nStage: {item.stage_key.value}\nType: {item.workflow_type}\nStatus: {item.status.value}\nError: {item.error_summary or '(none)'}")
                for event in store.get_workflow_history(args.project_id,item.workflow_run_id): print(f"{event.changed_at.isoformat()}\t{event.to_status.value}\t{event.note}")
                return
            elif args.workflow_command=="complete": item=store.complete_workflow_run(args.project_id,args.workflow_run_id)
            elif args.workflow_command=="fail": item=store.fail_workflow_run(args.project_id,args.workflow_run_id,args.summary)
            else: item=store.interrupt_workflow_run(args.project_id,args.workflow_run_id)
            print(f"Workflow {item.workflow_run_id}: {item.status.value}")
            return
        if args.project_command=="research":
            if args.project_research_command=="attach":
                store.associate_research_run(args.project_id,args.run_id)
                print(f"Associated research run {args.run_id} with project {args.project_id}.")
                return
            items=store.list_project_research_runs(args.project_id)
            if not items: print("No project research runs found.")
            for item in items: print(f"{item.run_id}\t{item.created_at.isoformat()}\tQ:{item.question_count} S:{item.source_count} E:{item.evidence_count}\t{item.prompt}")
            return
        if args.project_command=="stage":
            if args.stage_command=="list":
                project=store.get_project(args.project_id)
                for item in store.list_project_stages(args.project_id):
                    current = " (current)" if item.stage_key.value == project.current_stage else ""
                    print(f"{item.stage_key.value}\t{PROJECT_STAGE_NAMES[item.stage_key]}\t{item.status.value}{current}")
                return
            if args.stage_command=="history":
                entries=store.get_project_stage_history(args.project_id,args.stage_key)
                if not entries:
                    print("No stage history found.")
                    return
                for entry in entries:
                    print(f"{entry.changed_at.isoformat()}\t{entry.stage_key.value}\t{entry.from_status.value} -> {entry.to_status.value}\t{entry.actor}\t{entry.note}")
                return
            methods={
                "start": lambda: store.start_project_stage(args.project_id,args.stage_key,actor=args.actor),
                "resume": lambda: store.resume_project_stage(args.project_id,args.stage_key,actor=args.actor),
                "complete": lambda: store.complete_project_stage(args.project_id,args.stage_key,actor=args.actor),
                "block": lambda: store.block_project_stage(args.project_id,args.stage_key,actor=args.actor,note=args.note),
                "skip": lambda: store.skip_project_stage(args.project_id,args.stage_key,args.reason,actor=args.actor),
                "reopen": lambda: store.reopen_project_stage(args.project_id,args.stage_key,actor=args.actor,note=args.note),
            }
            changed=methods[args.stage_command]()
            print(f"{changed.stage_key.value} {args.stage_command}: {changed.status.value}")
            return
        if args.project_command=="create":
            project=store.create_project(ProjectCreate(name=args.name,description=args.description,problem_domain=args.problem_domain))
            print(f"Created project: {project.project_id} ({project.name})")
            return
        if args.project_command=="list":
            projects=store.list_projects(include_archived=not args.active_only)
            if not projects:
                print("No projects found.")
                return
            for project in projects:
                print(f"{project.project_id}\t{project.status.value}\t{project.name}")
            return
        if args.project_command=="show":
            _print_project(store.get_project(args.project_id))
            return
        if args.project_command=="update":
            if args.name is None and args.description is None and args.problem_domain is None:
                raise ValueError("Provide at least one of --name, --description, or --problem-domain.")
            project=store.update_project(args.project_id,ProjectUpdate.model_validate({
                key:value for key,value in {"name":args.name,"description":args.description,"problem_domain":args.problem_domain}.items() if value is not None
            }))
            print(f"Updated project: {project.project_id} ({project.name})")
            return
        if args.project_command=="archive":
            project=store.archive_project(args.project_id)
            print(f"Archived project: {project.project_id} ({project.name})")
            return
    if args.command=="run":
        if not settings.tavily_api_key:
            raise ResearchProviderError("TAVILY_API_KEY is not configured. Set it in the environment or .env file to use the single-question run command.")
        require_llm_configuration(settings.get_llm_config(reasoning=False))
        if args.project_id:
            with ResearchStore(args.database).db() as connection:
                ResearchStore._validate_project_for_research(connection,args.project_id)
        run_id,output=run_vertical_slice(prompt=args.prompt,question_text=args.question,database_path=args.database,output_path=args.output,project_id=args.project_id)
        print(f"Research run: {run_id}\nReport: {output}")
        return
    if args.command in {"plan", "research", "design", "pitch"}:
        require_llm_configuration(settings.get_llm_config(reasoning=args.command in {"plan", "design", "pitch"}))
    store=ResearchStore(args.database)
    if args.command=="plan":
        if args.project_id:
            with store.db() as connection:
                store._validate_project_for_research(connection,args.project_id)
        rules=Path(args.rules_file).read_text(encoding="utf-8") if args.rules_file else None
        competition=CompetitionInput(competition_name=args.competition_name,rules_text=rules,
            judging_criteria=args.judging_criterion,team_size=args.team_size,time_limit_hours=args.time_limit_hours,
            required_technologies=args.required_technology,budget=args.budget,demo_requirements=args.demo_requirement)
        plan=generate_research_plan(args.prompt,competition)
        store.save_plan(plan,project_id=args.project_id)
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
        if args.project_id: store.validate_run_project(plan.run_id,args.project_id)
        try: reviewed=review_plan(plan)
        except EOFError:
            print(f"Review ended; draft remains saved: {plan.id}")
            return
        store.update_plan(reviewed)
        print(f"Plan {reviewed.status}: {reviewed.id} (run {reviewed.run_id})")
        return
    if args.command=="collect":
        plan=store.get_plan(args.plan_id)
        if args.project_id: store.validate_run_project(plan.run_id,args.project_id)
        budget=SearchBudget(max_search_calls=args.max_searches,max_tavily_credits=args.max_tavily_credits,
            max_pages=args.max_pages,max_seconds=args.max_seconds)
        tavily=TavilyConnector(settings.tavily_api_key) if settings.tavily_api_key else None
        orchestrator=SearchOrchestrator(tavily,
            HackerNewsConnector(),fetcher=SafeFetcher(),max_workers=args.workers)
        result=collect_plan_sources(plan,store,budget=budget,orchestrator=orchestrator)
        print(f"Collected {len(result.sources)} unique sources for run {plan.run_id}.")
        _print_collection_outcome(attempted=result.searches_attempted,succeeded=result.searches_succeeded,
            failed=result.searches_failed,sources=len(result.sources))
        print(f"Search calls: {result.search_calls}; Tavily credits: {result.tavily_credits}; pages fetched: {result.pages_fetched}; elapsed: {result.elapsed_seconds:.1f}s")
        for failure in result.failures:
            print(f"Warning [{failure.connector}/{failure.category}]: {failure.reason}")
        return
    if args.command=="research":
        plan=store.get_plan(args.plan_id)
        if args.project_id: store.validate_run_project(plan.run_id,args.project_id)
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
        if args.project_id:
            project_store=ProjectStore(args.database)
            decision=project_store.create_decision(DecisionCreate(project_id=args.project_id,
                stage_key=ProjectStageKey.P2,title="Review research report"))
            artifact=project_store.create_artifact(ArtifactCreate(project_id=args.project_id,
                stage_key=ProjectStageKey.P2,decision_id=decision.decision_id,artifact_type="research_report",
                title="Research report",description=f"Project-scoped report for run {plan.run_id}.",content=report))
            print(f"Project report artifact: {artifact.artifact_id}; review decision: {decision.decision_id}")
        print(f"Research rounds: {execution.rounds_run}; sources collected: {execution.sources_collected}; evidence items processed: {execution.evidence_extracted}")
        _print_collection_outcome(attempted=execution.searches_attempted,succeeded=execution.searches_succeeded,
            failed=execution.searches_failed,sources=execution.sources_collected)
        print(f"Coverage: {'sufficient by configured rule' if execution.gap_report.complete else 'gaps remain'}; report: {output}")
        for failure in execution.failures:
            print(f"Warning [{failure.connector}/{failure.category}]: {failure.reason}")
        return
    if args.command=="design":
        plan=store.get_plan(args.plan_id)
        if args.project_id: store.validate_run_project(plan.run_id,args.project_id)
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
        if args.project_id: store.validate_run_project(plan.run_id,args.project_id)
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

if __name__=="__main__": raise SystemExit(main() or 0)
