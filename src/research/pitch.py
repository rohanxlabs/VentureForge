from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from src.llm.client import get_structured_llm
from src.research.models import EvidenceRecord, ResearchPlan, SourceSnapshot, VerificationStatus
from src.research.solution import SolutionDesign
from src.research.storage import ResearchStore


class RubricMapping(BaseModel):
    criterion: str = Field(min_length=2)
    pitch_claim: str = Field(min_length=5)
    proof_points: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    demo_moment: str = Field(min_length=5)
    confidence: Literal["strong", "partial", "weak"]
    gap: str = ""


class JudgeQuestion(BaseModel):
    question: str = Field(min_length=10)
    what_judges_are_testing: str = Field(min_length=5)
    suggested_answer: str = Field(min_length=10)
    evidence_ids: list[str] = Field(default_factory=list)


class PresentationSlide(BaseModel):
    title: str = Field(min_length=2)
    key_message: str = Field(min_length=5)
    talking_points: list[str] = Field(min_length=1, max_length=5)
    evidence_ids: list[str] = Field(default_factory=list)


class DemoStep(BaseModel):
    elapsed_seconds: int = Field(ge=0)
    duration_seconds: int = Field(gt=0, le=300)
    narrator: str = Field(min_length=5)
    action: str = Field(min_length=5)
    expected_result: str = Field(min_length=5)
    fallback: str = Field(min_length=5)


class PitchPackageDraft(BaseModel):
    pitch_summary: str = Field(min_length=40)
    rubric_mapping: list[RubricMapping] = Field(min_length=1, max_length=12)
    judge_questions: list[JudgeQuestion] = Field(min_length=5, max_length=10)
    presentation_outline: list[PresentationSlide] = Field(min_length=5, max_length=10)
    demo_script: list[DemoStep] = Field(min_length=3, max_length=12)
    open_gaps: list[str] = Field(default_factory=list)


class PitchPackage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    design_id: str
    status: Literal["draft"] = "draft"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    criteria_source: Literal["competition", "generic"]
    pitch_summary: str
    rubric_mapping: list[RubricMapping]
    judge_questions: list[JudgeQuestion]
    presentation_outline: list[PresentationSlide]
    demo_script: list[DemoStep]
    open_gaps: list[str]


GENERIC_CRITERIA = [
    "Problem importance and user understanding",
    "Innovation and differentiation",
    "Technical implementation and feasibility",
    "Impact and responsible use",
    "Demo quality and clarity",
]


def _build_context(plan: ResearchPlan, design: SolutionDesign, evidence: list[EvidenceRecord],
                   sources: list[SourceSnapshot]) -> tuple[list[str], str, set[str]]:
    criteria = list(dict.fromkeys(item.strip() for item in plan.brief.user_provided.judging_criteria if item.strip())) or GENERIC_CRITERIA
    criteria_source: Literal["competition", "generic"] = "competition" if plan.brief.user_provided.judging_criteria else "generic"
    valid_evidence = [item for item in evidence if item.verification_status == VerificationStatus.VERIFIED]
    valid_ids = {item.id for item in valid_evidence}
    sources_by_id = {source.id: source for source in sources}
    provided=plan.brief.user_provided
    constraints={"competition_name":provided.competition_name,"judging_criteria":criteria,
        "team_size":provided.team_size,"time_limit_hours":provided.time_limit_hours,
        "required_technologies":provided.required_technologies,"budget":provided.budget,
        "demo_requirements":provided.demo_requirements,"rules_excerpt":(provided.rules_text or "")[:3000]}
    lines = ["APPROVED COMPETITION BRIEF", plan.brief.problem_statement,
        "Competition constraints: " + str(constraints),
        "Judging criteria: " + "; ".join(criteria),
        "RECOMMENDED SOLUTION: " + design.recommended_option_id]
    option = next(item for item in design.alternatives if item.id == design.recommended_option_id)
    lines.extend([f"Solution: {option.name}", option.concept, f"Differentiator: {option.differentiator}",
        f"Architecture: {design.technical_architecture}", f"Demo plan: {design.demo_plan}",
        "VERIFIED EVIDENCE: use these IDs only for factual support; label unsupported points as assumptions."])
    for item in valid_evidence[:30]:
        source = sources_by_id.get(item.source_id)
        lines.append(f"evidence_id={item.id}; claim={item.claim}; exact_passage={item.passage[:500]}; "
            f"source={source.title if source else 'unknown'}; url={(source.final_url or source.url) if source else 'unknown'}")
    if not valid_evidence:
        lines.append("No verified evidence exists. Do not claim user validation, impact, or technical performance.")
    return criteria, "\n".join(lines), valid_ids


def _generate_draft(context: str, criteria: list[str], *, llm=None) -> PitchPackageDraft:
    llm = llm or get_structured_llm(PitchPackageDraft, temperature=0, reasoning=True)
    system = (
        "You are a skeptical hackathon judge and pitch coach. Prepare an evidence-traceable pitch package for a live demo. "
        "Map every supplied judging criterion exactly once. Show a concrete proof point and demo moment for each; mark weak "
        "or missing proof as a gap. Ask difficult judge questions and provide honest, concise answers. Never invent user "
        "research, benchmarks, integrations, implementation status, or citations. Reference only supplied evidence IDs, and "
        "make assumptions explicit. The outline should tell a clear problem-solution-why-now-how-it-works-impact story. "
        "Demo steps must be actions a small hackathon team could actually perform, include observable expected results, "
        "and provide a fallback for network, model, or data failure. Keep the pitch honest about prototype limitations."
    )
    user = "Map exactly these criteria: " + " | ".join(criteria) + "\n\n" + context
    value = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    return value if isinstance(value, PitchPackageDraft) else PitchPackageDraft.model_validate(value)


