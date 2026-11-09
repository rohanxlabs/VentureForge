# P2 Research Intelligence

P2 turns the latest approved P1 Problem Frame into a question driven research plan, collects source material through the existing research pipeline, records evidence and coverage, and prepares a project owned report for human review. P2 does not decide whether the problem is validated; that is a later stage.

## Prerequisites

The project must be active. P1 must be complete, and the latest Problem Frame and its linked decision must both be approved. P2 planning checks these conditions against the project store. A plan records the exact P1 artifact ID and version it used; plan approval fails if that frame is no longer the latest approved frame.

## CLI workflow

Use the same SQLite database for each command (the default is `data/research.sqlite`):

```text
python -m src.research_cli project research plan <project-id>
python -m src.research_cli project research plan-show <project-id> --output plan.json
python -m src.research_cli project research plan-revise <project-id> --input edited-plan.json
python -m src.research_cli project research plan-approve <project-id> <plan-artifact-id>
python -m src.research_cli project research run <project-id> --max-searches 8 --max-pages 20 --max-rounds 2
python -m src.research_cli project research status <project-id>
python -m src.research_cli project research sources <project-id>
python -m src.research_cli project research findings <project-id>
python -m src.research_cli project research coverage <project-id>
python -m src.research_cli project research gaps <project-id>
python -m src.research_cli project research report <project-id>
python -m src.research_cli project research report-approve <project-id> <report-artifact-id>
```

Planning is deterministic by default and works offline. Add `--ai` to request draft questions from the configured structured planning provider. AI generated questions remain drafts and require human review. `plan-show --output` writes editable JSON; `plan-revise --input` accepts the edited file and persists a new version with fresh run and question IDs. Use `plan-approve --status changes_requested --comment "..."` or the `request-changes` command to record requested edits before revising.

The `run` command requires an approved latest plan. It uses existing Tavily and Hacker News connectors, safe fetching, extraction, exact passage verification, and the bounded search budget. Configure Tavily credentials through the existing application settings/environment when that connector is needed. Extraction uses the configured LLM provider. A complete run persists a workflow record and a versioned report artifact with a linked M1 decision. Connector or provider failures are recorded; a partial report remains reviewable but its approval cannot complete P2. Re-run the approved plan to produce a complete execution. A successfully completed run may still report unanswered questions or zero sources; the report exposes those outcomes for the human reviewer.

## Evidence and interpretation

Sources are associated with plan questions and research runs. Extracted evidence points to its source, question, claim, passage, and relation. The existing verifier checks whether the exact passage exists in captured text. That check establishes citation validity only; it does not establish semantic support or factual truth. Model generated interpretations and source relevance still need human review. Coverage is a heuristic based on verified evidence, source host independence, and recorded support/weakening relations. It is not a validation score.

Reports include questions, evidence references, coverage, unresolved gaps, connector attempts (connector, query, status, source count, elapsed time and error), configured and consumed budgets, and known limitations. A successful empty search is distinguished from a failed connector. If every attempted connector fails, the workflow is marked failed and no normal report is persisted. No automatic resume or mid workflow recovery is provided.

P2 approval uses the existing append only M1 approval history and project artifact records. Only the latest report can be approved. P2 completion requires a complete research workflow whose output is that report, plus matching artifact and decision approvals. P2 approval does not start P3.

## Known limits

- Coverage is heuristic; source host is used as a rough independence signal and does not establish true source independence.
- The existing relational schema associates a source with one question. URL deduplication can therefore reduce duplicate pages, but does not model a many question to one source relationship.
- Search queries are built from the question, problem statement, and connector category hints; geographic/time scope is recorded in the plan but is not guaranteed to be enforced by every connector.
- Finding relations are candidate extraction interpretations. There is no separate human adjudication record for each semantic assessment or a formal source quality score.
- A new numbered database migration was not needed; P2 uses existing plan, run, evidence, artifact, decision, workflow, and approval storage.
- Benchmark scenarios and live source backed research must be evaluated separately from synthetic unit fixtures. Unit tests use mocked connectors and do not establish findings for any real world benchmark.

P2 adds no P3 or later stage behavior.
