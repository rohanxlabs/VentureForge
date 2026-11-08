"""Project-specific persistence APIs layered on the shared SQLite store."""

import json
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from src.research.models import (
    ApprovalEvent, ApprovalEventStatus, DecisionCreate, DecisionEvidenceLink,
    DecisionEvidenceRelationship, DecisionOption, DecisionOptionCreate,
    DecisionUpdate, ProjectDecision, ProjectStatus,
    ArtifactCreate, ArtifactStatus, ProjectArtifact, ProjectStageKey,
    ProjectWorkflowRun, WorkflowRunCreate, WorkflowRunEvent, WorkflowStatus,
    ProjectResearchRunSummary,
)
from src.research.storage import ResearchStore


class ProjectStore(ResearchStore):
    """Decision and project domain operations using ResearchStore's DB lifecycle."""

    _ARTIFACT_STATUS_TRANSITIONS = {
        ArtifactStatus.DRAFT: {ArtifactStatus.READY, ArtifactStatus.ARCHIVED},
        # A ready artifact is superseded only as part of version creation below.
        ArtifactStatus.READY: {ArtifactStatus.ARCHIVED},
        ArtifactStatus.SUPERSEDED: {ArtifactStatus.ARCHIVED},
        ArtifactStatus.ARCHIVED: set(),
    }

    @staticmethod
    def _require_project(c, project_id: str, *, writable: bool = False):
        row = c.execute("SELECT * FROM hackathon_projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown project: {project_id}")
        if writable and row["status"] == ProjectStatus.ARCHIVED.value:
            raise ValueError("Archived projects cannot be changed.")
        return row

    @staticmethod
    def _require_decision(c, project_id: str, decision_id: str):
        row = c.execute("SELECT * FROM decisions WHERE project_id=? AND decision_id=?",
                        (project_id, decision_id)).fetchone()
        if row is None:
            project = c.execute("SELECT 1 FROM hackathon_projects WHERE project_id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(f"Unknown project: {project_id}")
            raise KeyError(f"Decision {decision_id} does not belong to project {project_id}.")
        return row

    @staticmethod
    def _require_unapproved_decision(c, project_id: str, decision_id: str) -> None:
        if c.execute("SELECT 1 FROM approval_events WHERE project_id=? AND decision_id=? LIMIT 1",
                     (project_id, decision_id)).fetchone():
            raise ValueError("Decision content cannot be changed after an approval event; record a new event or create a new decision.")

    @staticmethod
    def _decision_from_row(row) -> ProjectDecision:
        return ProjectDecision.model_validate(dict(row))

    @staticmethod
    def _option_from_row(row) -> DecisionOption:
        data = dict(row)
        for field in ("advantages", "disadvantages", "assumptions"):
            data[field] = json.loads(data.pop(f"{field}_json", "[]"))
        data["is_recommended"] = bool(data["is_recommended"])
        data["is_selected"] = bool(data["is_selected"])
        return DecisionOption.model_validate(data)

    def create_decision(self, decision: DecisionCreate) -> ProjectDecision:
        now = datetime.now(timezone.utc)
        decision_id = str(uuid4())
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c, decision.project_id, writable=True)
            if decision.stage_key is not None:
                stage = c.execute("SELECT 1 FROM project_stages WHERE project_id=? AND stage_key=?",
                                  (decision.project_id, decision.stage_key.value)).fetchone()
                if stage is None:
                    raise KeyError(f"Stage {decision.stage_key.value} does not belong to project {decision.project_id}.")
            c.execute("""INSERT INTO decisions
                (decision_id,project_id,stage_key,title,description,decision_type,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (decision_id, decision.project_id, decision.stage_key.value if decision.stage_key else None,
                 decision.title, decision.description, decision.decision_type, decision.status,
                 now.isoformat(), now.isoformat()))
        return self.get_decision(decision.project_id, decision_id)

    def get_decision(self, project_id: str, decision_id: str) -> ProjectDecision:
        with self.db() as c:
            row = c.execute("SELECT * FROM decisions WHERE project_id=? AND decision_id=?",
                            (project_id, decision_id)).fetchone()
            if row is None:
                self._require_decision(c, project_id, decision_id)
        return self._decision_from_row(row)

    def list_project_decisions(self, project_id: str) -> list[ProjectDecision]:
        with self.db() as c:
            self._require_project(c, project_id)
            rows = c.execute("SELECT * FROM decisions WHERE project_id=? ORDER BY created_at,decision_id",
                             (project_id,)).fetchall()
        return [self._decision_from_row(row) for row in rows]

    def update_decision(self, project_id: str, decision_id: str, update: DecisionUpdate) -> ProjectDecision:
        changes = update.model_dump(exclude_unset=True)
        if not changes:
            return self.get_decision(project_id, decision_id)
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c, project_id, writable=True)
            self._require_decision(c, project_id, decision_id)
            self._require_unapproved_decision(c, project_id, decision_id)
            stage_key = changes.get("stage_key")
            if "stage_key" in changes:
                if stage_key is not None:
                    stage = c.execute("SELECT 1 FROM project_stages WHERE project_id=? AND stage_key=?",
                                      (project_id, stage_key.value)).fetchone()
                    if stage is None:
                        raise KeyError(f"Stage {stage_key.value} does not belong to project {project_id}.")
                changes["stage_key"] = stage_key.value if stage_key else None
            assignments = ",".join(f"{key}=?" for key in changes)
            c.execute(f"UPDATE decisions SET {assignments},updated_at=? WHERE project_id=? AND decision_id=?",
                      (*changes.values(), datetime.now(timezone.utc).isoformat(), project_id, decision_id))
        return self.get_decision(project_id, decision_id)

    def add_decision_option(self, project_id: str, decision_id: str, option: DecisionOptionCreate) -> DecisionOption:
        option_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c, project_id, writable=True)
            self._require_decision(c, project_id, decision_id)
            self._require_unapproved_decision(c, project_id, decision_id)
            c.execute("""INSERT INTO decision_options
                (option_id,project_id,decision_id,title,description,advantages_json,disadvantages_json,
                 assumptions_json,estimated_cost,feasibility,is_recommended,is_selected,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,0,0,?)""",
                (option_id, project_id, decision_id, option.title, option.description,
                 json.dumps(option.advantages), json.dumps(option.disadvantages), json.dumps(option.assumptions),
                 option.estimated_cost, option.feasibility, now))
        return next(item for item in self.list_decision_options(project_id, decision_id) if item.option_id == option_id)

    def list_decision_options(self, project_id: str, decision_id: str) -> list[DecisionOption]:
        with self.db() as c:
            self._require_decision(c, project_id, decision_id)
            rows = c.execute("SELECT * FROM decision_options WHERE project_id=? AND decision_id=? ORDER BY created_at,option_id",
                             (project_id, decision_id)).fetchall()
        return [self._option_from_row(row) for row in rows]

    def _set_decision_option_flag(self, project_id: str, decision_id: str, option_id: str, flag: str) -> DecisionOption:
        if flag not in {"is_recommended", "is_selected"}:
            raise ValueError("Unsupported decision option flag.")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c, project_id, writable=True)
            self._require_decision(c, project_id, decision_id)
            self._require_unapproved_decision(c, project_id, decision_id)
            option = c.execute("SELECT 1 FROM decision_options WHERE project_id=? AND decision_id=? AND option_id=?",
                               (project_id, decision_id, option_id)).fetchone()
            if option is None:
                raise KeyError(f"Option {option_id} does not belong to decision {decision_id}.")
            c.execute(f"UPDATE decision_options SET {flag}=0 WHERE project_id=? AND decision_id=?",
                      (project_id, decision_id))
            c.execute(f"UPDATE decision_options SET {flag}=1 WHERE project_id=? AND decision_id=? AND option_id=?",
                      (project_id, decision_id, option_id))
        return next(item for item in self.list_decision_options(project_id, decision_id) if item.option_id == option_id)

    def recommend_decision_option(self, project_id: str, decision_id: str, option_id: str) -> DecisionOption:
        return self._set_decision_option_flag(project_id, decision_id, option_id, "is_recommended")

    def select_decision_option(self, project_id: str, decision_id: str, option_id: str) -> DecisionOption:
        return self._set_decision_option_flag(project_id, decision_id, option_id, "is_selected")

    def link_decision_evidence(
        self, project_id: str, decision_id: str, evidence_id: str,
        relationship_type: DecisionEvidenceRelationship, explanation: str = "",
    ) -> DecisionEvidenceLink:
        if not isinstance(relationship_type, DecisionEvidenceRelationship):
            relationship_type = DecisionEvidenceRelationship(relationship_type)
        now = datetime.now(timezone.utc)
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c, project_id, writable=True)
            self._require_decision(c, project_id, decision_id)
            columns = {row["name"] for row in c.execute("PRAGMA table_info(research_runs)")}
            if "project_id" not in columns:
                raise ValueError("Evidence ownership cannot be verified until M1.5 research integration is installed.")
            evidence = c.execute("""SELECT e.id,e.run_id,r.project_id,s.run_id AS source_run_id,
                    eq.run_id AS evidence_question_run_id,sq.run_id AS source_question_run_id
                FROM evidence e
                JOIN research_runs r ON r.id=e.run_id
                JOIN research_questions eq ON eq.id=e.question_id
                JOIN sources s ON s.id=e.source_id
                JOIN research_questions sq ON sq.id=s.question_id
                WHERE e.id=?""", (evidence_id,)).fetchone()
            if evidence is None:
                raise KeyError(f"Unknown evidence record: {evidence_id}")
            if any(evidence[key] != evidence["run_id"] for key in
                   ("source_run_id","evidence_question_run_id","source_question_run_id")):
                raise ValueError("Evidence, source, and both question records must belong to the same research run.")
            if evidence["project_id"] != project_id:
                raise ValueError("Evidence must come from a research run owned by the same project.")
            c.execute("""INSERT INTO decision_evidence
                (decision_id,evidence_id,relationship_type,explanation,created_at) VALUES(?,?,?,?,?)""",
                (decision_id, evidence_id, relationship_type.value, explanation, now.isoformat()))
        return DecisionEvidenceLink(decision_id=decision_id, evidence_id=evidence_id,
                                   relationship_type=relationship_type, explanation=explanation, created_at=now)

    def list_decision_evidence(self, project_id: str, decision_id: str) -> list[DecisionEvidenceLink]:
        with self.db() as c:
            self._require_decision(c, project_id, decision_id)
            rows = c.execute("SELECT * FROM decision_evidence WHERE decision_id=? ORDER BY rowid", (decision_id,)).fetchall()
        return [DecisionEvidenceLink.model_validate(dict(row)) for row in rows]

    def record_decision_approval(
        self, project_id: str, decision_id: str, status: ApprovalEventStatus,
        *, actor: str = "user", comment: str = "",
    ) -> ApprovalEvent:
        if not isinstance(status, ApprovalEventStatus):
            status = ApprovalEventStatus(status)
        if not actor.strip():
            raise ValueError("Approval actor cannot be empty.")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c, project_id, writable=True)
            self._require_decision(c, project_id, decision_id)
            sequence = c.execute("SELECT COALESCE(MAX(event_sequence),0)+1 FROM approval_events").fetchone()[0]
            event = ApprovalEvent(project_id=project_id, decision_id=decision_id, status=status,
                                  actor=actor.strip(), comment=comment, event_sequence=sequence)
            c.execute("""INSERT INTO approval_events
                (approval_id,event_sequence,project_id,decision_id,status,actor,comment,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (event.approval_id, sequence, project_id, decision_id, status.value, event.actor,
                 comment, event.created_at.isoformat()))
        return event

    def get_decision_approval_status(self, project_id: str, decision_id: str) -> str:
        with self.db() as c:
            self._require_decision(c, project_id, decision_id)
            row = c.execute("SELECT status FROM approval_events WHERE project_id=? AND decision_id=? ORDER BY event_sequence DESC LIMIT 1",
                            (project_id, decision_id)).fetchone()
        return row["status"] if row else "pending"

    def get_decision_approval_history(self, project_id: str, decision_id: str) -> list[ApprovalEvent]:
        with self.db() as c:
            self._require_decision(c, project_id, decision_id)
            rows = c.execute("SELECT * FROM approval_events WHERE project_id=? AND decision_id=? ORDER BY event_sequence",
                             (project_id, decision_id)).fetchall()
        return [ApprovalEvent.model_validate(dict(row)) for row in rows]

    @staticmethod
    def _artifact_from_row(row) -> ProjectArtifact:
        return ProjectArtifact.model_validate(dict(row))

    @staticmethod
    def _safe_storage_ref(storage_ref: str | None) -> str | None:
        if storage_ref is None:
            return None
        path = PurePosixPath(storage_ref.replace("\\", "/"))
        if path.is_absolute() or not path.parts or ":" in path.parts[0] or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("Artifact storage reference must be a safe relative path without traversal segments.")
        return path.as_posix()

    def create_artifact(self, artifact: ArtifactCreate, *, artifact_key: str | None = None,
                        _expected_previous_id: str | None = None) -> ProjectArtifact:
        if artifact.status not in {ArtifactStatus.DRAFT, ArtifactStatus.READY}:
            raise ValueError("New artifact versions must start as draft or ready; superseded is assigned during replacement.")
        if artifact.content is not None and len(artifact.content.encode("utf-8")) > 512 * 1024:
            raise ValueError("Artifact content is limited to 512 KiB; store larger output as a safe relative reference.")
        storage_ref = self._safe_storage_ref(artifact.storage_ref)
        if artifact.content is not None and storage_ref is not None:
            raise ValueError("Provide artifact content or a storage reference, not both.")
        key, artifact_id = artifact_key or str(uuid4()), str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c, artifact.project_id, writable=True)
            if artifact.stage_key is not None and not c.execute(
                "SELECT 1 FROM project_stages WHERE project_id=? AND stage_key=?",
                (artifact.project_id, artifact.stage_key.value),
            ).fetchone():
                raise KeyError(f"Stage {artifact.stage_key.value} does not belong to project {artifact.project_id}.")
            if artifact.decision_id is not None and not c.execute(
                "SELECT 1 FROM decisions WHERE project_id=? AND decision_id=?",
                (artifact.project_id,artifact.decision_id),
            ).fetchone():
                raise KeyError(f"Decision {artifact.decision_id} does not belong to project {artifact.project_id}.")
            previous = c.execute("SELECT artifact_id,version,status FROM project_artifacts WHERE project_id=? AND artifact_key=? ORDER BY version DESC LIMIT 1",
                                 (artifact.project_id,key)).fetchone()
            version = (previous["version"] + 1) if previous else 1
            if _expected_previous_id is not None and (previous is None or previous["artifact_id"] != _expected_previous_id):
                raise ValueError("Artifact version changed concurrently; reload the latest version and retry.")
            if previous is not None and ArtifactStatus(previous["status"]) not in {ArtifactStatus.DRAFT,ArtifactStatus.READY}:
                raise ValueError("A new artifact version can only supersede a draft or ready latest version.")
            c.execute("""INSERT INTO project_artifacts
                (artifact_id,artifact_key,project_id,stage_key,decision_id,artifact_type,title,description,content,storage_ref,version,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (artifact_id,key,artifact.project_id,artifact.stage_key.value if artifact.stage_key else None,
                 artifact.decision_id,artifact.artifact_type,artifact.title,artifact.description,artifact.content,storage_ref,version,
                 artifact.status.value,now,now))
            if previous is not None:
                c.execute("UPDATE project_artifacts SET status='superseded',updated_at=? WHERE project_id=? AND artifact_id=? AND status=?",
                          (now,artifact.project_id,previous["artifact_id"],previous["status"]))
        return self.get_artifact(artifact.project_id, artifact_id)

    def create_artifact_version(self, project_id: str, artifact_id: str, artifact: ArtifactCreate) -> ProjectArtifact:
        if artifact.project_id != project_id:
            raise ValueError("Artifact version must belong to the requested project.")
        current = self.get_artifact(project_id, artifact_id)
        return self.create_artifact(artifact, artifact_key=current.artifact_key,
                                    _expected_previous_id=current.artifact_id)

    def get_artifact(self, project_id: str, artifact_id: str) -> ProjectArtifact:
        with self.db() as c:
            row = c.execute("SELECT * FROM project_artifacts WHERE project_id=? AND artifact_id=?", (project_id,artifact_id)).fetchone()
            if row is None:
                self._require_project(c, project_id)
                raise KeyError(f"Artifact {artifact_id} does not belong to project {project_id}.")
        return self._artifact_from_row(row)

    def list_project_artifacts(self, project_id: str) -> list[ProjectArtifact]:
        with self.db() as c:
            self._require_project(c, project_id)
            rows = c.execute("SELECT * FROM project_artifacts WHERE project_id=? ORDER BY artifact_key,version",(project_id,)).fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def update_artifact_status(self, project_id: str, artifact_id: str, status: ArtifactStatus) -> ProjectArtifact:
        status = ArtifactStatus(status)
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c, project_id, writable=True)
            row=c.execute("SELECT status FROM project_artifacts WHERE project_id=? AND artifact_id=?",(project_id,artifact_id)).fetchone()
            if row is None:
                raise KeyError(f"Artifact {artifact_id} does not belong to project {project_id}.")
            current=ArtifactStatus(row["status"])
            if current != status and status not in self._ARTIFACT_STATUS_TRANSITIONS[current]:
                raise ValueError(f"Invalid artifact status transition: {current.value} -> {status.value}.")
            if current != status:
                c.execute("UPDATE project_artifacts SET status=?,updated_at=? WHERE project_id=? AND artifact_id=? AND status=?",
                    (status.value,datetime.now(timezone.utc).isoformat(),project_id,artifact_id,current.value))
        return self.get_artifact(project_id,artifact_id)

    def record_artifact_approval(self, project_id: str, artifact_id: str, status: ApprovalEventStatus,
                                 *, actor: str="user", comment: str="") -> ApprovalEvent:
        status = ApprovalEventStatus(status)
        if not actor.strip():
            raise ValueError("Approval actor cannot be empty.")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c,project_id,writable=True)
            if not c.execute("SELECT 1 FROM project_artifacts WHERE project_id=? AND artifact_id=?",(project_id,artifact_id)).fetchone():
                raise KeyError(f"Artifact {artifact_id} does not belong to project {project_id}.")
            sequence = c.execute("SELECT COALESCE(MAX(event_sequence),0)+1 FROM approval_events").fetchone()[0]
            event = ApprovalEvent(project_id=project_id,artifact_id=artifact_id,status=status,actor=actor.strip(),comment=comment,event_sequence=sequence)
            c.execute("INSERT INTO approval_events(approval_id,event_sequence,project_id,artifact_id,status,actor,comment,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (event.approval_id,sequence,project_id,artifact_id,status.value,event.actor,comment,event.created_at.isoformat()))
        return event

    def get_artifact_approval_status(self, project_id: str, artifact_id: str) -> str:
        self.get_artifact(project_id,artifact_id)
        with self.db() as c:
            row = c.execute("SELECT status FROM approval_events WHERE project_id=? AND artifact_id=? ORDER BY event_sequence DESC LIMIT 1",
                            (project_id,artifact_id)).fetchone()
        return row["status"] if row else "pending"

    def get_artifact_approval_history(self, project_id: str, artifact_id: str) -> list[ApprovalEvent]:
        self.get_artifact(project_id,artifact_id)
        with self.db() as c:
            rows = c.execute("SELECT * FROM approval_events WHERE project_id=? AND artifact_id=? ORDER BY event_sequence",
                             (project_id,artifact_id)).fetchall()
        return [ApprovalEvent.model_validate(dict(row)) for row in rows]

    @staticmethod
    def _workflow_from_row(row) -> ProjectWorkflowRun:
        return ProjectWorkflowRun.model_validate(dict(row))

    def start_workflow_run(self, workflow: WorkflowRunCreate) -> ProjectWorkflowRun:
        run_id, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c,workflow.project_id,writable=True)
            if not c.execute("SELECT 1 FROM project_stages WHERE project_id=? AND stage_key=?",
                             (workflow.project_id,workflow.stage_key.value)).fetchone():
                raise KeyError(f"Stage {workflow.stage_key.value} does not belong to project {workflow.project_id}.")
            c.execute("INSERT INTO workflow_runs(workflow_run_id,project_id,stage_key,workflow_type,status,started_at,completed_at,error_summary,input_ref,output_artifact_id,created_at) VALUES(?,?,?,?,'running',?,NULL,NULL,?,NULL,?)",
                (run_id,workflow.project_id,workflow.stage_key.value,workflow.workflow_type,now,workflow.input_ref,now))
            self._add_workflow_event(c,run_id,None,WorkflowStatus.RUNNING,"Started")
        return self.get_workflow_run(workflow.project_id,run_id)

    @staticmethod
    def _add_workflow_event(c, run_id: str, old: WorkflowStatus | None, new: WorkflowStatus, note: str) -> None:
        c.execute("INSERT INTO workflow_run_events(event_id,workflow_run_id,from_status,to_status,actor,note,changed_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()),run_id,old.value if old else None,new.value,"user",note,datetime.now(timezone.utc).isoformat()))

    def _transition_workflow(self, project_id: str, run_id: str, target: WorkflowStatus,
                             *, error_summary: str | None=None, output_artifact_id: str | None=None) -> ProjectWorkflowRun:
        target = WorkflowStatus(target)
        terminal = {WorkflowStatus.COMPLETED,WorkflowStatus.FAILED,WorkflowStatus.INTERRUPTED}
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c,project_id,writable=True)
            row = c.execute("SELECT * FROM workflow_runs WHERE project_id=? AND workflow_run_id=?",(project_id,run_id)).fetchone()
            if row is None:
                raise KeyError(f"Unknown workflow run: {run_id}")
            old = WorkflowStatus(row["status"])
            allowed = {WorkflowStatus.RUNNING:{WorkflowStatus.COMPLETED,WorkflowStatus.FAILED,WorkflowStatus.INTERRUPTED}}
            if target not in allowed.get(old,set()):
                raise ValueError(f"Invalid workflow transition: {old.value} -> {target.value}.")
            if target == WorkflowStatus.FAILED and not (error_summary and error_summary.strip()):
                raise ValueError("A failure summary is required for failed workflows.")
            if output_artifact_id is not None and not c.execute("SELECT 1 FROM project_artifacts WHERE project_id=? AND artifact_id=?",(project_id,output_artifact_id)).fetchone():
                raise KeyError("Output artifact must belong to the workflow project.")
            completed_at = datetime.now(timezone.utc).isoformat() if target in terminal else None
            c.execute("UPDATE workflow_runs SET status=?,completed_at=?,error_summary=?,output_artifact_id=COALESCE(?,output_artifact_id) WHERE project_id=? AND workflow_run_id=?",
                (target.value,completed_at,error_summary,output_artifact_id,project_id,run_id))
            self._add_workflow_event(c,run_id,old,target,error_summary or "")
        return self.get_workflow_run(project_id,run_id)

    def complete_workflow_run(self, project_id: str, run_id: str, *, output_artifact_id: str | None=None) -> ProjectWorkflowRun:
        return self._transition_workflow(project_id,run_id,WorkflowStatus.COMPLETED,output_artifact_id=output_artifact_id)

    def fail_workflow_run(self, project_id: str, run_id: str, error_summary: str) -> ProjectWorkflowRun:
        return self._transition_workflow(project_id,run_id,WorkflowStatus.FAILED,error_summary=error_summary)

    def interrupt_workflow_run(self, project_id: str, run_id: str, reason: str="Process interrupted") -> ProjectWorkflowRun:
        return self._transition_workflow(project_id,run_id,WorkflowStatus.INTERRUPTED,error_summary=reason)

    def get_workflow_run(self, project_id: str, run_id: str) -> ProjectWorkflowRun:
        with self.db() as c:
            row = c.execute("SELECT * FROM workflow_runs WHERE project_id=? AND workflow_run_id=?",(project_id,run_id)).fetchone()
            if row is None:
                self._require_project(c,project_id)
                raise KeyError(f"Unknown workflow run: {run_id}")
        return self._workflow_from_row(row)

    def list_project_workflows(self, project_id: str) -> list[ProjectWorkflowRun]:
        with self.db() as c:
            self._require_project(c,project_id)
            rows = c.execute("SELECT * FROM workflow_runs WHERE project_id=? ORDER BY created_at,workflow_run_id",(project_id,)).fetchall()
        return [self._workflow_from_row(row) for row in rows]

    def get_workflow_history(self, project_id: str, run_id: str) -> list[WorkflowRunEvent]:
        self.get_workflow_run(project_id,run_id)
        with self.db() as c:
            rows = c.execute("SELECT e.* FROM workflow_run_events e JOIN workflow_runs w USING(workflow_run_id) WHERE w.project_id=? AND w.workflow_run_id=? ORDER BY e.rowid",
                             (project_id,run_id)).fetchall()
        return [WorkflowRunEvent.model_validate(dict(row)) for row in rows]

    def associate_research_run(self, project_id: str, run_id: str) -> None:
        """Explicitly attach only an unowned legacy run to an active project."""
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c,project_id,writable=True)
            row = c.execute("SELECT project_id FROM research_runs WHERE id=?",(run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown research run: {run_id}")
            if row["project_id"] is not None:
                if row["project_id"] == project_id:
                    return
                raise ValueError("Research run already belongs to another project and cannot be reassigned.")
            c.execute("UPDATE research_runs SET project_id=? WHERE id=? AND project_id IS NULL",(project_id,run_id))

    def list_project_research_runs(self, project_id: str) -> list[ProjectResearchRunSummary]:
        with self.db() as c:
            self._require_project(c,project_id)
            rows = c.execute("""SELECT r.id AS run_id,r.prompt,r.created_at,p.status AS plan_status,
                    (SELECT COUNT(*) FROM research_questions q WHERE q.run_id=r.id) AS question_count,
                    (SELECT COUNT(*) FROM sources s WHERE s.run_id=r.id) AS source_count,
                    (SELECT COUNT(*) FROM evidence e WHERE e.run_id=r.id) AS evidence_count
                FROM research_runs r LEFT JOIN research_plans p ON p.run_id=r.id
                WHERE r.project_id=? ORDER BY r.created_at,r.id""",(project_id,)).fetchall()
        return [ProjectResearchRunSummary.model_validate(dict(row)) for row in rows]
