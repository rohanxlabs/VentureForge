# Milestone 5: evidence coverage and research gaps

Run the bounded collect/extract/verify/follow-up workflow for an approved plan:

```powershell
.venv\Scripts\python.exe -m src.research_cli research <approved-plan-id>
```

The command runs one initial collection round and, by default, up to one targeted follow-up round (`--max-rounds 2`). Existing search, Tavily-credit, page, and time budgets apply across all rounds. It saves candidate evidence, checks exact quote membership in captured source text, updates question coverage, and exports `reports/<run-id>/research.md`.

Coverage is a conservative operational heuristic: a question needs at least one verified passage interpreted as supporting or weakening and evidence from two distinct source hosts. Both supporting and weakening interpretations flag a potential disagreement and keep the question open for human review. Host identity is only a rough independence proxy: it does not establish publisher ownership, source quality, or factual truth. Exact quote verification proves that text was captured from the cited source; it does not prove the claim is true or that the passage answers the question.

Use `--max-rounds`, `--max-searches`, `--max-tavily-credits`, `--max-pages`, and `--max-seconds` to keep the run bounded. If a question remains open when a budget is exhausted, the report shows the gap rather than claiming completion. Research requires a plan explicitly approved through `plan` or `plan-review`.
