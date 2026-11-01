import tempfile
import unittest
from pathlib import Path

from src.research.budget import SearchBudget
from src.research.models import CandidateExtraction, CompetitionInput, IntakeExtraction, ProblemBrief, ResearchPlan, ResearchQuestion, ResearchRun, SourceSnapshot, SupportRelation
from src.research.pipeline import run_gap_research
from src.research.pitch import DemoStep, JudgeQuestion, PitchPackageDraft, PresentationSlide, RubricMapping, generate_pitch_package, render_pitch_markdown
from src.research.search import SearchOrchestrator
from src.research.solution import BuildTask, SolutionDesignDraft, SolutionOption, SolutionScores, generate_solution_design
from src.research.storage import ResearchStore


class EndToEndResearchTests(unittest.TestCase):
    def test_research_to_solution_and_pitch_persistence(self):
        run=ResearchRun(prompt="Improve landslide warnings")
        plan=ResearchPlan(run_id=run.id,status="approved",brief=ProblemBrief(problem_statement=run.prompt,
            user_provided=CompetitionInput(judging_criteria=["Problem understanding","Technical feasibility"],team_size=3,time_limit_hours=24),
            extracted=IntakeExtraction(problem_summary="Improve warnings",domain="disaster response")),
            questions=[ResearchQuestion(run_id=run.id,text=f"Question {i}?",source_categories=["official"]) for i in range(3)])
        class Connector:
            name="tavily"; cost_per_search=1
            def search(self,*,run_id,question_id,query,category,max_results):
                return [SourceSnapshot(run_id=run_id,question_id=question_id,url=f"https://source{i}.example.org/{question_id}",title=f"Source {i}",content="Warning delivery can fail during monsoon outages.",source_categories=[category]) for i in (1,2)]
        with tempfile.TemporaryDirectory() as temp:
            store=ResearchStore(Path(temp)/"research.sqlite"); store.save_plan(plan)
            outcome=run_gap_research(plan,store,budget=SearchBudget(max_search_calls=8,max_tavily_credits=8),
                orchestrator=SearchOrchestrator(Connector(),None,fetcher=object()),
                extractor=lambda q,s: CandidateExtraction(relevant=True,claim="Warning delivery fails.",passage="Warning delivery can fail during monsoon outages.",relation=SupportRelation.SUPPORTS))
            self.assertTrue(outcome.gap_report.complete, repr(outcome.gap_report))
            _,questions,sources,evidence=store.get_run_records(run.id)
            self.assertEqual((len(sources),len(evidence)),(6,6))
            self.assertTrue(all(q.status=="covered" for q in questions))
            def solution_provider(context,passed_plan):
                options=[SolutionOption(name=f"Option {i}",concept=f"Distinct solution concept {i} for the target users.",target_user="warning officers",differentiator=f"Option {i} has a distinct differentiator.",implementation_approach=f"Build option {i} as a small demoable vertical slice.",evidence_ids=[evidence[0].id],scores=SolutionScores(problem_fit=5 if i==1 else 3,impact=4,feasibility=4,demoability=5,differentiation=i)) for i in (1,2,3)]
                tasks=[BuildTask(id="t1",title="Build intake",description="Create and persist a warning intake form.",priority="must",estimated_person_hours=8,acceptance_criteria=["Warning is saved"]),BuildTask(id="t2",title="Build demo",description="Show a saved warning in the operator view.",priority="must",estimated_person_hours=5,depends_on=["task_1"],acceptance_criteria=["Warning is displayed"]),BuildTask(id="t3",title="Prepare fallback",description="Capture a fallback screenshot for the presentation.",priority="should",estimated_person_hours=2,depends_on=["Build demo"],acceptance_criteria=["Screenshot is available"])]
                return SolutionDesignDraft(alternatives=options,technical_architecture="A web client calls a Python API that stores warning records in SQLite.",mvp_tasks=tasks,deferred_scope=["Sensor integrations"],key_risks=["No live sensor feed"],demo_plan="Enter a warning, save it, and show it to the operator.")
            design=generate_solution_design(plan,store,draft_provider=solution_provider)
            self.assertEqual(design.recommended_option_id,design.alternatives[0].id)
            self.assertEqual(design.mvp_tasks[1].depends_on,[design.mvp_tasks[0].id])
            store.save_solution_design(design); design=store.get_solution_design(design.id)
            def pitch_provider(context,criteria):
                refs=[evidence[0].id]
                return PitchPackageDraft(pitch_summary="A concise evidence-based prototype pitch with explicit limitations.",rubric_mapping=[RubricMapping(criterion=c,pitch_claim=f"We address {c} with a focused prototype.",proof_points=["A warning record is saved."],evidence_ids=refs,demo_moment="Show the saved warning.",confidence="partial",gap="Test with operators.") for c in criteria],judge_questions=[JudgeQuestion(question=f"How will you validate question {i} with users?",what_judges_are_testing="Validation",suggested_answer="Test with warning operators.",evidence_ids=refs) for i in range(5)],presentation_outline=[PresentationSlide(title=f"Slide {i}",key_message="A focused warning workflow.",talking_points=["Show the prototype."],evidence_ids=refs) for i in range(5)],demo_script=[DemoStep(elapsed_seconds=0,duration_seconds=15,narrator="Introduce the workflow.",action="Open the warning screen.",expected_result="The form is visible.",fallback="Use a screenshot."),DemoStep(elapsed_seconds=15,duration_seconds=20,narrator="Show a saved warning.",action="Submit a sample warning.",expected_result="The warning appears.",fallback="Use a preloaded warning."),DemoStep(elapsed_seconds=35,duration_seconds=15,narrator="Close with impact.",action="Show the result summary.",expected_result="The key benefit is visible.",fallback="Use the summary slide.")])
            package=generate_pitch_package(plan,design,store,draft_provider=pitch_provider)
            store.save_pitch_package(package); package=store.get_pitch_package(package.id)
            report=render_pitch_markdown(package,evidence,sources)
            self.assertIn("Judging rubric mapping",report)
            self.assertIn("https://source1.example.org",report)
            self.assertIn("fallback",report.lower())


if __name__ == "__main__":
    unittest.main()
