import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src.research.extraction import extract_candidate
from src.research.models import CandidateExtraction, ResearchQuestion, ResearchRun, SourceSnapshot, SupportRelation, VerificationStatus
from src.research.report import render_markdown
from src.research.storage import ResearchStore
from src.research.verification import verify_candidate
from src.research.workflow import run_vertical_slice

class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.run=ResearchRun(prompt="Example")
        self.question=ResearchQuestion(run_id=self.run.id,text="What is the issue?")
        self.source=SourceSnapshot(run_id=self.run.id,question_id=self.question.id,url="https://example.org/report",title="Example report",content="Warnings can be delayed when field observations arrive late.",search_snippet="Warnings can be delayed.")
        self.candidate=CandidateExtraction(relevant=True,claim="Warnings can be delayed.",passage="Warnings can be delayed when field observations arrive late.",relation=SupportRelation.SUPPORTS)
    def check(self,candidate=None,source=None,collected=None):
        source=source or self.source
        return verify_candidate(run_id=self.run.id,question=self.question,source=source,candidate=candidate or self.candidate,collected_sources=collected if collected is not None else [source])
    def test_exact_quote_verified(self):
        self.assertEqual(self.check().verification_status,VerificationStatus.VERIFIED)
    def test_altered_quote_unsupported(self):
        candidate=self.candidate.model_copy(update={"passage":"Warnings are always delayed."})
        self.assertEqual(self.check(candidate).verification_status,VerificationStatus.UNSUPPORTED)
    def test_uncaptured_source_invalid(self):
        self.assertEqual(self.check(collected=[]).verification_status,VerificationStatus.INVALID_CITATION)
    def test_snippet_only_not_verified(self):
        source=self.source.model_copy(update={"content":""})
        self.assertEqual(self.check(source=source).verification_status,VerificationStatus.SEARCH_RESULT_ONLY)

    def test_empty_relation_from_structured_provider_falls_back_to_context(self):
        class FakeLLM:
            def invoke(self,messages):
                return {"relevant":True,"claim":"Warnings can be delayed.",
                    "passage":"Warnings can be delayed when field observations arrive late.","relation":""}
        with patch("src.research.extraction.get_structured_llm",return_value=FakeLLM()):
            candidate=extract_candidate(self.question.text,self.source)
        self.assertEqual(candidate.relation,SupportRelation.CONTEXT)

class StorageReportTests(unittest.TestCase):
    def test_database_round_trip_and_cited_report(self):
        with tempfile.TemporaryDirectory() as temp:
            store=ResearchStore(Path(temp)/"research.sqlite")
            run=ResearchRun(prompt="Example"); q=ResearchQuestion(run_id=run.id,text="What is the issue?")
            source=SourceSnapshot(run_id=run.id,question_id=q.id,url="https://example.org/report",title="Report",content="Exact passage.")
            item=verify_candidate(run_id=run.id,question=q,source=source,candidate=CandidateExtraction(relevant=True,claim="A finding.",passage="Exact passage."),collected_sources=[source])
            store.save_run(run); store.save_question(q); store.save_source(source); store.save_evidence(item)
            loaded,questions,sources,evidence=store.get_run_records(run.id)
            report=render_markdown(loaded,questions,sources,evidence)
            self.assertIn("Exact passage.",report)
            self.assertIn("[Report](https://example.org/report)",report)
            self.assertIn("verified",report)
            self.assertIn("does not establish truth",report)
    def test_missing_run_raises(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(KeyError):
                ResearchStore(Path(temp)/"db.sqlite").get_run_records("missing")

class WorkflowTests(unittest.TestCase):
    def test_mocked_run_persists_and_exports(self):
        class Connector:
            def search(self,*,run_id,question_id,query,max_results=1):
                return [SourceSnapshot(run_id=run_id,question_id=question_id,url="https://example.org/a",title="Captured source",content="The warning process depends on local observation.")]
        def extractor(question,source):
            return CandidateExtraction(relevant=True,claim="Local observation matters.",passage="The warning process depends on local observation.",relation=SupportRelation.SUPPORTS)
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/"data.sqlite"; output=Path(temp)/"report.md"
            run_id,path=run_vertical_slice(prompt="Test",question_text="How?",database_path=db,output_path=output,connector=Connector(),extractor=extractor)
            self.assertEqual(path,output)
            report=output.read_text(encoding="utf-8")
            self.assertIn("verified",report); self.assertIn("Captured source",report)
            self.assertEqual(ResearchStore(db).get_run_records(run_id)[3][0].verification_status,VerificationStatus.VERIFIED)

if __name__=="__main__":
    unittest.main()
