"""PART IV cross-run CR1a (§21.20.3) — concept UID + alias resolver (operator merge/purge).

Stable content-addressed UIDs, append-only alias/purge records applied at READ time (non-destructive),
cycle-safe chain resolution, and the read-model merge (aliased concepts collapse, purged drop). Plus the
`concept-merge` CLI. The raw per-run tags are never rewritten (§21.20.1 taxonomy-doesn't-rewrite-history).
"""
from __future__ import annotations

import json
import pytest

from looplab.engine.concept_registry import (
    ConceptGovernanceConflict, ConceptGovernanceGlobalConflict,
    ConceptGovernanceIdempotencyConflict, canonicalize_concepts, clear_concept_alias,
    clear_concept_split,
    concept_governance_global_revision, concept_governance_revision,
    concept_governance_snapshot, concept_uid,
    load_concept_aliases, load_concept_splits, normalize_key,
    prepare_concept_alias, prepare_concept_split, record_concept_alias,
    record_concept_split, resolve_slug, resolve_split,
)
from looplab.engine.memory import build_concept_capsule, portfolio_concept_overview


def test_uid_is_stable_and_content_addressed():
    assert concept_uid("hard-neg") == concept_uid("  Hard-Neg ")   # trim + casefold
    assert concept_uid("hard-neg") != concept_uid("distillation")
    assert concept_uid("x").startswith("c_")


# --------------------------------------------------------------------------- #
# Versioned normalization contract — one key for writes and reads.
# --------------------------------------------------------------------------- #

def test_normalize_key_is_nfkc_casefold_and_whitespace_collapsed():
    assert normalize_key("  Hard  Neg ") == "hard neg"            # collapse internal ws + strip + casefold
    assert normalize_key("ﬁle") == "file"                        # NFKC folds the fi ligature
    assert normalize_key("ДАННЫЕ") == "данные"                   # casefold non-ASCII (Cyrillic)


def test_normalize_key_strips_control_chars_no_tombstone_collision():
    # An untrusted slug must not normalize to the '\x00purged' sentinel and turn a
    # merge into a covert purge.
    assert "\x00" not in normalize_key("\x00purged")
    assert normalize_key("\x00purged") == "purged"          # sentinel stripped -> ordinary slug


def test_prepare_payload_matches_normalized_durable_semantics():
    assert prepare_concept_alias(" LOSS/A ", "LOSS/B") == {
        "from": "loss/a", "to": "loss/b",
    }
    assert prepare_concept_split(
        " DATA/AUG ",
        [{"to": "DATA/IMAGE", "when_any": [" Vision ", "vision"]},
         {"to": "", "when_any": ["inert"]}],
        " DATA/KEEP ",
    ) == {
        "from": "data/aug",
        "rules": [{"to": "data/image", "when_any": ["vision"]}],
        "default": "data/keep",
    }


def test_governance_snapshot_returns_states_and_revisions_from_one_receipt(tmp_path):
    record_concept_alias(
        tmp_path, from_concept="loss/a", to_concept="loss/b",
        expected_revision=0, expected_governance_revision=0,
    )
    record_concept_split(
        tmp_path, from_concept="data/aug",
        rules=[{"to": "data/image", "when_any": ["vision"]}],
        expected_revision=0, expected_governance_revision=1,
    )

    snapshot = concept_governance_snapshot(tmp_path)

    assert snapshot["aliases"] == {"loss/a": "loss/b"}
    assert snapshot["splits"]["data/aug"]["rules"][0]["to"] == "data/image"
    assert snapshot["alias_revision"] == 1
    assert snapshot["split_revision"] == 1
    assert snapshot["governance_revision"] == 2


def test_uid_follows_canonical_identity_not_display():
    aliases = {"hn": "hard-neg"}
    assert concept_uid("hn", aliases) == concept_uid("hard-neg", aliases)   # aliased -> same identity
    assert concept_uid("gone", {"gone": "\x00purged"}) == ""               # purged -> no identity


def test_record_and_load_aliases_last_write_wins(tmp_path):
    record_concept_alias(str(tmp_path), from_concept="hn", to_concept="hard-neg")
    record_concept_alias(str(tmp_path), from_concept="hn", to_concept="hard-negative-mining")
    a = load_concept_aliases(str(tmp_path))
    assert a["hn"] == "hard-negative-mining"


