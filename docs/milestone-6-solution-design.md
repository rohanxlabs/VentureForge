# Milestone 6: solution and execution design

Generate a draft from an approved plan:

```powershell
.venv\Scripts\python.exe -m src.research_cli design <approved-plan-id>
```

The generator compares exactly three alternatives, scores them on problem fit, feasibility, demoability, impact, and differentiation, and selects the highest weighted score. The weights are deterministic: 30%, 25%, 20%, 15%, and 10%, respectively. It then drafts the technical architecture, MVP tasks with estimates and acceptance criteria, deferred scope, risks, and a demo plan. Evidence references must match verified evidence IDs from the research run. The design is stored as a draft and exported to `reports/<run-id>/solution-design.md`.

Scores are model estimates for comparison, not empirical results. Evidence can be sparse or uncertain. Person-hour totals are compared with supplied team capacity when both event length and team size are available; over-capacity plans produce a warning. Review the alternatives, assumptions, task dependencies, and estimates with the team before committing.
