import json
import tempfile
import unittest
from pathlib import Path

from src.problem_intelligence import ProblemInput, approve_frame, create_frame, save_frame
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ArtifactCreate, ProjectCreate, ProjectStageKey


class ArtifactDependencyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
