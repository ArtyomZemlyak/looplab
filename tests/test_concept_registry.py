"""PART IV cross-run CR1a (§21.20.3) — concept UID + alias resolver (operator merge/purge).

Stable content-addressed UIDs, append-only alias/purge records applied at READ time (non-destructive),
cycle-safe chain resolution, and the read-model merge (aliased concepts collapse, purged drop). Plus the
`concept-merge` CLI. The raw per-run tags are never rewritten (§21.20.1 taxonomy-doesn't-rewrite-history).
"""
from __future__ import annotations

import orjson

import pytest

from looplab.engine.concept_registry import (
    canonicalize_concepts, concept_uid, load_concept_aliases, load_concept_splits, normalize_key,
    record_concept_alias, record_concept_split, resolve_slug, resolve_split,
)
from looplab.engine.memory import build_concept_capsule, portfolio_concept_overview


def test_uid_is_stable_and_content_addressed():
    assert concept_uid("hard-neg") == concept_uid("  Hard-Neg ")   # trim + casefold
    assert concept_uid("hard-neg") != concept_uid("distillation")
    assert concept_uid("x").startswith("c_")


# --------------------------------------------------------------------------- #
# Versioned normalization contract — ONE key for writes AND reads (CODEX)
# --------------------------------------------------------------------------- #

def test_normalize_key_is_nfkc_casefold_and_whitespace_collapsed():
    assert normalize_key("  Hard  Neg ") == "hard neg"            # collapse internal ws + strip + casefold
    assert normalize_key("ﬁle") == "file"                        # NFKC folds the fi ligature
    assert normalize_key("ДАННЫЕ") == "данные"                   # casefold non-ASCII (Cyrillic)


def test_uid_follows_canonical_identity_not_display():
    aliases = {"hn": "hard-neg"}
    assert concept_uid("hn", aliases) == concept_uid("hard-neg", aliases)   # aliased -> same identity
    assert concept_uid("gone", {"gone": "\x00purged"}) == ""               # purged -> no identity


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
    with pytest.raises(ValueError):
        record_concept_alias(str(tmp_path), from_concept="", to_concept="x")


def test_alias_self_link_and_cycle_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        record_concept_alias(str(tmp_path), from_concept="x", to_concept="x")       # self-link
    record_concept_alias(str(tmp_path), from_concept="a", to_concept="b")
    record_concept_alias(str(tmp_path), from_concept="b", to_concept="c")
    with pytest.raises(ValueError):                                                 # c->a closes a->b->c->a
        record_concept_alias(str(tmp_path), from_concept="c", to_concept="a")
    # the rejected write never landed: the chain still resolves cleanly to the canonical 'c'
    assert resolve_slug("a", load_concept_aliases(str(tmp_path))) == "c"


# --------------------------------------------------------------------------- #
# SPLIT — one coarse concept -> finer ones, re-tagged from each run's OWN sibling concepts (§21.20.13)
# --------------------------------------------------------------------------- #

def test_record_and_load_split(tmp_path):
    record_concept_split(str(tmp_path), from_concept="data/augmentation",
                         rules=[{"to": "data/hard-negative-mining", "when_any": ["hard", "negative"]},
                                {"to": "data/synonym-aug", "when_any": ["synonym", "eda"]}],
                         default="data/augmentation")
    sp = load_concept_splits(str(tmp_path))
    spec = sp["data/augmentation"]
    assert spec["default"] == "data/augmentation" and len(spec["rules"]) == 2
    assert spec["rules"][0]["to"] == "data/hard-negative-mining"


def test_resolve_split_picks_first_matching_rule_from_context():
    splits = {"data/aug": {"rules": [{"to": "data/hn", "when_any": ["hard"]},
                                     {"to": "data/syn", "when_any": ["synonym"]}], "default": "data/aug"}}
    # a run whose siblings mention 'hard' -> hn; 'synonym' -> syn; neither -> default
    assert resolve_split("data/aug", {"hard", "loss", "mnr"}, splits) == "data/hn"
    assert resolve_split("data/aug", {"synonym"}, splits) == "data/syn"
    assert resolve_split("data/aug", {"loss"}, splits) == "data/aug"
    assert resolve_split("unrelated", {"hard"}, splits) == "unrelated"      # not in the split -> unchanged


def test_split_rejects_no_progress_and_empty(tmp_path):
    with pytest.raises(ValueError):        # a rule re-tagging the source to itself is pointless
        record_concept_split(str(tmp_path), from_concept="x", rules=[{"to": "x", "when_any": ["a"]}])
    with pytest.raises(ValueError):        # no rule + no (real) default is inert
        record_concept_split(str(tmp_path), from_concept="x", rules=[], default="")
    with pytest.raises(ValueError):        # no rule + bare identity default is also inert
        record_concept_split(str(tmp_path), from_concept="x", rules=[], default="x")


def test_canonicalize_applies_split_then_alias():
    splits = {"data/aug": {"rules": [{"to": "data/hn", "when_any": ["hard"]}], "default": "data/aug"}}
    aliases = {"data/hn": "data/hard-negative-mining"}
    # 'data/aug' sees sibling 'data/hard-neg' (token 'hard') -> split to data/hn -> alias to the canonical
    got = canonicalize_concepts(["data/aug", "data/hard-neg"], aliases=aliases, splits=splits)
    assert "data/hard-negative-mining" in got


def test_overview_re_tags_split_by_sibling_context(tmp_path):
    record_concept_split(str(tmp_path), from_concept="data/aug",
                         rules=[{"to": "data/hard-neg", "when_any": ["hard"]}], default="data/aug")
    caps = [
        # run r1 also explored a 'hard' concept -> its data/aug re-tags to data/hard-neg
        build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                              concepts=["data/aug", "loss/hard-margin"], concept_outcomes={}),
        # run r2 has no 'hard' sibling -> its data/aug stays data/aug (default)
        build_concept_capsule(run_id="r2", fingerprint=["k"], direction="max",
                              concepts=["data/aug"], concept_outcomes={}),
    ]
    ov = portfolio_concept_overview(caps, splits=load_concept_splits(str(tmp_path)))
    names = {e["concept"]: e["n_runs"] for e in ov["concepts"]}
    assert names.get("data/hard-neg") == 1 and names.get("data/aug") == 1   # re-tagged apart by context


def test_overview_dedupes_aliased_concepts_within_one_run():
    # two raw concepts in ONE capsule both alias to 'hard-neg' -> ONE run-row, not two (CODEX double-row fix)
    caps = [build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                                  concepts=["hn", "hnm"], concept_outcomes={"hn": 0.9})]
    ov = portfolio_concept_overview(caps, aliases={"hn": "hard-neg", "hnm": "hard-neg"})
    hn = [e for e in ov["concepts"] if e["concept"] == "hard-neg"][0]
    assert hn["n_runs"] == 1 and len(hn["runs"]) == 1 and hn["runs"][0]["metric"] == 0.9


def test_cli_concept_split(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    r = CliRunner().invoke(app, ["concept-split", str(tmp_path), "data/aug",
                                 "--rule", "data/hard-neg:hard,negative", "--default", "data/aug"])
    assert r.exit_code == 0 and "split: 'data/aug'" in r.stdout and "data/hard-neg" in r.stdout
    sp = load_concept_splits(str(tmp_path))
    assert sp["data/aug"]["rules"][0]["to"] == "data/hard-neg"


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
