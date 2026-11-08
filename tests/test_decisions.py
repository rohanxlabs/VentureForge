import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.project_store import ProjectStore
from src.research.models import (
    ApprovalEventStatus, DecisionCreate, DecisionEvidenceRelationship,
    DecisionOptionCreate, DecisionUpdate, ProjectCreate, ProjectStageKey,
)


class DecisionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.temp.name) / "decisions.sqlite")
        self.project = self.store.create_project(ProjectCreate(name="First project"))
        self.other = self.store.create_project(ProjectCreate(name="Second project"))

    def tearDown(self):
        self.temp.cleanup()

    def test_create_retrieve_update_and_project_isolation(self):
        decision = self.store.create_decision(DecisionCreate(
            project_id=self.project.project_id, stage_key=ProjectStageKey.P1,
            title="Choose architecture", decision_type="architecture:storage", description="Compare options"))
        self.assertEqual(self.store.get_decision(self.project.project_id, decision.decision_id), decision)
        self.assertEqual(self.store.list_project_decisions(self.project.project_id), [decision])
        self.assertEqual(self.store.list_project_decisions(self.other.project_id), [])
        updated = self.store.update_decision(self.project.project_id, decision.decision_id,
                                             DecisionUpdate(description="Updated scope"))
        self.assertEqual(updated.description, "Updated scope")
        with self.assertRaisesRegex(KeyError, "does not belong"):
            self.store.get_decision(self.other.project_id, decision.decision_id)
        with self.store.db() as connection:
            connection.execute("DELETE FROM project_stages WHERE project_id=? AND stage_key='P1'",
                               (self.other.project_id,))
        with self.assertRaisesRegex(KeyError, "Stage P1 does not belong"):
            self.store.create_decision(DecisionCreate(project_id=self.other.project_id,
                stage_key=ProjectStageKey.P1, title="bad stage"))

    def test_options_allow_multiple_and_separate_recommendation_from_selection(self):
        decision = self.store.create_decision(DecisionCreate(project_id=self.project.project_id, title="Choose an approach"))
        first = self.store.add_decision_option(self.project.project_id, decision.decision_id,
            DecisionOptionCreate(title="Edge sensors", advantages=["Local data"], estimated_cost=None))
        second = self.store.add_decision_option(self.project.project_id, decision.decision_id,
            DecisionOptionCreate(title="Existing PLC telemetry", disadvantages=["Uneven coverage"], feasibility="Moderate"))
        self.store.recommend_decision_option(self.project.project_id, decision.decision_id, first.option_id)
        selected = self.store.select_decision_option(self.project.project_id, decision.decision_id, second.option_id)
        options = self.store.list_decision_options(self.project.project_id, decision.decision_id)
        self.assertEqual(len(options), 2)
        self.assertIsNone(first.estimated_cost)
        self.assertTrue(next(o for o in options if o.option_id == first.option_id).is_recommended)
        self.assertFalse(next(o for o in options if o.option_id == first.option_id).is_selected)
        self.assertTrue(selected.is_selected)
        with self.assertRaisesRegex(KeyError, "does not belong"):
            self.store.select_decision_option(self.project.project_id, decision.decision_id, "foreign-option")

    def test_no_approval_means_pending_and_latest_event_defines_current_state(self):
        decision = self.store.create_decision(DecisionCreate(project_id=self.project.project_id, title="Approve design"))
        self.assertEqual(self.store.get_decision_approval_status(self.project.project_id, decision.decision_id), "pending")
        events = [
            self.store.record_decision_approval(self.project.project_id, decision.decision_id,
                ApprovalEventStatus.APPROVED, actor="reviewer", comment="Looks sound"),
            self.store.record_decision_approval(self.project.project_id, decision.decision_id,
                ApprovalEventStatus.REJECTED, comment="Reconsider this"),
            self.store.record_decision_approval(self.project.project_id, decision.decision_id,
                ApprovalEventStatus.APPROVED, comment="Reconsidered"),
            self.store.record_decision_approval(self.project.project_id, decision.decision_id,
                ApprovalEventStatus.REVOKED, comment="New information"),
        ]
        self.assertEqual(self.store.get_decision_approval_status(self.project.project_id, decision.decision_id), "revoked")
        self.assertEqual(self.store.get_decision_approval_history(self.project.project_id, decision.decision_id), events)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as connection:
                connection.execute("UPDATE approval_events SET comment='erased' WHERE approval_id=?",(events[0].approval_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as connection:
                connection.execute("DELETE FROM approval_events WHERE approval_id=?",(events[0].approval_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db() as connection:
                connection.execute("""INSERT OR REPLACE INTO approval_events
                    (approval_id,event_sequence,project_id,decision_id,status,actor,comment,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (events[0].approval_id,events[0].event_sequence,self.project.project_id,decision.decision_id,
                     "rejected","replacement","must not replace",events[0].created_at.isoformat()))
        with self.assertRaisesRegex(ValueError, "cannot be changed after an approval event"):
            self.store.update_decision(self.project.project_id, decision.decision_id, DecisionUpdate(title="Changed"))
        with self.assertRaisesRegex(ValueError, "cannot be changed after an approval event"):
            self.store.add_decision_option(self.project.project_id, decision.decision_id, DecisionOptionCreate(title="Late option"))

    def test_option_selection_rolls_back_as_one_transaction(self):
        decision = self.store.create_decision(DecisionCreate(project_id=self.project.project_id, title="Select option"))
        first = self.store.add_decision_option(self.project.project_id, decision.decision_id, DecisionOptionCreate(title="A"))
        second = self.store.add_decision_option(self.project.project_id, decision.decision_id, DecisionOptionCreate(title="B"))
        self.store.select_decision_option(self.project.project_id, decision.decision_id, first.option_id)
        with self.store.db() as connection:
            connection.execute(f"""CREATE TRIGGER reject_option_b BEFORE UPDATE OF is_selected ON decision_options
                WHEN NEW.option_id = '{second.option_id}' AND NEW.is_selected=1
                BEGIN SELECT RAISE(ABORT,'injected option failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.select_decision_option(self.project.project_id, decision.decision_id, second.option_id)
        options = self.store.list_decision_options(self.project.project_id, decision.decision_id)
        self.assertTrue(next(option for option in options if option.option_id == first.option_id).is_selected)
        self.assertFalse(next(option for option in options if option.option_id == second.option_id).is_selected)

    def test_standalone_legacy_evidence_cannot_be_linked_to_project_decision(self):
        from src.research.models import ResearchQuestion, ResearchRun, SourceSnapshot
        decision = self.store.create_decision(DecisionCreate(project_id=self.project.project_id, title="Evidence check"))
        run = ResearchRun(prompt="unowned run")
        self.store.save_run(run)
        question = ResearchQuestion(run_id=run.id, text="question")
        self.store.save_question(question)
        source = SourceSnapshot(run_id=run.id, question_id=question.id, url="https://standalone.test", content="passage")
        self.store.save_source(source)
        with self.store.db() as connection:
            connection.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("legacy-evidence",run.id,question.id,source.id,"claim","passage","supports","verified","reason","now"))
        with self.assertRaisesRegex(ValueError, "same project"):
            self.store.link_decision_evidence(self.project.project_id, decision.decision_id,
                "legacy-evidence", DecisionEvidenceRelationship.SUPPORTS)


if __name__ == "__main__":
    unittest.main()
