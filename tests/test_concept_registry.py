"""PART IV cross-run CR1a (§21.20.3) — concept UID + alias resolver (operator merge/purge).

Stable content-addressed UIDs, append-only alias/purge records applied at READ time (non-destructive),
cycle-safe chain resolution, and the read-model merge (aliased concepts collapse, purged drop). Plus the
`concept-merge` CLI. The raw per-run tags are never rewritten (§21.20.1 taxonomy-doesn't-rewrite-history).
"""
from __future__ import annotations

import orjson

from looplab.engine.concept_registry import (
    canonicalize_concepts, concept_uid, load_concept_aliases, record_concept_alias, resolve_slug,
)
from looplab.engine.memory import build_concept_capsule, portfolio_concept_overview


def test_uid_is_stable_and_content_addressed():
    assert concept_uid("hard-neg") == concept_uid("  Hard-Neg ")   # trim + casefold
    assert concept_uid("hard-neg") != concept_uid("distillation")
    assert concept_uid("x").startswith("c_")


def test_record_and_load_aliases_last_write_wins(tmp_path):
    record_concept_alias(str(tmp_path), from_concept="hn", to_concept="hard-neg")
    record_concept_alias(str(tmp_path), from_concept="hn", to_concept="hard-negative-mining")
    a = load_concept_aliases(str(tmp_path))
    assert a["hn"] == "hard-negative-mining"


def test_resolve_follows_chain_and_is_cycle_safe():
    aliases = {"a": "b", "b": "c", "x": "y", "y": "x"}   # x<->y is a cycle
    assert resolve_slug("a", aliases) == "c"
    assert resolve_slug("x", aliases) in ("x", "y")      # cycle-safe: terminates, no hang
    assert resolve_slug("lone", aliases) == "lone"       # unaliased -> itself


def test_purge_resolves_to_none():
    aliases = {"bad": "\x00purged"}
    assert resolve_slug("bad", aliases) is None
    assert canonicalize_concepts(["bad", "good"], aliases) == ["good"]


def test_canonicalize_merges_aliases():
    aliases = {"hn": "hard-neg", "hnm": "hard-neg"}
    assert canonicalize_concepts(["hn", "hnm", "distill"], aliases) == ["distill", "hard-neg"]


def test_overview_merges_aliased_concepts(tmp_path):
    record_concept_alias(str(tmp_path), from_concept="hn", to_concept="hard-neg")
    caps = [
        build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max", concepts=["hn"], concept_outcomes={}),
        build_concept_capsule(run_id="r2", fingerprint=["k"], direction="max", concepts=["hard-neg"], concept_outcomes={}),
    ]
    ov = portfolio_concept_overview(caps, aliases=load_concept_aliases(str(tmp_path)))
    # both runs collapse under the canonical 'hard-neg' -> one concept explored in 2 runs
    assert ov["n_concepts"] == 1
    hn = ov["concepts"][0]
    assert hn["concept"] == "hard-neg" and hn["n_runs"] == 2


def test_empty_from_concept_raises(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        record_concept_alias(str(tmp_path), from_concept="", to_concept="x")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_concept_merge_and_purge(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    r = CliRunner().invoke(app, ["concept-merge", str(tmp_path), "hn", "hard-neg"])
    assert r.exit_code == 0 and "merged: 'hn' -> 'hard-neg'" in r.stdout
    r2 = CliRunner().invoke(app, ["concept-merge", str(tmp_path), "spam"])   # no target -> purge
    assert r2.exit_code == 0 and "purged: 'spam'" in r2.stdout
    a = load_concept_aliases(str(tmp_path))
    assert a["hn"] == "hard-neg" and a["spam"] == "\x00purged"
