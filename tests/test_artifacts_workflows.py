import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.project_store import ProjectStore
from src.research.storage import ResearchStore
from src.research.models import (
    ApprovalEventStatus, ArtifactCreate, ArtifactStatus, DecisionCreate,
    ProjectCreate, ProjectStageKey, WorkflowRunCreate, WorkflowStatus,
)
from src.research.pitch import PitchPackage
from src.research.solution import SolutionDesign, SolutionOption, SolutionScores


class ArtifactWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "foundation.sqlite"
        self.store = ProjectStore(self.db_path)
        self.project = self.store.create_project(ProjectCreate(name="Project A"))
        self.other = self.store.create_project(ProjectCreate(name="Project B"))

    def tearDown(self):
        self.temp.cleanup()

    def _make_v3_database(self,path:Path)->tuple[str,str]:
        from unittest.mock import patch
        from src.research.storage import ResearchStore
        no_migration=staticmethod(lambda connection: None)
        with patch.object(ResearchStore,"_migrate_artifacts_workflows",no_migration), \
             patch.object(ResearchStore,"_migrate_research_projects",no_migration), \
             patch.object(ResearchStore,"_migrate_integrity_v6",no_migration):
            store=ProjectStore(path)
            project=store.create_project(ProjectCreate(name="M1.3 project"))
            decision=store.create_decision(DecisionCreate(project_id=project.project_id,title="M1.3 decision"))
            with store.db() as c:
                c.execute("INSERT INTO approval_events(approval_id,project_id,decision_id,status,actor,comment,created_at) VALUES('kept',?,?,'approved','reviewer','preserve','2026-01-01T00:00:00+00:00')",
                          (project.project_id,decision.decision_id))
                self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],3)
        return project.project_id,decision.decision_id

    def _make_v5_database(self,path:Path)->None:
        from unittest.mock import patch
        no_v6=staticmethod(lambda connection: None)
        with patch.object(ResearchStore,"_migrate_integrity_v6",no_v6):
            ProjectStore(path)
        with closing(sqlite3.connect(path)) as c:
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],5)

    @staticmethod
    def _design(run_id:str)->SolutionDesign:
        scores=SolutionScores(problem_fit=4,impact=4,feasibility=4,demoability=4,differentiation=4)
        options=[SolutionOption(name=f"Option {index}",concept="A useful manufacturing maintenance concept.",
            target_user="Small manufacturers",differentiator="Affordable operational insight",implementation_approach="Use existing telemetry",scores=scores)
            for index in range(1,4)]
        return SolutionDesign(run_id=run_id,alternatives=options,recommended_option_id=options[0].id,
            recommendation_reason="Best fit",score_weights={},technical_architecture="A simple local-first architecture.",
            mvp_tasks=[],deferred_scope=[],key_risks=[],demo_plan="Show one alert from a machine signal.")

    @staticmethod
    def _pitch(run_id:str,design_id:str)->PitchPackage:
        return PitchPackage(run_id=run_id,design_id=design_id,criteria_source="generic",pitch_summary="A concise pitch summary for this test.",
            rubric_mapping=[],judge_questions=[],presentation_outline=[],demo_script=[],open_gaps=[])

    def test_artifacts_are_versioned_and_project_scoped(self):
        first = self.store.create_artifact(ArtifactCreate(project_id=self.project.project_id,
            stage_key=ProjectStageKey.P1,artifact_type="problem_statement",title="Problem",content="v1"))
        second = self.store.create_artifact_version(self.project.project_id,first.artifact_id,
            ArtifactCreate(project_id=self.project.project_id,stage_key=ProjectStageKey.P1,
                artifact_type="problem_statement",title="Problem",content="v2"))
        self.assertEqual((first.version,second.version),(1,2))
        self.assertEqual(self.store.get_artifact(self.project.project_id,first.artifact_id).content,"v1")
        self.assertEqual(self.store.get_artifact(self.project.project_id,first.artifact_id).status,ArtifactStatus.SUPERSEDED)
        with self.assertRaises(KeyError):
            self.store.get_artifact(self.other.project_id,first.artifact_id)

    def test_artifact_rejects_traversal_and_large_inline_content(self):
        with self.assertRaisesRegex(ValueError,"relative path"):
            self.store.create_artifact(ArtifactCreate(project_id=self.project.project_id,
                artifact_type="report",title="Unsafe",storage_ref="../outside.txt"))
        with self.assertRaisesRegex(ValueError,"512 KiB"):
            self.store.create_artifact(ArtifactCreate(project_id=self.project.project_id,
                artifact_type="report",title="Large",content="x"*(512*1024+1)))

    def test_artifact_reference_rejects_windows_unc_absolute_and_traversal_paths(self):
        for reference in (r"C:\reports\private.md",r"C:private.md",r"\\server\share\private.md",
                          "/etc/passwd",r"reports\..\private.md","reports/../private.md"):
            with self.subTest(reference=reference), self.assertRaisesRegex(ValueError,"safe relative path"):
                self.store.create_artifact(ArtifactCreate(project_id=self.project.project_id,
                    artifact_type="report",title="Unsafe path",storage_ref=reference))
        safe=self.store.create_artifact(ArtifactCreate(project_id=self.project.project_id,
            artifact_type="report",title="Metadata reference",storage_ref="reports/project/report.md"))
        self.assertEqual(safe.storage_ref,"reports/project/report.md")

    def test_artifact_status_transition_cannot_revive_a_superseded_version(self):
        first=self.store.create_artifact(ArtifactCreate(project_id=self.project.project_id,
            artifact_type="report",title="Version one",content="one"))
        self.store.update_artifact_status(self.project.project_id,first.artifact_id,ArtifactStatus.READY)
        second=self.store.create_artifact_version(self.project.project_id,first.artifact_id,
            ArtifactCreate(project_id=self.project.project_id,artifact_type="report",title="Version two",content="two"))
        self.assertEqual(self.store.get_artifact(self.project.project_id,first.artifact_id).status,ArtifactStatus.SUPERSEDED)
        with self.assertRaisesRegex(ValueError,"Invalid artifact status transition"):
            self.store.update_artifact_status(self.project.project_id,first.artifact_id,ArtifactStatus.READY)
        self.assertEqual(self.store.update_artifact_status(self.project.project_id,second.artifact_id,ArtifactStatus.READY).status,
                         ArtifactStatus.READY)

    def test_latest_artifact_cannot_be_superseded_without_atomic_replacement(self):
        first=self.store.create_artifact(ArtifactCreate(project_id=self.project.project_id,
            artifact_type="report",title="Current",content="old"))
        self.store.update_artifact_status(self.project.project_id,first.artifact_id,ArtifactStatus.READY)
        with self.assertRaisesRegex(ValueError,"Invalid artifact status transition"):
            self.store.update_artifact_status(self.project.project_id,first.artifact_id,ArtifactStatus.SUPERSEDED)
        with self.store.db() as c:
            c.execute("""CREATE TRIGGER reject_supersede BEFORE UPDATE OF status ON project_artifacts
                WHEN NEW.status='superseded' BEGIN SELECT RAISE(ABORT,'injected replacement failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_artifact_version(self.project.project_id,first.artifact_id,
                ArtifactCreate(project_id=self.project.project_id,artifact_type="report",title="Replacement",content="new"))
        self.assertEqual(self.store.list_project_artifacts(self.project.project_id),[
            self.store.get_artifact(self.project.project_id,first.artifact_id)])
        self.assertEqual(self.store.get_artifact(self.project.project_id,first.artifact_id).status,ArtifactStatus.READY)

    def test_same_run_pitch_package_persists_and_reopens(self):
        from unittest.mock import patch
        from src.research.models import ResearchRun
        path=Path(self.temp.name)/"pitch-migration.sqlite"
        with patch.object(ResearchStore,"_migrate_integrity_v6",staticmethod(lambda connection: None)):
            store=ProjectStore(path)
            run=ResearchRun(prompt="Pitch source")
            store.save_run(run)
            design=self._design(run.id)
            store.save_solution_design(design)
            package=self._pitch(run.id,design.id)
            store.save_pitch_package(package)
        migrated=ResearchStore(path)
        restored=migrated.get_pitch_package(package.id)
        self.assertEqual((restored.run_id,restored.design_id),(run.id,design.id))
        with migrated.db() as c: self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],6)

    def test_pitch_package_rejects_cross_run_and_cross_project_designs(self):
        from src.research.models import ResearchRun
        run_a=ResearchRun(prompt="Project A"); run_b=ResearchRun(prompt="Project B")
        self.store.save_run(run_a,project_id=self.project.project_id)
        self.store.save_run(run_b,project_id=self.other.project_id)
        design_b=self._design(run_b.id)
        self.store.save_solution_design(design_b)
        with self.assertRaisesRegex(ValueError,"same research run/project"):
            self.store.save_pitch_package(self._pitch(run_a.id,design_b.id))
        with self.assertRaisesRegex(KeyError,"Unknown solution design"):
            self.store.save_pitch_package(self._pitch(run_a.id,"missing-design"))
        with self.assertRaisesRegex(KeyError,"Unknown research run"):
            self.store.save_pitch_package(self._pitch("missing-run",design_b.id))

    def test_v5_to_v6_trigger_name_collision_fails_before_changes(self):
        path=Path(self.temp.name)/"v5-collision.sqlite"
        self._make_v5_database(path)
        with closing(sqlite3.connect(path)) as c:
            c.execute("CREATE TRIGGER project_stage_history_prevent_replace BEFORE INSERT ON hackathon_projects BEGIN SELECT RAISE(ABORT,'unrelated'); END")
            c.commit()
        with self.assertRaisesRegex(RuntimeError,"required trigger project_stage_history_prevent_replace"):
            ResearchStore(path)
        with closing(sqlite3.connect(path)) as c:
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],5)
            self.assertNotIn("event_sequence",[row[1] for row in c.execute("PRAGMA table_info(approval_events)")])
            self.assertEqual(c.execute("SELECT tbl_name FROM sqlite_master WHERE type='trigger' AND name='project_stage_history_prevent_replace'").fetchone()[0],"hackathon_projects")
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name='approval_events_prevent_replace'").fetchone())

    def test_normal_v5_to_v6_migration_verifies_required_triggers(self):
        path=Path(self.temp.name)/"v5-normal.sqlite"
        self._make_v5_database(path)
        migrated=ResearchStore(path)
        required={
            "approval_events_immutable_update":"approval_events",
            "approval_events_immutable_delete":"approval_events",
            "approval_events_prevent_replace":"approval_events",
            "project_stage_history_immutable_update":"project_stage_history",
            "project_stage_history_immutable_delete":"project_stage_history",
            "project_stage_history_prevent_replace":"project_stage_history",
            "workflow_run_events_immutable_update":"workflow_run_events",
            "workflow_run_events_immutable_delete":"workflow_run_events",
            "workflow_run_events_prevent_replace":"workflow_run_events",
        }
        with migrated.db() as c:
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],6)
            actual={row["name"]:row["tbl_name"] for row in c.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger'")}
            for name,target in required.items(): self.assertEqual(actual.get(name),target)

    def test_approval_target_xor_and_decision_history_survive_migration(self):
        decision = self.store.create_decision(DecisionCreate(project_id=self.project.project_id,title="Decision"))
        decision_event = self.store.record_decision_approval(self.project.project_id,decision.decision_id,
            ApprovalEventStatus.APPROVED,comment="Keep history")
        artifact = self.store.create_artifact(ArtifactCreate(project_id=self.project.project_id,
            artifact_type="report",title="Report",content="text"))
        event = self.store.record_artifact_approval(self.project.project_id,artifact.artifact_id,
            ApprovalEventStatus.REJECTED,comment="Revise")
        self.assertEqual(self.store.get_decision_approval_history(self.project.project_id,decision.decision_id),[decision_event])
        self.assertEqual(self.store.get_artifact_approval_history(self.project.project_id,artifact.artifact_id),[event])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as c:
                c.execute("""INSERT OR REPLACE INTO approval_events
                    (approval_id,event_sequence,project_id,artifact_id,status,actor,comment,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (event.approval_id,event.event_sequence,self.project.project_id,artifact.artifact_id,
                     "approved","replacement","must not replace",event.created_at.isoformat()))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as c:
                c.execute("INSERT INTO approval_events(approval_id,project_id,status,actor,comment,created_at) VALUES('bad',?,'approved','u','','now')",
                          (self.project.project_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as c:
                c.execute("INSERT INTO approval_events(approval_id,project_id,decision_id,artifact_id,status,actor,comment,created_at) VALUES('bad2',?,?,?,'approved','u','','now')",
                          (self.project.project_id,decision.decision_id,artifact.artifact_id))

    def test_workflow_lifecycle_history_and_ownership(self):
        run = self.store.start_workflow_run(WorkflowRunCreate(project_id=self.project.project_id,
            stage_key=ProjectStageKey.P1,workflow_type="planning"))
        self.assertEqual(run.status,WorkflowStatus.RUNNING)
        with self.assertRaisesRegex(ValueError,"failure summary"):
            self.store.fail_workflow_run(self.project.project_id,run.workflow_run_id," ")
        done = self.store.complete_workflow_run(self.project.project_id,run.workflow_run_id)
        self.assertIsNotNone(done.completed_at)
        self.assertEqual([item.to_status for item in self.store.get_workflow_history(self.project.project_id,run.workflow_run_id)],
                         [WorkflowStatus.RUNNING,WorkflowStatus.COMPLETED])
        event=self.store.get_workflow_history(self.project.project_id,run.workflow_run_id)[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as c:
                c.execute("INSERT OR REPLACE INTO workflow_run_events VALUES(?,?,?,?,?,?,?)",
                    (event.event_id,event.workflow_run_id,event.from_status.value if event.from_status else None,
                     event.to_status.value,"replacer","must not replace",event.changed_at.isoformat()))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as c: c.execute("UPDATE workflow_run_events SET note='overwrite' WHERE event_id=?",(event.event_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as c: c.execute("DELETE FROM workflow_run_events WHERE event_id=?",(event.event_id,))
        with self.assertRaisesRegex(ValueError,"Invalid workflow transition"):
            self.store.fail_workflow_run(self.project.project_id,run.workflow_run_id,"late failure")
        failed = self.store.start_workflow_run(WorkflowRunCreate(project_id=self.project.project_id,
            stage_key=ProjectStageKey.P2,workflow_type="research"))
        self.assertEqual(self.store.fail_workflow_run(self.project.project_id,failed.workflow_run_id,"provider unavailable").status,
                         WorkflowStatus.FAILED)
        interrupted = self.store.start_workflow_run(WorkflowRunCreate(project_id=self.project.project_id,
            stage_key=ProjectStageKey.P3,workflow_type="validation"))
        self.assertEqual(self.store.interrupt_workflow_run(self.project.project_id,interrupted.workflow_run_id).status,
                         WorkflowStatus.INTERRUPTED)
        with self.assertRaises(KeyError):
            self.store.get_workflow_run(self.other.project_id,run.workflow_run_id)

    def test_reopen_preserves_rows_and_migration_is_idempotent(self):
        artifact = self.store.create_artifact(ArtifactCreate(project_id=self.project.project_id,
            artifact_type="report",title="Report",content="saved"))
        self.store = ProjectStore(self.db_path)
        self.assertEqual(self.store.get_artifact(self.project.project_id,artifact.artifact_id).content,"saved")
        with self.store.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM schema_versions").fetchone()[0],6)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(),[])

    def test_m1_4_approval_rebuild_rolls_back_and_preserves_history_on_retry(self):
        from src.research.storage import ResearchStore
        path=Path(self.temp.name)/"m14-rollback.sqlite"
        migrate4=ResearchStore._migrate_artifacts_workflows
        migrate5=ResearchStore._migrate_research_projects
        migrate6=ResearchStore._migrate_integrity_v6
        ResearchStore._migrate_artifacts_workflows=staticmethod(lambda connection: None)
        ResearchStore._migrate_research_projects=staticmethod(lambda connection: None)
        ResearchStore._migrate_integrity_v6=staticmethod(lambda connection: None)
        try: ResearchStore(path)
        finally:
            ResearchStore._migrate_artifacts_workflows=staticmethod(migrate4)
            ResearchStore._migrate_research_projects=staticmethod(migrate5)
            ResearchStore._migrate_integrity_v6=staticmethod(migrate6)
        c=sqlite3.connect(path)
        c.executescript("""
            INSERT INTO hackathon_projects(project_id,name,status,current_stage,created_at,updated_at,archived_at)
            VALUES('p','P','active','P1','now','now',NULL);
            INSERT INTO decisions(decision_id,project_id,title,description,decision_type,status,created_at,updated_at)
            VALUES('d','p','D','','general','open','now','now');
            INSERT INTO approval_events VALUES('a','p','d','approved','reviewer','keep','now');
            CREATE TRIGGER fail_v4 BEFORE INSERT ON schema_versions WHEN NEW.version=4
            BEGIN SELECT RAISE(ABORT,'injected v4 failure'); END;
        """)
        c.commit(); c.close()
        with self.assertRaises(sqlite3.IntegrityError): ResearchStore(path)
        c=sqlite3.connect(path)
        try:
            self.assertEqual(c.execute("SELECT approval_id,decision_id,status FROM approval_events").fetchall(),[("a","d","approved")])
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_artifacts'").fetchone())
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],3)
            c.execute("DROP TRIGGER fail_v4"); c.commit()
        finally: c.close()
        ResearchStore(path)
        c=sqlite3.connect(path)
        try: self.assertEqual(c.execute("SELECT approval_id,decision_id,status FROM approval_events").fetchall(),[("a","d","approved")])
        finally: c.close()

    def test_expected_v3_approval_schema_migrates_and_keeps_history(self):
        path=Path(self.temp.name)/"m14-expected.sqlite"
        project_id,decision_id=self._make_v3_database(path)
        migrated=ProjectStore(path)
        history=migrated.get_decision_approval_history(project_id,decision_id)
        self.assertEqual([(event.approval_id,event.status.value,event.comment) for event in history],[("kept","approved","preserve")])
        self.assertIsNotNone(history[0].event_sequence)
        with migrated.db() as c:
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],6)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(),[])

    def test_m14_guard_rejects_extra_check_before_rebuilding(self):
        path=Path(self.temp.name)/"m14-extra-check.sqlite"
        self._make_v3_database(path)
        c=sqlite3.connect(path)
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("DROP TRIGGER approval_events_immutable_update")
        c.execute("DROP TRIGGER approval_events_immutable_delete")
        c.execute("DROP INDEX idx_approval_events_decision")
        c.execute("ALTER TABLE approval_events RENAME TO approval_events_expected")
        c.execute("""CREATE TABLE approval_events(
            approval_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,decision_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('approved','rejected','changes_requested','revoked')),
            actor TEXT NOT NULL,comment TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
            CHECK(comment!='reserved'),
            FOREIGN KEY(project_id,decision_id) REFERENCES decisions(project_id,decision_id) ON DELETE RESTRICT)""")
        c.execute("INSERT INTO approval_events SELECT * FROM approval_events_expected")
        c.execute("DROP TABLE approval_events_expected")
        c.execute("CREATE INDEX idx_approval_events_decision ON approval_events(project_id,decision_id,created_at)")
        c.execute("CREATE TRIGGER approval_events_immutable_update BEFORE UPDATE ON approval_events BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END")
        c.execute("CREATE TRIGGER approval_events_immutable_delete BEFORE DELETE ON approval_events BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END")
        c.commit(); c.close()
        with self.assertRaisesRegex(RuntimeError,"complete expected M1.3 schema"):
            ResearchStore(path)
        c=sqlite3.connect(path)
        try:
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],3)
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_artifacts'").fetchone())
        finally:c.close()

    def test_m14_guard_rejects_incoming_foreign_key_before_rebuilding(self):
        path=Path(self.temp.name)/"m14-incoming.sqlite"
        self._make_v3_database(path)
        c=sqlite3.connect(path)
        c.execute("CREATE TABLE approval_reference(id TEXT PRIMARY KEY, approval_id TEXT REFERENCES approval_events(approval_id))")
        c.commit(); c.close()
        with self.assertRaisesRegex(RuntimeError,"incoming approval_events foreign key"):
            ResearchStore(path)
        c=sqlite3.connect(path)
        try:
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],3)
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_artifacts'").fetchone())
        finally:c.close()

    def test_project_artifact_and_workflow_cli_commands(self):
        import contextlib,io
        from src import research_cli
        captured=io.StringIO()
        with contextlib.redirect_stdout(captured):
            research_cli.main(["project","artifact","create",self.project.project_id,"--stage","P1",
                "--type","problem_statement","--title","CLI artifact","--content","content","--database",str(self.db_path)])
        self.assertIn("Created artifact:",captured.getvalue())
        with contextlib.redirect_stdout(captured):
            research_cli.main(["project","workflow","start",self.project.project_id,"P1","--type","planning","--database",str(self.db_path)])
        self.assertIn("Workflow ",captured.getvalue())
        with contextlib.redirect_stdout(captured):
            research_cli.main(["project","artifact","list",self.project.project_id,"--database",str(self.db_path)])
            research_cli.main(["project","workflow","list",self.project.project_id,"--database",str(self.db_path)])
        self.assertIn("CLI artifact",captured.getvalue())
        self.assertIn("planning",captured.getvalue())
if __name__ == "__main__":
    unittest.main()
