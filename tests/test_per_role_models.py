"""H3 per-role model presets: Researcher and Developer on different models/endpoints."""
from __future__ import annotations

import json
from pathlib import Path

from looplab.config import Settings
from looplab.tasks import load_task, make_roles

ROOT = Path(__file__).resolve().parents[1]


def _client_model(role):
    c = getattr(role, "client", None)
    return getattr(c, "model", None)


def test_per_role_models_point_at_distinct_endpoints():
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    s = Settings(backend="llm", llm_model="shared-model", unified_agent=False,
                 researcher_model="fast-researcher", developer_model="coder-30b")
    researcher, developer = make_roles(task, s)
    assert _client_model(researcher) == "fast-researcher"
    assert _client_model(developer) == "coder-30b"


def test_per_role_unset_falls_back_to_shared():
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    s = Settings(backend="llm", llm_model="shared-model", unified_agent=False)
    researcher, developer = make_roles(task, s)
    assert _client_model(researcher) == "shared-model"
    assert _client_model(developer) == "shared-model"


def test_settings_accepts_per_role_base_urls():
    s = Settings(researcher_base_url="http://a/v1", developer_base_url="http://b/v1")
    assert s.researcher_base_url == "http://a/v1" and s.developer_base_url == "http://b/v1"
