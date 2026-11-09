from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path
from typing import Iterator
from datetime import datetime, timezone
from uuid import uuid4
from src.research.models import (
    PROJECT_STAGE_NAMES, ApprovalEvent, ApprovalEventStatus, DecisionCreate,
    DecisionEvidenceLink, DecisionEvidenceRelationship, DecisionOption,
    DecisionOptionCreate, DecisionUpdate, EvidenceRecord, HackathonProject,
    ProjectArtifact, ProjectCreate, ProjectDecision, ProjectResearchRunSummary,
    ProjectStage, ProjectStageHistory, ProjectStageKey, ProjectStageStatus,
    ProjectStatus, ProjectUpdate, ProjectWorkflowRun, ResearchPlan, ResearchQuestion,
    ResearchRun, SourceSnapshot, WorkflowRunCreate, WorkflowRunEvent, WorkflowStatus,
)


class ProjectNotFoundError(KeyError):
    """A project or research run referenced by a project-scoped research call is missing."""


class ProjectAssociationError(ValueError):
    """A research run cannot be associated with or scoped to the requested project."""

_STAGE_TRANSITIONS = {
    ProjectStageStatus.PENDING: {ProjectStageStatus.IN_PROGRESS, ProjectStageStatus.SKIPPED},
    ProjectStageStatus.IN_PROGRESS: {ProjectStageStatus.COMPLETED, ProjectStageStatus.BLOCKED, ProjectStageStatus.SKIPPED},
    ProjectStageStatus.BLOCKED: {ProjectStageStatus.IN_PROGRESS, ProjectStageStatus.SKIPPED},
    ProjectStageStatus.COMPLETED: {ProjectStageStatus.IN_PROGRESS},
    ProjectStageStatus.SKIPPED: {ProjectStageStatus.IN_PROGRESS},
}

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS research_runs(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_questions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,text TEXT NOT NULL,priority TEXT NOT NULL DEFAULT 'high',source_categories_json TEXT NOT NULL DEFAULT '[]',evidence_required TEXT NOT NULL DEFAULT '',decision_impact TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,question_id TEXT NOT NULL REFERENCES research_questions(id) ON DELETE CASCADE,url TEXT NOT NULL,title TEXT NOT NULL,publisher TEXT NOT NULL,published_at TEXT,retrieved_at TEXT NOT NULL,content TEXT NOT NULL,search_snippet TEXT NOT NULL,search_score REAL,source_categories_json TEXT NOT NULL DEFAULT '[]',content_origin TEXT NOT NULL DEFAULT 'provider_raw_content',content_type TEXT NOT NULL DEFAULT '',final_url TEXT NOT NULL DEFAULT '',fetch_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_sources_run_url ON sources(run_id,url);
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,question_id TEXT NOT NULL REFERENCES research_questions(id) ON DELETE CASCADE,source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,claim TEXT NOT NULL,passage TEXT NOT NULL,relation TEXT NOT NULL,verification_status TEXT NOT NULL,verification_reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_plans(plan_id TEXT PRIMARY KEY,run_id TEXT NOT NULL UNIQUE REFERENCES research_runs(id) ON DELETE CASCADE,plan_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,approved_at TEXT);
CREATE TABLE IF NOT EXISTS solution_designs(design_id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,status TEXT NOT NULL,created_at TEXT NOT NULL,design_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pitch_packages(package_id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,design_id TEXT NOT NULL REFERENCES solution_designs(design_id),status TEXT NOT NULL,created_at TEXT NOT NULL,package_json TEXT NOT NULL);
"""

class ResearchStore:
    def __init__(self, path: str | Path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as c:
            c.executescript(SCHEMA)
            # Compatibility bootstrap for legacy research layouts predating
            # versioned project migrations. Keep this additive, idempotent set atomic.
            with c:
                c.execute("BEGIN IMMEDIATE")
                self._migrate_columns(c,"research_questions",{
                    "priority":"TEXT NOT NULL DEFAULT 'high'",
                    "source_categories_json":"TEXT NOT NULL DEFAULT '[]'",
                    "evidence_required":"TEXT NOT NULL DEFAULT ''",
                    "decision_impact":"TEXT NOT NULL DEFAULT ''",
                    "status":"TEXT NOT NULL DEFAULT 'pending'",
                })
                self._migrate_columns(c,"sources",{
                    "source_categories_json":"TEXT NOT NULL DEFAULT '[]'",
                    "content_origin":"TEXT NOT NULL DEFAULT 'provider_raw_content'",
                    "content_type":"TEXT NOT NULL DEFAULT ''",
                    "final_url":"TEXT NOT NULL DEFAULT ''",
                    "fetch_error":"TEXT NOT NULL DEFAULT ''",
                })
            self._migrate_projects(c)
            self._migrate_stages(c)
            self._migrate_decisions(c)
            self._migrate_artifacts_workflows(c)
            self._migrate_research_projects(c)
            self._migrate_integrity_v6(c)

    @staticmethod
    def _migrate_projects(c: sqlite3.Connection) -> None:
        """Apply the additive project foundation migration exactly once."""
        with c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            current = c.execute("SELECT COALESCE(MAX(version), 0) FROM schema_versions").fetchone()[0]
            if current < 1:
                c.execute("""CREATE TABLE hackathon_projects(
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
                    description TEXT,
                    problem_domain TEXT,
                    status TEXT NOT NULL CHECK(status IN ('active','archived')) DEFAULT 'active',
                    current_stage TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT,
                    CHECK((status='active' AND archived_at IS NULL) OR
                          (status='archived' AND archived_at IS NOT NULL))
                )""")
                c.execute("INSERT INTO schema_versions(version,applied_at) VALUES(1,?)",
                          (datetime.now(timezone.utc).isoformat(),))

    @staticmethod
    def _upgrade_project_status_constraint(c: sqlite3.Connection) -> None:
        columns = tuple(row["name"] for row in c.execute("PRAGMA table_info(hackathon_projects)"))
        expected = ("project_id", "name", "description", "problem_domain", "status",
                    "current_stage", "created_at", "updated_at", "archived_at")
        if columns != expected:
            raise RuntimeError("Project status migration stopped: hackathon_projects has an unexpected column layout.")

        definition = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='hackathon_projects'").fetchone()
        normalized = "".join(definition["sql"].lower().split()) if definition and definition["sql"] else ""
        expected_definition = """CREATE TABLE hackathon_projects(
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
            description TEXT,
            problem_domain TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','archived')) DEFAULT 'active',
            current_stage TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            CHECK((status='active' AND archived_at IS NULL) OR
                  (status='archived' AND archived_at IS NOT NULL))
        )"""
        expected_normalized = "".join(expected_definition.lower().split())
        if normalized != expected_normalized:
            raise RuntimeError("Project status migration stopped: hackathon_projects is not the expected M1.1 schema.")

        # Rebuilding a SQLite table drops its attached indexes/triggers and is unsafe
        # when other tables reference it. Fail closed if this schema has extensions.
        extras = c.execute("""SELECT name FROM sqlite_master
            WHERE (type='index' AND tbl_name='hackathon_projects' AND sql IS NOT NULL)
               OR (type='trigger' AND tbl_name='hackathon_projects')""").fetchall()
        if extras:
            raise RuntimeError("Project status migration stopped: custom indexes or triggers reference hackathon_projects.")
        tables = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for table_row in tables:
            table_name = table_row["name"]
            quoted_name = '"' + table_name.replace('"', '""') + '"'
            refs = c.execute(f"PRAGMA foreign_key_list({quoted_name})").fetchall()
            if table_name != "hackathon_projects" and any(row["table"] == "hackathon_projects" for row in refs):
                raise RuntimeError("Project status migration stopped: another table references hackathon_projects.")

        c.execute("""CREATE TABLE hackathon_projects_m2(
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
            description TEXT,
            problem_domain TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','completed','archived')) DEFAULT 'active',
            current_stage TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            CHECK((status IN ('active','completed') AND archived_at IS NULL) OR
                  (status='archived' AND archived_at IS NOT NULL))
        )""")
        c.execute("""INSERT INTO hackathon_projects_m2
            (project_id,name,description,problem_domain,status,current_stage,created_at,updated_at,archived_at)
            SELECT project_id,name,description,problem_domain,status,current_stage,created_at,updated_at,archived_at
            FROM hackathon_projects""")
        c.execute("DROP TABLE hackathon_projects")
        c.execute("ALTER TABLE hackathon_projects_m2 RENAME TO hackathon_projects")

    @staticmethod
    def _migrate_stages(c: sqlite3.Connection) -> None:
        with c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT COALESCE(MAX(version), 0) FROM schema_versions").fetchone()[0]
            if current >= 2:
                return
            ResearchStore._upgrade_project_status_constraint(c)
            c.execute("""CREATE TABLE project_stages(
                project_id TEXT NOT NULL REFERENCES hackathon_projects(project_id) ON DELETE RESTRICT,
                stage_key TEXT NOT NULL CHECK(stage_key IN ('P1','P2','P3','P4','P5','P6','P7','P8')),
                status TEXT NOT NULL CHECK(status IN ('pending','in_progress','completed','blocked','skipped')) DEFAULT 'pending',
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id,stage_key)
            )""")
            c.execute("""CREATE TABLE project_stage_history(
                history_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                stage_key TEXT NOT NULL,
                from_status TEXT NOT NULL CHECK(from_status IN ('pending','in_progress','completed','blocked','skipped')),
                to_status TEXT NOT NULL CHECK(to_status IN ('pending','in_progress','completed','blocked','skipped')),
                changed_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(project_id,stage_key) REFERENCES project_stages(project_id,stage_key) ON DELETE RESTRICT
            )""")
            c.execute("CREATE INDEX idx_project_stages_project_status ON project_stages(project_id,status)")
            c.execute("CREATE INDEX idx_project_stage_history_project_stage ON project_stage_history(project_id,stage_key,changed_at)")

            now = datetime.now(timezone.utc).isoformat()
            project_ids = [row["project_id"] for row in c.execute("SELECT project_id FROM hackathon_projects").fetchall()]
            for project_id in project_ids:
                ResearchStore._insert_initial_stages(c, project_id, now)
                c.execute("UPDATE hackathon_projects SET current_stage='P1' WHERE project_id=? AND status!='completed'",
                          (project_id,))
            c.execute("INSERT INTO schema_versions(version,applied_at) VALUES(2,?)", (now,))

    @staticmethod
    def _migrate_decisions(c: sqlite3.Connection) -> None:
        with c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT COALESCE(MAX(version), 0) FROM schema_versions").fetchone()[0]
            if current >= 3:
                return
            c.execute("""CREATE TABLE decisions(
                decision_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES hackathon_projects(project_id) ON DELETE RESTRICT,
                stage_key TEXT,
                title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 240),
                description TEXT NOT NULL DEFAULT '',
                decision_type TEXT NOT NULL DEFAULT 'general',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id,decision_id),
                FOREIGN KEY(project_id,stage_key) REFERENCES project_stages(project_id,stage_key) ON DELETE RESTRICT
            )""")
            c.execute("CREATE INDEX idx_decisions_project_status ON decisions(project_id,status,created_at)")
            c.execute("""CREATE TABLE decision_options(
                option_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 240),
                description TEXT NOT NULL DEFAULT '',
                advantages_json TEXT NOT NULL DEFAULT '[]',
                disadvantages_json TEXT NOT NULL DEFAULT '[]',
                assumptions_json TEXT NOT NULL DEFAULT '[]',
                estimated_cost TEXT,
                feasibility TEXT,
                is_recommended INTEGER NOT NULL DEFAULT 0 CHECK(is_recommended IN (0,1)),
                is_selected INTEGER NOT NULL DEFAULT 0 CHECK(is_selected IN (0,1)),
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id,decision_id) REFERENCES decisions(project_id,decision_id) ON DELETE RESTRICT
            )""")
            c.execute("CREATE INDEX idx_decision_options_decision ON decision_options(project_id,decision_id,created_at)")
            c.execute("CREATE UNIQUE INDEX idx_decision_option_recommended ON decision_options(project_id,decision_id) WHERE is_recommended=1")
            c.execute("CREATE UNIQUE INDEX idx_decision_option_selected ON decision_options(project_id,decision_id) WHERE is_selected=1")
            c.execute("""CREATE TABLE decision_evidence(
                decision_id TEXT NOT NULL REFERENCES decisions(decision_id) ON DELETE RESTRICT,
                evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE RESTRICT,
                relationship_type TEXT NOT NULL CHECK(relationship_type IN ('supports','contradicts','contextualizes')),
                explanation TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY(decision_id,evidence_id)
            )""")
            c.execute("CREATE INDEX idx_decision_evidence_evidence ON decision_evidence(evidence_id)")
            c.execute("""CREATE TABLE approval_events(
                approval_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('approved','rejected','changes_requested','revoked')),
                actor TEXT NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id,decision_id) REFERENCES decisions(project_id,decision_id) ON DELETE RESTRICT
            )""")
            c.execute("CREATE INDEX idx_approval_events_decision ON approval_events(project_id,decision_id,created_at)")
            c.execute("CREATE TRIGGER approval_events_immutable_update BEFORE UPDATE ON approval_events BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END")
            c.execute("CREATE TRIGGER approval_events_immutable_delete BEFORE DELETE ON approval_events BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END")
            c.execute("INSERT INTO schema_versions(version,applied_at) VALUES(3,?)", (datetime.now(timezone.utc).isoformat(),))

    @staticmethod
    def _migrate_artifacts_workflows(c: sqlite3.Connection) -> None:
        """Add versioned artifacts, generalized approvals, and workflow records."""
        with c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT COALESCE(MAX(version), 0) FROM schema_versions").fetchone()[0]
            if current >= 4:
                return
            ResearchStore._validate_m3_approval_schema(c)
            c.execute("""CREATE TABLE project_artifacts(
                artifact_id TEXT PRIMARY KEY,
                artifact_key TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES hackathon_projects(project_id) ON DELETE RESTRICT,
                stage_key TEXT,
                decision_id TEXT,
                artifact_type TEXT NOT NULL,
                title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 240),
                description TEXT NOT NULL DEFAULT '',
                content TEXT,
                storage_ref TEXT,
                version INTEGER NOT NULL CHECK(version >= 1),
                status TEXT NOT NULL CHECK(status IN ('draft','ready','superseded','archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(content IS NULL OR storage_ref IS NULL),
                UNIQUE(project_id,artifact_key,version),
                UNIQUE(project_id,artifact_id),
                FOREIGN KEY(project_id,stage_key) REFERENCES project_stages(project_id,stage_key) ON DELETE RESTRICT,
                FOREIGN KEY(project_id,decision_id) REFERENCES decisions(project_id,decision_id) ON DELETE RESTRICT
            )""")
            c.execute("CREATE INDEX idx_project_artifacts_project_stage ON project_artifacts(project_id,stage_key,created_at)")
            c.execute("CREATE INDEX idx_project_artifacts_key_version ON project_artifacts(project_id,artifact_key,version)")
            c.execute("DROP INDEX IF EXISTS idx_approval_events_decision")
            c.execute("ALTER TABLE approval_events RENAME TO decision_approval_events_m3")
            c.execute("""CREATE TABLE approval_events(
                approval_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                decision_id TEXT,
                artifact_id TEXT,
                status TEXT NOT NULL CHECK(status IN ('approved','rejected','changes_requested','revoked')),
                actor TEXT NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                CHECK((decision_id IS NOT NULL AND artifact_id IS NULL) OR
                      (decision_id IS NULL AND artifact_id IS NOT NULL)),
                FOREIGN KEY(project_id,decision_id) REFERENCES decisions(project_id,decision_id) ON DELETE RESTRICT,
                FOREIGN KEY(project_id,artifact_id) REFERENCES project_artifacts(project_id,artifact_id) ON DELETE RESTRICT
            )""")
            c.execute("""INSERT INTO approval_events(approval_id,project_id,decision_id,artifact_id,status,actor,comment,created_at)
                SELECT approval_id,project_id,decision_id,NULL,status,actor,comment,created_at FROM decision_approval_events_m3""")
            c.execute("DROP TABLE decision_approval_events_m3")
            c.execute("CREATE INDEX idx_approval_events_decision ON approval_events(project_id,decision_id,created_at)")
            c.execute("CREATE INDEX idx_approval_events_artifact ON approval_events(project_id,artifact_id,created_at)")
            c.execute("CREATE TRIGGER approval_events_immutable_update BEFORE UPDATE ON approval_events BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END")
            c.execute("CREATE TRIGGER approval_events_immutable_delete BEFORE DELETE ON approval_events BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END")
            c.execute("""CREATE TABLE workflow_runs(
                workflow_run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                stage_key TEXT NOT NULL,
                workflow_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','interrupted')),
                started_at TEXT,
                completed_at TEXT,
                error_summary TEXT,
                input_ref TEXT,
                output_artifact_id TEXT,
                created_at TEXT NOT NULL,
                CHECK((status NOT IN ('completed','failed','interrupted')) OR completed_at IS NOT NULL),
                CHECK(status!='failed' OR length(trim(COALESCE(error_summary,''))) > 0),
                FOREIGN KEY(project_id,stage_key) REFERENCES project_stages(project_id,stage_key) ON DELETE RESTRICT,
                FOREIGN KEY(project_id,output_artifact_id) REFERENCES project_artifacts(project_id,artifact_id) ON DELETE RESTRICT
            )""")
            c.execute("CREATE INDEX idx_workflow_runs_project_stage ON workflow_runs(project_id,stage_key,created_at)")
            c.execute("""CREATE TABLE workflow_run_events(
                event_id TEXT PRIMARY KEY,
                workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(workflow_run_id) ON DELETE RESTRICT,
                from_status TEXT CHECK(from_status IS NULL OR from_status IN ('pending','running','completed','failed','interrupted')),
                to_status TEXT NOT NULL CHECK(to_status IN ('pending','running','completed','failed','interrupted')),
                actor TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                changed_at TEXT NOT NULL
            )""")
            c.execute("CREATE INDEX idx_workflow_run_events_run ON workflow_run_events(workflow_run_id,changed_at)")
            c.execute("CREATE TRIGGER workflow_run_events_immutable_update BEFORE UPDATE ON workflow_run_events BEGIN SELECT RAISE(ABORT,'workflow history is append-only'); END")
            c.execute("CREATE TRIGGER workflow_run_events_immutable_delete BEFORE DELETE ON workflow_run_events BEGIN SELECT RAISE(ABORT,'workflow history is append-only'); END")
            c.execute("INSERT INTO schema_versions(version,applied_at) VALUES(4,?)", (datetime.now(timezone.utc).isoformat(),))

    @staticmethod
    def _normalize_sql(sql: str | None) -> str:
        return "".join((sql or "").lower().split()).rstrip(";")

    @staticmethod
    def _validate_m3_approval_schema(c: sqlite3.Connection) -> None:
        expected_sql = """CREATE TABLE approval_events(
            approval_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('approved','rejected','changes_requested','revoked')),
            actor TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id,decision_id) REFERENCES decisions(project_id,decision_id) ON DELETE RESTRICT
        )"""
        table = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='approval_events'").fetchone()
        if table is None or ResearchStore._normalize_sql(table["sql"]) != ResearchStore._normalize_sql(expected_sql):
            raise RuntimeError("M1.4 migration stopped: approval_events does not match the complete expected M1.3 schema.")

        expected_triggers = {
            "approval_events_immutable_update": "CREATE TRIGGER approval_events_immutable_update BEFORE UPDATE ON approval_events BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END",
            "approval_events_immutable_delete": "CREATE TRIGGER approval_events_immutable_delete BEFORE DELETE ON approval_events BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END",
        }
        triggers = {row["name"]: row["sql"] for row in c.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='approval_events'")}
        if set(triggers) != set(expected_triggers) or any(
            ResearchStore._normalize_sql(triggers[name]) != ResearchStore._normalize_sql(sql)
            for name, sql in expected_triggers.items()
        ):
            raise RuntimeError("M1.4 migration stopped: approval_events triggers differ from the expected M1.3 schema.")

        index_rows = c.execute("PRAGMA index_list(approval_events)").fetchall()
        explicit_indexes = [row for row in index_rows if not row["name"].startswith("sqlite_autoindex")]
        if len(explicit_indexes) != 1 or explicit_indexes[0]["name"] != "idx_approval_events_decision" or explicit_indexes[0]["unique"]:
            raise RuntimeError("M1.4 migration stopped: approval_events indexes differ from the expected M1.3 schema.")
        index_columns = tuple(row["name"] for row in c.execute("PRAGMA index_info(idx_approval_events_decision)"))
        if index_columns != ("project_id", "decision_id", "created_at"):
            raise RuntimeError("M1.4 migration stopped: decision approval index has unexpected columns.")

        foreign_keys = c.execute("PRAGMA foreign_key_list(approval_events)").fetchall()
        if len(foreign_keys) != 2 or tuple((row["from"], row["table"], row["to"], row["on_delete"]) for row in foreign_keys) != (
            ("project_id", "decisions", "project_id", "RESTRICT"),
            ("decision_id", "decisions", "decision_id", "RESTRICT"),
        ):
            raise RuntimeError("M1.4 migration stopped: approval_events foreign keys differ from the expected M1.3 schema.")

        tables = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for table_row in tables:
            name = table_row["name"]
            if name == "approval_events":
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            if any(row["table"] == "approval_events" for row in c.execute(f"PRAGMA foreign_key_list({quoted})")):
                raise RuntimeError("M1.4 migration stopped: another table has an incoming approval_events foreign key.")
        dependent_triggers = c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name!='approval_events' AND lower(sql) LIKE '%approval_events%'"
        ).fetchall()
        if dependent_triggers:
            raise RuntimeError("M1.4 migration stopped: another trigger references approval_events.")

    @staticmethod
    def _migrate_integrity_v6(c: sqlite3.Connection) -> None:
        """Add stable event ordering and harden append-only history protections."""
        with c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT COALESCE(MAX(version), 0) FROM schema_versions").fetchone()[0]
            if current >= 6:
                return
            if current != 5:
                raise RuntimeError(f"M1.6 integrity migration requires schema version 5; found {current}.")

            columns = tuple(row["name"] for row in c.execute("PRAGMA table_info(approval_events)"))
            expected_columns = ("approval_id", "project_id", "decision_id", "artifact_id", "status", "actor", "comment", "created_at")
            if columns != expected_columns:
                raise RuntimeError("M1.6 migration stopped: approval_events has an unexpected column layout.")
            expected_approval_triggers = {
                "approval_events_immutable_update", "approval_events_immutable_delete"
            }
            actual_approval_triggers = {row["name"] for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='approval_events'")}
            if actual_approval_triggers != expected_approval_triggers:
                raise RuntimeError("M1.6 migration stopped: approval_events triggers have an unexpected layout.")
            stage_columns = tuple(row["name"] for row in c.execute("PRAGMA table_info(project_stage_history)"))
            if stage_columns != ("history_id", "project_id", "stage_key", "from_status", "to_status", "changed_at", "actor", "note"):
                raise RuntimeError("M1.6 migration stopped: project_stage_history has an unexpected column layout.")
            stage_triggers = c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='project_stage_history'"
            ).fetchall()
            if stage_triggers:
                raise RuntimeError("M1.6 migration stopped: project_stage_history has unexpected triggers.")
            expected_workflow_triggers = {
                "workflow_run_events_immutable_update": "CREATE TRIGGER workflow_run_events_immutable_update BEFORE UPDATE ON workflow_run_events BEGIN SELECT RAISE(ABORT,'workflow history is append-only'); END",
                "workflow_run_events_immutable_delete": "CREATE TRIGGER workflow_run_events_immutable_delete BEFORE DELETE ON workflow_run_events BEGIN SELECT RAISE(ABORT,'workflow history is append-only'); END",
            }
            workflow_triggers = {row["name"]: row["sql"] for row in c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='workflow_run_events'")}
            if set(workflow_triggers) != set(expected_workflow_triggers) or any(
                ResearchStore._normalize_sql(workflow_triggers[name]) != ResearchStore._normalize_sql(sql)
                for name, sql in expected_workflow_triggers.items()
            ):
                raise RuntimeError("M1.6 migration stopped: workflow history triggers have an unexpected layout.")

            required_triggers = {
                "approval_events_immutable_update": ("approval_events", "CREATE TRIGGER approval_events_immutable_update BEFORE UPDATE ON approval_events BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END"),
                "approval_events_immutable_delete": ("approval_events", "CREATE TRIGGER approval_events_immutable_delete BEFORE DELETE ON approval_events BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END"),
                "approval_events_prevent_replace": ("approval_events", "CREATE TRIGGER approval_events_prevent_replace BEFORE INSERT ON approval_events WHEN NEW.event_sequence <= 0 OR EXISTS(SELECT 1 FROM approval_events WHERE approval_id=NEW.approval_id OR event_sequence=NEW.event_sequence) BEGIN SELECT RAISE(ABORT,'approval events are append-only'); END"),
                "project_stage_history_immutable_update": ("project_stage_history", "CREATE TRIGGER project_stage_history_immutable_update BEFORE UPDATE ON project_stage_history BEGIN SELECT RAISE(ABORT,'stage history is append-only'); END"),
                "project_stage_history_immutable_delete": ("project_stage_history", "CREATE TRIGGER project_stage_history_immutable_delete BEFORE DELETE ON project_stage_history BEGIN SELECT RAISE(ABORT,'stage history is append-only'); END"),
                "project_stage_history_prevent_replace": ("project_stage_history", "CREATE TRIGGER project_stage_history_prevent_replace BEFORE INSERT ON project_stage_history WHEN EXISTS(SELECT 1 FROM project_stage_history WHERE history_id=NEW.history_id) BEGIN SELECT RAISE(ABORT,'stage history is append-only'); END"),
                "workflow_run_events_immutable_update": ("workflow_run_events", expected_workflow_triggers["workflow_run_events_immutable_update"]),
                "workflow_run_events_immutable_delete": ("workflow_run_events", expected_workflow_triggers["workflow_run_events_immutable_delete"]),
                "workflow_run_events_prevent_replace": ("workflow_run_events", "CREATE TRIGGER workflow_run_events_prevent_replace BEFORE INSERT ON workflow_run_events WHEN EXISTS(SELECT 1 FROM workflow_run_events WHERE event_id=NEW.event_id) BEGIN SELECT RAISE(ABORT,'workflow history is append-only'); END"),
            }
            existing_triggers = {row["name"]: row for row in c.execute(
                "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'")}
            for name, (target, definition) in required_triggers.items():
                existing = existing_triggers.get(name)
                if existing is not None and (existing["tbl_name"] != target or
                        ResearchStore._normalize_sql(existing["sql"]) != ResearchStore._normalize_sql(definition)):
                    raise RuntimeError(
                        f"M1.6 migration stopped: required trigger {name} exists with an unexpected target or definition.")

            c.execute("ALTER TABLE approval_events ADD COLUMN event_sequence INTEGER NOT NULL DEFAULT 0")
            c.execute("DROP TRIGGER approval_events_immutable_update")
            c.execute("DROP TRIGGER approval_events_immutable_delete")
            c.execute("UPDATE approval_events SET event_sequence=(SELECT COUNT(*) FROM approval_events newer WHERE newer.rowid<=approval_events.rowid)")
            c.execute("CREATE UNIQUE INDEX idx_approval_events_event_sequence ON approval_events(event_sequence)")
            for name, (_, definition) in required_triggers.items():
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone() is None:
                    c.execute(definition)
            for name, (target, definition) in required_triggers.items():
                actual = c.execute("SELECT tbl_name,sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
                if actual is None or actual["tbl_name"] != target or ResearchStore._normalize_sql(actual["sql"]) != ResearchStore._normalize_sql(definition):
                    raise RuntimeError(f"M1.6 migration stopped: required trigger {name} did not verify after creation.")
            c.execute("INSERT INTO schema_versions(version,applied_at) VALUES(6,?)", (datetime.now(timezone.utc).isoformat(),))

    @staticmethod
    def _migrate_research_projects(c: sqlite3.Connection) -> None:
        with c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT COALESCE(MAX(version), 0) FROM schema_versions").fetchone()[0]
            if current >= 5:
                return
            columns = tuple(row["name"] for row in c.execute("PRAGMA table_info(research_runs)"))
            if columns != ("id", "prompt", "created_at"):
                raise RuntimeError("M1.5 migration stopped: research_runs has an unexpected column layout.")
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='research_runs'").fetchone():
                raise RuntimeError("M1.5 migration stopped: custom research_runs triggers need explicit migration support.")
            c.execute("ALTER TABLE research_runs ADD COLUMN project_id TEXT REFERENCES hackathon_projects(project_id) ON DELETE RESTRICT")
            c.execute("CREATE INDEX idx_research_runs_project ON research_runs(project_id,created_at)")
            c.execute("INSERT INTO schema_versions(version,applied_at) VALUES(5,?)", (datetime.now(timezone.utc).isoformat(),))

    @staticmethod
    def _insert_initial_stages(c: sqlite3.Connection, project_id: str, now: str) -> None:
        c.executemany("""INSERT INTO project_stages
            (project_id,stage_key,status,started_at,completed_at,updated_at)
            VALUES(?,?,'pending',NULL,NULL,?) ON CONFLICT(project_id,stage_key) DO NOTHING""",
            [(project_id, key.value, now) for key in ProjectStageKey])

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> HackathonProject:
        return HackathonProject.model_validate(dict(row))

    def create_project(self, project: ProjectCreate | HackathonProject) -> HackathonProject:
        if isinstance(project, ProjectCreate):
            now = datetime.now(timezone.utc)
            project = HackathonProject(**project.model_dump(), created_at=now, updated_at=now)
        if project.status == ProjectStatus.COMPLETED:
            raise ValueError("Project completion is derived from its stages and cannot be set during creation.")
        with self.db() as c:
            c.execute("""INSERT INTO hackathon_projects
                (project_id,name,description,problem_domain,status,current_stage,created_at,updated_at,archived_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (project.project_id, project.name, project.description, project.problem_domain,
                 project.status.value, project.current_stage, project.created_at.isoformat(),
                 project.updated_at.isoformat(), project.archived_at.isoformat() if project.archived_at else None))
            self._insert_initial_stages(c, project.project_id, project.created_at.isoformat())
            c.execute("UPDATE hackathon_projects SET current_stage='P1' WHERE project_id=? AND status!='completed'",
                      (project.project_id,))
        return self.get_project(project.project_id)

    def get_project(self, project_id: str) -> HackathonProject:
        with self.db() as c:
            row = c.execute("SELECT * FROM hackathon_projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown project: {project_id}")
        return self._project_from_row(row)

    def list_projects(self, *, include_archived: bool = True) -> list[HackathonProject]:
        sql = "SELECT * FROM hackathon_projects"
        if not include_archived:
            sql += " WHERE status='active'"
        sql += " ORDER BY created_at, project_id"
        with self.db() as c:
            rows = c.execute(sql).fetchall()
        return [self._project_from_row(row) for row in rows]

    def update_project(self, project_id: str, update: ProjectUpdate) -> HackathonProject:
        changes = update.model_dump(exclude_unset=True)
        if not changes:
            project = self.get_project(project_id)
            if project.status == ProjectStatus.ARCHIVED:
                raise ValueError("Archived projects cannot be updated.")
            return project
        with self.db() as c:
            assignments = ",".join(f"{column}=?" for column in changes)
            values = list(changes.values()) + [datetime.now(timezone.utc).isoformat(), project_id]
            cursor = c.execute(
                f"UPDATE hackathon_projects SET {assignments},updated_at=? "
                "WHERE project_id=? AND status IN ('active','completed')",
                values,
            )
            if cursor.rowcount == 0:
                row = c.execute("SELECT status FROM hackathon_projects WHERE project_id=?", (project_id,)).fetchone()
                if row is None:
                    raise KeyError(f"Unknown project: {project_id}")
                raise ValueError("Archived projects cannot be updated.")
        return self.get_project(project_id)

    def archive_project(self, project_id: str) -> HackathonProject:
        now = datetime.now(timezone.utc).isoformat()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status FROM hackathon_projects WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown project: {project_id}")
            if row["status"] in {ProjectStatus.ACTIVE.value, ProjectStatus.COMPLETED.value}:
                c.execute("UPDATE hackathon_projects SET status='archived',archived_at=?,updated_at=? WHERE project_id=?",
                          (now, now, project_id))
        return self.get_project(project_id)

    @staticmethod
    def _project_row(c: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM hackathon_projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown project: {project_id}")
        return row

    @staticmethod
    def _stage_key(value: str | ProjectStageKey) -> ProjectStageKey:
        try:
            return ProjectStageKey(value)
        except ValueError as exc:
            raise ValueError(f"Invalid stage key: {value}. Choose P1 through P8.") from exc

    @staticmethod
    def _recalculate_project_stage_state(c: sqlite3.Connection, project_id: str, now: str) -> None:
        project = ResearchStore._project_row(c, project_id)
        stages = c.execute("SELECT stage_key,status FROM project_stages WHERE project_id=? ORDER BY stage_key",
                           (project_id,)).fetchall()
        if len(stages) != len(ProjectStageKey):
            raise RuntimeError(f"Project {project_id} does not have exactly eight stage records.")
        current = next((row["stage_key"] for row in stages
                        if row["status"] not in {ProjectStageStatus.COMPLETED.value, ProjectStageStatus.SKIPPED.value}), None)
        if project["status"] != ProjectStatus.ARCHIVED.value:
            status = ProjectStatus.COMPLETED.value if current is None else ProjectStatus.ACTIVE.value
            c.execute("UPDATE hackathon_projects SET current_stage=?,status=?,updated_at=? WHERE project_id=?",
                      (current, status, now, project_id))
        else:
            c.execute("UPDATE hackathon_projects SET current_stage=?,updated_at=? WHERE project_id=?",
                      (current, now, project_id))

    def initialize_project_stages(self, project_id: str) -> list[ProjectStage]:
        now = datetime.now(timezone.utc).isoformat()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            project = self._project_row(c, project_id)
            self._insert_initial_stages(c, project_id, now)
            self._recalculate_project_stage_state(c, project_id, now)
        return self.list_project_stages(project_id)

    def list_project_stages(self, project_id: str) -> list[ProjectStage]:
        with self.db() as c:
            self._project_row(c, project_id)
            rows = c.execute("SELECT * FROM project_stages WHERE project_id=? ORDER BY stage_key", (project_id,)).fetchall()
        return [ProjectStage.model_validate(dict(row)) for row in rows]

    def get_project_stage(self, project_id: str, stage_key: str | ProjectStageKey) -> ProjectStage:
        key = self._stage_key(stage_key)
        with self.db() as c:
            self._project_row(c, project_id)
            row = c.execute("SELECT * FROM project_stages WHERE project_id=? AND stage_key=?",
                            (project_id, key.value)).fetchone()
        if row is None:
            raise KeyError(f"Stage {key.value} is not initialized for project {project_id}.")
        return ProjectStage.model_validate(dict(row))

    def transition_project_stage(
        self, project_id: str, stage_key: str | ProjectStageKey,
        to_status: str | ProjectStageStatus, *, actor: str = "user",
        note: str | None = None, reason: str | None = None,
        _expected_from: set[ProjectStageStatus] | None = None,
    ) -> ProjectStage:
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._transition_project_stage_in_transaction(
                c, project_id, stage_key, to_status, actor=actor, note=note,
                reason=reason, expected_from=_expected_from,
            )
        return self.get_project_stage(project_id, self._stage_key(stage_key))

    def _transition_project_stage_in_transaction(
        self, c: sqlite3.Connection, project_id: str, stage_key: str | ProjectStageKey,
        to_status: str | ProjectStageStatus, *, actor: str = "user",
        note: str | None = None, reason: str | None = None,
        expected_from: set[ProjectStageStatus] | None = None,
    ) -> ProjectStage:
        """Apply the standard stage rules using a caller-owned transaction."""
        key = self._stage_key(stage_key)
        try:
            target = ProjectStageStatus(to_status)
        except ValueError as exc:
            raise ValueError(f"Invalid stage status: {to_status}.") from exc
        if not actor.strip():
            raise ValueError("Stage transition actor cannot be empty.")
        if target == ProjectStageStatus.SKIPPED and (reason is None or not reason.strip()):
            raise ValueError("A non-empty reason is required to skip a stage.")

        now = datetime.now(timezone.utc).isoformat()
        project = self._project_row(c, project_id)
        if project["status"] == ProjectStatus.ARCHIVED.value:
            raise ValueError("Archived projects cannot transition stages.")
        row = c.execute("SELECT * FROM project_stages WHERE project_id=? AND stage_key=?",
                        (project_id, key.value)).fetchone()
        if row is None:
            raise KeyError(f"Stage {key.value} is not initialized for project {project_id}.")
        current = ProjectStageStatus(row["status"])
        if expected_from is not None and current not in expected_from:
            allowed = ", ".join(sorted(status.value for status in expected_from))
            raise ValueError(f"Stage {key.value} must be in {allowed} for this operation; it is {current.value}.")
        if target not in _STAGE_TRANSITIONS[current]:
            raise ValueError(f"Invalid stage transition for {key.value}: {current.value} -> {target.value}.")

        transition_note = reason.strip() if target == ProjectStageStatus.SKIPPED else (note or "").strip()
        if current in {ProjectStageStatus.COMPLETED, ProjectStageStatus.SKIPPED} and target == ProjectStageStatus.IN_PROGRESS and not transition_note:
            transition_note = "Explicitly reopened."
        started_at = row["started_at"] or now if target == ProjectStageStatus.IN_PROGRESS else row["started_at"]
        completed_at = now if target == ProjectStageStatus.COMPLETED else None
        c.execute("""UPDATE project_stages SET status=?,started_at=?,completed_at=?,updated_at=?
            WHERE project_id=? AND stage_key=? AND status=?""",
            (target.value, started_at, completed_at, now, project_id, key.value, current.value))
        c.execute("""INSERT INTO project_stage_history
            (history_id,project_id,stage_key,from_status,to_status,changed_at,actor,note)
            VALUES(?,?,?,?,?,?,?,?)""",
            (str(uuid4()), project_id, key.value, current.value, target.value, now, actor.strip(), transition_note))
        self._recalculate_project_stage_state(c, project_id, now)
        updated = c.execute("SELECT * FROM project_stages WHERE project_id=? AND stage_key=?",
                            (project_id, key.value)).fetchone()
        return ProjectStage.model_validate(dict(updated))

    def start_project_stage(self, project_id: str, stage_key: str | ProjectStageKey, *, actor: str = "user") -> ProjectStage:
        return self.transition_project_stage(project_id, stage_key, ProjectStageStatus.IN_PROGRESS,
                                             actor=actor, _expected_from={ProjectStageStatus.PENDING})

    def resume_project_stage(self, project_id: str, stage_key: str | ProjectStageKey, *, actor: str = "user") -> ProjectStage:
        return self.transition_project_stage(project_id, stage_key, ProjectStageStatus.IN_PROGRESS,
                                             actor=actor, _expected_from={ProjectStageStatus.BLOCKED})

    def complete_project_stage(self, project_id: str, stage_key: str | ProjectStageKey, *, actor: str = "user", note: str | None = None) -> ProjectStage:
        return self.transition_project_stage(project_id, stage_key, ProjectStageStatus.COMPLETED, actor=actor, note=note,
                                             _expected_from={ProjectStageStatus.IN_PROGRESS})

    def block_project_stage(self, project_id: str, stage_key: str | ProjectStageKey, *, actor: str = "user", note: str | None = None) -> ProjectStage:
        return self.transition_project_stage(project_id, stage_key, ProjectStageStatus.BLOCKED, actor=actor, note=note,
                                             _expected_from={ProjectStageStatus.IN_PROGRESS})

    def skip_project_stage(self, project_id: str, stage_key: str | ProjectStageKey, reason: str, *, actor: str = "user") -> ProjectStage:
        return self.transition_project_stage(project_id, stage_key, ProjectStageStatus.SKIPPED,
                                             actor=actor, reason=reason)

    def reopen_project_stage(self, project_id: str, stage_key: str | ProjectStageKey, *, actor: str = "user", note: str | None = None) -> ProjectStage:
        return self.transition_project_stage(project_id, stage_key, ProjectStageStatus.IN_PROGRESS, actor=actor, note=note,
                                             _expected_from={ProjectStageStatus.COMPLETED, ProjectStageStatus.SKIPPED})

    def get_project_stage_history(self, project_id: str, stage_key: str | ProjectStageKey | None = None) -> list[ProjectStageHistory]:
        with self.db() as c:
            self._project_row(c, project_id)
            if stage_key is None:
                rows = c.execute("SELECT * FROM project_stage_history WHERE project_id=? ORDER BY rowid", (project_id,)).fetchall()
            else:
                key = self._stage_key(stage_key)
                rows = c.execute("SELECT * FROM project_stage_history WHERE project_id=? AND stage_key=? ORDER BY rowid",
                                  (project_id, key.value)).fetchall()
        return [ProjectStageHistory.model_validate(dict(row)) for row in rows]

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        c=sqlite3.connect(self.path)
        c.row_factory=sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA recursive_triggers=ON")
        if c.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            c.close()
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")
        if c.execute("PRAGMA recursive_triggers").fetchone()[0] != 1:
            c.close()
            raise RuntimeError("SQLite recursive triggers could not be enabled.")
        try:
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def _migrate_columns(c: sqlite3.Connection, table: str, columns: dict[str,str]) -> None:
        present={row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
        for name,definition in columns.items():
            if name not in present:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def _insert_question(c: sqlite3.Connection, q: ResearchQuestion) -> None:
        c.execute(
            "INSERT INTO research_questions(id,run_id,text,priority,source_categories_json,evidence_required,decision_impact,status) VALUES(?,?,?,?,?,?,?,?)",
            (q.id,q.run_id,q.text,q.priority.value,json.dumps(q.source_categories),q.evidence_required,q.decision_impact,q.status),
        )

    @staticmethod
    def _question_from_row(row: sqlite3.Row) -> ResearchQuestion:
        data=dict(row)
        data["source_categories"]=json.loads(data.pop("source_categories_json","[]"))
        return ResearchQuestion.model_validate(data)

    @staticmethod
    def _validate_project_for_research(c: sqlite3.Connection, project_id: str | None) -> None:
        if project_id is None:
            return
        project = c.execute("SELECT status FROM hackathon_projects WHERE project_id=?",(project_id,)).fetchone()
        if project is None:
            raise ProjectNotFoundError(f"Unknown project: {project_id}")
        if project["status"] != ProjectStatus.ACTIVE.value:
            raise ProjectAssociationError("New research runs can only be associated with active projects.")

    def save_run(self, run: ResearchRun, *, project_id: str | None = None) -> None:
        project_id = project_id if project_id is not None else run.project_id
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._validate_project_for_research(c,project_id)
            c.execute("INSERT INTO research_runs(id,prompt,created_at,project_id) VALUES(?,?,?,?)",
                (run.id,run.prompt,run.created_at.isoformat(),project_id))

    def validate_run_project(self, run_id: str, project_id: str) -> None:
        """Validate an explicitly scoped existing run without attaching or reassigning it."""
        with self.db() as c:
            self._validate_project_for_research(c,project_id)
            row = c.execute("SELECT project_id FROM research_runs WHERE id=?",(run_id,)).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Unknown research run: {run_id}")
            if row["project_id"] != project_id:
                raise ProjectAssociationError("Research run is not associated with the supplied project; use the explicit project research attach command for an unowned run.")

    def save_question(self, question: ResearchQuestion) -> None:
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM research_runs WHERE id=?",(question.run_id,)).fetchone():
                raise KeyError(f"Unknown research run: {question.run_id}")
            self._insert_question(c,question)

    def save_source(self, source: SourceSnapshot) -> SourceSnapshot:
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            question = c.execute("SELECT run_id FROM research_questions WHERE id=?",(source.question_id,)).fetchone()
            if question is None:
                raise KeyError(f"Unknown research question: {source.question_id}")
            if question["run_id"] != source.run_id:
                raise ValueError("A source and its research question must belong to the same run.")
            if not c.execute("SELECT 1 FROM research_runs WHERE id=?",(source.run_id,)).fetchone():
                raise KeyError(f"Unknown research run: {source.run_id}")
            url_candidates=list(dict.fromkeys(value for value in (source.url,source.final_url) if value))
            placeholders=",".join("?" for _ in url_candidates)
            existing=c.execute(f"SELECT * FROM sources WHERE run_id=? AND (url IN ({placeholders}) OR final_url IN ({placeholders})) ORDER BY rowid LIMIT 1",
                (source.run_id,*url_candidates,*url_candidates)).fetchone()
            if existing is not None:
                categories=list(dict.fromkeys(json.loads(existing["source_categories_json"] or "[]")+source.source_categories))
                has_new_content=not existing["content"] and bool(source.content)
                c.execute("""UPDATE sources SET source_categories_json=?,
                    content=CASE WHEN content='' AND ? THEN ? ELSE content END,
                    search_snippet=CASE WHEN search_snippet='' THEN ? ELSE search_snippet END,
                    title=CASE WHEN title='' THEN ? ELSE title END,
                    publisher=CASE WHEN publisher='' THEN ? ELSE publisher END,
                    search_score=COALESCE(search_score,?),
                    content_origin=CASE WHEN content='' AND ? THEN ? ELSE content_origin END,
                    content_type=CASE WHEN content='' AND ? THEN ? ELSE content_type END,
                    final_url=CASE WHEN final_url='' THEN ? ELSE final_url END,
                    fetch_error=CASE WHEN fetch_error='' THEN ? ELSE fetch_error END
                    WHERE id=?""",
                    (json.dumps(categories),int(has_new_content),source.content,source.search_snippet,
                     source.title,source.publisher,source.search_score,int(has_new_content),source.content_origin,
                     int(has_new_content),source.content_type,source.final_url,source.fetch_error,existing["id"]))
                return source.model_copy(update={"id":existing["id"],
                    "source_categories":categories,
                    "content":source.content if has_new_content else existing["content"],
                    "content_origin":source.content_origin if has_new_content else existing["content_origin"],
                    "content_type":source.content_type if has_new_content else existing["content_type"],
                    "final_url":source.final_url or existing["final_url"]})
            c.execute(
                """INSERT INTO sources
                (id,run_id,question_id,url,title,publisher,published_at,retrieved_at,content,
                 search_snippet,search_score,source_categories_json,content_origin,content_type,final_url,fetch_error)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source.id,source.run_id,source.question_id,source.url,source.title,source.publisher,
                 source.published_at,source.retrieved_at.isoformat(),source.content,source.search_snippet,
                 source.search_score,json.dumps(source.source_categories),source.content_origin,
                 source.content_type,source.final_url,source.fetch_error),
            )
            return source

    def save_evidence(self, evidence: EvidenceRecord) -> None:
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            question = c.execute("SELECT run_id FROM research_questions WHERE id=?",(evidence.question_id,)).fetchone()
            source = c.execute("SELECT run_id FROM sources WHERE id=?",(evidence.source_id,)).fetchone()
            if question is None:
                raise KeyError(f"Unknown research question: {evidence.question_id}")
            if source is None:
                raise KeyError(f"Unknown source: {evidence.source_id}")
            if question["run_id"] != evidence.run_id or source["run_id"] != evidence.run_id:
                raise ValueError("Evidence, its question, and its source must belong to the same research run.")
            existing=c.execute("SELECT id FROM evidence WHERE question_id=? AND source_id=? AND claim=? AND passage=?",
                (evidence.question_id,evidence.source_id,evidence.claim,evidence.passage)).fetchone()
            if existing:
                existing_owner=c.execute("SELECT run_id FROM evidence WHERE id=?",(existing["id"],)).fetchone()
                if existing_owner["run_id"] != evidence.run_id:
                    raise ValueError("Existing evidence has inconsistent run ownership and cannot be updated.")
                c.execute("UPDATE evidence SET relation=?,verification_status=?,verification_reason=? WHERE id=?",
                    (evidence.relation.value,evidence.verification_status.value,evidence.verification_reason,existing["id"]))
                return
            c.execute(
                "INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                (evidence.id,evidence.run_id,evidence.question_id,evidence.source_id,evidence.claim,
                 evidence.passage,evidence.relation.value,evidence.verification_status.value,
                 evidence.verification_reason,evidence.created_at.isoformat()),
            )

    def update_question_status(self, question_id: str, status: str) -> None:
        if status not in {"pending", "in_progress", "covered", "blocked"}:
            raise ValueError(f"Invalid question status: {status}")
        with self.db() as c:
            cursor=c.execute("UPDATE research_questions SET status=? WHERE id=?",(status,question_id))
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown research question: {question_id}")

    def save_solution_design(self, design) -> None:
        with self.db() as c:
            c.execute("INSERT INTO solution_designs VALUES(?,?,?,?,?)",
                (design.id,design.run_id,design.status,design.created_at.isoformat(),design.model_dump_json()))

    def get_solution_design(self, design_id: str):
        from src.research.solution import SolutionDesign
        with self.db() as c:
            row=c.execute("SELECT design_json FROM solution_designs WHERE design_id=?",(design_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown solution design: {design_id}")
        return SolutionDesign.model_validate_json(row["design_json"])

    def get_latest_solution_design(self, run_id: str):
        from src.research.solution import SolutionDesign
        with self.db() as c:
            row=c.execute("SELECT design_json FROM solution_designs WHERE run_id=? ORDER BY created_at DESC LIMIT 1",(run_id,)).fetchone()
        if row is None:
            raise KeyError(f"No solution design exists for research run: {run_id}")
        return SolutionDesign.model_validate_json(row["design_json"])

    def save_pitch_package(self, package) -> None:
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            run = c.execute("SELECT project_id FROM research_runs WHERE id=?", (package.run_id,)).fetchone()
            if run is None:
                raise KeyError(f"Unknown research run: {package.run_id}")
            design = c.execute("SELECT run_id FROM solution_designs WHERE design_id=?", (package.design_id,)).fetchone()
            if design is None:
                raise KeyError(f"Unknown solution design: {package.design_id}")
            if design["run_id"] != package.run_id:
                raise ValueError("A pitch package and its solution design must belong to the same research run/project.")
            c.execute("INSERT INTO pitch_packages VALUES(?,?,?,?,?,?)",
                (package.id,package.run_id,package.design_id,package.status,package.created_at.isoformat(),package.model_dump_json()))

    def get_pitch_package(self, package_id: str):
        from src.research.pitch import PitchPackage
        with self.db() as c:
            row=c.execute("SELECT package_json FROM pitch_packages WHERE package_id=?",(package_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown pitch package: {package_id}")
        return PitchPackage.model_validate_json(row["package_json"])

    def save_plan(self, plan: ResearchPlan, *, project_id: str | None = None) -> None:
        if any(question.run_id != plan.run_id for question in plan.questions):
            raise ValueError("Every research plan question must belong to the plan's research run.")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._validate_project_for_research(c,project_id)
            c.execute("INSERT INTO research_runs(id,prompt,created_at,project_id) VALUES(?,?,?,?)",
                (plan.run_id,plan.brief.problem_statement,plan.created_at.isoformat(),project_id))
            c.execute("INSERT INTO research_plans VALUES(?,?,?,?,?,?)",
                (plan.id,plan.run_id,plan.model_dump_json(),plan.status,plan.created_at.isoformat(),
                 plan.approved_at.isoformat() if plan.approved_at else None))
            for question in plan.questions:
                self._insert_question(c,question)

    def update_plan(self, plan: ResearchPlan) -> None:
        if any(question.run_id != plan.run_id for question in plan.questions):
            raise ValueError("Every research plan question must belong to the plan's research run.")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT status,run_id FROM research_plans WHERE plan_id=?",(plan.id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown research plan: {plan.id}")
            if row["run_id"] != plan.run_id:
                raise ValueError("A research plan cannot be reassigned to another research run.")
            if row["status"]!="draft" and plan.status!=row["status"]:
                raise ValueError("Only a draft research plan can change status.")
            for question in plan.questions:
                owner=c.execute("SELECT run_id FROM research_questions WHERE id=?",(question.id,)).fetchone()
                if owner is None or owner["run_id"] != plan.run_id:
                    raise ValueError(f"Research question {question.id} does not belong to plan run {plan.run_id}.")
            c.execute("UPDATE research_plans SET plan_json=?,status=?,approved_at=? WHERE plan_id=?",
                (plan.model_dump_json(),plan.status,plan.approved_at.isoformat() if plan.approved_at else None,plan.id))
            for q in plan.questions:
                c.execute("""UPDATE research_questions SET text=?,priority=?,source_categories_json=?,
                    evidence_required=?,decision_impact=?,status=? WHERE id=? AND run_id=?""",
                    (q.text,q.priority.value,json.dumps(q.source_categories),q.evidence_required,
                     q.decision_impact,q.status,q.id,plan.run_id))

    def get_plan(self, plan_id_or_run_id: str) -> ResearchPlan:
        with self.db() as c:
            row=c.execute("SELECT plan_json FROM research_plans WHERE plan_id=? OR run_id=?",
                (plan_id_or_run_id,plan_id_or_run_id)).fetchone()
        if row is None:
            raise KeyError(f"Unknown research plan: {plan_id_or_run_id}")
        return ResearchPlan.model_validate_json(row["plan_json"])

    def get_run_records(self, run_id: str):
        with self.db() as c:
            run=c.execute("SELECT * FROM research_runs WHERE id=?",(run_id,)).fetchone()
            if run is None:
                raise KeyError(f"Unknown research run: {run_id}")
            questions=c.execute("SELECT * FROM research_questions WHERE run_id=? ORDER BY rowid",(run_id,)).fetchall()
            source_rows=c.execute("SELECT * FROM sources WHERE run_id=? ORDER BY rowid",(run_id,)).fetchall()
            evidence=c.execute("SELECT * FROM evidence WHERE run_id=? ORDER BY rowid",(run_id,)).fetchall()
        sources=[]
        for row in source_rows:
            data=dict(row)
            data["source_categories"]=json.loads(data.pop("source_categories_json","[]"))
            sources.append(SourceSnapshot.model_validate(data))
        return (
            ResearchRun.model_validate(dict(run)),
            [self._question_from_row(row) for row in questions],
            sources,
            [EvidenceRecord.model_validate(dict(row)) for row in evidence],
        )
