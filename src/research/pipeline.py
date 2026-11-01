from __future__ import annotations

from dataclasses import dataclass

from src.research.budget import SearchBudget
from src.research.collection import collect_plan_sources
from src.research.extraction import extract_candidate
from src.research.gaps import ResearchGapReport, evaluate_research_gaps
from src.research.models import ResearchPlan, VerificationStatus
from src.research.search import CollectionFailure, SearchOrchestrator
from src.research.storage import ResearchStore
from src.research.verification import verify_candidate


@dataclass
class ResearchExecution:
    gap_report: ResearchGapReport
    rounds_run: int
    sources_collected: int
    evidence_extracted: int
    failures: list


def run_gap_research(
    plan: ResearchPlan,
    store: ResearchStore,
    *,
    budget: SearchBudget | None = None,
    orchestrator: SearchOrchestrator | None = None,
    max_rounds: int = 2,
    min_independent_sources: int = 2,
    extractor=extract_candidate,
) -> ResearchExecution:
    """Collect, extract, verify, and target remaining gaps for a bounded number of rounds."""
    if plan.status != "approved":
        raise ValueError("Research execution requires an approved plan.")
    if max_rounds < 1:
        raise ValueError("At least one research round is required.")
    if min_independent_sources < 1:
        raise ValueError("At least one independent source is required for coverage.")

    budget = budget or SearchBudget()
    failures: list[CollectionFailure] = []
    sources_collected = 0
    evidence_extracted = 0
    current_questions = list(plan.questions)
    gap_report = evaluate_research_gaps(current_questions, [], [], min_independent_sources=min_independent_sources)

    for round_number in range(1, max_rounds + 1):
        if budget.remaining_seconds() <= 0:
            break
        active_plan = plan.model_copy(update={"questions": current_questions})
        collected = collect_plan_sources(active_plan, store, budget=budget, orchestrator=orchestrator)
        failures.extend(collected.failures)
        sources_collected += len(collected.sources)
        _, _, _, prior_evidence = store.get_run_records(plan.run_id)
        already_verified={(item.question_id,item.source_id) for item in prior_evidence
            if item.verification_status == VerificationStatus.VERIFIED}

        for question in current_questions:
            store.update_question_status(question.id, "in_progress")
            matching_sources = [source for source in collected.sources if source.question_id == question.id]
            for source in matching_sources:
                if (question.id,source.id) in already_verified:
                    continue
                try:
                    candidate = extractor(question.text, source)
                    record = verify_candidate(run_id=plan.run_id, question=question, source=source,
                        candidate=candidate, collected_sources=collected.sources)
                    store.save_evidence(record)
                    evidence_extracted += 1
                except Exception as exc:
                    failures.append(CollectionFailure(question.id,"extraction","evidence_extractor",str(exc)))

        _, saved_questions, saved_sources, saved_evidence = store.get_run_records(plan.run_id)
        gap_report = evaluate_research_gaps(saved_questions, saved_sources, saved_evidence,
            rounds=round_number, min_independent_sources=min_independent_sources)
        for coverage in gap_report.questions:
            store.update_question_status(coverage.question_id, "covered" if coverage.covered else "pending")
        if gap_report.complete or round_number == max_rounds or budget.remaining_seconds() <= 0:
            break

        current_questions = []
        questions_by_id = {question.id: question for question in plan.questions}
        for coverage in gap_report.uncovered:
            original = questions_by_id[coverage.question_id]
            current_questions.append(original.model_copy(update={"text":coverage.follow_up_query}))
        if not current_questions:
            break

    _, saved_questions, saved_sources, saved_evidence = store.get_run_records(plan.run_id)
    gap_report = evaluate_research_gaps(saved_questions, saved_sources, saved_evidence,
        rounds=gap_report.rounds, min_independent_sources=min_independent_sources)
    return ResearchExecution(gap_report, gap_report.rounds, sources_collected, evidence_extracted, failures)
