"""Pure serve-side data models that remain importable without the optional ``[ui]`` stack.

Route modules depend on FastAPI, while offline tooling and historical imports use these schemas for
planning/validation. Keeping them here prevents a harmless model import from pulling in the server.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class _GenesisSpec(BaseModel):
    """The BOSS's editable proposal for a brand-new run."""

    run_id: str = ""
    task: dict = Field(default_factory=dict)
    task_file: str = ""
    settings: dict = Field(default_factory=dict)
    rationale: str = ""
    reply: str = ""
    setup_steps: list[str] = Field(default_factory=list)