def generate_pitch_package(plan: ResearchPlan, design: SolutionDesign, store: ResearchStore,
                           *, draft_provider=None) -> PitchPackage:
    if plan.status != "approved":
        raise ValueError("Pitch generation requires an approved research plan.")
    if design.run_id != plan.run_id:
        raise ValueError("Solution design and research plan must belong to the same run.")
    if design.recommended_option_id not in {item.id for item in design.alternatives}:
        raise ValueError("Solution design recommendation must reference one of its alternatives.")
    _, _, sources, evidence = store.get_run_records(plan.run_id)
    criteria, context, valid_ids = _build_context(plan, design, evidence, sources)
    draft = (draft_provider or _generate_draft)(context, criteria)
    canonical_criteria={" ".join(item.casefold().split()):item for item in criteria}
    mapped=[]
    normalized_mapping=[]
    for item in draft.rubric_mapping:
        canonical=canonical_criteria.get(" ".join(item.criterion.casefold().split()))
        if canonical is None:
            raise ValueError(f"Pitch mapped an unknown judging criterion: {item.criterion}")
        mapped.append(canonical)
        normalized_mapping.append(item.model_copy(update={"criterion":canonical}))
    if len(mapped) != len(set(mapped)) or set(mapped) != set(criteria):
        raise ValueError("Pitch must map every judging criterion exactly once, without adding criteria.")
    references = []
    for mapping in draft.rubric_mapping:
        references.extend(mapping.evidence_ids)
    for question in draft.judge_questions:
        references.extend(question.evidence_ids)
    for slide in draft.presentation_outline:
        references.extend(slide.evidence_ids)
    invalid = set(references) - valid_ids
    if invalid:
        raise ValueError(f"Pitch package references unknown or unverified evidence IDs: {sorted(invalid)}")
    elapsed = 0
    for step in draft.demo_script:
        if step.elapsed_seconds < elapsed:
            raise ValueError("Demo script steps must be ordered by elapsed time.")
        elapsed = step.elapsed_seconds + step.duration_seconds
    criteria_source = "competition" if plan.brief.user_provided.judging_criteria else "generic"
    return PitchPackage(run_id=plan.run_id, design_id=design.id, criteria_source=criteria_source,
        pitch_summary=draft.pitch_summary, rubric_mapping=normalized_mapping,
        judge_questions=draft.judge_questions, presentation_outline=draft.presentation_outline,
        demo_script=draft.demo_script, open_gaps=draft.open_gaps)


def render_pitch_markdown(package: PitchPackage, evidence: list[EvidenceRecord],
                          sources: list[SourceSnapshot]) -> str:
    evidence_by_id = {item.id: item for item in evidence}
    source_by_id = {item.id: item for item in sources}
    lines = ["# Hackathon pitch package", "", f"**Run:** {package.run_id}",
        f"**Package:** {package.id} (draft)", f"**Criteria source:** {package.criteria_source}",
        "", "## Pitch summary", "", package.pitch_summary, "", "## Judging rubric mapping", ""]
    for item in package.rubric_mapping:
        lines.extend([f"### {item.criterion} — {item.confidence}", "", item.pitch_claim, "",
            "**Proof points:** " + ("; ".join(item.proof_points) or "No proof point supplied."),
            f"**Demo moment:** {item.demo_moment}"])
        if item.gap: lines.append("**Gap:** " + item.gap)
        lines.extend(_render_citations(item.evidence_ids, evidence_by_id, source_by_id))
        lines.append("")
    lines.extend(["## Judge questions to rehearse", ""])
    for index, question in enumerate(package.judge_questions, 1):
        lines.extend([f"{index}. **{question.question}**", f"   Judges are testing: {question.what_judges_are_testing}",
            f"   Suggested answer: {question.suggested_answer}"])
        lines.extend(_render_citations(question.evidence_ids, evidence_by_id, source_by_id))
    lines.extend(["", "## Presentation outline", ""])
    for index, slide in enumerate(package.presentation_outline, 1):
        lines.extend([f"### {index}. {slide.title}", "", f"**Key message:** {slide.key_message}"])
        lines.extend(f"- {point}" for point in slide.talking_points)
        lines.extend(_render_citations(slide.evidence_ids, evidence_by_id, source_by_id))
        lines.append("")
    lines.extend(["## Demo script", "", "| Time | Duration | Narration | Action | Expected result | Fallback |",
        "|---:|---:|---|---|---|---|"])
    for step in package.demo_script:
        lines.append(f"| {step.elapsed_seconds}s | {step.duration_seconds}s | {step.narrator} | {step.action} | {step.expected_result} | {step.fallback} |")
    lines.extend(["", "## Open gaps", ""])
    lines.extend([f"- {gap}" for gap in package.open_gaps] or ["- None listed."])
    lines.extend(["", "Generated content is a draft. Verify every claim, demo behavior, timing, and competition rule before presenting.", ""])
    return "\n".join(lines)


def _render_citations(ids: list[str], evidence_by_id: dict, source_by_id: dict) -> list[str]:
    lines = []
    for evidence_id in ids:
        item = evidence_by_id.get(evidence_id)
        source = source_by_id.get(item.source_id) if item else None
        if item and source:
            lines.append(f"   Evidence `{evidence_id}`: [{source.title or source.url}]({source.final_url or source.url}) — {item.passage}")
    return lines
