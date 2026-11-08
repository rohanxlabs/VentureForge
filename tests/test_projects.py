import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from src.research.models import ProjectCreate, ProjectUpdate, ProjectStatus
from src.research.storage import ResearchStore
from src import research_cli


class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "projects.sqlite"
        self.store = ResearchStore(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_validate_retrieve_list_and_reopen(self):
        project = self.store.create_project(ProjectCreate(name="  Factory Copilot ", problem_domain="Manufacturing"))
        self.assertEqual(project.name, "Factory Copilot")
        self.assertEqual(self.store.get_project(project.project_id), project)
        self.assertEqual(self.store.list_projects(), [project])
        reopened = ResearchStore(self.database)
        self.assertEqual(reopened.get_project(project.project_id), project)
        with self.assertRaises(ValidationError):
            ProjectCreate(name="   ")
        with self.assertRaises(ValidationError):
            ProjectCreate(name="")

    def test_updates_and_archive_keeps_project_readable(self):
        project = self.store.create_project(ProjectCreate(name="Before"))
        updated = self.store.update_project(project.project_id, ProjectUpdate(name="After", description="Details"))
        self.assertEqual(updated.name, "After")
        self.assertEqual(updated.description, "Details")
        archived = self.store.archive_project(project.project_id)
        self.assertEqual(archived.status, ProjectStatus.ARCHIVED)
        self.assertIsNotNone(archived.archived_at)
        self.assertEqual(self.store.get_project(project.project_id), archived)
        self.assertEqual(self.store.list_projects(include_archived=False), [])
        self.assertEqual(self.store.list_projects(), [archived])
        with self.assertRaises(ValueError):
            self.store.update_project(project.project_id, ProjectUpdate(name="No"))

    def test_unknown_project_is_a_clear_not_found(self):
        with self.assertRaisesRegex(KeyError, "Unknown project"):
            self.store.get_project("missing")
        with self.assertRaisesRegex(KeyError, "Unknown project"):
            self.store.update_project("missing", ProjectUpdate(name="Still missing"))

    def test_archived_project_cannot_be_updated(self):
        project = self.store.create_project(ProjectCreate(name="Before"))
        archived = self.store.archive_project(project.project_id)
        with self.assertRaisesRegex(ValueError, "Archived projects cannot be updated"):
            self.store.update_project(project.project_id, ProjectUpdate(name="After"))
        self.assertEqual(self.store.get_project(project.project_id), archived)

    def test_archive_committed_before_conditional_update_prevents_race(self):
        project = self.store.create_project(ProjectCreate(name="Before"))
        original_db = self.store.db
        archiver = ResearchStore(self.database)
        intercepted = False

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, parameters=()):
                nonlocal intercepted
                if sql.startswith("UPDATE hackathon_projects SET") and not intercepted:
                    intercepted = True
                    archiver.archive_project(project.project_id)
                return self.connection.execute(sql, parameters)

        @contextmanager
        def race_db():
            with original_db() as connection:
                yield ConnectionProxy(connection)

        with patch.object(self.store, "db", race_db):
            with self.assertRaisesRegex(ValueError, "Archived projects cannot be updated"):
                self.store.update_project(project.project_id, ProjectUpdate(name="After"))

        saved = self.store.get_project(project.project_id)
        self.assertTrue(intercepted)
        self.assertEqual(saved.name, "Before")
        self.assertEqual(saved.status, ProjectStatus.ARCHIVED)

    def test_databases_are_isolated(self):
        other = ResearchStore(Path(self.temp.name) / "other.sqlite")
        project = self.store.create_project(ProjectCreate(name="Only here"))
        self.assertEqual(other.list_projects(), [])
        self.assertEqual(self.store.get_project(project.project_id).name, "Only here")

    def test_migration_runs_once_and_preserves_research_records(self):
        legacy = Path(self.temp.name) / "legacy.sqlite"
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE research_runs(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO research_runs VALUES('old-run','old prompt','2025-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        ResearchStore(legacy)
        ResearchStore(legacy)
        conn = sqlite3.connect(legacy)
        self.assertEqual(conn.execute("SELECT * FROM research_runs").fetchall(),
                         [("old-run", "old prompt", "2025-01-01T00:00:00+00:00", None)])
        self.assertEqual(conn.execute("SELECT version FROM schema_versions ORDER BY version").fetchall(), [(1,), (2,), (3,), (4,), (5,), (6,)])
        self.assertEqual(conn.execute("PRAGMA table_info(research_runs)").fetchall()[1][1], "prompt")
        conn.close()


class ProjectCliTests(unittest.TestCase):
    def test_project_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            db = str(Path(temp) / "cli.sqlite")
            self.assertIsNone(research_cli.main(["project", "create", "--name", "CLI project", "--database", db]))
            project = ResearchStore(db).list_projects()[0]
            self.assertIsNone(research_cli.main(["project", "show", project.project_id, "--database", db]))
            self.assertIsNone(research_cli.main(["project", "update", project.project_id, "--name", "Updated", "--database", db]))
            self.assertIsNone(research_cli.main(["project", "archive", project.project_id, "--database", db]))
            self.assertIsNone(research_cli.main(["project", "list", "--database", db]))

    def test_cli_returns_nonzero_for_invalid_and_missing_projects(self):
        with tempfile.TemporaryDirectory() as temp:
            db = str(Path(temp) / "cli.sqlite")
            self.assertEqual(research_cli.main(["project", "create", "--name", "   ", "--database", db]), 2)
            self.assertEqual(research_cli.main(["project", "show", "missing", "--database", db]), 2)


if __name__ == "__main__":
    unittest.main()
