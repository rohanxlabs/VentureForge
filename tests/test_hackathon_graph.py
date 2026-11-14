import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.hackathon_graph as hackathon_graph
from src.hackathon_intake import (
    ChallengeBrief,
    approve_challenge_brief,
    save_challenge_brief,
)
from src.problem_intelligence import ProblemInput, approve_frame, create_frame, save_frame
from src.project_store import ProjectStore
from src.research.models import ApprovalEventStatus, ProjectCreate, ProjectStageKey


class HackathonGraphTests(unittest.TestCase):
    def test_workflow_pauses_recovers_and_advances_from_project_stage_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "project.sqlite"
            checkpoint_file = root / "workflow.json"
            store = ProjectStore(database)
            project = store.create_project(ProjectCreate(name="Graph coordinator test"))

            original_graph = hackathon_graph.HACKATHON_GRAPH
            try:
                with patch.dict("os.environ", {"VENTUREFORGE_DATABASE": str(database)}), \
                        patch.object(hackathon_graph, "_CHECKPOINT_PATH", checkpoint_file):
                    state = hackathon_graph.start_or_resume_hackathon_workflow(project.project_id)
                    self.assertEqual(state["current_stage"], "Challenge Setup")
                    self.assertEqual(state["workflow_status"], "waiting_for_human")
                    self.assertEqual(json.loads(checkpoint_file.read_text())[project.project_id]["current_stage"],
                                     "Challenge Setup")

                    # A fresh graph instance simulates process restart; recover from the JSON snapshot.
                    hackathon_graph.HACKATHON_GRAPH = hackathon_graph.build_hackathon_graph()
                    recovered = hackathon_graph.start_or_resume_hackathon_workflow(project.project_id)
                    self.assertEqual(recovered["current_stage"], "Challenge Setup")

                    brief, decision = save_challenge_brief(store, project.project_id, ChallengeBrief(
                        event_name="Workflow test", challenge_title="Test challenge",
                        challenge_type="assigned", official_prompt="Help teams validate a challenge workflow."))
                    approve_challenge_brief(store, project.project_id, brief.artifact_id,
                        decision.decision_id, ApprovalEventStatus.APPROVED)
                    after_setup = hackathon_graph.start_or_resume_hackathon_workflow(project.project_id)
                    self.assertEqual(after_setup["current_stage"], ProjectStageKey.P1.value)

                    frame, frame_decision = save_frame(store, project.project_id,
                        create_frame(ProblemInput(title="Warning delays",
                            description="Residents receive local warnings too late.",
                            primary_users=["Residents in hazard-prone areas"])))
                    approve_frame(store, project.project_id, frame.artifact_id,
                        frame_decision.decision_id, ApprovalEventStatus.APPROVED)
                    after_p1 = hackathon_graph.start_or_resume_hackathon_workflow(project.project_id)
                    self.assertEqual(after_p1["current_stage"], ProjectStageKey.P2.value)
            finally:
                hackathon_graph.HACKATHON_GRAPH = original_graph


if __name__ == "__main__":
    unittest.main()