def test_alias_clear_is_distinct_from_purge_and_revision_is_cas(tmp_path):
    first = record_concept_alias(str(tmp_path), from_concept="hn", to_concept="hard-neg",
                                 expected_revision=0)
    assert first["revision"] == concept_governance_revision(str(tmp_path), "aliases") == 1
    with pytest.raises(ConceptGovernanceConflict) as stale:
        record_concept_alias(str(tmp_path), from_concept="hn", to_concept="other", expected_revision=0)
    assert stale.value.expected == 0 and stale.value.actual == 1

    cleared = clear_concept_alias(str(tmp_path), from_concept="hn", expected_revision=1)
    assert cleared["action"] == "clear" and cleared["revision"] == 2
    assert "hn" not in load_concept_aliases(str(tmp_path))
    purged = record_concept_alias(str(tmp_path), from_concept="hn", to_concept="", expected_revision=2)
    assert purged["action"] == "purge" and resolve_slug("hn", load_concept_aliases(str(tmp_path))) is None
    clear_concept_alias(str(tmp_path), from_concept="hn", expected_revision=3)
    assert resolve_slug("hn", load_concept_aliases(str(tmp_path))) == "hn"


def test_alias_action_id_retry_returns_original_before_stale_cas(tmp_path):
    first = record_concept_alias(str(tmp_path), from_concept="hn", to_concept="hard-neg",
                                 expected_revision=0, action_id="req-1", at="first",
                                 by="first-operator")
    retry = record_concept_alias(str(tmp_path), from_concept="hn", to_concept="hard-neg",
                                 expected_revision=0, action_id="req-1", at="retry",
                                 by="retry-operator")
    assert (retry == first and retry["revision"] == 1 and retry["at"] == "first"
            and retry["by"] == "first-operator")
    assert len((tmp_path / "concept_aliases.jsonl").read_text().splitlines()) == 1
    with pytest.raises(ConceptGovernanceIdempotencyConflict):
        record_concept_alias(str(tmp_path), from_concept="hn", to_concept="different",
                             expected_revision=0, action_id="req-1")


