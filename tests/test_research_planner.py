import sqlite3
import tempfile
import unittest
from pathlib import Path
from src.research.models import CompetitionInput,PlanningDraft,QuestionDraft,QuestionPriority,ResearchPlan
from src.research.planner import generate_research_plan,review_plan
from src.research.storage import ResearchStore

def sample_draft():
    return PlanningDraft(problem_summary="Landslide warnings need reliable delivery.",domain="disaster risk",
        target_users=["district disaster managers"],geographic_context="Northeast India",
        extracted_constraints=["24-hour prototype"],inferred_assumptions=["local warning data can be accessed"],
        missing_information=["availability of local historical warning data"],
        questions=[
            QuestionDraft(text="How are warnings currently issued in the region?",priority="high",source_categories=["official","research_literature"],evidence_required="Documented warning workflows",decision_impact="Defines the baseline process"),
            QuestionDraft(text="Which operational delays affect warning delivery?",priority="high",source_categories=["research_literature","community"],evidence_required="Reported warning delays",decision_impact="Identifies the primary user problem"),
            QuestionDraft(text="What local data can a prototype access?",priority="high",source_categories=["official","technical_docs"],evidence_required="Accessible datasets and APIs",decision_impact="Determines technical feasibility"),
        ])

class PlannerTests(unittest.TestCase):
    def test_plan_preserves_user_inputs_and_distinguishes_inferences(self):
        competition=CompetitionInput(competition_name="Demo Hack",judging_criteria=["impact"],team_size=3,time_limit_hours=24)
        plan=generate_research_plan("Landslide warnings in Northeast India.",competition,draft_provider=lambda prompt,comp: sample_draft())
        self.assertEqual(plan.brief.user_provided.competition_name,"Demo Hack")
        self.assertEqual(plan.brief.user_provided.judging_criteria,["impact"])
        self.assertEqual(plan.brief.extracted.inferred_assumptions,["local warning data can be accessed"])
        self.assertEqual(len(plan.questions),3)
        self.assertEqual(plan.status,"draft")

    def test_duplicate_questions_are_removed_but_minimum_is_enforced(self):
        draft=sample_draft()
        draft.questions[1]=draft.questions[0].model_copy(update={"text":draft.questions[0].text})
        with self.assertRaisesRegex(ValueError,"fewer than three distinct"):
            generate_research_plan("Prompt",draft_provider=lambda prompt,comp:draft)

    def test_review_can_edit_and_approve(self):
        plan=generate_research_plan("Prompt",draft_provider=lambda prompt,comp:sample_draft())
        answers=iter(["e","1","How quickly do local agencies distribute warnings?","","a"])
        reviewed=review_plan(plan,input_fn=lambda _:next(answers),output_fn=lambda _:None)
        self.assertEqual(reviewed.status,"approved")
        self.assertIsNotNone(reviewed.approved_at)
        self.assertEqual(reviewed.questions[0].text,"How quickly do local agencies distribute warnings?")

    def test_rejection_does_not_set_approval_time(self):
        plan=generate_research_plan("Prompt",draft_provider=lambda prompt,comp:sample_draft())
        reviewed=review_plan(plan,input_fn=lambda _:"r",output_fn=lambda _:None)
        self.assertEqual(reviewed.status,"rejected")
        self.assertIsNone(reviewed.approved_at)

class PlannerStorageTests(unittest.TestCase):
    def test_plan_and_approval_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            store=ResearchStore(Path(temp)/"research.sqlite")
            plan=generate_research_plan("Prompt",draft_provider=lambda prompt,comp:sample_draft())
            store.save_plan(plan)
            edited=plan.model_copy(update={"status":"approved"})
            store.update_plan(edited)
            loaded=store.get_plan(plan.id)
            self.assertEqual(loaded.status,"approved")
            self.assertEqual(len(loaded.questions),3)
            records=store.get_run_records(plan.run_id)
            self.assertEqual(records[1][0].source_categories,["official","research_literature"])

    def test_migrates_existing_milestone2_question_table(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"old.sqlite"
            conn=sqlite3.connect(path)
            conn.executescript("CREATE TABLE research_runs(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,created_at TEXT NOT NULL); CREATE TABLE research_questions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,text TEXT NOT NULL);")
            conn.close()
            store=ResearchStore(path)
            with store.db() as conn:
                names={row["name"] for row in conn.execute("PRAGMA table_info(research_questions)")}
            self.assertIn("priority",names)
            self.assertIn("source_categories_json",names)
            self.assertIn("status",names)

if __name__=="__main__":
    unittest.main()
