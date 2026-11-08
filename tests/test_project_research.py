import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.project_store import ProjectStore
from src.research.models import (DecisionCreate, DecisionEvidenceRelationship, ProjectCreate,
                                 ResearchRun, ResearchQuestion, SourceSnapshot, EvidenceRecord,
                                 DecisionOptionCreate, ApprovalEventStatus, ArtifactCreate,
                                 ProjectStageKey, WorkflowRunCreate, VerificationStatus,
                                 SupportRelation, ResearchPlan, ProblemBrief, CompetitionInput,
                                 IntakeExtraction)
from src.research.storage import ResearchStore


class ProjectResearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "research.sqlite"
        self.store = ProjectStore(self.db_path)
        self.project = self.store.create_project(ProjectCreate(name="Owned"))
        self.other = self.store.create_project(ProjectCreate(name="Other"))

    def tearDown(self):
        self.temp.cleanup()

    def test_associated_and_legacy_runs_are_isolated_and_reopenable(self):
        legacy = ResearchRun(prompt="legacy standalone")
        self.store.save_run(legacy)
        owned = ResearchRun(prompt="owned research")
        self.store.save_run(owned,project_id=self.project.project_id)
        self.assertIsNone(self.store.get_run_records(legacy.id)[0].project_id)
        self.assertEqual(self.store.get_run_records(owned.id)[0].project_id,self.project.project_id)
        self.assertEqual([item.run_id for item in self.store.list_project_research_runs(self.project.project_id)],[owned.id])
        self.store.validate_run_project(owned.id,self.project.project_id)
        with self.assertRaisesRegex(ValueError,"not associated"):
            self.store.validate_run_project(legacy.id,self.project.project_id)
        with self.assertRaises(KeyError): self.store.save_run(ResearchRun(prompt="bad"),project_id="missing")
        self.store.archive_project(self.other.project_id)
        with self.assertRaisesRegex(ValueError,"active projects"):
            self.store.save_run(ResearchRun(prompt="archived"),project_id=self.other.project_id)
        self.store = ProjectStore(self.db_path)
        self.assertEqual(self.store.get_run_records(owned.id)[0].project_id,self.project.project_id)

    def test_explicit_association_only_accepts_unowned_runs(self):
        run = ResearchRun(prompt="legacy run")
        self.store.save_run(run)
        self.store.associate_research_run(self.project.project_id,run.id)
        self.store.associate_research_run(self.project.project_id,run.id)
        with self.assertRaisesRegex(ValueError,"cannot be reassigned"):
            self.store.associate_research_run(self.other.project_id,run.id)
        with self.assertRaises(KeyError): self.store.associate_research_run(self.project.project_id,"unknown")

    def test_project_research_cli_lists_and_attaches_runs(self):
        import contextlib,io
        from src import research_cli
        run=ResearchRun(prompt="CLI standalone")
        self.store.save_run(run)
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            research_cli.main(["project","research","attach",self.project.project_id,run.id,"--database",str(self.db_path)])
            research_cli.main(["project","research","list",self.project.project_id,"--database",str(self.db_path)])
        self.assertIn("Associated research run",output.getvalue())
        self.assertIn(run.id,output.getvalue())
        self.assertIn("Q:0 S:0 E:0",output.getvalue())

    def test_evidence_links_require_same_project_owned_research_run(self):
        decision=self.store.create_decision(DecisionCreate(project_id=self.project.project_id,title="Assess evidence"))
        run=ResearchRun(prompt="same project")
        self.store.save_run(run,project_id=self.project.project_id)
        question_id="q1"
        self.store.save_question(__import__("src.research.models",fromlist=["ResearchQuestion"]).ResearchQuestion(run_id=run.id,text="question",id=question_id))
        source=SourceSnapshot(run_id=run.id,question_id=question_id,url="https://example.test",content="A machine vibration signal can indicate wear.")
        self.store.save_source(source)
        with self.store.db() as c:
            c.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)",("e1",run.id,question_id,source.id,"wear increases vibration",source.content,"supports","verified","exact passage","2026-01-01T00:00:00+00:00"))
        link=self.store.link_decision_evidence(self.project.project_id,decision.decision_id,"e1",DecisionEvidenceRelationship.SUPPORTS)
        self.assertEqual(link.evidence_id,"e1")
        foreign=ResearchRun(prompt="other project")
        self.store.save_run(foreign,project_id=self.other.project_id)
        self.store.save_question(__import__("src.research.models",fromlist=["ResearchQuestion"]).ResearchQuestion(run_id=foreign.id,text="question",id="q2"))
        foreign_source=SourceSnapshot(run_id=foreign.id,question_id="q2",url="https://foreign.test",content="passage",id="s2")
        self.store.save_source(foreign_source)
        with self.store.db() as c:
            c.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)",("e2",foreign.id,"q2","s2","claim","passage","supports","verified","exact","2026-01-01T00:00:00+00:00"))
        with self.assertRaisesRegex(ValueError,"same project"):
            self.store.link_decision_evidence(self.project.project_id,decision.decision_id,"e2",DecisionEvidenceRelationship.CONTRADICTS)

    def test_storage_apis_reject_cross_run_question_source_and_evidence_ids(self):
        run_a=ResearchRun(prompt="A"); run_b=ResearchRun(prompt="B")
        self.store.save_run(run_a,project_id=self.project.project_id)
        self.store.save_run(run_b,project_id=self.other.project_id)
        qa=ResearchQuestion(run_id=run_a.id,text="A question")
        qb=ResearchQuestion(run_id=run_b.id,text="B question")
        self.store.save_question(qa); self.store.save_question(qb)
        with self.assertRaisesRegex(ValueError,"same run"):
            self.store.save_source(SourceSnapshot(run_id=run_a.id,question_id=qb.id,url="https://bad-q.test"))
        source_a=SourceSnapshot(run_id=run_a.id,question_id=qa.id,url="https://source-a.test",content="passage")
        source_b=SourceSnapshot(run_id=run_b.id,question_id=qb.id,url="https://source-b.test",content="other passage")
        self.store.save_source(source_a); self.store.save_source(source_b)
        bad_question_evidence=EvidenceRecord(run_id=run_a.id,question_id=qb.id,source_id=source_a.id,
            claim="claim",passage="passage",relation=SupportRelation.SUPPORTS,
            verification_status=VerificationStatus.VERIFIED,verification_reason="exact")
        bad_source_evidence=EvidenceRecord(run_id=run_a.id,question_id=qa.id,source_id=source_b.id,
            claim="claim",passage="other passage",relation=SupportRelation.SUPPORTS,
            verification_status=VerificationStatus.VERIFIED,verification_reason="exact")
        with self.assertRaisesRegex(ValueError,"same research run"):
            self.store.save_evidence(bad_question_evidence)
        with self.assertRaisesRegex(ValueError,"same research run"):
            self.store.save_evidence(bad_source_evidence)
        valid=EvidenceRecord(run_id=run_a.id,question_id=qa.id,source_id=source_a.id,claim="valid",
            passage="passage",relation=SupportRelation.SUPPORTS,verification_status=VerificationStatus.VERIFIED,
            verification_reason="exact")
        self.store.save_evidence(valid)

    def test_plan_save_and_update_cannot_cross_runs(self):
        run_id="plan-run"
        brief=ProblemBrief(problem_statement="Research prompt",user_provided=CompetitionInput(),
            extracted=IntakeExtraction(problem_summary="Useful summary",domain="manufacturing"))
        questions=[ResearchQuestion(run_id=run_id,text=f"Question {i}?") for i in range(3)]
        plan=ResearchPlan(run_id=run_id,brief=brief,questions=questions)
        self.store.save_plan(plan,project_id=self.project.project_id)
        foreign=ResearchQuestion(run_id="foreign-run",text="Foreign question?")
        bad=plan.model_copy(update={"questions":[questions[0],questions[1],foreign]})
        with self.assertRaisesRegex(ValueError,"must belong to the plan"):
            self.store.save_plan(bad)
        reassigned=plan.model_copy(update={"run_id":"another-run","questions":[q.model_copy(update={"run_id":"another-run"}) for q in questions]})
        with self.assertRaisesRegex(ValueError,"cannot be reassigned"):
            self.store.update_plan(reassigned)
        self.assertEqual(self.store.get_plan(plan.id).run_id,run_id)

    def test_migrates_legacy_research_rows_as_unassigned_and_preserves_relationships(self):
        legacy_path=Path(self.temp.name)/"legacy.sqlite"
        c=sqlite3.connect(legacy_path)
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript("""
            CREATE TABLE research_runs(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE research_questions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id),text TEXT NOT NULL);
            CREATE TABLE sources(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id),question_id TEXT NOT NULL REFERENCES research_questions(id),url TEXT NOT NULL,title TEXT NOT NULL,publisher TEXT NOT NULL,published_at TEXT,retrieved_at TEXT NOT NULL,content TEXT NOT NULL,search_snippet TEXT NOT NULL,search_score REAL);
            CREATE TABLE evidence(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id),question_id TEXT NOT NULL REFERENCES research_questions(id),source_id TEXT NOT NULL REFERENCES sources(id),claim TEXT NOT NULL,passage TEXT NOT NULL,relation TEXT NOT NULL,verification_status TEXT NOT NULL,verification_reason TEXT NOT NULL,created_at TEXT NOT NULL);
            INSERT INTO research_runs VALUES('legacy-run','keep prompt','2026-01-01T00:00:00+00:00');
            INSERT INTO research_questions VALUES('legacy-q','legacy-run','keep question');
            INSERT INTO sources VALUES('legacy-s','legacy-run','legacy-q','https://legacy.test','Title','Publisher',NULL,'2026-01-01T00:00:00+00:00','content','snippet',0.5);
            INSERT INTO evidence VALUES('legacy-e','legacy-run','legacy-q','legacy-s','claim','passage','supports','verified','reason','2026-01-01T00:00:00+00:00');
        """)
        c.commit(); c.close()
        ResearchStore(legacy_path)
        c=sqlite3.connect(legacy_path)
        try:
            self.assertEqual(c.execute("SELECT id,prompt,project_id FROM research_runs").fetchall(),[("legacy-run","keep prompt",None)])
            self.assertEqual(c.execute("SELECT id,run_id FROM research_questions").fetchall(),[("legacy-q","legacy-run")])
            self.assertEqual(c.execute("SELECT id,run_id FROM sources").fetchall(),[("legacy-s","legacy-run")])
            self.assertEqual(c.execute("SELECT id,run_id,source_id FROM evidence").fetchall(),[("legacy-e","legacy-run","legacy-s")])
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(),[])
            self.assertEqual(c.execute("SELECT version FROM schema_versions ORDER BY version").fetchall(),[(1,),(2,),(3,),(4,),(5,),(6,)])
        finally: c.close()

    @staticmethod
    def _make_pre_compat_research_database(path):
        c=sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE research_runs(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE research_questions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id),text TEXT NOT NULL);
            CREATE TABLE sources(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id),question_id TEXT NOT NULL REFERENCES research_questions(id),url TEXT NOT NULL,title TEXT NOT NULL,publisher TEXT NOT NULL,published_at TEXT,retrieved_at TEXT NOT NULL,content TEXT NOT NULL,search_snippet TEXT NOT NULL,search_score REAL);
            CREATE TABLE evidence(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id),question_id TEXT NOT NULL REFERENCES research_questions(id),source_id TEXT NOT NULL REFERENCES sources(id),claim TEXT NOT NULL,passage TEXT NOT NULL,relation TEXT NOT NULL,verification_status TEXT NOT NULL,verification_reason TEXT NOT NULL,created_at TEXT NOT NULL);
            INSERT INTO research_runs VALUES('old-run','preserve prompt','2025-01-01T00:00:00+00:00');
            INSERT INTO research_questions VALUES('old-question','old-run','preserve question');
            INSERT INTO sources VALUES('old-source','old-run','old-question','https://old.test','Old','Old publisher',NULL,'2025-01-01T00:00:00+00:00','old content','old snippet',0.5);
            INSERT INTO evidence VALUES('old-evidence','old-run','old-question','old-source','old claim','old passage','supports','verified','old reason','2025-01-01T00:00:00+00:00');
        """)
        c.commit(); c.close()

    def test_compatibility_column_bootstrap_is_atomic_idempotent_and_retryable(self):
        from unittest.mock import patch
        path=Path(self.temp.name)/"compat-atomic.sqlite"
        self._make_pre_compat_research_database(path)
        original=ResearchStore._migrate_columns
        def fail_after_questions(connection,table,columns):
            original(connection,table,columns)
            if table=="research_questions": raise RuntimeError("injected compatibility bootstrap failure")
        with patch.object(ResearchStore,"_migrate_columns",staticmethod(fail_after_questions)):
            with self.assertRaisesRegex(RuntimeError,"compatibility bootstrap"):
                ResearchStore(path)
        with closing(sqlite3.connect(path)) as c:
            self.assertNotIn("priority",[row[1] for row in c.execute("PRAGMA table_info(research_questions)")])
            self.assertNotIn("source_categories_json",[row[1] for row in c.execute("PRAGMA table_info(sources)")])
            self.assertEqual(c.execute("SELECT id,prompt FROM research_runs").fetchall(),[("old-run","preserve prompt")])
        store=ResearchStore(path)
        store=ResearchStore(path)
        run,questions,sources,evidence=store.get_run_records("old-run")
        self.assertEqual((run.prompt,questions[0].text,sources[0].content,evidence[0].passage),
                         ("preserve prompt","preserve question","old content","old passage"))
        with store.db() as c: self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],6)

    def test_later_migration_failure_keeps_complete_bootstrap_and_retry_preserves_legacy_rows(self):
        from unittest.mock import patch
        path=Path(self.temp.name)/"compat-later-failure.sqlite"
        self._make_pre_compat_research_database(path)
        with patch.object(ResearchStore,"_migrate_integrity_v6",staticmethod(lambda connection: None)):
            ResearchStore(path)
        with closing(sqlite3.connect(path)) as c:
            c.execute("CREATE INDEX idx_approval_events_event_sequence ON hackathon_projects(name)")
            c.commit()
        with self.assertRaisesRegex(sqlite3.OperationalError,"already exists"):
            ResearchStore(path)
        with closing(sqlite3.connect(path)) as c:
            question_columns=[row[1] for row in c.execute("PRAGMA table_info(research_questions)")]
            source_columns=[row[1] for row in c.execute("PRAGMA table_info(sources)")]
            self.assertTrue({"priority","source_categories_json","evidence_required","decision_impact","status"}.issubset(question_columns))
            self.assertTrue({"source_categories_json","content_origin","content_type","final_url","fetch_error"}.issubset(source_columns))
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],5)
            self.assertNotIn("event_sequence",[row[1] for row in c.execute("PRAGMA table_info(approval_events)")])
            self.assertEqual(c.execute("SELECT id,prompt FROM research_runs").fetchall(),[("old-run","preserve prompt")])
            c.execute("DROP INDEX idx_approval_events_event_sequence")
            c.commit()
        store=ResearchStore(path)
        run,questions,sources,evidence=store.get_run_records("old-run")
        self.assertEqual((run.prompt,questions[0].text,sources[0].content,evidence[0].passage),
                         ("preserve prompt","preserve question","old content","old passage"))
        with store.db() as c: self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],6)

    def test_complete_foundation_workflow_survives_database_reopen(self):
        self.store.start_project_stage(self.project.project_id,"P1")
        decision=self.store.create_decision(DecisionCreate(project_id=self.project.project_id,
            stage_key=ProjectStageKey.P1,title="Choose MVP architecture"))
        option_a=self.store.add_decision_option(self.project.project_id,decision.decision_id,DecisionOptionCreate(title="Edge monitoring"))
        option_b=self.store.add_decision_option(self.project.project_id,decision.decision_id,DecisionOptionCreate(title="Existing PLC telemetry"))
        self.store.select_decision_option(self.project.project_id,decision.decision_id,option_b.option_id)
        run=ResearchRun(prompt="Research evidence",project_id=self.project.project_id)
        self.store.save_run(run)
        question_id="workflow-q"
        self.store.save_question(__import__("src.research.models",fromlist=["ResearchQuestion"]).ResearchQuestion(id=question_id,run_id=run.id,text="Question"))
        source=SourceSnapshot(id="workflow-s",run_id=run.id,question_id=question_id,url="https://workflow.test",content="Captured passage")
        self.store.save_source(source)
        with self.store.db() as c:
            c.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)",("workflow-e",run.id,question_id,source.id,"supported claim",source.content,"supports","verified","exact match","2026-01-01T00:00:00+00:00"))
        self.store.link_decision_evidence(self.project.project_id,decision.decision_id,"workflow-e",DecisionEvidenceRelationship.SUPPORTS)
        self.store.record_decision_approval(self.project.project_id,decision.decision_id,ApprovalEventStatus.APPROVED,comment="Reviewed")
        artifact=self.store.create_artifact(ArtifactCreate(project_id=self.project.project_id,stage_key=ProjectStageKey.P1,
            decision_id=decision.decision_id,artifact_type="problem_statement",title="Problem Statement",content="Draft content"))
        self.store.record_artifact_approval(self.project.project_id,artifact.artifact_id,ApprovalEventStatus.APPROVED)
        workflow=self.store.start_workflow_run(WorkflowRunCreate(project_id=self.project.project_id,
            stage_key=ProjectStageKey.P1,workflow_type="problem-framing"))
        self.store.complete_workflow_run(self.project.project_id,workflow.workflow_run_id,output_artifact_id=artifact.artifact_id)
        self.assertEqual(len(self.store.list_project_stages(self.project.project_id)),8)
        self.assertEqual(len(self.store.list_project_research_runs(self.project.project_id)),1)
        self.assertEqual(self.store.list_project_research_runs(self.other.project_id),[])
        self.store=ProjectStore(self.db_path)
        self.assertEqual(self.store.get_decision_approval_status(self.project.project_id,decision.decision_id),"approved")
        self.assertEqual(self.store.get_artifact_approval_status(self.project.project_id,artifact.artifact_id),"approved")
        self.assertEqual(self.store.get_artifact(self.project.project_id,artifact.artifact_id).decision_id,decision.decision_id)
        self.assertEqual(self.store.get_workflow_run(self.project.project_id,workflow.workflow_run_id).output_artifact_id,artifact.artifact_id)
        self.assertEqual(len(self.store.list_decision_evidence(self.project.project_id,decision.decision_id)),1)
        self.assertEqual(self.store.get_run_records(run.id)[0].project_id,self.project.project_id)
        with self.store.db() as c: self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(),[])

    def test_m1_5_migration_failure_rolls_back_and_can_be_retried(self):
        legacy=Path(self.temp.name)/"retry.sqlite"
        c=sqlite3.connect(legacy)
        c.executescript("CREATE TABLE research_runs(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,created_at TEXT NOT NULL); INSERT INTO research_runs VALUES('r','p','t');")
        c.commit(); c.close()
        # Apply through M1.4, then inject failure when v5 is recorded.
        from src.research.storage import ResearchStore as Store
        original=Store._migrate_research_projects
        original_integrity=Store._migrate_integrity_v6
        Store._migrate_research_projects=staticmethod(lambda connection: None)
        Store._migrate_integrity_v6=staticmethod(lambda connection: None)
        try: Store(legacy)
        finally:
            Store._migrate_research_projects=staticmethod(original)
            Store._migrate_integrity_v6=staticmethod(original_integrity)
        c=sqlite3.connect(legacy)
        c.execute("CREATE TRIGGER fail_v5 BEFORE INSERT ON schema_versions WHEN NEW.version=5 BEGIN SELECT RAISE(ABORT,'injected v5 failure'); END")
        c.commit(); c.close()
        with self.assertRaises(sqlite3.IntegrityError): Store(legacy)
        c=sqlite3.connect(legacy)
        try:
            self.assertNotIn("project_id",[row[1] for row in c.execute("PRAGMA table_info(research_runs)")])
            self.assertEqual(c.execute("SELECT version FROM schema_versions ORDER BY version").fetchall(),[(1,),(2,),(3,),(4,)])
            c.execute("DROP TRIGGER fail_v5"); c.commit()
        finally: c.close()
        Store(legacy)
        c=sqlite3.connect(legacy)
        try:
            self.assertIsNone(c.execute("SELECT project_id FROM research_runs WHERE id='r'").fetchone()[0])
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],6)
        finally: c.close()


if __name__=="__main__": unittest.main()