def test_alias_loader_quarantines_malformed_explicit_actions(tmp_path):
    rows = [
        {"v": 1, "action": "set", "from": "empty-target", "to": ""},
        {"v": 1, "action": "purge", "from": "purged", "to": "must-not-become-alias"},
        {"v": 999, "action": "set", "from": "future", "to": "unknown-schema"},
    ]
    (tmp_path / "concept_aliases.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    aliases = load_concept_aliases(str(tmp_path))
    assert "empty-target" not in aliases and aliases["purged"] == "\x00purged" and "future" not in aliases


def test_resolve_follows_chain_and_is_cycle_safe():
    aliases = {"a": "b", "b": "c", "x": "y", "y": "x", "prefix": "m", "m": "n", "n": "m"}
    assert resolve_slug("a", aliases) == "c"
    assert resolve_slug("x", aliases) == resolve_slug("y", aliases) == "x"  # stable legacy-cycle identity
    assert resolve_slug("prefix", aliases) == resolve_slug("m", aliases) == resolve_slug("n", aliases) == "m"
    assert resolve_slug("lone", aliases) == "lone"       # unaliased -> itself


@pytest.mark.parametrize("revision", [True, -1, "1", 1.0])
def test_governance_expected_revision_is_strict_non_negative_int(tmp_path, revision):
    with pytest.raises(ValueError, match="expected_revision"):
        record_concept_alias(str(tmp_path), from_concept="a", to_concept="b",
                             expected_revision=revision)


@pytest.mark.parametrize("revision", [True, -1, "1", 1.0])
def test_governance_expected_global_revision_is_strict_non_negative_int(tmp_path, revision):
    with pytest.raises(ValueError, match="expected_governance_revision"):
        record_concept_alias(
            str(tmp_path), from_concept="a", to_concept="b",
            expected_governance_revision=revision,
        )


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


def test_merge_into_purged_target_is_rejected_without_purging_source(tmp_path):
    record_concept_alias(str(tmp_path), from_concept="retired", to_concept="")
    with pytest.raises(ValueError, match="target .* purged"):
        record_concept_alias(str(tmp_path), from_concept="live", to_concept="retired")
    aliases = load_concept_aliases(str(tmp_path))
    assert resolve_slug("retired", aliases) is None
    assert resolve_slug("live", aliases) == "live"
    assert concept_governance_revision(str(tmp_path), "aliases") == 1


# --------------------------------------------------------------------------- #
# SPLIT — one coarse concept -> finer ones, re-tagged from each run's OWN sibling concepts (§21.20.13)
# --------------------------------------------------------------------------- #

def test_record_and_load_split(tmp_path):
    record_concept_split(str(tmp_path), from_concept="data/augmentation",
                         rules=[{"to": "data/hard-negative-mining", "when_any": ["hard", "negative"]},
                                {"to": "data/synonym-aug", "when_any": ["synonym", "eda"]}])
    sp = load_concept_splits(str(tmp_path))
    spec = sp["data/augmentation"]
    assert spec["default"] == "" and len(spec["rules"]) == 2
    assert spec["rules"][0]["to"] == "data/hard-negative-mining"


def test_resolve_split_picks_first_matching_rule_from_context():
    splits = {"data/aug": {"rules": [{"to": "data/hn", "when_any": ["hard"]},
                                     {"to": "data/syn", "when_any": ["synonym"]}], "default": "data/aug"}}
    # a run whose siblings mention 'hard' -> hn; 'synonym' -> syn; neither -> default
    assert resolve_split("data/aug", {"hard", "loss", "mnr"}, splits) == "data/hn"
    assert resolve_split("data/aug", {"synonym"}, splits) == "data/syn"
    assert resolve_split("data/aug", {"loss"}, splits) == "data/aug"
    assert resolve_split("unrelated", {"hard"}, splits) == "unrelated"      # not in the split -> unchanged


def test_split_hyphenated_trigger_matches_unicode_word_tokens():
    splits = {"data/aug": {"rules": [{"to": "data/hn", "when_any": ["hard-negative"]}],
                             "default": "data/aug"}}
    assert resolve_split("data/aug", {"loss/hard-negative"}, splits) == "data/hn"
    assert resolve_split("data/aug", {"hard"}, splits) == "data/aug"  # all phrase tokens are required


def test_split_rejects_no_progress_and_empty(tmp_path):
    with pytest.raises(ValueError):        # a rule re-tagging the source to itself is pointless
        record_concept_split(str(tmp_path), from_concept="x", rules=[{"to": "x", "when_any": ["a"]}])
    with pytest.raises(ValueError):        # no rule + no (real) default is inert
        record_concept_split(str(tmp_path), from_concept="x", rules=[], default="")
    with pytest.raises(ValueError):        # no rule + bare identity default is also inert
        record_concept_split(str(tmp_path), from_concept="x", rules=[], default="x")


def test_split_rejects_purged_rule_and_default_targets(tmp_path):
    record_concept_alias(str(tmp_path), from_concept="retired", to_concept="")
    with pytest.raises(ValueError, match="target .* purged"):
        record_concept_split(
            str(tmp_path), from_concept="coarse",
            rules=[{"to": "retired", "when_any": ["legacy"]}], default="live-default")
    with pytest.raises(ValueError, match="target .* purged"):
        record_concept_split(
            str(tmp_path), from_concept="other-coarse",
            rules=[{"to": "live-target", "when_any": ["fresh"]}], default="retired")
    assert load_concept_splits(str(tmp_path)) == {}
    assert concept_governance_revision(str(tmp_path), "splits") == 0


def test_split_rejects_aliased_source_and_target_that_resolves_back_to_source(tmp_path):
    record_concept_alias(
        str(tmp_path), from_concept="x", to_concept="y",
        expected_revision=0, expected_governance_revision=0,
    )
    with pytest.raises(ValueError, match="source 'x' is aliased to 'y'"):
        record_concept_split(
            str(tmp_path), from_concept="x",
            rules=[{"to": "fine", "when_any": ["match"]}],
            expected_revision=0, expected_governance_revision=1,
        )
    with pytest.raises(ValueError, match="resolves to its source 'y'"):
        record_concept_split(
            str(tmp_path), from_concept="y",
            rules=[{"to": "x", "when_any": ["match"]}],
            expected_revision=0, expected_governance_revision=1,
        )
    assert load_concept_splits(str(tmp_path)) == {}
    assert concept_governance_revision(str(tmp_path), "splits") == 0
    assert concept_governance_global_revision(str(tmp_path)) == 1


def test_split_rejects_purged_source_without_appending(tmp_path):
    record_concept_alias(
        str(tmp_path), from_concept="retired", to_concept="",
        expected_revision=0, expected_governance_revision=0,
    )
    with pytest.raises(ValueError, match="source 'retired' is purged"):
        record_concept_split(
            str(tmp_path), from_concept="retired",
            rules=[{"to": "fine", "when_any": ["match"]}],
            expected_revision=0, expected_governance_revision=1,
        )
    assert concept_governance_revision(str(tmp_path), "splits") == 0
    assert concept_governance_global_revision(str(tmp_path)) == 1


def test_global_governance_cas_is_cross_ledger_and_retry_precedes_it(tmp_path):
    first = record_concept_alias(
        str(tmp_path), from_concept="x", to_concept="y", expected_revision=0,
        expected_governance_revision=0, action_id="global-retry", at="first",
    )
    retry = record_concept_alias(
        str(tmp_path), from_concept="x", to_concept="y", expected_revision=0,
        expected_governance_revision=0, action_id="global-retry", at="retry",
    )
    assert retry == first
    assert first["governance_revision"] == concept_governance_global_revision(str(tmp_path)) == 1

    with pytest.raises(ConceptGovernanceGlobalConflict) as stale:
        record_concept_split(
            str(tmp_path), from_concept="coarse",
            rules=[{"to": "fine", "when_any": ["match"]}], expected_revision=0,
            expected_governance_revision=0, action_id="stale-cross-ledger",
        )
    assert stale.value.expected == 0 and stale.value.actual == 1
    assert concept_governance_revision(str(tmp_path), "splits") == 0


def test_action_id_is_per_ledger_and_alias_retry_survives_later_split(tmp_path):
    alias = record_concept_alias(
        str(tmp_path), from_concept="x", to_concept="y", expected_revision=0,
        expected_governance_revision=0, action_id="endpoint-scoped-action",
    )
    split = record_concept_split(
        str(tmp_path), from_concept="coarse",
        rules=[{"to": "fine", "when_any": ["match"]}], expected_revision=0,
        expected_governance_revision=1, action_id="endpoint-scoped-action",
    )
    retry = record_concept_alias(
        str(tmp_path), from_concept="x", to_concept="y", expected_revision=0,
        expected_governance_revision=0, action_id="endpoint-scoped-action",
    )
    assert retry == alias
    assert alias["governance_revision"] == 1 and split["governance_revision"] == 2
    assert concept_governance_global_revision(str(tmp_path)) == 2


def test_split_target_validation_is_atomic_with_concurrent_purge(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    start = Barrier(3)

    def split():
        start.wait(timeout=5)
        try:
            return record_concept_split(
                str(tmp_path), from_concept="coarse",
                rules=[{"to": "target", "when_any": ["match"]}])
        except ValueError as exc:
            return exc

    def purge():
        start.wait(timeout=5)
        return record_concept_alias(str(tmp_path), from_concept="target", to_concept="")

    with ThreadPoolExecutor(max_workers=2) as pool:
        split_future, purge_future = pool.submit(split), pool.submit(purge)
        start.wait(timeout=5)
        split_result, purge_result = split_future.result(timeout=10), purge_future.result(timeout=10)

    assert purge_result["action"] == "purge"
    if isinstance(split_result, ValueError):
        assert "target" in str(split_result) and "purged" in str(split_result)
        assert concept_governance_global_revision(str(tmp_path)) == purge_result["governance_revision"] == 1
    else:
        # The only legal both-success order is split first, followed by the explicit purge. Receipts make
        # that cross-ledger linearization visible instead of leaving two incomparable local revisions.
        assert split_result["governance_revision"] < purge_result["governance_revision"]
        assert concept_governance_global_revision(str(tmp_path)) == purge_result["governance_revision"] == 2


def test_canonicalize_applies_split_then_alias():
    splits = {"data/aug": {"rules": [{"to": "data/hn", "when_any": ["hard"]}], "default": "data/aug"}}
    aliases = {"data/hn": "data/hard-negative-mining"}
    # 'data/aug' sees sibling 'data/hard-neg' (token 'hard') -> split to data/hn -> alias to the canonical
    got = canonicalize_concepts(["data/aug", "data/hard-neg"], aliases=aliases, splits=splits)
    assert "data/hard-negative-mining" in got


def test_canonicalize_aliases_source_before_split_and_target_after_split():
    aliases = {"augmentation": "data/aug", "data/hn": "data/hard-negative-mining"}
    splits = {"data/aug": {"rules": [{"to": "data/hn", "when_any": ["hard-negative"]}],
                            "default": "data/aug"}}
    got = canonicalize_concepts(["augmentation", "loss/hard-negative"], aliases=aliases, splits=splits)
    assert "data/hard-negative-mining" in got and "data/aug" not in got


def test_split_clear_restores_unsplit_source_with_independent_revision(tmp_path):
    rec = record_concept_split(str(tmp_path), from_concept="data/aug",
                               rules=[{"to": "data/hn", "when_any": ["hard"]}], expected_revision=0)
    assert rec["revision"] == 1 and concept_governance_revision(str(tmp_path), "splits") == 1
    clear = clear_concept_split(str(tmp_path), from_concept="data/aug", expected_revision=1)
    assert clear["revision"] == 2 and "data/aug" not in load_concept_splits(str(tmp_path))


def test_split_trigger_cannot_match_its_own_source_slug():
    splits = {"data/augmentation": {
        "rules": [{"to": "data/synthetic", "when_any": ["augmentation"]}],
        "default": "data/augmentation",
    }}
    # No sibling mentions augmentation: the source token itself must not trigger a retag.
    assert canonicalize_concepts(["data/augmentation"], splits=splits) == ["data/augmentation"]


def test_split_trigger_cannot_match_duplicate_or_alias_of_its_source():
    splits = {"data/augmentation": {
        "rules": [{"to": "data/synthetic", "when_any": ["augmentation"]}],
        "default": "data/augmentation",
    }}
    aliases = {"augmentation": "data/augmentation"}
    # Canonicalizing the sibling before filtering used to turn the alias into a self-trigger.
    assert canonicalize_concepts(
        ["data/augmentation", "augmentation"], aliases=aliases, splits=splits,
    ) == ["data/augmentation"]


def test_overview_re_tags_split_by_sibling_context(tmp_path):
    record_concept_split(str(tmp_path), from_concept="data/aug",
                         rules=[{"to": "data/hard-neg", "when_any": ["hard"]}])
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
    # Two raw concepts in one capsule both alias to 'hard-neg' -> one run-row, not two.
    caps = [build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                                  concepts=["hn", "hnm"], concept_outcomes={"hn": 0.9})]
    ov = portfolio_concept_overview(caps, aliases={"hn": "hard-neg", "hnm": "hard-neg"})
    hn = [e for e in ov["concepts"] if e["concept"] == "hard-neg"][0]
    assert hn["n_runs"] == 1 and len(hn["runs"]) == 1 and hn["runs"][0]["metric"] == 0.9


def test_overview_always_uses_versioned_normalization_without_governance_maps():
    caps = [build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                                  concepts=[" Hard  Neg ", "hard neg"],
                                  concept_outcomes={" Hard  Neg ": 0.9})]
    ov = portfolio_concept_overview(caps)
    assert ov["n_concepts"] == 1 and ov["concepts"][0]["concept"] == "hard neg"
    assert ov["runs"][0]["concepts"] == ["hard neg"] and ov["runs"][0]["n_concepts"] == 1


