# Milestone 7: judge and pitch package

Generate a draft package from an approved plan and saved solution design:

```powershell
.venv\Scripts\python.exe -m src.research_cli pitch <approved-plan-id>
```

By default, the command uses the latest saved solution design for that run. Select a saved design explicitly with `--design-id`. The output maps each official judging criterion (or labels generic criteria when none were supplied), prepares judge questions and suggested answers, creates a presentation outline, and writes a timed demo script with failure fallbacks. References are restricted to exact verified evidence IDs and rendered with source links. The package is stored as a draft and exported to `reports/<run-id>/pitch-package.md`.

The existing `src/agents/pitch_writer.py` and `src/agents/critic.py` remain available for the legacy startup-discovery pipeline. Their investor-oriented rubric and `VentureForgeState` data model do not match competition judging criteria or the evidence-first research records, so this feature uses separate schemas while following the existing structured-LLM client pattern.

Review every claim, suggested answer, demo action, and fallback before presenting. If official competition criteria were not supplied, the package says so and uses generic criteria rather than implying official alignment.
