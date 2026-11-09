import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.research.models import (
    ProjectCreate, ProjectStageKey, ProjectStageStatus, ProjectStatus,
)
from src.research.storage import ResearchStore


def make_m1_1_database(path: Path) -> None:
    """Build an M1.1 database with linked research rows and project metadata."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_versions VALUES(1,'2026-01-01T00:00:00+00:00');
        CREATE TABLE research_runs(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE research_questions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id),text TEXT NOT NULL);
        CREATE TABLE sources(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id),question_id TEXT NOT NULL REFERENCES research_questions(id),url TEXT NOT NULL,title TEXT NOT NULL,publisher TEXT NOT NULL,published_at TEXT,retrieved_at TEXT NOT NULL,content TEXT NOT NULL,search_snippet TEXT NOT NULL,search_score REAL);
        CREATE TABLE evidence(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id),question_id TEXT NOT NULL REFERENCES research_questions(id),source_id TEXT NOT NULL REFERENCES sources(id),claim TEXT NOT NULL,passage TEXT NOT NULL,relation TEXT NOT NULL,verification_status TEXT NOT NULL,verification_reason TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE hackathon_projects(
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
            description TEXT,
            problem_domain TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','archived')) DEFAULT 'active',
            current_stage TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            CHECK((status='active' AND archived_at IS NULL) OR (status='archived' AND archived_at IS NOT NULL))
        );
        INSERT INTO research_runs VALUES('run-1','preserve this prompt','2026-01-01T00:00:00+00:00');
        INSERT INTO research_questions(id,run_id,text) VALUES('question-1','run-1','preserve this question');
        INSERT INTO sources VALUES('source-1','run-1','question-1','https://example.test','Title','Publisher',NULL,'2026-01-01T00:00:00+00:00','Captured passage','Snippet',0.9);
        INSERT INTO evidence VALUES('evidence-1','run-1','question-1','source-1','Claim','Captured passage','supports','verified','exact passage','2026-01-01T00:00:00+00:00');
        INSERT INTO hackathon_projects VALUES('project-active','Active legacy','description','domain','active',NULL,'2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00',NULL);
        INSERT INTO hackathon_projects VALUES('project-archived','Archived legacy','old description','old domain','archived',NULL,'2025-01-01T00:00:00+00:00','2025-02-01T00:00:00+00:00','2025-02-01T00:00:00+00:00');
    """)
    connection.commit()
    connection.close()


class ProjectStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "stages.sqlite"
        self.store = ResearchStore(self.database)
        self.project = self.store.create_project(ProjectCreate(name="Stage test"))

    def tearDown(self):
        self.temp.cleanup()

    def test_new_project_gets_exactly_eight_stages(self):
        stages = self.store.list_project_stages(self.project.project_id)
        self.assertEqual(len(stages), 8)
        self.assertEqual([stage.stage_key.value for stage in stages], [f"P{i}" for i in range(1, 9)])
        self.assertTrue(all(stage.status == ProjectStageStatus.PENDING for stage in stages))
        self.assertEqual(self.project.current_stage, "P1")
        self.assertEqual(len(self.store.initialize_project_stages(self.project.project_id)), 8)

    def test_all_legal_transitions_are_recorded(self):
        stage = self.store.start_project_stage(self.project.project_id, "P1")
        self.assertEqual(stage.status, ProjectStageStatus.IN_PROGRESS)
        self.assertEqual(self.store.block_project_stage(self.project.project_id, "P1").status, ProjectStageStatus.BLOCKED)
        self.assertEqual(self.store.resume_project_stage(self.project.project_id, "P1").status, ProjectStageStatus.IN_PROGRESS)
        self.assertEqual(self.store.complete_project_stage(self.project.project_id, "P1").status, ProjectStageStatus.COMPLETED)
        self.assertEqual(self.store.reopen_project_stage(self.project.project_id, "P1").status, ProjectStageStatus.IN_PROGRESS)
        self.assertEqual(self.store.skip_project_stage(self.project.project_id, "P1", "Work waived").status,
                         ProjectStageStatus.SKIPPED)
        self.assertEqual(self.store.reopen_project_stage(self.project.project_id, "P1").status,
                         ProjectStageStatus.IN_PROGRESS)
        self.store.block_project_stage(self.project.project_id, "P1")
        self.assertEqual(self.store.skip_project_stage(self.project.project_id, "P1", "Blocked and waived").status,
                         ProjectStageStatus.SKIPPED)
        history = self.store.get_project_stage_history(self.project.project_id, "P1")
        self.assertEqual([(item.from_status.value, item.to_status.value) for item in history], [
            ("pending", "in_progress"), ("in_progress", "blocked"), ("blocked", "in_progress"),
            ("in_progress", "completed"), ("completed", "in_progress"),
            ("in_progress", "skipped"), ("skipped", "in_progress"),
            ("in_progress", "blocked"), ("blocked", "skipped"),
        ])
        self.assertEqual(history[5].note, "Work waived")
        self.assertEqual(history[6].note, "Explicitly reopened.")
        self.assertEqual(history[-1].note, "Blocked and waived")

    def test_illegal_transitions_and_skip_without_reason_fail(self):
        with self.assertRaisesRegex(ValueError, "Invalid stage transition"):
            self.store.transition_project_stage(self.project.project_id, "P1", ProjectStageStatus.COMPLETED)
        with self.assertRaisesRegex(ValueError, "reason is required"):
            self.store.skip_project_stage(self.project.project_id, "P1", "   ")
        self.assertEqual(self.store.get_project_stage_history(self.project.project_id), [])
        self.store.start_project_stage(self.project.project_id, "P1")
        self.store.complete_project_stage(self.project.project_id, "P1")
        with self.assertRaisesRegex(ValueError, "Invalid stage transition"):
            self.store.transition_project_stage(self.project.project_id, "P1", "skipped", reason="too late")

    def test_current_stage_derivation_and_project_completion(self):
        self.store.start_project_stage(self.project.project_id, "P1")
        self.store.complete_project_stage(self.project.project_id, "P1")
        self.assertEqual(self.store.get_project(self.project.project_id).current_stage, "P2")
        for index in range(2, 9):
            self.store.skip_project_stage(self.project.project_id, f"P{index}", f"Stage {index} waived")
        project = self.store.get_project(self.project.project_id)
        self.assertIsNone(project.current_stage)
        self.assertEqual(project.status, ProjectStatus.COMPLETED)
        reopened = self.store.reopen_project_stage(self.project.project_id, "P4")
        self.assertEqual(reopened.status, ProjectStageStatus.IN_PROGRESS)
        project = self.store.get_project(self.project.project_id)
        self.assertEqual(project.current_stage, "P4")
        self.assertEqual(project.status, ProjectStatus.ACTIVE)

    def test_cli_stage_commands_work_and_report_invalid_transitions(self):
        import contextlib
        import io
        from src import research_cli

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            # P1/P2 have stage-specific artifact gates; cover generic transitions
            # on P3 and cover P1/P2 gates in their own service tests.
            self.assertIsNone(research_cli.main(["project", "stage", "start", self.project.project_id, "P3",
                                                 "--database", str(self.database)]))
            self.assertIsNone(research_cli.main(["project", "stage", "complete", self.project.project_id, "P3",
                                                 "--database", str(self.database)]))
            self.assertIsNone(research_cli.main(["project", "stage", "history", self.project.project_id, "P3",
                                                 "--database", str(self.database)]))
        self.assertIn("P3 complete: completed", output.getvalue())
        self.assertIn("in_progress -> completed", output.getvalue())
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = research_cli.main(["project", "stage", "complete", self.project.project_id, "P3",
                                        "--database", str(self.database)])
        self.assertEqual(result, 2)
        self.assertIn("must be in in_progress", errors.getvalue())

    def test_transition_rolls_back_status_history_and_project_pointer_together(self):
        with self.store.db() as connection:
            connection.execute("""CREATE TRIGGER reject_stage_pointer BEFORE UPDATE OF current_stage ON hackathon_projects
                BEGIN SELECT RAISE(ABORT,'injected transition failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.start_project_stage(self.project.project_id, "P1")
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P1").status,
                         ProjectStageStatus.PENDING)
        self.assertEqual(self.store.get_project_stage_history(self.project.project_id), [])
        self.assertEqual(self.store.get_project(self.project.project_id).current_stage, "P1")

    def test_stage_history_is_append_only_even_for_replace(self):
        self.store.start_project_stage(self.project.project_id,"P1")
        item=self.store.get_project_stage_history(self.project.project_id,"P1")[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as c:
                c.execute("UPDATE project_stage_history SET note='overwrite' WHERE history_id=?",(item.history_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as c:
                c.execute("DELETE FROM project_stage_history WHERE history_id=?",(item.history_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as c:
                c.execute("INSERT OR REPLACE INTO project_stage_history VALUES(?,?,?,?,?,?,?,?)",
                    (item.history_id,item.project_id,item.stage_key.value,item.from_status.value,item.to_status.value,
                     item.changed_at.isoformat(),item.actor,"replacement"))
        with self.store.db() as c:
            self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0],1)
            self.assertEqual(c.execute("PRAGMA recursive_triggers").fetchone()[0],1)

    def test_projects_are_isolated_and_archived_projects_are_read_only(self):
        other = self.store.create_project(ProjectCreate(name="Other"))
        self.store.start_project_stage(self.project.project_id, "P1")
        self.assertEqual(self.store.get_project_stage(other.project_id, "P1").status, ProjectStageStatus.PENDING)
        archived = self.store.archive_project(self.project.project_id)
        with self.assertRaisesRegex(ValueError, "Archived projects cannot transition stages"):
            self.store.start_project_stage(self.project.project_id, "P2")
        self.assertEqual(self.store.get_project(self.project.project_id), archived)
        self.assertEqual(self.store.get_project_stage(self.project.project_id, "P1").status,
                         ProjectStageStatus.IN_PROGRESS)

    def test_migration_backfills_existing_projects_and_preserves_research_relationships(self):
        legacy = Path(self.temp.name) / "legacy.sqlite"
        make_m1_1_database(legacy)
        migrated = ResearchStore(legacy)
        for project_id in ("project-active", "project-archived"):
            self.assertEqual(len(migrated.list_project_stages(project_id)), 8)
            self.assertEqual(migrated.get_project(project_id).current_stage, "P1")
        self.assertEqual(migrated.get_project("project-active").description, "description")
        archived = migrated.get_project("project-archived")
        self.assertEqual(archived.status, ProjectStatus.ARCHIVED)
        self.assertEqual(archived.archived_at.isoformat(), "2025-02-01T00:00:00+00:00")
        with migrated.db() as connection:
            self.assertEqual(connection.execute("SELECT id,prompt FROM research_runs").fetchall()[0][:],
                             ("run-1", "preserve this prompt"))
            self.assertEqual(connection.execute("SELECT id,run_id,text FROM research_questions").fetchall()[0][:],
                             ("question-1", "run-1", "preserve this question"))
            self.assertEqual(connection.execute("SELECT id,question_id FROM sources").fetchall()[0][:],
                             ("source-1", "question-1"))
            self.assertEqual(connection.execute("SELECT id,source_id,passage FROM evidence").fetchall()[0][:],
                             ("evidence-1", "source-1", "Captured passage"))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual([row[0] for row in connection.execute("SELECT version FROM schema_versions ORDER BY version").fetchall()], [1, 2, 3, 4, 5, 6])
        reopened = ResearchStore(legacy)
        self.assertEqual(len(reopened.list_project_stages("project-active")), 8)
        with reopened.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_versions").fetchone()[0], 6)

    def test_persisted_m1_2_database_migrates_without_changing_stage_state_or_history(self):
        from unittest.mock import patch
        legacy=Path(self.temp.name)/"persisted-m12.sqlite"
        make_m1_1_database(legacy)
        no_migration=staticmethod(lambda connection: None)
        with patch.object(ResearchStore,"_migrate_decisions",no_migration), \
             patch.object(ResearchStore,"_migrate_artifacts_workflows",no_migration), \
             patch.object(ResearchStore,"_migrate_research_projects",no_migration), \
             patch.object(ResearchStore,"_migrate_integrity_v6",no_migration):
            m12=ResearchStore(legacy)
            m12.start_project_stage("project-active","P1")
            m12.block_project_stage("project-active","P1",note="waiting for access")
            m12.skip_project_stage("project-active","P2","not needed")
            with m12.db() as c:
                self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],2)
                before_stages=c.execute("SELECT * FROM project_stages WHERE project_id='project-active' ORDER BY stage_key").fetchall()
                before_history=c.execute("SELECT * FROM project_stage_history WHERE project_id='project-active' ORDER BY rowid").fetchall()
                before_project=c.execute("SELECT status,current_stage FROM hackathon_projects WHERE project_id='project-active'").fetchone()
        migrated=ResearchStore(legacy)
        with migrated.db() as c:
            after_stages=c.execute("SELECT * FROM project_stages WHERE project_id='project-active' ORDER BY stage_key").fetchall()
            after_history=c.execute("SELECT * FROM project_stage_history WHERE project_id='project-active' ORDER BY rowid").fetchall()
            after_project=c.execute("SELECT status,current_stage FROM hackathon_projects WHERE project_id='project-active'").fetchone()
            self.assertEqual([tuple(row) for row in after_stages],[tuple(row) for row in before_stages])
            self.assertEqual([tuple(row) for row in after_history],[tuple(row) for row in before_history])
            self.assertEqual(tuple(after_project),tuple(before_project))
            self.assertEqual(c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0],6)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(),[])

    def test_failed_stage_migration_rolls_back_and_can_be_retried(self):
        legacy = Path(self.temp.name) / "rollback.sqlite"
        make_m1_1_database(legacy)
        connection = sqlite3.connect(legacy)
        connection.execute("""CREATE TRIGGER fail_v2 BEFORE INSERT ON schema_versions WHEN NEW.version=2
            BEGIN SELECT RAISE(ABORT,'injected migration failure'); END""")
        connection.commit()
        connection.close()

        with self.assertRaises(sqlite3.IntegrityError):
            ResearchStore(legacy)
        connection = sqlite3.connect(legacy)
        self.assertEqual(connection.execute("SELECT version FROM schema_versions ORDER BY version").fetchall(), [(1,)])
        self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_stages'").fetchone())
        self.assertEqual(connection.execute("SELECT project_id,name,status,archived_at FROM hackathon_projects ORDER BY project_id").fetchall(), [
            ("project-active", "Active legacy", "active", None),
            ("project-archived", "Archived legacy", "archived", "2025-02-01T00:00:00+00:00"),
        ])
        connection.execute("DROP TRIGGER fail_v2")
        connection.commit()
        connection.close()

        retried = ResearchStore(legacy)
        self.assertEqual(len(retried.list_project_stages("project-active")), 8)
        with retried.db() as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT version FROM schema_versions ORDER BY version").fetchall()], [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
