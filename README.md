---
title: VentureForge - Hackathon Research Workflow
emoji: 🚀
colorFrom: blue
colorTo: purple
sdk: gradio
sdk_version: 6.14.0
app_file: app.py
python_version: '3.13'
pinned: false
tags:
  - amd-hackathon-2026
  - ai-agents
  - startup-ideas
  - langgraph
short_description: Human-led hackathon research, validation, MVP, and pitch workflow
---

# VentureForge

VentureForge is a Gradio workspace for taking a hackathon challenge from its official brief through problem research, validation, an MVP plan, and judging preparation. People review and approve the work at each stage; desk research and AI-generated drafts do not automatically validate a problem.

## Workflow

1. **Challenge Setup:** Capture the official prompt, rules, judging criteria, team capacity, deadline, and submission requirements. Approve the brief before starting P1.
2. **P1 Problem Framing:** Describe affected users, current behavior, reported pain, known evidence, assumptions, and unknowns.
3. **P2 Research:** Approve a question-led plan, collect secondary sources, review evidence and gaps, and approve the resulting report.
4. **P3 Problem Validation:** Make a human decision to proceed, gather more evidence, reframe, or stop. Structured records support interviews, observations, and surveys.
5. **P4 Solution Intelligence:** Compare options against evidence, event criteria, constraints, team skills, and MVP scope; select a direction or return to earlier work.
6. **P5 Solution Engineering:** Turn the selected direction into an implementation and demo plan.
7. **P6 MVP Execution & Testing:** Track implementation tasks and record test outcomes and demo evidence.
8. **P7 Business, Impact & Risk:** Explain expected impact, feasibility, risks, and evidence limits.
9. **P8 Pitch & Judge Preparation:** Assemble the pitch, demo narrative, and submission checklist.

Artifacts are project-scoped and versioned. Approvals apply to a specific artifact version. Revising upstream work can require downstream review again. The separate LangGraph coordinator pauses for human work and can be started or resumed from Challenge Setup.

## Run locally

Create and activate a virtual environment, install the pinned project requirements, then run `app.py`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

The app starts at `http://127.0.0.1:7860` by default. Set `GRADIO_HOST` or `GRADIO_PORT` to change the bind address or port.

P2 research uses Hacker News and can also use Tavily when `TAVILY_API_KEY` is configured. AI-assisted P1 framing, P2 planning, and optional startup discovery require an OpenAI-compatible LLM configuration (`LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL`). The workflow can be used without AI assistance where those controls are optional.

## Database and workflow recovery

The Hackathon UI, research CLI, and research services use `data/ventureforge.sqlite` by default, resolved from the repository root. Set `VENTUREFORGE_DATABASE` to use another path. The legacy `VENTUREFORGE_PROJECT_DB` variable is accepted, and CLI commands accept `--database`. Existing records in `data/research.sqlite` are not imported or combined automatically.

The Hackathon coordinator is a separate graph from the startup-discovery graph. LangGraph interrupt checkpoints are held in memory for the current process. A metadata-only snapshot at `.cache/hackathon_workflow_checkpoints.json` supports reconstruction after restart; project artifacts and stage history in SQLite remain authoritative. Set `VENTUREFORGE_HACKATHON_CHECKPOINTS` to change the snapshot path.

## Optional startup discovery

The bottom section of the app retains the original startup idea discovery pipeline as a separate tool. It mines pain points, generates and scores startup ideas, and prepares pitch briefs. It is not part of the Challenge Setup and P1-P8 LangGraph workflow. Its LangGraph checkpoints are process-local.

## Tests

Run the repository test suite with:

```powershell
python -m unittest discover -s tests -v
```

The tests use temporary databases and deterministic fixtures for research integrations.
