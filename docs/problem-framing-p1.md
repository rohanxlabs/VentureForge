# P1 Problem Framing

P1 turns a project's supplied problem details into a reviewable Problem Frame. It records affected users, current process, pain points, possible causes, alternatives, impacts, boundaries, unknowns, and draft questions for later investigation. P1 does not execute research or validate the real-world problem.

## Input

Create a UTF-8 JSON file with a required `title` and `description` (at least 10 characters). Optional fields include `domain`, `environment`, `geography`, `hackathon_theme`, `additional_context`, `primary_users`, `secondary_users`, `stakeholders`, `buyer_or_decision_maker`, `current_process`, `where_it_occurs`, `when_it_occurs`, `frequency`, `existing_tools`, `workarounds`, `pain_points`, `root_cause_hypotheses`, `existing_alternatives`, `alternative_limitations`, `measured_impacts`, `qualitative_consequences`, `constraints`, `assumptions`, and `unknowns`. Lists contain non-empty strings. Only supplied impact measurements are recorded as measured.

Example:

```json
{
  "title": "Delayed landslide warnings",
  "description": "Residents in landslide-prone communities may receive warnings too late to evacuate safely.",
  "domain": "disaster response",
  "geography": "Northeast India",
  "primary_users": ["residents in landslide-prone communities"],
  "current_process": ["Residents receive warnings through channels available in their community"],
  "unknowns": ["Warning receipt time has not been measured", "The most reliable local channels are not known"]
}
```

## Workflow

Use the existing CLI (`.venv\Scripts\python.exe -m src.research_cli`) to find an existing project, then:

```text
.venv\Scripts\python.exe -m src.research_cli project list
.venv\Scripts\python.exe -m src.research_cli project problem start <project-id>
.venv\Scripts\python.exe -m src.research_cli project problem frame <project-id> problem.json
.venv\Scripts\python.exe -m src.research_cli project problem review <project-id>
.venv\Scripts\python.exe -m src.research_cli project problem approve-frame <project-id> <artifact-id> <decision-id> --status approved
.venv\Scripts\python.exe -m src.research_cli project problem status <project-id>
```

Framing is deterministic and works offline by default. Add `--ai` to `frame` to use the configured LLM provider; configure `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`. Provider/schema errors are reported instead of silently falling back to a manual draft.

To revise, edit the JSON and run `.venv\Scripts\python.exe -m src.research_cli project problem revise <project-id> revised_problem.json`. This creates a new M1 artifact version and requires a new human review. Record requested changes with `approve-frame ... --status changes_requested --comment "..."`; the event is retained in approval history. Only approval of the latest frame version can complete P1. The frame and decision use the existing project-owned M1 artifact, version, decision, approval-event, and stage APIs.

P1 approval means a human accepts the framing for the project workflow. It does not establish that the problem or its causes are factually validated. P1 only drafts questions for possible later research; it does not collect sources, start P2, or mark another stage complete.
