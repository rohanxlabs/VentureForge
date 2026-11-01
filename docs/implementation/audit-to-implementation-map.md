# Audit-to-implementation map

- Keep the startup-discovery workflow, Gradio UI, and src.main CLI available.
- Reuse Pydantic, the configured LLM client, Tavily patterns, and current environment settings.
- Keep research models separate from startup pain-point evidence; it lacks source snapshots and question links.
- Research SQLite storage is independent of graph checkpoints.
- src.graph uses MemorySaver; the SQLite checkpoint statement in src/run_controller.py is inaccurate.
- Milestone 2 uses plain Python, one Tavily result, and returned raw content. Missing raw content is search-result-only.
- Python verifies citation membership and exact passage presence; model output remains a candidate.
- Defer URL fetching, additional connectors, planner, graph orchestration, vectors, and research UI.
