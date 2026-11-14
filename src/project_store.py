"""Project-specific persistence APIs layered on the shared SQLite store."""

import json
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from src.research.models import (
    ApprovalEvent, ApprovalEventStatus, DecisionCreate, DecisionEvidenceLink,
    DecisionEvidenceRelationship, DecisionOption, DecisionOptionCreate,
    DecisionUpdate, ProjectDecision, ProjectStatus,
    ArtifactCreate, ArtifactStatus, ProjectArtifact, ProjectStageKey, ProjectStageStatus,
    ProjectWorkflowRun, WorkflowRunCreate, WorkflowRunEvent, WorkflowStatus,
    ProjectResearchRunSummary, ResearchPlan,
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
    def _insert_approval_event(c, *, project_id: str, status: ApprovalEventStatus,
                               actor: str, comment: str, decision_id: str | None = None,
                               artifact_id: str | None = None) -> ApprovalEvent:
        sequence = c.execute("SELECT COALESCE(MAX(event_sequence),0)+1 FROM approval_events").fetchone()[0]
        event = ApprovalEvent(project_id=project_id, decision_id=decision_id, artifact_id=artifact_id,
                              status=status, actor=actor, comment=comment, event_sequence=sequence)
        c.execute("""INSERT INTO approval_events
            (approval_id,event_sequence,project_id,decision_id,artifact_id,status,actor,comment,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (event.approval_id, sequence, project_id, decision_id, artifact_id, status.value,
             actor, comment, event.created_at.isoformat()))
        return event

    @staticmethod
    def _latest_approval_event(c, *, project_id: str, decision_id: str | None = None,
                               artifact_id: str | None = None):
        if (decision_id is None) == (artifact_id is None):
            raise ValueError("Specify exactly one decision or artifact for approval history lookup.")
        field, value = ("decision_id", decision_id) if decision_id is not None else ("artifact_id", artifact_id)
        row = c.execute(f"SELECT * FROM approval_events WHERE project_id=? AND {field}=? ORDER BY event_sequence DESC LIMIT 1",
                         (project_id, value)).fetchone()
        return ApprovalEvent.model_validate(dict(row)) if row else None

    def _p1_approval_checkpoint(self, point: str, connection) -> None:
        """Test seam for proving rollback at transaction boundaries."""

    def approve_challenge_brief(self, project_id: str, artifact_id: str, decision_id: str,
                                status: ApprovalEventStatus, *, actor: str = "user",
                                comment: str = "") -> ApprovalEvent:
        """Atomically record the pre-P1 Challenge Brief and linked decision review."""
        status = ApprovalEventStatus(status)
        if status not in {ApprovalEventStatus.APPROVED, ApprovalEventStatus.CHANGES_REQUESTED,
                          ApprovalEventStatus.REJECTED}:
            raise ValueError("Challenge Brief review must be approved, changes_requested, or rejected.")
        if not actor.strip():
            raise ValueError("Approval actor cannot be empty.")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c, project_id, writable=True)
            artifact = c.execute("SELECT * FROM project_artifacts WHERE project_id=? AND artifact_id=?",
                                 (project_id, artifact_id)).fetchone()
            if artifact is None:
                raise KeyError(f"Artifact {artifact_id} does not belong to project {project_id}.")
            if artifact["stage_key"] is not None or artifact["artifact_type"] != "challenge_brief":
                raise ValueError("Artifact is not a pre-P1 Challenge Brief.")
            latest = c.execute("""SELECT artifact_id FROM project_artifacts
                WHERE project_id=? AND stage_key IS NULL AND artifact_type='challenge_brief'
                ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""", (project_id,)).fetchone()
            if latest is None or latest["artifact_id"] != artifact_id:
                raise ValueError("Only the latest Challenge Brief version can be reviewed.")
            if artifact["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                raise ValueError("Only a draft or ready Challenge Brief can be reviewed.")
            decision = c.execute("SELECT * FROM decisions WHERE project_id=? AND decision_id=?",
                                 (project_id, decision_id)).fetchone()
            if decision is None:
                raise KeyError(f"Decision {decision_id} does not belong to project {project_id}.")
            if decision["stage_key"] is not None or artifact["decision_id"] != decision_id:
                raise ValueError("Review decision is not linked to this Challenge Brief.")
            self._insert_approval_event(c, project_id=project_id, artifact_id=artifact_id,
                status=status, actor=actor.strip(), comment=comment)
            event = self._insert_approval_event(c, project_id=project_id, decision_id=decision_id,
                status=status, actor=actor.strip(), comment=comment)
        return event

    def approve_problem_frame(self, project_id: str, artifact_id: str, decision_id: str,
                              status: ApprovalEventStatus, *, actor: str = "user", comment: str = "") -> ApprovalEvent:
        """Atomically record a P1 frame decision and transition P1 when accepted."""
        status = ApprovalEventStatus(status)
        if status not in {ApprovalEventStatus.APPROVED, ApprovalEventStatus.CHANGES_REQUESTED,
                          ApprovalEventStatus.REJECTED}:
            raise ValueError("P1 review status must be approved, changes_requested, or rejected.")
        if not actor.strip():
            raise ValueError("Approval actor cannot be empty.")
        actor = actor.strip()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._p1_approval_checkpoint("before_writes", c)
            project = self._require_project(c, project_id, writable=True)
            if project["status"] != ProjectStatus.ACTIVE.value:
                raise ValueError("Problem Frames can only be approved for active projects.")
            artifact = c.execute("""SELECT * FROM project_artifacts
                WHERE project_id=? AND artifact_id=?""", (project_id, artifact_id)).fetchone()
            if artifact is None:
                raise KeyError(f"Artifact {artifact_id} does not belong to project {project_id}.")
            if artifact["stage_key"] != ProjectStageKey.P1.value or artifact["artifact_type"] != "problem_frame":
                raise ValueError("Artifact is not a P1 Problem Frame.")
            latest = c.execute("""SELECT artifact_id FROM project_artifacts
                WHERE project_id=? AND stage_key='P1' AND artifact_type='problem_frame'
                ORDER BY created_at DESC, version DESC, artifact_id DESC LIMIT 1""", (project_id,)).fetchone()
            if latest is None or latest["artifact_id"] != artifact_id:
                raise ValueError("Only the latest Problem Frame version can be reviewed or approved.")
            if artifact["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                raise ValueError("Only a draft or ready Problem Frame can be reviewed.")
            decision = c.execute("SELECT * FROM decisions WHERE project_id=? AND decision_id=?",
                                 (project_id, decision_id)).fetchone()
            if decision is None:
                raise KeyError(f"Decision {decision_id} does not belong to project {project_id}.")
            if decision["stage_key"] != ProjectStageKey.P1.value or artifact["decision_id"] != decision_id:
                raise ValueError("Approval decision is not linked to this P1 Problem Frame.")
            stage = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P1'",
                              (project_id,)).fetchone()
            if stage is None:
                raise KeyError(f"Stage P1 is not initialized for project {project_id}.")
            current = ProjectStageStatus(stage["status"])
            artifact_prior = self._latest_approval_event(c, project_id=project_id, artifact_id=artifact_id)
            decision_prior = self._latest_approval_event(c, project_id=project_id, decision_id=decision_id)

            # A fully committed repeat is idempotent. A split/legacy partial state is surfaced.
            if (artifact_prior and decision_prior and artifact_prior.status == decision_prior.status
                    and artifact_prior.status == status):
                if status == ApprovalEventStatus.APPROVED and current == ProjectStageStatus.COMPLETED:
                    return artifact_prior
                if status != ApprovalEventStatus.APPROVED and current == ProjectStageStatus.IN_PROGRESS:
                    return artifact_prior
                raise RuntimeError("P1 approval history and stage status are inconsistent; inspect before retrying.")
            if ((artifact_prior is None) != (decision_prior is None) or
                    (artifact_prior and decision_prior and artifact_prior.status != decision_prior.status)):
                raise RuntimeError("P1 approval history is inconsistent; inspect existing approval events before retrying.")
            if status == ApprovalEventStatus.APPROVED and current != ProjectStageStatus.IN_PROGRESS:
                raise ValueError(f"P1 must be in in_progress before approval; it is {current.value}.")
            if status != ApprovalEventStatus.APPROVED and current not in {
                    ProjectStageStatus.IN_PROGRESS, ProjectStageStatus.COMPLETED}:
                raise ValueError(f"P1 review requires in_progress or completed status; it is {current.value}.")

            artifact_event = self._insert_approval_event(c, project_id=project_id, artifact_id=artifact_id,
                status=status, actor=actor, comment=comment)
            self._p1_approval_checkpoint("after_artifact_approval", c)
            self._insert_approval_event(c, project_id=project_id, decision_id=decision_id,
                status=status, actor=actor, comment=comment)
            self._p1_approval_checkpoint("after_decision_approval", c)
            if status == ApprovalEventStatus.APPROVED:
                self._p1_approval_checkpoint("before_stage_completion", c)
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P1,
                    ProjectStageStatus.COMPLETED, actor=actor, note="Problem frame approved by human.",
                    expected_from={ProjectStageStatus.IN_PROGRESS})
            elif current == ProjectStageStatus.COMPLETED:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P1,
                    ProjectStageStatus.IN_PROGRESS, actor=actor,
                    note=("A review rejected the Problem Frame." if status == ApprovalEventStatus.REJECTED
                          else "A review requested changes to the Problem Frame."),
                    expected_from={ProjectStageStatus.COMPLETED, ProjectStageStatus.SKIPPED})
            self._p1_approval_checkpoint("after_stage_transition", c)
            self._p1_approval_checkpoint("before_commit", c)
            return artifact_event

    def approve_p2_artifact(self, project_id: str, artifact_id: str, decision_id: str,
                            artifact_type: str, status: ApprovalEventStatus = ApprovalEventStatus.APPROVED,
                            *, actor: str = "user", comment: str = "") -> ApprovalEvent:
        """Atomically approve a P2 plan/report artifact and optionally complete P2."""
        status = ApprovalEventStatus(status)
        if status not in {ApprovalEventStatus.APPROVED, ApprovalEventStatus.CHANGES_REQUESTED,
                          ApprovalEventStatus.REJECTED}:
            raise ValueError("P2 review status must be approved, changes_requested, or rejected.")
        if artifact_type not in {"research_plan", "research_report"}:
            raise ValueError("Unsupported P2 artifact type.")
        if not actor.strip():
            raise ValueError("Approval actor cannot be empty.")
        actor = actor.strip()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._p2_approval_checkpoint("before_writes", c)
            project = self._require_project(c, project_id, writable=True)
            if project["status"] != ProjectStatus.ACTIVE.value:
                raise ValueError("P2 artifacts can only be approved for active projects.")
            artifact = c.execute("SELECT * FROM project_artifacts WHERE project_id=? AND artifact_id=?",
                                 (project_id, artifact_id)).fetchone()
            if artifact is None:
                raise KeyError(f"Artifact {artifact_id} does not belong to project {project_id}.")
            if artifact["stage_key"] != ProjectStageKey.P2.value or artifact["artifact_type"] != artifact_type:
                raise ValueError(f"Artifact is not a P2 {artifact_type.replace('_', ' ')}.")
            latest = c.execute("""SELECT artifact_id FROM project_artifacts WHERE project_id=?
                AND stage_key='P2' AND artifact_type=? ORDER BY created_at DESC,version DESC,artifact_id DESC LIMIT 1""",
                (project_id, artifact_type)).fetchone()
            if latest is None or latest["artifact_id"] != artifact_id:
                raise ValueError(f"Only the latest {artifact_type.replace('_', ' ')} version can be reviewed.")
            if artifact["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                raise ValueError("Only a draft or ready P2 artifact can be reviewed.")
            decision = c.execute("SELECT stage_key FROM decisions WHERE project_id=? AND decision_id=?",
                                 (project_id, decision_id)).fetchone()
            if decision is None:
                raise KeyError(f"Decision {decision_id} does not belong to project {project_id}.")
            if decision["stage_key"] != ProjectStageKey.P2.value or artifact["decision_id"] != decision_id:
                raise ValueError("Approval decision is not linked to this P2 artifact.")
            stage_row = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P2'",
                                  (project_id,)).fetchone()
            if stage_row is None:
                raise KeyError(f"Stage P2 is not initialized for project {project_id}.")
            stage = ProjectStageStatus(stage_row["status"])
            artifact_prior = self._latest_approval_event(c, project_id=project_id, artifact_id=artifact_id)
            decision_prior = self._latest_approval_event(c, project_id=project_id, decision_id=decision_id)
            if artifact_prior and decision_prior and artifact_prior.status == decision_prior.status == status:
                if artifact_type == "research_report" and status == ApprovalEventStatus.APPROVED and stage == ProjectStageStatus.COMPLETED:
                    return artifact_prior
                if not (artifact_type == "research_report" and status == ApprovalEventStatus.APPROVED):
                    return artifact_prior
                raise RuntimeError("P2 report approval history and stage state are inconsistent.")
            if ((artifact_prior is None) != (decision_prior is None) or
                    (artifact_prior and decision_prior and artifact_prior.status != decision_prior.status)):
                raise RuntimeError("P2 approval history is inconsistent; inspect existing events before retrying.")

            if artifact_type == "research_plan":
                if stage != ProjectStageStatus.IN_PROGRESS:
                    raise ValueError(f"P2 must be in in_progress to review a plan; it is {stage.value}.")
                content = json.loads(artifact["content"] or "{}")
                plan_id = content.get("id")
                run_id = content.get("run_id")
                row = c.execute("SELECT plan_json,status,run_id FROM research_plans WHERE plan_id=?",
                                (plan_id,)).fetchone()
                run = c.execute("SELECT project_id FROM research_runs WHERE id=?", (run_id,)).fetchone()
                if row is None or row["run_id"] != run_id or run is None or run["project_id"] != project_id:
                    raise ValueError("Research plan is not backed by a run owned by this project.")
                plan_data = json.loads(row["plan_json"])
                if (plan_data.get("problem_frame_artifact_id") != content.get("problem_frame_artifact_id") or
                        plan_data.get("problem_frame_version") != content.get("problem_frame_version")):
                    raise ValueError("Research plan artifact content does not match its persisted plan record.")
                p1 = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P1'",
                               (project_id,)).fetchone()
                latest_frame = c.execute("""SELECT artifact_id,decision_id,status FROM project_artifacts
                    WHERE project_id=? AND stage_key='P1' AND artifact_type='problem_frame'
                    ORDER BY version DESC,created_at DESC LIMIT 1""", (project_id,)).fetchone()
                if (p1 is None or p1["status"] != ProjectStageStatus.COMPLETED.value or
                        latest_frame is None or latest_frame["artifact_id"] != plan_data.get("problem_frame_artifact_id") or
                        latest_frame["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}):
                    raise ValueError("Research plan must reference the latest approved Problem Frame.")
                frame_approval = self._latest_approval_event(c, project_id=project_id,
                    artifact_id=latest_frame["artifact_id"])
                frame_decision = (self._latest_approval_event(c, project_id=project_id,
                    decision_id=latest_frame["decision_id"]) if latest_frame["decision_id"] else None)
                if (not frame_approval or not frame_decision or
                        frame_approval.status != ApprovalEventStatus.APPROVED or
                        frame_decision.status != ApprovalEventStatus.APPROVED):
                    raise ValueError("Research plan source Problem Frame is no longer approved.")
                if row["status"] != "draft" and status == ApprovalEventStatus.APPROVED:
                    raise ValueError("Only a draft research plan can be approved.")
                plan_status = ("approved" if status == ApprovalEventStatus.APPROVED else
                               "draft" if status == ApprovalEventStatus.CHANGES_REQUESTED else "rejected")
                plan_data["status"] = plan_status
                plan_data["approved_at"] = datetime.now(timezone.utc).isoformat() if plan_status == "approved" else None
                c.execute("UPDATE research_plans SET plan_json=?,status=?,approved_at=? WHERE plan_id=?",
                          (json.dumps(plan_data, ensure_ascii=False), plan_status,
                           plan_data["approved_at"], plan_id))
                self._p2_approval_checkpoint("after_plan_update", c)
            else:
                if stage != ProjectStageStatus.IN_PROGRESS and not (status != ApprovalEventStatus.APPROVED and stage == ProjectStageStatus.COMPLETED):
                    raise ValueError(f"P2 must be in in_progress to review a report; it is {stage.value}.")
                report_data = json.loads(artifact["content"] or "{}")
                if status == ApprovalEventStatus.APPROVED and report_data.get("outcome", {}).get("execution_status") != "complete":
                    raise ValueError("A partial research report can be reviewed, but cannot complete P2. Run the approved plan again to address execution failures.")
                run_id = report_data.get("run_id")
                run = c.execute("SELECT project_id FROM research_runs WHERE id=?", (run_id,)).fetchone()
                if run is None or run["project_id"] != project_id:
                    raise ValueError("Research report is not backed by a run owned by this project.")
                workflow = c.execute("""SELECT workflow_run_id FROM workflow_runs
                    WHERE project_id=? AND stage_key='P2' AND status='completed' AND output_artifact_id=?
                    ORDER BY completed_at DESC LIMIT 1""", (project_id, artifact_id)).fetchone()
                if status == ApprovalEventStatus.APPROVED and workflow is None:
                    raise ValueError("P2 completion requires a successfully completed research workflow for this report.")

            event = self._insert_approval_event(c, project_id=project_id, artifact_id=artifact_id,
                                                status=status, actor=actor, comment=comment)
            self._p2_approval_checkpoint("after_artifact_approval", c)
            self._insert_approval_event(c, project_id=project_id, decision_id=decision_id,
                                        status=status, actor=actor, comment=comment)
            self._p2_approval_checkpoint("after_decision_approval", c)
            if artifact_type == "research_report" and status == ApprovalEventStatus.APPROVED:
                self._p2_approval_checkpoint("before_stage_completion", c)
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P2,
                    ProjectStageStatus.COMPLETED, actor=actor, note="Research report approved by human.",
                    expected_from={ProjectStageStatus.IN_PROGRESS})
                self._p2_approval_checkpoint("after_stage_transition", c)
            elif artifact_type == "research_report" and stage == ProjectStageStatus.COMPLETED:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P2,
                    ProjectStageStatus.IN_PROGRESS, actor=actor, note="Research report review requested changes.",
                    expected_from={ProjectStageStatus.COMPLETED})
            self._p2_approval_checkpoint("before_commit", c)
            return event

    def _p2_approval_checkpoint(self, point: str, connection) -> None:
        """Test seam for P2 approval failure injection."""

    def approve_p5_plan(self, project_id: str, artifact_id: str, decision_id: str,
                        status: ApprovalEventStatus, *, actor: str = "user", comment: str = "") -> ApprovalEvent:
        """Atomically review the latest P5 engineering plan and transition its stage."""
        status = ApprovalEventStatus(status)
        if status not in {ApprovalEventStatus.APPROVED, ApprovalEventStatus.CHANGES_REQUESTED,
                          ApprovalEventStatus.REJECTED}:
            raise ValueError("P5 review must be approved, changes_requested, or rejected.")
        if not actor.strip():
            raise ValueError("Approval actor cannot be empty.")
        actor = actor.strip()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            project = self._require_project(c, project_id, writable=True)
            if project["status"] != ProjectStatus.ACTIVE.value:
                raise ValueError("P5 plans can only be reviewed for active projects.")
            artifact = c.execute("SELECT * FROM project_artifacts WHERE project_id=? AND artifact_id=?",
                                 (project_id, artifact_id)).fetchone()
            if artifact is None:
                raise KeyError(f"Artifact {artifact_id} does not belong to project {project_id}.")
            if artifact["stage_key"] != ProjectStageKey.P5.value or artifact["artifact_type"] != "engineering_plan":
                raise ValueError("Artifact is not a P5 engineering plan.")
            latest = c.execute("""SELECT artifact_id FROM project_artifacts
                WHERE project_id=? AND stage_key='P5' AND artifact_type='engineering_plan'
                ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""", (project_id,)).fetchone()
            if latest is None or latest["artifact_id"] != artifact_id:
                raise ValueError("Only the latest P5 engineering plan can be reviewed.")
            if artifact["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                raise ValueError("Only a draft or ready P5 plan can be reviewed.")
            decision = c.execute("SELECT stage_key FROM decisions WHERE project_id=? AND decision_id=?",
                                  (project_id, decision_id)).fetchone()
            if decision is None:
                raise KeyError(f"Decision {decision_id} does not belong to project {project_id}.")
            if decision["stage_key"] != ProjectStageKey.P5.value or artifact["decision_id"] != decision_id:
                raise ValueError("Review decision is not linked to this P5 engineering plan.")
            stage_row = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P5'",
                                  (project_id,)).fetchone()
            if stage_row is None:
                raise KeyError(f"Stage P5 is not initialized for project {project_id}.")
            stage = ProjectStageStatus(stage_row["status"])
            artifact_prior = self._latest_approval_event(c, project_id=project_id, artifact_id=artifact_id)
            decision_prior = self._latest_approval_event(c, project_id=project_id, decision_id=decision_id)
            if artifact_prior and decision_prior and artifact_prior.status == decision_prior.status == status:
                if status == ApprovalEventStatus.APPROVED and stage == ProjectStageStatus.COMPLETED:
                    return artifact_prior
                if status != ApprovalEventStatus.APPROVED and stage == ProjectStageStatus.IN_PROGRESS:
                    return artifact_prior
            if ((artifact_prior is None) != (decision_prior is None) or
                    (artifact_prior and decision_prior and artifact_prior.status != decision_prior.status)):
                raise RuntimeError("P5 artifact and decision review histories are inconsistent.")
            if status == ApprovalEventStatus.APPROVED and stage != ProjectStageStatus.IN_PROGRESS:
                raise ValueError(f"P5 must be in progress before approval; it is {stage.value}.")
            if status != ApprovalEventStatus.APPROVED and stage not in {
                    ProjectStageStatus.IN_PROGRESS, ProjectStageStatus.COMPLETED}:
                raise ValueError(f"P5 review requires in_progress or completed status; it is {stage.value}.")

            self._insert_approval_event(c, project_id=project_id, artifact_id=artifact_id,
                status=status, actor=actor, comment=comment)
            event = self._insert_approval_event(c, project_id=project_id, decision_id=decision_id,
                status=status, actor=actor, comment=comment)
            if status == ApprovalEventStatus.APPROVED:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P5,
                    ProjectStageStatus.COMPLETED, actor=actor, note="Engineering plan approved by human.",
                    expected_from={ProjectStageStatus.IN_PROGRESS})
            elif stage == ProjectStageStatus.COMPLETED:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P5,
                    ProjectStageStatus.IN_PROGRESS, actor=actor,
                    note=f"Engineering plan review {status.value}.",
                    expected_from={ProjectStageStatus.COMPLETED})
        return event

    def approve_p6_execution(self, project_id: str, artifact_id: str, decision_id: str,
                             status: ApprovalEventStatus, *, actor: str = "user",
                             comment: str = "") -> ApprovalEvent:
        """Atomically review the latest P6 execution record and transition its stage."""
        status = ApprovalEventStatus(status)
        if status not in {ApprovalEventStatus.APPROVED, ApprovalEventStatus.CHANGES_REQUESTED,
                          ApprovalEventStatus.REJECTED}:
            raise ValueError("P6 review must be approved, changes_requested, or rejected.")
        if not actor.strip():
            raise ValueError("Approval actor cannot be empty.")
        actor = actor.strip()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            project = self._require_project(c, project_id, writable=True)
            if project["status"] != ProjectStatus.ACTIVE.value:
                raise ValueError("P6 execution records can only be reviewed for active projects.")
            artifact = c.execute("SELECT * FROM project_artifacts WHERE project_id=? AND artifact_id=?",
                                 (project_id, artifact_id)).fetchone()
            if artifact is None:
                raise KeyError(f"Artifact {artifact_id} does not belong to project {project_id}.")
            if artifact["stage_key"] != ProjectStageKey.P6.value or artifact["artifact_type"] != "mvp_execution":
                raise ValueError("Artifact is not a P6 MVP execution record.")
            latest = c.execute("""SELECT artifact_id FROM project_artifacts
                WHERE project_id=? AND stage_key='P6' AND artifact_type='mvp_execution'
                ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""", (project_id,)).fetchone()
            if latest is None or latest["artifact_id"] != artifact_id:
                raise ValueError("Only the latest P6 execution record can be reviewed.")
            if artifact["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                raise ValueError("Only a draft or ready P6 execution record can be reviewed.")
            decision = c.execute("SELECT stage_key FROM decisions WHERE project_id=? AND decision_id=?",
                                  (project_id, decision_id)).fetchone()
            if decision is None:
                raise KeyError(f"Decision {decision_id} does not belong to project {project_id}.")
            if decision["stage_key"] != ProjectStageKey.P6.value or artifact["decision_id"] != decision_id:
                raise ValueError("Review decision is not linked to this P6 execution record.")
            stage_row = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P6'",
                                  (project_id,)).fetchone()
            if stage_row is None:
                raise KeyError(f"Stage P6 is not initialized for project {project_id}.")
            stage = ProjectStageStatus(stage_row["status"])
            artifact_prior = self._latest_approval_event(c, project_id=project_id, artifact_id=artifact_id)
            decision_prior = self._latest_approval_event(c, project_id=project_id, decision_id=decision_id)
            if artifact_prior and decision_prior and artifact_prior.status == decision_prior.status == status:
                if status == ApprovalEventStatus.APPROVED and stage == ProjectStageStatus.COMPLETED:
                    return artifact_prior
                if status != ApprovalEventStatus.APPROVED and stage == ProjectStageStatus.IN_PROGRESS:
                    return artifact_prior
            if ((artifact_prior is None) != (decision_prior is None) or
                    (artifact_prior and decision_prior and artifact_prior.status != decision_prior.status)):
                raise RuntimeError("P6 artifact and decision review histories are inconsistent.")
            if status == ApprovalEventStatus.APPROVED:
                self._validate_p6_completion_in_transaction(c, project_id, artifact)
                if stage != ProjectStageStatus.IN_PROGRESS:
                    raise ValueError(f"P6 must be in progress before approval; it is {stage.value}.")
            elif stage not in {ProjectStageStatus.IN_PROGRESS, ProjectStageStatus.COMPLETED}:
                raise ValueError(f"P6 review requires in_progress or completed status; it is {stage.value}.")

            self._insert_approval_event(c, project_id=project_id, artifact_id=artifact_id,
                status=status, actor=actor, comment=comment)
            event = self._insert_approval_event(c, project_id=project_id, decision_id=decision_id,
                status=status, actor=actor, comment=comment)
            if status == ApprovalEventStatus.APPROVED:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P6,
                    ProjectStageStatus.COMPLETED, actor=actor,
                    note="MVP execution and test record approved by human.",
                    expected_from={ProjectStageStatus.IN_PROGRESS})
            elif stage == ProjectStageStatus.COMPLETED:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P6,
                    ProjectStageStatus.IN_PROGRESS, actor=actor,
                    note=f"MVP execution review {status.value}.",
                    expected_from={ProjectStageStatus.COMPLETED})
        return event

    @staticmethod
    def _validate_p6_completion_in_transaction(c, project_id: str, artifact) -> None:
        p5_stage = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P5'",
                              (project_id,)).fetchone()
        p5 = c.execute("""SELECT * FROM project_artifacts WHERE project_id=? AND stage_key='P5'
            AND artifact_type='engineering_plan' ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""",
            (project_id,)).fetchone()
        if p5 is None or p5_stage is None or p5_stage["status"] != ProjectStageStatus.COMPLETED.value:
            raise ValueError("P6 requires completed and approved P5 engineering work.")
        p5_artifact_event = ProjectStore._latest_approval_event(c, project_id=project_id, artifact_id=p5["artifact_id"])
        p5_decision_event = (ProjectStore._latest_approval_event(c, project_id=project_id, decision_id=p5["decision_id"])
                             if p5["decision_id"] else None)
        if (not p5_artifact_event or not p5_decision_event
                or p5_artifact_event.status != ApprovalEventStatus.APPROVED
                or p5_decision_event.status != ApprovalEventStatus.APPROVED):
            raise ValueError("P6 requires the latest P5 plan and its linked decision to be approved.")
        execution = json.loads(artifact["content"] or "{}")
        p5_data = json.loads(p5["content"] or "{}")
        if (execution.get("p5_artifact_id") != p5["artifact_id"]
                or execution.get("p5_artifact_version") != p5["version"]):
            raise ValueError("P6 execution record does not match the latest P5 plan.")
        expected_tasks = {task.get("id") for task in p5_data.get("build_tasks", [])}
        actual_tasks = {task.get("task_id") for task in execution.get("tasks", [])}
        if actual_tasks != expected_tasks or any(task.get("status") not in {"done", "deferred"}
                                                  for task in execution.get("tasks", [])):
            raise ValueError("P6 completion requires every P5 task to be done or explicitly deferred.")
        expected_tests = {(kind, scenario) for kind, scenarios in (
            ("scenario", p5_data.get("test_scenarios", [])),
            ("acceptance", p5_data.get("demo_acceptance_criteria", [])))
            for scenario in scenarios}
        actual_tests = {(test.get("test_kind"), test.get("scenario")) for test in execution.get("tests", [])}
        if actual_tests != expected_tests or any(test.get("outcome") in {None, "not_run"}
                                                  for test in execution.get("tests", [])):
            raise ValueError("P6 completion requires a recorded result for every P5 test and demo criterion.")
        if any(test.get("outcome") == "failed" and not test.get("known_failure")
               for test in execution.get("tests", [])):
            raise ValueError("Every failed test needs a known failure or limitation recorded.")

    def approve_p7_impact(self, project_id: str, artifact_id: str, decision_id: str,
                          status: ApprovalEventStatus, *, actor: str = "user",
                          comment: str = "") -> ApprovalEvent:
        """Atomically review the latest P7 impact brief and transition its stage."""
        status = ApprovalEventStatus(status)
        if status not in {ApprovalEventStatus.APPROVED, ApprovalEventStatus.CHANGES_REQUESTED,
                          ApprovalEventStatus.REJECTED}:
            raise ValueError("P7 review must be approved, changes_requested, or rejected.")
        if not actor.strip():
            raise ValueError("Approval actor cannot be empty.")
        actor = actor.strip()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            project = self._require_project(c, project_id, writable=True)
            if project["status"] != ProjectStatus.ACTIVE.value:
                raise ValueError("P7 briefs can only be reviewed for active projects.")
            artifact = c.execute("SELECT * FROM project_artifacts WHERE project_id=? AND artifact_id=?",
                                 (project_id, artifact_id)).fetchone()
            if artifact is None:
                raise KeyError(f"Artifact {artifact_id} does not belong to project {project_id}.")
            if artifact["stage_key"] != ProjectStageKey.P7.value or artifact["artifact_type"] != "impact_feasibility_brief":
                raise ValueError("Artifact is not a P7 impact and feasibility brief.")
            latest = c.execute("""SELECT artifact_id FROM project_artifacts
                WHERE project_id=? AND stage_key='P7' AND artifact_type='impact_feasibility_brief'
                ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""", (project_id,)).fetchone()
            if latest is None or latest["artifact_id"] != artifact_id:
                raise ValueError("Only the latest P7 impact brief can be reviewed.")
            if artifact["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                raise ValueError("Only a draft or ready P7 brief can be reviewed.")
            decision = c.execute("SELECT stage_key FROM decisions WHERE project_id=? AND decision_id=?",
                                  (project_id, decision_id)).fetchone()
            if decision is None:
                raise KeyError(f"Decision {decision_id} does not belong to project {project_id}.")
            if decision["stage_key"] != ProjectStageKey.P7.value or artifact["decision_id"] != decision_id:
                raise ValueError("Review decision is not linked to this P7 impact brief.")
            stage_row = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P7'",
                                  (project_id,)).fetchone()
            if stage_row is None:
                raise KeyError(f"Stage P7 is not initialized for project {project_id}.")
            stage = ProjectStageStatus(stage_row["status"])
            artifact_prior = self._latest_approval_event(c, project_id=project_id, artifact_id=artifact_id)
            decision_prior = self._latest_approval_event(c, project_id=project_id, decision_id=decision_id)
            if artifact_prior and decision_prior and artifact_prior.status == decision_prior.status == status:
                if status == ApprovalEventStatus.APPROVED and stage == ProjectStageStatus.COMPLETED:
                    return artifact_prior
                if status != ApprovalEventStatus.APPROVED and stage == ProjectStageStatus.IN_PROGRESS:
                    return artifact_prior
            if ((artifact_prior is None) != (decision_prior is None) or
                    (artifact_prior and decision_prior and artifact_prior.status != decision_prior.status)):
                raise RuntimeError("P7 artifact and decision review histories are inconsistent.")
            if status == ApprovalEventStatus.APPROVED and stage != ProjectStageStatus.IN_PROGRESS:
                raise ValueError(f"P7 must be in progress before approval; it is {stage.value}.")
            if status != ApprovalEventStatus.APPROVED and stage not in {
                    ProjectStageStatus.IN_PROGRESS, ProjectStageStatus.COMPLETED}:
                raise ValueError(f"P7 review requires in_progress or completed status; it is {stage.value}.")
            if status == ApprovalEventStatus.APPROVED:
                self._validate_p7_completion_in_transaction(c, project_id, artifact)
            self._insert_approval_event(c, project_id=project_id, artifact_id=artifact_id,
                status=status, actor=actor, comment=comment)
            event = self._insert_approval_event(c, project_id=project_id, decision_id=decision_id,
                status=status, actor=actor, comment=comment)
            if status == ApprovalEventStatus.APPROVED:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P7,
                    ProjectStageStatus.COMPLETED, actor=actor,
                    note="Impact and feasibility brief approved by human.",
                    expected_from={ProjectStageStatus.IN_PROGRESS})
            elif stage == ProjectStageStatus.COMPLETED:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P7,
                    ProjectStageStatus.IN_PROGRESS, actor=actor,
                    note=f"Impact and feasibility review {status.value}.",
                    expected_from={ProjectStageStatus.COMPLETED})
        return event

    @staticmethod
    def _validate_p7_completion_in_transaction(c, project_id: str, artifact) -> None:
        p6_stage = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P6'",
                              (project_id,)).fetchone()
        p6 = c.execute("""SELECT * FROM project_artifacts WHERE project_id=? AND stage_key='P6'
            AND artifact_type='mvp_execution' ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""",
            (project_id,)).fetchone()
        if p6 is None or p6_stage is None or p6_stage["status"] != ProjectStageStatus.COMPLETED.value:
            raise ValueError("P7 requires a completed P6 execution and test record.")
        p6_artifact_event = ProjectStore._latest_approval_event(c, project_id=project_id, artifact_id=p6["artifact_id"])
        p6_decision_event = (ProjectStore._latest_approval_event(c, project_id=project_id, decision_id=p6["decision_id"])
                             if p6["decision_id"] else None)
        if (not p6_artifact_event or not p6_decision_event
                or p6_artifact_event.status != ApprovalEventStatus.APPROVED
                or p6_decision_event.status != ApprovalEventStatus.APPROVED):
            raise ValueError("P7 requires the latest P6 record and its linked decision to be approved.")
        brief = json.loads(artifact["content"] or "{}")
        if brief.get("p6_artifact_id") != p6["artifact_id"] or brief.get("p6_artifact_version") != p6["version"]:
            raise ValueError("P7 impact brief does not match the latest P6 execution record.")
        upstream_types = {
            "p1": ("P1", "problem_frame"), "p2": ("P2", "research_report"),
            "p3": ("P3", "validation_decision"), "p4": ("P4", "solution_options"),
            "p5": ("P5", "engineering_plan"),
        }
        for prefix, (stage_key, artifact_type) in upstream_types.items():
            upstream = c.execute("""SELECT artifact_id,version FROM project_artifacts WHERE project_id=?
                AND stage_key=? AND artifact_type=? ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""",
                (project_id, stage_key, artifact_type)).fetchone()
            if (upstream is None or brief.get(f"{prefix}_artifact_id") != upstream["artifact_id"]
                    or brief.get(f"{prefix}_artifact_version") != upstream["version"]):
                raise ValueError("P7 impact brief references stale upstream project work.")
        p4 = c.execute("""SELECT content FROM project_artifacts WHERE project_id=? AND stage_key='P4'
            AND artifact_type='solution_options' ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""",
            (project_id,)).fetchone()
        p4_data = json.loads(p4["content"] or "{}") if p4 else {}
        if brief.get("selected_option_id") != p4_data.get("selected_option_id"):
            raise ValueError("P7 impact brief does not match the selected P4 direction.")

    def approve_p8_pitch(self, project_id: str, artifact_id: str, decision_id: str,
                         status: ApprovalEventStatus, *, actor: str = "user",
                         comment: str = "") -> ApprovalEvent:
        """Atomically review the latest P8 package and transition its stage."""
        status = ApprovalEventStatus(status)
        if status not in {ApprovalEventStatus.APPROVED, ApprovalEventStatus.CHANGES_REQUESTED,
                          ApprovalEventStatus.REJECTED}:
            raise ValueError("P8 review must be approved, changes_requested, or rejected.")
        if not actor.strip():
            raise ValueError("Approval actor cannot be empty.")
        actor = actor.strip()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            project = self._require_project(c, project_id, writable=True)
            if project["status"] != ProjectStatus.ACTIVE.value:
                raise ValueError("P8 packages can only be reviewed for active projects.")
            artifact = c.execute("SELECT * FROM project_artifacts WHERE project_id=? AND artifact_id=?",
                                 (project_id, artifact_id)).fetchone()
            if artifact is None:
                raise KeyError(f"Artifact {artifact_id} does not belong to project {project_id}.")
            if artifact["stage_key"] != ProjectStageKey.P8.value or artifact["artifact_type"] != "pitch_preparation_package":
                raise ValueError("Artifact is not a P8 pitch preparation package.")
            latest = c.execute("""SELECT artifact_id FROM project_artifacts
                WHERE project_id=? AND stage_key='P8' AND artifact_type='pitch_preparation_package'
                ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""", (project_id,)).fetchone()
            if latest is None or latest["artifact_id"] != artifact_id:
                raise ValueError("Only the latest P8 pitch package can be reviewed.")
            if artifact["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                raise ValueError("Only a draft or ready P8 package can be reviewed.")
            decision = c.execute("SELECT stage_key FROM decisions WHERE project_id=? AND decision_id=?",
                                  (project_id, decision_id)).fetchone()
            if decision is None:
                raise KeyError(f"Decision {decision_id} does not belong to project {project_id}.")
            if decision["stage_key"] != ProjectStageKey.P8.value or artifact["decision_id"] != decision_id:
                raise ValueError("Review decision is not linked to this P8 package.")
            stage_row = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P8'",
                                  (project_id,)).fetchone()
            if stage_row is None:
                raise KeyError(f"Stage P8 is not initialized for project {project_id}.")
            stage = ProjectStageStatus(stage_row["status"])
            artifact_prior = self._latest_approval_event(c, project_id=project_id, artifact_id=artifact_id)
            decision_prior = self._latest_approval_event(c, project_id=project_id, decision_id=decision_id)
            if artifact_prior and decision_prior and artifact_prior.status == decision_prior.status == status:
                if status == ApprovalEventStatus.APPROVED and stage == ProjectStageStatus.COMPLETED:
                    return artifact_prior
                if status != ApprovalEventStatus.APPROVED and stage == ProjectStageStatus.IN_PROGRESS:
                    return artifact_prior
            if ((artifact_prior is None) != (decision_prior is None) or
                    (artifact_prior and decision_prior and artifact_prior.status != decision_prior.status)):
                raise RuntimeError("P8 artifact and decision review histories are inconsistent.")
            if status == ApprovalEventStatus.APPROVED and stage != ProjectStageStatus.IN_PROGRESS:
                raise ValueError(f"P8 must be in progress before approval; it is {stage.value}.")
            if status != ApprovalEventStatus.APPROVED and stage not in {
                    ProjectStageStatus.IN_PROGRESS, ProjectStageStatus.COMPLETED}:
                raise ValueError(f"P8 review requires in_progress or completed status; it is {stage.value}.")
            if status == ApprovalEventStatus.APPROVED:
                self._validate_p8_completion_in_transaction(c, project_id, artifact)
            self._insert_approval_event(c, project_id=project_id, artifact_id=artifact_id,
                status=status, actor=actor, comment=comment)
            event = self._insert_approval_event(c, project_id=project_id, decision_id=decision_id,
                status=status, actor=actor, comment=comment)
            if status == ApprovalEventStatus.APPROVED:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P8,
                    ProjectStageStatus.COMPLETED, actor=actor,
                    note="Pitch and judge preparation package approved by human.",
                    expected_from={ProjectStageStatus.IN_PROGRESS})
            elif stage == ProjectStageStatus.COMPLETED:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P8,
                    ProjectStageStatus.IN_PROGRESS, actor=actor,
                    note=f"Pitch package review {status.value}.",
                    expected_from={ProjectStageStatus.COMPLETED})
        return event

    @staticmethod
    def _validate_p8_completion_in_transaction(c, project_id: str, artifact) -> None:
        p7_stage = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P7'",
                              (project_id,)).fetchone()
        p7 = c.execute("""SELECT * FROM project_artifacts WHERE project_id=? AND stage_key='P7'
            AND artifact_type='impact_feasibility_brief' ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""",
            (project_id,)).fetchone()
        if p7 is None or p7_stage is None or p7_stage["status"] != ProjectStageStatus.COMPLETED.value:
            raise ValueError("P8 requires completed and approved P7 impact work.")
        p7_artifact_event = ProjectStore._latest_approval_event(c, project_id=project_id, artifact_id=p7["artifact_id"])
        p7_decision_event = (ProjectStore._latest_approval_event(c, project_id=project_id, decision_id=p7["decision_id"])
                             if p7["decision_id"] else None)
        if (not p7_artifact_event or not p7_decision_event
                or p7_artifact_event.status != ApprovalEventStatus.APPROVED
                or p7_decision_event.status != ApprovalEventStatus.APPROVED):
            raise ValueError("P8 requires the latest P7 brief and its linked decision to be approved.")
        package = json.loads(artifact["content"] or "{}")
        if package.get("p7_artifact_id") != p7["artifact_id"] or package.get("p7_artifact_version") != p7["version"]:
            raise ValueError("P8 package does not match the latest P7 brief.")
        upstream_types = {
            "p1": ("P1", "problem_frame"), "p2": ("P2", "research_report"),
            "p3": ("P3", "validation_decision"), "p4": ("P4", "solution_options"),
            "p5": ("P5", "engineering_plan"), "p6": ("P6", "mvp_execution"),
        }
        for prefix, (stage_key, artifact_type) in upstream_types.items():
            upstream = c.execute("""SELECT artifact_id,version FROM project_artifacts WHERE project_id=?
                AND stage_key=? AND artifact_type=? ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""",
                (project_id, stage_key, artifact_type)).fetchone()
            if (upstream is None or package.get(f"{prefix}_artifact_id") != upstream["artifact_id"]
                    or package.get(f"{prefix}_artifact_version") != upstream["version"]):
                raise ValueError("P8 package references stale upstream project work.")
        p4 = c.execute("""SELECT content FROM project_artifacts WHERE project_id=? AND stage_key='P4'
            AND artifact_type='solution_options' ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""",
            (project_id,)).fetchone()
        p4_data = json.loads(p4["content"] or "{}") if p4 else {}
        if package.get("selected_option_id") != p4_data.get("selected_option_id"):
            raise ValueError("P8 package does not match the selected P4 direction.")

    def create_p2_research_plan(self, plan: ResearchPlan, *, title: str = "Research plan"):
        """Persist plan, P2 start, linked decision, and versioned artifact atomically."""
        if any(question.run_id != plan.run_id for question in plan.questions):
            raise ValueError("Every research plan question must belong to the plan's research run.")
        if plan.status != "draft":
            raise ValueError("A new project research plan must begin as a draft.")
        project_id = next((question.project_id for question in plan.questions if question.project_id), None)
        if project_id is None:
            raise ValueError("Project-scoped research questions must identify their project.")
        if any(question.project_id != project_id for question in plan.questions):
            raise ValueError("Every research plan question must belong to the same project.")
        now = datetime.now(timezone.utc).isoformat()
        decision_id, artifact_id = str(uuid4()), str(uuid4())
        decision = DecisionCreate(project_id=project_id, stage_key=ProjectStageKey.P2,
            title="Approve research plan")
        content = plan.model_dump_json(indent=2)
        if len(content.encode("utf-8")) > 512 * 1024:
            raise ValueError("Artifact content is limited to 512 KiB; store larger output as a safe relative reference.")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            project = self._require_project(c, project_id, writable=True)
            if project["status"] != ProjectStatus.ACTIVE.value:
                raise ValueError("Research planning requires an active project.")
            p1 = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P1'",
                           (project_id,)).fetchone()
            frame = c.execute("""SELECT * FROM project_artifacts WHERE project_id=? AND stage_key='P1'
                AND artifact_type='problem_frame' ORDER BY version DESC,created_at DESC LIMIT 1""",
                (project_id,)).fetchone()
            if p1 is None or p1["status"] != ProjectStageStatus.COMPLETED.value or frame is None:
                raise ValueError("P1 must be completed before creating a P2 plan.")
            frame_approval = self._latest_approval_event(c, project_id=project_id, artifact_id=frame["artifact_id"])
            decision_approval = (self._latest_approval_event(c, project_id=project_id, decision_id=frame["decision_id"])
                                 if frame["decision_id"] else None)
            if (frame["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}
                    or not frame_approval or not decision_approval
                    or frame_approval.status != ApprovalEventStatus.APPROVED
                    or decision_approval.status != ApprovalEventStatus.APPROVED):
                raise ValueError("The latest Problem Frame and its linked decision must be approved.")
            if plan.problem_frame_artifact_id != frame["artifact_id"] or plan.problem_frame_version != frame["version"]:
                raise ValueError("Research plan must be based on the latest approved Problem Frame version.")
            stage = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P2'",
                              (project_id,)).fetchone()
            if stage is None:
                raise KeyError(f"Stage P2 is not initialized for project {project_id}.")
            current = ProjectStageStatus(stage["status"])
            if current == ProjectStageStatus.PENDING:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P2,
                    ProjectStageStatus.IN_PROGRESS, expected_from={ProjectStageStatus.PENDING})
            elif current in {ProjectStageStatus.COMPLETED, ProjectStageStatus.SKIPPED}:
                self._transition_project_stage_in_transaction(c, project_id, ProjectStageKey.P2,
                    ProjectStageStatus.IN_PROGRESS, note="A revised research plan requires a new review.",
                    expected_from={ProjectStageStatus.COMPLETED, ProjectStageStatus.SKIPPED})
            elif current != ProjectStageStatus.IN_PROGRESS:
                raise ValueError(f"A research plan cannot be created while P2 is {current.value}.")

            c.execute("INSERT INTO research_runs(id,prompt,created_at,project_id) VALUES(?,?,?,?)",
                      (plan.run_id, plan.brief.problem_statement, plan.created_at.isoformat(), project_id))
            c.execute("INSERT INTO research_plans VALUES(?,?,?,?,?,?)",
                      (plan.id, plan.run_id, plan.model_dump_json(), plan.status,
                       plan.created_at.isoformat(), None))
            for question in plan.questions:
                self._insert_question(c, question)

            previous = c.execute("""SELECT * FROM project_artifacts WHERE project_id=? AND stage_key='P2'
                AND artifact_type='research_plan' ORDER BY version DESC,created_at DESC LIMIT 1""",
                (project_id,)).fetchone()
            artifact_key = previous["artifact_key"] if previous else str(uuid4())
            version = previous["version"] + 1 if previous else 1
            if previous is not None:
                if previous["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                    raise ValueError("A new research-plan version can only supersede a draft or ready plan.")
                c.execute("UPDATE project_artifacts SET status='superseded',updated_at=? WHERE project_id=? AND artifact_id=?",
                          (now, project_id, previous["artifact_id"]))
            c.execute("""INSERT INTO decisions
                (decision_id,project_id,stage_key,title,description,decision_type,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (decision_id, project_id, ProjectStageKey.P2.value, decision.title,
                 "Human review of the plan is required before research execution.", decision.decision_type,
                 decision.status, now, now))
            c.execute("""INSERT INTO project_artifacts
                (artifact_id,artifact_key,project_id,stage_key,decision_id,artifact_type,title,description,content,storage_ref,version,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,NULL,?,'draft',?,?)""",
                (artifact_id, artifact_key, project_id, ProjectStageKey.P2.value, decision_id,
                 "research_plan", title, "Question-driven P2 research plan.", content, version, now, now))
        artifact = self.get_artifact(project_id, artifact_id)
        return plan, artifact, self.get_decision(project_id, decision_id)

    def persist_p2_report(self, project_id: str, workflow_run_id: str, run_id: str,
                          content: str, *, title: str = "Research report"):
        """Persist the P2 report, linked decision, and workflow completion atomically."""
        if len(content.encode("utf-8")) > 512 * 1024:
            raise ValueError("Artifact content is limited to 512 KiB; store larger output as a safe relative reference.")
        now = datetime.now(timezone.utc).isoformat()
        decision_id, artifact_id = str(uuid4()), str(uuid4())
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_project(c, project_id, writable=True)
            workflow = c.execute("SELECT * FROM workflow_runs WHERE project_id=? AND workflow_run_id=?",
                                 (project_id, workflow_run_id)).fetchone()
            if workflow is None:
                raise KeyError(f"Unknown workflow run: {workflow_run_id}")
            if workflow["stage_key"] != ProjectStageKey.P2.value or workflow["status"] != WorkflowStatus.RUNNING.value:
                raise ValueError("Only a running P2 research workflow can persist its report.")
            run = c.execute("SELECT project_id FROM research_runs WHERE id=?", (run_id,)).fetchone()
            if run is None or run["project_id"] != project_id:
                raise ValueError("Research report run does not belong to this project.")
            stage = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P2'",
                              (project_id,)).fetchone()
            if stage is None or stage["status"] != ProjectStageStatus.IN_PROGRESS.value:
                raise ValueError("P2 must remain in progress while its report is persisted.")
            previous = c.execute("""SELECT * FROM project_artifacts WHERE project_id=? AND stage_key='P2'
                AND artifact_type='research_report' ORDER BY version DESC,created_at DESC LIMIT 1""",
                (project_id,)).fetchone()
            artifact_key = previous["artifact_key"] if previous else str(uuid4())
            version = previous["version"] + 1 if previous else 1
            if previous:
                if previous["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                    raise ValueError("A new research report can only supersede a draft or ready report.")
                c.execute("UPDATE project_artifacts SET status='superseded',updated_at=? WHERE project_id=? AND artifact_id=?",
                          (now, project_id, previous["artifact_id"]))
            c.execute("""INSERT INTO decisions
                (decision_id,project_id,stage_key,title,description,decision_type,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (decision_id, project_id, ProjectStageKey.P2.value, "Approve research report",
                 "Human review of source traceability, coverage, contradictions, and limitations.",
                 "general", "open", now, now))
            c.execute("""INSERT INTO project_artifacts
                (artifact_id,artifact_key,project_id,stage_key,decision_id,artifact_type,title,description,content,storage_ref,version,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,NULL,?,'draft',?,?)""",
                (artifact_id, artifact_key, project_id, ProjectStageKey.P2.value, decision_id,
                 "research_report", title, "Source traceable P2 research output.", content, version, now, now))
            c.execute("UPDATE workflow_runs SET status='completed',completed_at=?,output_artifact_id=? WHERE workflow_run_id=? AND status='running'",
                      (now, artifact_id, workflow_run_id))
            self._add_workflow_event(c, workflow_run_id, WorkflowStatus.RUNNING,
                                     WorkflowStatus.COMPLETED, "Research report persisted.")
        return self.get_artifact(project_id, artifact_id), self.get_decision(project_id, decision_id), \
            self.get_workflow_run(project_id, workflow_run_id)

    def start_project_stage(self, project_id, stage_key, *, actor="user"):
        key = ProjectStageKey(stage_key)
        if key == ProjectStageKey.P1:
            with self.db() as c:
                c.execute("BEGIN IMMEDIATE")
                self._require_project(c, project_id, writable=True)
                brief = c.execute("""SELECT artifact_id,decision_id FROM project_artifacts
                    WHERE project_id=? AND stage_key IS NULL AND artifact_type='challenge_brief'
                    ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""", (project_id,)).fetchone()
                if brief is not None:
                    artifact_approval = self._latest_approval_event(c, project_id=project_id,
                                                                   artifact_id=brief["artifact_id"])
                    decision_approval = (self._latest_approval_event(c, project_id=project_id,
                        decision_id=brief["decision_id"]) if brief["decision_id"] else None)
                    if (not artifact_approval or not decision_approval
                            or artifact_approval.status != ApprovalEventStatus.APPROVED
                            or decision_approval.status != ApprovalEventStatus.APPROVED):
                        raise ValueError("The latest Challenge Brief and its linked decision must both be approved before P1.")
                stage = self._transition_project_stage_in_transaction(c, project_id, key,
                    ProjectStageStatus.IN_PROGRESS, actor=actor, expected_from={ProjectStageStatus.PENDING})
            return stage
        if key != ProjectStageKey.P2:
            return super().start_project_stage(project_id, key, actor=actor)
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            project = self._require_project(c, project_id, writable=True)
            if project["status"] != ProjectStatus.ACTIVE.value:
                raise ValueError("P2 can only start for active projects.")
            p1 = c.execute("SELECT status FROM project_stages WHERE project_id=? AND stage_key='P1'",
                           (project_id,)).fetchone()
            if p1 is None or p1["status"] != ProjectStageStatus.COMPLETED.value:
                raise ValueError("P1 must be completed before P2 can start.")
            frame = c.execute("""SELECT artifact_id,decision_id,status FROM project_artifacts
                WHERE project_id=? AND stage_key='P1' AND artifact_type='problem_frame'
                ORDER BY version DESC,created_at DESC LIMIT 1""", (project_id,)).fetchone()
            if frame is None or frame["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                raise ValueError("P2 requires an eligible latest approved Problem Frame.")
            decision = frame["decision_id"]
            artifact_approval = self._latest_approval_event(c, project_id=project_id, artifact_id=frame["artifact_id"])
            decision_approval = self._latest_approval_event(c, project_id=project_id, decision_id=decision) if decision else None
            if (not artifact_approval or not decision_approval or
                    artifact_approval.status != ApprovalEventStatus.APPROVED or
                    decision_approval.status != ApprovalEventStatus.APPROVED):
                raise ValueError("P2 requires the latest Problem Frame and its linked decision to be approved.")
            stage = self._transition_project_stage_in_transaction(c, project_id, key,
                ProjectStageStatus.IN_PROGRESS, actor=actor, expected_from={ProjectStageStatus.PENDING})
        return stage

    def complete_project_stage(self, project_id, stage_key, *, actor="user", note=None):
        """Gate implemented stages on their latest approved project artifacts."""
        key = ProjectStageKey(stage_key)
        if key not in {ProjectStageKey.P1, ProjectStageKey.P2, ProjectStageKey.P5,
                       ProjectStageKey.P6, ProjectStageKey.P7, ProjectStageKey.P8}:
            return super().complete_project_stage(project_id, key, actor=actor, note=note)
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            project = self._require_project(c, project_id, writable=True)
            if project["status"] != ProjectStatus.ACTIVE.value:
                raise ValueError(f"{key.value} can only be completed for active projects.")
            artifact_type = {
                ProjectStageKey.P1: "problem_frame",
                ProjectStageKey.P2: "research_report",
                ProjectStageKey.P5: "engineering_plan",
                ProjectStageKey.P6: "mvp_execution",
                ProjectStageKey.P7: "impact_feasibility_brief",
                ProjectStageKey.P8: "pitch_preparation_package",
            }[key]
            latest = c.execute("""SELECT * FROM project_artifacts WHERE project_id=?
                AND stage_key=? AND artifact_type=?
                ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""",
                (project_id, key.value, artifact_type)).fetchone()
            if latest is None:
                raise ValueError(f"{key.value} cannot be completed without an approved {artifact_type.replace('_', ' ')}.")
            if latest["status"] not in {ArtifactStatus.DRAFT.value, ArtifactStatus.READY.value}:
                raise ValueError(f"{key.value} cannot be completed from an archived or superseded artifact.")
            linked_decision = c.execute("SELECT stage_key FROM decisions WHERE project_id=? AND decision_id=?",
                                        (project_id, latest["decision_id"])).fetchone() if latest["decision_id"] else None
            if linked_decision is None or linked_decision["stage_key"] != key.value:
                raise ValueError(f"{key.value} artifact does not have a linked {key.value} approval decision.")
            if key == ProjectStageKey.P2:
                report_data = json.loads(latest["content"] or "{}")
                if report_data.get("outcome", {}).get("execution_status") != "complete":
                    raise ValueError("A partial research report cannot complete P2.")
                run = c.execute("SELECT project_id FROM research_runs WHERE id=?",
                                (report_data.get("run_id"),)).fetchone()
                if report_data.get("project_id") != project_id or run is None or run["project_id"] != project_id:
                    raise ValueError("P2 report run does not belong to the project.")
                workflow = c.execute("""SELECT 1 FROM workflow_runs
                    WHERE project_id=? AND stage_key='P2' AND status='completed' AND output_artifact_id=? LIMIT 1""",
                    (project_id, latest["artifact_id"])).fetchone()
                if workflow is None:
                    raise ValueError("P2 report is not the output of a successfully completed research workflow.")
            if key == ProjectStageKey.P5:
                p4 = c.execute("""SELECT artifact_id,version,content FROM project_artifacts
                    WHERE project_id=? AND stage_key='P4' AND artifact_type='solution_options'
                    ORDER BY version DESC,created_at DESC,artifact_id DESC LIMIT 1""", (project_id,)).fetchone()
                if p4 is None:
                    raise ValueError("P5 requires a selected P4 solution direction.")
                p4_data = json.loads(p4["content"] or "{}")
                if p4_data.get("action") != "select":
                    raise ValueError("P5 requires a selected P4 solution direction.")
                plan_data = json.loads(latest["content"] or "{}")
                if (plan_data.get("p4_artifact_id") != p4["artifact_id"]
                        or plan_data.get("p4_artifact_version") != p4["version"]
                        or plan_data.get("selected_option_id") != p4_data.get("selected_option_id")):
                    raise ValueError("P5 engineering plan does not match the latest selected P4 direction.")
            if key == ProjectStageKey.P6:
                self._validate_p6_completion_in_transaction(c, project_id, latest)
            if key == ProjectStageKey.P7:
                self._validate_p7_completion_in_transaction(c, project_id, latest)
            if key == ProjectStageKey.P8:
                self._validate_p8_completion_in_transaction(c, project_id, latest)
            artifact_event = self._latest_approval_event(c, project_id=project_id, artifact_id=latest["artifact_id"])
            decision_event = (self._latest_approval_event(c, project_id=project_id, decision_id=latest["decision_id"])
                              if latest["decision_id"] else None)
            if not artifact_event or artifact_event.status != ApprovalEventStatus.APPROVED:
                raise ValueError(f"{key.value} cannot be completed until the latest artifact is approved.")
            if not decision_event or decision_event.status != ApprovalEventStatus.APPROVED:
                raise ValueError(f"{key.value} cannot be completed until its linked decision is approved.")
            if artifact_event.status != decision_event.status:
                raise RuntimeError("P1 artifact and decision approval histories are inconsistent.")
            stage = self._transition_project_stage_in_transaction(c, project_id, key, ProjectStageStatus.COMPLETED,
                actor=actor, note=note, expected_from={ProjectStageStatus.IN_PROGRESS})
        return stage

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
            event = self._insert_approval_event(c, project_id=project_id, decision_id=decision_id,
                                                status=status, actor=actor.strip(), comment=comment)
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
            event = self._insert_approval_event(c, project_id=project_id, artifact_id=artifact_id,
                                                status=status, actor=actor.strip(), comment=comment)
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
