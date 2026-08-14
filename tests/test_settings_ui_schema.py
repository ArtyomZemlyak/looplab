from __future__ import annotations

import re
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
    assert len(keys) == SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT == 177
    # 199 -> 201 Settings and 168 -> 170 catalogued rows: TWO rows landed together on 2026-08-13.
    # `assistant_time_budget_s` gave the CHAT its own wall clock — `run_turn` read the engine-wide
    # `agent_time_budget_s` and then applied `or 300.0`, so the neighbouring row's documented
    # "0 = no cap" was false for the assistant and nothing could raise the chat's limit without
    # raising every engine role's. `eval_deadline_grace_s` (doc 39 §2.2) is the one-shot,
    # judge-granted extension a stage may get at its wall-clock deadline — a row rather than an
    # uncurated omission because it is an LLM-judged switch that spends GPU time when an operator
    # turns it on, and they have to be able to see the number they set.
    # (Previously 194 -> 195 Settings and 163 -> 164 catalogued rows when `task_facets_finalize` split the
    # paid-but-behaviorally-inert task-facet call from the concept/claim curation umbrella. The
    # literal is a review tripwire, not a gate (the gate is the two-way reconciliation), and the
    # separate default-off warning row is part of this same contract.
    # 194 -> 195 Settings and 163 -> 164 catalogued rows when `task_facets_finalize` split the
    # paid-but-behaviorally-inert task-facet call from the concept/claim curation umbrella. The
    # literal is a review tripwire, not a gate (the gate is the two-way reconciliation), and the
    # separate default-off warning row is part of this same contract.
    # 199 -> 201 Settings and 168 -> 170 rows for the Developer's PROBE (F2): `developer_probe` and
    # `developer_probe_timeout_s`. Both are ROWS rather than uncurated omissions for the reason
    # `read_fence` is one — the probe is an EXECUTION surface running inside the engine process, so
    # an operator has to be able to see that it exists and close it, and its timeout is the line
    # between "a question" and "a job that belongs in an eval stage".
    assert len(Settings.model_fields) == SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT == 209
    # 199 -> 200 Settings and 168 -> 169 catalogued rows when F8 added `repair_critic_after`
    # (2026-08-13), the cadence at which the repair critic gets its veto. It is catalogued rather
    # than left uncurated because the knob directly above it, `inline_repair_attempts`, changed
    # meaning in the same commit — its default is now 0 — and an operator reading one without the
    # other would conclude that in-node repair had become unbounded.
    assert hashlib.sha256("\0".join(sorted(keys)).encode()).hexdigest() == SETTINGS_UI_SCHEMA_KEYSET_REVISION

    # THE JS HALF OF THIS TRIPWIRE, pinned from the suite that actually gets run.
    # `ui/test/settingsSchemaResource.test.js` carries the same field count as a literal, and its own
    # comment records the count drifting FIVE times — 162->163, 165->167, 167->168, and finally
    # 168->175 across five merged branches. Every occurrence has the same cause, which that comment
    # also names: the Python guard is in `python -m pytest` and the JS one is in `npm test`, so it is
    # always the JS half that is left behind. A note telling the next contributor to grep for the old
    # number is the weakest possible fix for a failure that has now recurred five times; reading the
    # literal here makes the drift impossible instead of merely documented.
    #
    # Read as TEXT on purpose: the point is to fail when the JS source still says the old number, and
    # importing or executing it would need a node toolchain this suite does not assume.
    js = (Path(__file__).resolve().parents[1] / "ui" / "test" / "settingsSchemaResource.test.js")
    if js.exists():                       # a source-only checkout without `ui/` is not a failure
        pinned = re.findall(r"assert\.equal\(Object\.keys\(schema\.fieldByKey\)\.length, (\d+)\)",
                            js.read_text(encoding="utf-8"))
        assert pinned == [str(SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT)], (
            f"ui/test/settingsSchemaResource.test.js pins {pinned}, catalogue has "
            f"{SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT} rows")
    assert set(keys) <= set(Settings.model_fields)
    by_key = {field["key"]: field for field in fields}
    # CODEX AGENT: curated settings expose the two independent canonical axes; legacy aliases still
    # parse in raw config/snapshots but must not remain as competing operator controls. Eval width is
    # per Run, while the conservative same-user host lease is a separate cross-Run admission layer.
    assert {"eval_parallel", "llm_parallel"} <= set(by_key)
    assert by_key["card_driven_selection"]["type"] == "bool"
    assert "pinned at run start" in by_key["card_driven_selection"]["help"]
    assert by_key["speculation_depth"]["type"] == "int"
    assert "only when Card queue selection is enabled" in by_key["speculation_depth"]["help"]
    assert "speculation-gate" in by_key["speculation_depth"]["help"]
    # The DEFAULT itself belongs to config.py (it flipped 0 -> -1/AUTO on 2026-08-05); what this file
    # pins is that the row was reviewed against whatever ships, which `_check_pinned_default` enforces
    # at load and this asserts on the packaged copy.
    assert by_key["speculation_depth"]["default"] == Settings.model_fields["speculation_depth"].default
    assert Settings.model_fields["speculation_depth"].metadata
    assert "speculation_gate_receipt" not in by_key
    assert {"max_parallel", "parallel_build"}.isdisjoint(by_key)
    assert "0 = AUTO at launch" in by_key["eval_parallel"]["help"]
    assert "inside one Run" in by_key["eval_parallel"]["help"]
    assert "host lease" in by_key["eval_parallel"]["help"]
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
    assert by_key["task_facets_finalize"]["default"] is False
    assert "no current retrieval" in by_key["task_facets_finalize"]["help"]
    assert "no behavioral consumer" in by_key["task_facets_finalize"]["warningTitle"]
    assert "synchronous paid" in by_key["task_facets_finalize"]["warning"]
    assert "manual task-facets" in by_key["task_facets_finalize"]["help"]
    assert "never applies" in by_key["cross_run_curation_auto"]["warning"]
    assert "configured run root" in by_key["all_runs_tools"]["help"]
    assert "not machine-wide" in by_key["all_runs_tools"]["help"]
    assert "richer run-root tools" in by_key["all_runs_tools"]["help"]
    assert "over that same root" in by_key["all_runs_tools"]["help"]
    assert "EVERY run on this machine" not in by_key["all_runs_tools"]["help"]
    assert "task's original in-process Developer" in by_key["validate_agent"]["help"]
    assert "LLM developer" not in by_key["validate_agent"]["help"]
    # CODEX AGENT: placeholders/default copy are executable UI behavior, not decoration; pin the two
    # defaults that previously instructed operators to configure the opposite of the product policy.
    assert Settings.model_fields["concurrent_research"].default is True
    assert "on by default" in by_key["concurrent_research"]["help"]
    assert "off by default" not in by_key["concurrent_research"]["help"]
    # CODEX AGENT: fold-neutral telemetry is not necessarily behaviorally inert. These defaults feed
    # later prompts/portfolio evidence, so UI copy must disclose that indirect effect precisely.
    assert "steer the next proposal" in by_key["concurrent_research"]["help"]
    assert "steer later proposals" in by_key["concurrent_research_repeat"]["help"]
    assert "steer later proposals" in by_key["track_hypotheses"]["help"]
    assert "positive D8 cross-run claim evidence" in by_key["research_verify"]["help"]
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
