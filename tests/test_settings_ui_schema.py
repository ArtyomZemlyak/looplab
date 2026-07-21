from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.serve.server import make_app
from looplab.serve.settings_ui_schema import (
    SETTINGS_UI_SCHEMA, SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT,
    SETTINGS_UI_SCHEMA_CATALOGUE_VERSION, SETTINGS_UI_SCHEMA_ETAG,
    SETTINGS_UI_SCHEMA_KEYSET_REVISION, SETTINGS_UI_SCHEMA_REVISION,
    SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT, SETTINGS_UI_SCHEMA_VERSION,
)


def test_settings_ui_schema_is_versioned_revalidated_and_conditionally_cacheable(tmp_path):
    client = TestClient(make_app(tmp_path))
    response = client.get(f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}")
    assert response.status_code == 200
    assert response.json() == SETTINGS_UI_SCHEMA
    assert response.headers["cache-control"] == "private, no-cache, max-age=0, must-revalidate"
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
    strong_equivalent = SETTINGS_UI_SCHEMA_ETAG.removeprefix("W/")
    for validator in (
        strong_equivalent,
        f'"some-other-revision", {SETTINGS_UI_SCHEMA_ETAG}',
        "*",
    ):
        conditional = client.get(
            f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}",
            headers={"If-None-Match": validator},
        )
        assert conditional.status_code == 304
        assert conditional.content == b""
        assert conditional.headers["etag"] == SETTINGS_UI_SCHEMA_ETAG
    changed = client.get(
        f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}",
        headers={"If-None-Match": 'W/"some-other-revision"'},
    )
    assert changed.status_code == 200
    invalid = client.get(
        f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}",
        headers={"If-None-Match": f"not-an-entity-tag {SETTINGS_UI_SCHEMA_ETAG}"},
    )
    assert invalid.status_code == 200
    assert client.get(f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION + 1}").status_code == 404


def test_settings_schema_keeps_revalidation_policy_with_owner_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    response = TestClient(make_app(tmp_path)).get(
        f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}",
        headers={"X-LoopLab-Token": "owner-secret"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache, max-age=0, must-revalidate"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["etag"] == SETTINGS_UI_SCHEMA_ETAG
    unknown = TestClient(make_app(tmp_path)).get(
        f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION + 1}",
        headers={"X-LoopLab-Token": "owner-secret"},
    )
    assert unknown.status_code == 404
    assert unknown.headers["cache-control"] == "no-store"


