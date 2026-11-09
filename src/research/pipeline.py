from __future__ import annotations

from dataclasses import dataclass,field

from src.research.budget import SearchBudget
from src.research.collection import collect_plan_sources
from src.research.extraction import extract_candidate
from src.research.gaps import ResearchGapReport, evaluate_research_gaps
from src.research.models import ResearchPlan, VerificationStatus
from src.research.planner import ResearchProviderError
from src.research.search import CollectionFailure, SearchAttempt, SearchOrchestrator
from src.research.storage import ResearchStore
from src.research.verification import verify_candidate


@dataclass
class ResearchExecution:
    gap_report: ResearchGapReport
    rounds_run: int
    sources_collected: int
    evidence_extracted: int
    failures: list
    searches_attempted: int = 0
    searches_succeeded: int = 0
    searches_failed: int = 0
    search_attempts: list[SearchAttempt] = field(default_factory=list)


class CollectionUnavailableError(RuntimeError):
    """Raised when every connector request fails and no research report is valid."""

    def __init__(self, failures: list[CollectionFailure], searches_attempted: int):
        self.failures = failures
        self.searches_attempted = searches_attempted
        connectors = ", ".join(dict.fromkeys(item.connector for item in failures if item.error is not None))
        if not connectors:
            connectors = "all configured search connectors"
        details = "; ".join(dict.fromkeys(item.reason for item in failures if item.error is not None))
        detail_text = f" Details: {details}" if details else ""
        super().__init__(
            f"All {searches_attempted} attempted search requests failed ({connectors}); no research report was generated. Check network access and connector availability.{detail_text}"
        )


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
    searches_attempted = 0
    searches_succeeded = 0
    searches_failed = 0
    search_attempts: list[SearchAttempt] = []
    current_questions = list(plan.questions)
    gap_report = evaluate_research_gaps(current_questions, [], [], min_independent_sources=min_independent_sources)

    for round_number in range(1, max_rounds + 1):
        if budget.remaining_seconds() <= 0:
            break
        active_plan = plan.model_copy(update={"questions": current_questions})
        collected = collect_plan_sources(active_plan, store, budget=budget, orchestrator=orchestrator)
        failures.extend(collected.failures)
        sources_collected += len(collected.sources)
        searches_attempted += collected.searches_attempted
        searches_succeeded += collected.searches_succeeded
        searches_failed += collected.searches_failed
        search_attempts.extend(collected.attempts)
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
                except ResearchProviderError as exc:
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
    if searches_attempted > 0 and searches_succeeded == 0 and searches_failed == searches_attempted:
        error = CollectionUnavailableError(failures, searches_attempted)
        cause = next((item.error for item in failures if item.error is not None), None)
        if cause is not None:
            raise error from cause
        raise error
    return ResearchExecution(gap_report, gap_report.rounds, sources_collected, evidence_extracted, failures,
        searches_attempted, searches_succeeded, searches_failed, search_attempts)
