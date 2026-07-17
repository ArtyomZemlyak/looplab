from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.serve.server import make_app
from looplab.serve.settings_ui_schema import (
    SETTINGS_UI_SCHEMA, SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT, SETTINGS_UI_SCHEMA_ETAG,
    SETTINGS_UI_SCHEMA_KEYSET_REVISION, SETTINGS_UI_SCHEMA_REVISION,
    SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT, SETTINGS_UI_SCHEMA_VERSION,
)


def test_settings_ui_schema_is_versioned_immutable_and_conditionally_cacheable(tmp_path):
    client = TestClient(make_app(tmp_path))
    response = client.get(f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}")
    assert response.status_code == 200
    assert response.json() == SETTINGS_UI_SCHEMA
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert response.headers["etag"] == SETTINGS_UI_SCHEMA_ETAG
    assert response.headers["x-looplab-schema-version"] == str(SETTINGS_UI_SCHEMA_VERSION)
    assert response.json()["revision"] == SETTINGS_UI_SCHEMA_REVISION

    unchanged = client.get(
        f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}",
        headers={"If-None-Match": SETTINGS_UI_SCHEMA_ETAG},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["cache-control"] == response.headers["cache-control"]
    assert unchanged.headers["etag"] == SETTINGS_UI_SCHEMA_ETAG
    assert client.get(f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION + 1}").status_code == 404


def test_packaged_settings_ui_schema_preserves_copy_and_only_known_unique_fields():
    packaged = json.loads(Path(__file__).parents[1].joinpath(
        "looplab", "serve", "settings_ui_schema.json").read_text(encoding="utf-8"))
    assert {key: value for key, value in SETTINGS_UI_SCHEMA.items() if key != "revision"} == packaged
    assert packaged["schema"] == SETTINGS_UI_SCHEMA_VERSION

    fields = [field for group in packaged["groups"] for field in group["fields"]]
    keys = [field["key"] for field in fields]
    assert len(keys) == len(set(keys))
    assert len(keys) == SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT == 141
    assert len(Settings.model_fields) == SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT == 164
    assert hashlib.sha256("\0".join(sorted(keys)).encode()).hexdigest() == SETTINGS_UI_SCHEMA_KEYSET_REVISION
    assert set(keys) <= set(Settings.model_fields)
    by_key = {field["key"]: field for field in fields}
    assert set(("concept_pivot", "graded_novelty", "capability_expansion")) <= set(by_key)
    assert "never ranks or selects" in by_key["concept_pivot"]["help"]
    assert "proposal admission" in by_key["graded_novelty"]["help"]
    assert "Concept coverage pivot" in by_key["capability_expansion"]["help"]
    assert "D8" in by_key["cross_run_concepts"]["help"]
    assert "never applies" in by_key["cross_run_curation_auto"]["warning"]
    assert packaged["agent_role_pills"]["researcher"]["short"] == "R"