def test_packaged_settings_ui_schema_preserves_copy_and_only_known_unique_fields():
    packaged = json.loads(Path(__file__).parents[1].joinpath(
        "looplab", "serve", "settings_ui_schema.json").read_text(encoding="utf-8"))
    catalogue_shape = deepcopy(SETTINGS_UI_SCHEMA)
    catalogue_shape.pop("revision")
    catalogue_shape["schema"] = SETTINGS_UI_SCHEMA_CATALOGUE_VERSION
    for group in catalogue_shape["groups"]:
        for field in group["fields"]:
            for name in ("minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum"):
                field.pop(name, None)
            field.pop("nullable", None)
    assert catalogue_shape == packaged
    assert packaged["schema"] == SETTINGS_UI_SCHEMA_CATALOGUE_VERSION

    fields = [field for group in packaged["groups"] for field in group["fields"]]
    keys = [field["key"] for field in fields]
    assert len(keys) == len(set(keys))
    assert len(keys) == SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT == 156
    assert len(Settings.model_fields) == SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT == 185
    assert hashlib.sha256("\0".join(sorted(keys)).encode()).hexdigest() == SETTINGS_UI_SCHEMA_KEYSET_REVISION
    assert set(keys) <= set(Settings.model_fields)
    by_key = {field["key"]: field for field in fields}
    # CODEX AGENT: curated settings expose the two independent canonical axes; legacy aliases still
    # parse in raw config/snapshots but must not remain as competing operator controls.
    assert {"eval_parallel", "llm_parallel"} <= set(by_key)
    assert by_key["card_driven_selection"]["type"] == "bool"
    assert "pinned at run start" in by_key["card_driven_selection"]["help"]
    assert by_key["speculation_depth"]["type"] == "int"
    assert "only when Card queue selection is enabled" in by_key["speculation_depth"]["help"]
    assert Settings.model_fields["speculation_depth"].default == 0
    assert Settings.model_fields["speculation_depth"].metadata
    assert {"max_parallel", "parallel_build"}.isdisjoint(by_key)
    assert "0 = AUTO at launch" in by_key["eval_parallel"]["help"]
    assert "Live Strategist/operator updates settle 0" in by_key["llm_parallel"]["help"]
    assert "agents" not in by_key["max_eval_timeout"]
    assert "hard ceiling" in by_key["max_eval_timeout"]["help"].lower()
    assert set(("concept_pivot", "concept_run_base", "concept_retag_every",
                "graded_novelty", "capability_expansion")) <= set(by_key)
    assert "does not itself rank candidates" in by_key["concept_pivot"]["help"]
    assert "materialization receipts" in by_key["concept_run_base"]["help"]
    assert "display-only" in by_key["concept_run_base"]["help"]
    assert "Researcher-authored additions" in by_key["concept_retag_every"]["help"]
    assert "proposal admission" in by_key["graded_novelty"]["help"]
    assert "Concept coverage pivot" in by_key["capability_expansion"]["help"]
    assert "D8" in by_key["cross_run_concepts"]["help"]
    # CODEX AGENT: product-default experimental switches must disclose behavioral and paid-work effects;
    # otherwise the Settings UI is materially less truthful than the config reference it controls.
    assert "affect downstream selection" in by_key["concept_pivot"]["help"]
    assert "model/tool-loop turns" in by_key["cross_run_read_tools"]["help"]
    assert "delay finalization" in by_key["cross_run_curation"]["help"]
    assert "paid model cost" in by_key["cross_run_curation"]["warning"]
    assert "never applies" in by_key["cross_run_curation_auto"]["warning"]
    # CODEX AGENT: placeholders/default copy are executable UI behavior, not decoration; pin the two
    # defaults that previously instructed operators to configure the opposite of the product policy.
    assert Settings.model_fields["concurrent_research"].default is True
    assert "on by default" in by_key["concurrent_research"]["help"]
    assert "off by default" not in by_key["concurrent_research"]["help"]
    assert by_key["deep_research_every"]["placeholder"] == str(
        Settings.model_fields["deep_research_every"].default)
    assert packaged["agent_role_pills"]["researcher"]["short"] == "R"

    served_fields = {
        field["key"]: field
        for group in SETTINGS_UI_SCHEMA["groups"]
        for field in group["fields"]
    }
    assert served_fields["max_nodes"] | {"minimum": 1, "maximum": 1_000_000} == served_fields["max_nodes"]
    assert served_fields["n_seeds"]["minimum"] == 1
    assert served_fields["n_seeds"]["maximum"] == 1024
    assert served_fields["eval_parallel"]["minimum"] == 0
    assert served_fields["eval_parallel"]["maximum"] == 1024
    assert served_fields["llm_parallel"]["minimum"] == 0
    assert served_fields["llm_parallel"]["maximum"] == 64
    assert served_fields["timeout"]["exclusiveMinimum"] == 0
    assert served_fields["timeout"]["nullable"] is False
    assert served_fields["max_seconds"]["nullable"] is True
    assert served_fields["holdout_fraction"]["minimum"] == 0
    assert served_fields["holdout_fraction"]["maximum"] == 0.9
    assert served_fields["select_verifier_samples"]["minimum"] == 1
    assert served_fields["select_verifier_samples"]["maximum"] == 32
    assert not ({"minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum"}
                & served_fields["llm_model"].keys())
