from src.research.models import CandidateExtraction,EvidenceRecord,ResearchQuestion,SourceSnapshot,VerificationStatus
def verify_candidate(*,run_id:str,question:ResearchQuestion,source:SourceSnapshot,candidate:CandidateExtraction,collected_sources:list[SourceSnapshot])->EvidenceRecord:
    present=any(x.id==source.id and x.run_id==run_id for x in collected_sources)
    status=VerificationStatus.UNSUPPORTED; reason="No relevant, supported passage was returned."
    if not present: status,reason=VerificationStatus.INVALID_CITATION,"Source is not part of this research run."
    elif not source.content.strip(): status,reason=VerificationStatus.SEARCH_RESULT_ONLY,"Search result has no captured source text."
    elif candidate.relevant and candidate.passage and candidate.passage in source.content: status,reason=VerificationStatus.VERIFIED,"Exact passage found in captured source text."
    elif candidate.relevant: reason="Candidate passage was not found exactly in captured source text."
    return EvidenceRecord(run_id=run_id,question_id=question.id,source_id=source.id,claim=candidate.claim,passage=candidate.passage,relation=candidate.relation,verification_status=status,verification_reason=reason)
