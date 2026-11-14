import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.problem_intelligence import ProblemInput, approve_frame, create_frame, save_frame
import src.project_store as project_store_module
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ArtifactCreate, ProjectCreate, ProjectStageKey


class ArtifactDependencyTests(unittest.TestCase):
    def _artifact(self, store, project_id, stage, kind, content):
        return store.create_artifact(ArtifactCreate(project_id=project_id, stage_key=stage,
            artifact_type=kind, title=kind, content=json.dumps(content)))

    def _seed_dependency_chain(self, store, project_id):
        frame = self._artifact(store, project_id, ProjectStageKey.P1, "problem_frame", {})
        plan = self._artifact(store, project_id, ProjectStageKey.P2, "research_plan", {
            "problem_frame_artifact_id": frame.artifact_id, "problem_frame_version": frame.version})
        report = self._artifact(store, project_id, ProjectStageKey.P2, "research_report", {
            "plan_artifact_id": plan.artifact_id, "plan_artifact_version": plan.version})
        primary = self._artifact(store, project_id, ProjectStageKey.P3, "primary_research_record", {})
        unrelated_primary = self._artifact(store, project_id, ProjectStageKey.P3,
            "primary_research_record", {"note": "Not cited by the validation decision"})
        validation = self._artifact(store, project_id, ProjectStageKey.P3, "validation_decision", {
            "problem_frame_artifact_id": frame.artifact_id, "problem_frame_version": frame.version,
            "research_report_artifact_id": report.artifact_id, "research_report_version": report.version,
            "primary_research_artifact_ids": [primary.artifact_id]})
        unrelated_validation = self._artifact(store, project_id, ProjectStageKey.P3,
            "validation_decision", {"primary_research_artifact_ids": []})
        solution = self._artifact(store, project_id, ProjectStageKey.P4, "solution_options", {
            "problem_frame_artifact_id": frame.artifact_id, "problem_frame_version": frame.version,
            "research_report_artifact_id": report.artifact_id, "research_report_version": report.version,
            "validation_decision_artifact_id": validation.artifact_id,
            "validation_decision_version": validation.version})
        engineering = self._artifact(store, project_id, ProjectStageKey.P5, "engineering_plan", {
            "p4_artifact_id": solution.artifact_id, "p4_artifact_version": solution.version,
            "p1_artifact_id": frame.artifact_id, "p1_artifact_version": frame.version,
            "p2_artifact_id": report.artifact_id, "p2_artifact_version": report.version,
            "p3_artifact_id": validation.artifact_id, "p3_artifact_version": validation.version})
        execution = self._artifact(store, project_id, ProjectStageKey.P6, "mvp_execution", {
            "p5_artifact_id": engineering.artifact_id, "p5_artifact_version": engineering.version})
        impact = self._artifact(store, project_id, ProjectStageKey.P7, "impact_feasibility_brief", {
            "p1_artifact_id": frame.artifact_id, "p1_artifact_version": frame.version,
            "p2_artifact_id": report.artifact_id, "p2_artifact_version": report.version,
            "p3_artifact_id": validation.artifact_id, "p3_artifact_version": validation.version,
            "p4_artifact_id": solution.artifact_id, "p4_artifact_version": solution.version,
            "p5_artifact_id": engineering.artifact_id, "p5_artifact_version": engineering.version,
            "p6_artifact_id": execution.artifact_id, "p6_artifact_version": execution.version})
        pitch = self._artifact(store, project_id, ProjectStageKey.P8, "pitch_preparation_package", {
            "p1_artifact_id": frame.artifact_id, "p1_artifact_version": frame.version,
            "p2_artifact_id": report.artifact_id, "p2_artifact_version": report.version,
            "p3_artifact_id": validation.artifact_id, "p3_artifact_version": validation.version,
            "p4_artifact_id": solution.artifact_id, "p4_artifact_version": solution.version,
            "p5_artifact_id": engineering.artifact_id, "p5_artifact_version": engineering.version,
            "p6_artifact_id": execution.artifact_id, "p6_artifact_version": execution.version,
            "p7_artifact_id": impact.artifact_id, "p7_artifact_version": impact.version})
        artifacts = {"P1": frame, "P2_plan": plan, "P2": report, "P3_primary": primary,
            "P3_unrelated_primary": unrelated_primary, "P3": validation,
            "P3_unrelated_validation": unrelated_validation,
            "P4": solution, "P5": engineering, "P6": execution,
            "P7": impact, "P8": pitch}
        for artifact in artifacts.values():
            store.record_artifact_approval(project_id, artifact.artifact_id,
                ApprovalEventStatus.APPROVED, actor="reviewer", comment="Initial approval")
        with store.db() as connection:
            connection.execute("UPDATE project_stages SET status='completed',started_at='2026-01-01',completed_at='2026-01-01' WHERE project_id=?",
                               (project_id,))
        return artifacts

    def test_upstream_change_does_not_invalidate_another_project(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory) / "isolation.sqlite")
            source = store.create_project(ProjectCreate(name="Source project"))
            other = store.create_project(ProjectCreate(name="Other project"))
            frame, decision = save_frame(store, source.project_id, create_frame(ProblemInput(
                title="A framed problem", description="A specific user group has a measurable problem.",
                primary_users=["Affected users"])))
            approve_frame(store, source.project_id, frame.artifact_id, decision.decision_id,
                          ApprovalEventStatus.APPROVED)
            dependent = store.create_artifact(ArtifactCreate(project_id=other.project_id,
                stage_key=ProjectStageKey.P2, artifact_type="research_plan", title="Foreign-reference plan",
                content=json.dumps({"problem_frame_artifact_id": frame.artifact_id,
                                    "problem_frame_version": frame.version})))

            revised, revised_decision = save_frame(store, source.project_id, create_frame(ProblemInput(
                title="A revised problem", description="The problem frame has changed for this project.",
                primary_users=["Affected users"])))
            approve_frame(store, source.project_id, revised.artifact_id, revised_decision.decision_id,
                          ApprovalEventStatus.APPROVED)

            self.assertEqual(store.get_artifact(other.project_id, dependent.artifact_id).status.value, "draft")
            self.assertEqual(store.get_artifact_approval_status(other.project_id, dependent.artifact_id), "pending")
            self.assertEqual(store.get_project_stage(other.project_id, ProjectStageKey.P2).status.value, "pending")
            self.assertEqual(store.get_project_stage_history(other.project_id), [])

    def test_revisions_from_p2_through_p7_invalidate_only_transitive_dependants(self):
        source_by_stage = {"P2": ("P2", "research_report"), "P3": ("P3", "validation_decision"),
            "P4": ("P4", "solution_options"), "P5": ("P5", "engineering_plan"),
            "P6": ("P6", "mvp_execution"), "P7": ("P7", "impact_feasibility_brief")}
        with tempfile.TemporaryDirectory() as directory:
            for source_stage, (artifact_key, artifact_type) in source_by_stage.items():
                with self.subTest(source_stage=source_stage):
                    store = ProjectStore(Path(directory) / f"{source_stage}.sqlite")
                    project = store.create_project(ProjectCreate(name=f"Revision from {source_stage}"))
                    artifacts = self._seed_dependency_chain(store, project.project_id)
                    source = artifacts[artifact_key]
                    revised = store.create_artifact_version(project.project_id, source.artifact_id,
                        ArtifactCreate(project_id=project.project_id,
                            stage_key=ProjectStageKey(source_stage), artifact_type=artifact_type,
                            title=f"Revised {artifact_type}", content=source.content))
                    expected_stages = [f"P{i}" for i in range(int(source_stage[1:]) + 1, 9)]
                    expected_ids = {artifacts[key].artifact_id for key in expected_stages}
                    self.assertTrue(expected_ids)
                    for stage in expected_stages:
                        dependent = artifacts[stage]
                        self.assertEqual(store.get_artifact_approval_status(project.project_id,
                            dependent.artifact_id), "changes_requested")
                        history = store.get_artifact_approval_history(project.project_id, dependent.artifact_id)
                        self.assertEqual([event.status.value for event in history], ["approved", "changes_requested"])
                        self.assertEqual(store.get_project_stage(project.project_id, stage).status.value,
                            "in_progress")
                        self.assertTrue(any(item.to_status.value == "in_progress" for item in
                            store.get_project_stage_history(project.project_id, stage)))
                        self.assertIn(dependent.artifact_id,
                            {item.artifact_id for item in store.list_project_artifacts(project.project_id)})
                    for key, unrelated in artifacts.items():
                        if key == artifact_key or key in expected_stages:
                            continue
                        self.assertEqual(store.get_artifact_approval_status(project.project_id,
                            unrelated.artifact_id), "approved")
                    self.assertEqual(store.get_artifact(project.project_id, source.artifact_id).status.value,
                        "superseded")
                    self.assertIn(source.artifact_id,
                        {item.artifact_id for item in store.list_project_artifacts(project.project_id)})
                    self.assertEqual(store.get_artifact(project.project_id, revised.artifact_id).version,
                        source.version + 1)

    def test_primary_record_revision_invalidates_its_p3_decision_and_downstream_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory) / "primary-record.sqlite")
            project = store.create_project(ProjectCreate(name="Primary record dependency"))
            artifacts = self._seed_dependency_chain(store, project.project_id)
            record = artifacts["P3_primary"]
            revised = store.create_artifact_version(project.project_id, record.artifact_id,
                ArtifactCreate(project_id=project.project_id, stage_key=ProjectStageKey.P3,
                    artifact_type="primary_research_record", title="Revised interview record", content="{}"))
            for stage in ("P3", "P4", "P5", "P6", "P7", "P8"):
                artifact = artifacts[stage]
                self.assertEqual(store.get_artifact_approval_status(project.project_id,
                    artifact.artifact_id), "changes_requested")
                self.assertEqual(store.get_project_stage(project.project_id, stage).status.value,
                    "in_progress")
            self.assertEqual(store.get_artifact_approval_status(project.project_id,
                artifacts["P3_primary"].artifact_id), "approved")
            self.assertEqual(store.get_artifact_approval_status(project.project_id,
                artifacts["P3_unrelated_primary"].artifact_id), "approved")
            self.assertEqual(store.get_artifact_approval_status(project.project_id,
                artifacts["P3_unrelated_validation"].artifact_id), "approved")
            self.assertEqual(store.get_artifact_approval_status(project.project_id,
                artifacts["P2"].artifact_id), "approved")
            self.assertIn(record.artifact_id,
                {item.artifact_id for item in store.list_project_artifacts(project.project_id)})
            self.assertIn(revised.artifact_id,
                {item.artifact_id for item in store.list_project_artifacts(project.project_id)})

    def test_dependency_walk_terminates_for_a_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory) / "cycle.sqlite")
            project = store.create_project(ProjectCreate(name="Dependency cycle"))
            item_a = self._artifact(store, project.project_id, ProjectStageKey.P5, "cycle_a", {})
            item_b = self._artifact(store, project.project_id, ProjectStageKey.P6, "cycle_b", {})
            with store.db() as connection:
                connection.execute("UPDATE project_stages SET status='completed',started_at='2026-01-01',completed_at='2026-01-01' WHERE project_id=?",
                                   (project.project_id,))
            with store.db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                with patch.object(project_store_module, "artifact_dependencies", side_effect=lambda kind, _content: {
                    "cycle_a": [("root", "root-id", 1), ("cycle_b", item_b.artifact_id, 1)],
                    "cycle_b": [("cycle_a", item_a.artifact_id, 1)],
                }.get(kind, [])):
                    invalidated = store._invalidate_downstream_artifacts_in_transaction(connection,
                        project.project_id, "root", "root-id", 1)
            self.assertEqual(set(invalidated), {item_a.artifact_id, item_b.artifact_id})


if __name__ == "__main__":
    unittest.main()
