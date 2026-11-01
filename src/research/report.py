from src.research.models import EvidenceRecord,ResearchQuestion,ResearchRun,SourceSnapshot

def render_markdown(run:ResearchRun,questions:list[ResearchQuestion],sources:list[SourceSnapshot],evidence:list[EvidenceRecord],gap_report=None)->str:
    by_id={s.id:s for s in sources}
    lines=["# Research report","",f"**Prompt:** {run.prompt}",f"**Run ID:** {run.id}",f"**Created:** {run.created_at.isoformat()}","","## Research questions",""]
    lines.extend([f"- [{q.priority.value}] {q.text}" for q in questions] or ["- No questions recorded."])
    lines.extend(["","## Findings",""])
    if not evidence: lines.append("No evidence items were extracted.")
    for i,e in enumerate(evidence,1):
        s=by_id.get(e.source_id)
        lines.extend([f"### {i}. {e.claim or 'No supported finding extracted'}","",f"**Evidence status:** {e.verification_status.value} - {e.verification_reason}",f"**Relation:** {e.relation.value}",""])
        lines.append(f"**Evidence ID:** `{e.id}`")
        if e.passage: lines.extend(["> "+x for x in e.passage.splitlines()]); lines.append("")
        if s:
            lines.append(f"**Source:** [{s.title or s.final_url or s.url}]({s.final_url or s.url})")
            if s.publisher: lines.append(f"**Publisher:** {s.publisher}")
            else: lines.append("**Publisher:** unavailable")
            if s.published_at: lines.append(f"**Published:** {s.published_at}")
            else: lines.append("**Published:** unavailable")
            lines.append(f"**Retrieved:** {s.retrieved_at.isoformat()}")
            if s.source_categories: lines.append("**Source categories:** "+", ".join(s.source_categories))
            lines.append("**Content origin:** "+s.content_origin)
            if s.fetch_error: lines.append("**Fetch note:** "+s.fetch_error)
        else: lines.append("**Source:** Invalid citation; source record is missing.")
        lines.append("")
    if gap_report is not None:
        lines.extend(["## Evidence coverage", "", f"Research rounds: {gap_report.rounds}",
            f"Overall status: {'sufficient by configured coverage rule' if gap_report.complete else 'gaps remain'}", ""])
        for coverage in gap_report.questions:
            lines.append(f"- {'Covered' if coverage.covered else 'Needs more research'}: {coverage.question} "
                f"({coverage.verified_evidence_count} verified item(s), {coverage.independent_sources} independent source(s))")
            lines.extend([f"  - Gap: {gap}" for gap in coverage.gaps])
        if gap_report.potential_disagreements:
            lines.extend(["", "### Potential evidence disagreements", ""])
            for question_id, claims in gap_report.potential_disagreements.items():
                question=next((item.text for item in questions if item.id==question_id),question_id)
                lines.append(f"- {question}: " + "; ".join(claims))
        lines.append("")
    lines.extend(["## Uncertainty and limits","","- Verified means the exact passage exists in captured text and its source belongs to this run; it does not establish truth, geographic fit, or extraction quality.","- Search snippets are labeled search-result-only.","- Model-generated findings are candidate interpretations and require human review.",""])
    return "\n".join(lines)
