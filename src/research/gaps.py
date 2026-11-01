from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from src.research.models import EvidenceRecord, ResearchQuestion, SourceSnapshot, VerificationStatus


@dataclass
class QuestionCoverage:
    question_id: str
    question: str
    covered: bool
    verified_evidence_count: int = 0
    independent_sources: int = 0
    gaps: list[str] = field(default_factory=list)
    follow_up_query: str = ""


@dataclass
class ResearchGapReport:
    questions: list[QuestionCoverage]
    potential_disagreements: dict[str, list[str]]
    complete: bool
    rounds: int

    @property
    def uncovered(self) -> list[QuestionCoverage]:
        return [item for item in self.questions if not item.covered]


def _source_identity(source: SourceSnapshot) -> str:
    host = (urlsplit(source.final_url or source.url).hostname or "").lower()
    return host.removeprefix("www.") or source.publisher.strip().casefold() or source.id


def evaluate_research_gaps(
    questions: list[ResearchQuestion],
    sources: list[SourceSnapshot],
    evidence: list[EvidenceRecord],
    *,
    rounds: int = 0,
    min_independent_sources: int = 2,
) -> ResearchGapReport:
    """Measure evidence coverage; this is a transparent heuristic, not a truth score."""
    if min_independent_sources < 1:
        raise ValueError("At least one independent source is required for coverage.")
    by_id = {source.id: source for source in sources}
    results: list[QuestionCoverage] = []
    disagreements: dict[str, list[str]] = {}

    for question in questions:
        verified = [item for item in evidence if item.question_id == question.id
                    and item.verification_status == VerificationStatus.VERIFIED]
        identities = {_source_identity(by_id[item.source_id]) for item in verified if item.source_id in by_id}
        gaps: list[str] = []
        if not verified:
            gaps.append("No exact, source-backed evidence has been verified.")
        if len(identities) < min_independent_sources:
            gaps.append(f"Need evidence from {min_independent_sources - len(identities)} more independent source(s).")
        supported = [item for item in verified if item.relation.value == "supports"]
        weakens = [item for item in verified if item.relation.value == "weakens"]
        if verified and not (supported or weakens):
            gaps.append("Only contextual evidence is available; direct support or weakening evidence is still needed.")
        if supported and weakens:
            disagreements[question.id] = list(dict.fromkeys(item.claim for item in supported + weakens if item.claim))
            gaps.append("Evidence includes both supporting and weakening interpretations; review the underlying passages.")
        covered = bool(supported or weakens) and len(identities) >= min_independent_sources and not (supported and weakens)
        follow_up = " ".join(part for part in (question.text, question.evidence_required) if part).strip()
        results.append(QuestionCoverage(question.id, question.text, covered, len(verified), len(identities), gaps, follow_up))

    return ResearchGapReport(results, disagreements, all(item.covered for item in results), rounds)