def test_cli_concept_split(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    r = CliRunner().invoke(app, ["concept-split", str(tmp_path), "data/aug",
                                 "--rule", "data/hard-neg:hard,negative"])
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


def test_append_governance_validation_aborts_append_atomically(tmp_path):
    # Cycle rejection for record_concept_alias runs as validation UNDER the append lock, so a concurrent
    # writer cannot slip a cycle-closing edge past a pre-append snapshot. A validation failure must abort
    # the append without leaving a partial record.
    from looplab.engine.concept_registry import _append_governance
    p = tmp_path / "gov.jsonl"
    p.write_text('{"from": "x", "to": "y"}\n', encoding="utf-8")
    before = p.read_text(encoding="utf-8")

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        _append_governance(
            p, {"from": "a", "to": "b"},
            validate=lambda: (_ for _ in ()).throw(_Boom()),
        )
    assert p.read_text(encoding="utf-8") == before
    _append_governance(p, {"from": "c", "to": "d"}, validate=lambda: None)
    assert '"c"' in p.read_text(encoding="utf-8")


def test_governance_append_survives_a_torn_jsonl_tail(tmp_path):
    from looplab.engine.concept_registry import (concept_governance_revision, load_concept_aliases,
                                                 record_concept_alias)
    p = tmp_path / "concept_aliases.jsonl"
    p.write_text('{"action":"set","from":"partial', encoding="utf-8")

    receipt = record_concept_alias(str(tmp_path), from_concept="b", to_concept="c",
                                   expected_revision=0, action_id="tail-retry")

    assert receipt["revision"] == 1
    assert load_concept_aliases(str(tmp_path)) == {"b": "c"}
    assert concept_governance_revision(str(tmp_path), "aliases") == 1
    assert record_concept_alias(str(tmp_path), from_concept="b", to_concept="c", expected_revision=0,
                                action_id="tail-retry") == receipt
