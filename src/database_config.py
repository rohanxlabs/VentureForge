"""Shared repository-wide database path configuration."""
from __future__ import annotations

import os
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DATABASE_PATH = REPOSITORY_ROOT / "data" / "ventureforge.sqlite"


def resolve_database_path(path: str | Path | None = None) -> Path:
    """Resolve the active DB path consistently for UI, CLI, and services.

    Precedence is an explicit function/CLI path, then VENTUREFORGE_DATABASE,
    then the legacy VENTUREFORGE_PROJECT_DB override, then the canonical DB.
    Relative configured paths are anchored to the repository root.
    """
    selected = path
    if selected is None:
        selected = (os.environ.get("VENTUREFORGE_DATABASE")
                    or os.environ.get("VENTUREFORGE_PROJECT_DB"))
    if selected is None or not str(selected).strip():
        return CANONICAL_DATABASE_PATH
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute():
        candidate = REPOSITORY_ROOT / candidate
    return candidate.resolve()


DEFAULT_DATABASE_PATH = resolve_database_path()
