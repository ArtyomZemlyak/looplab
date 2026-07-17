"""PART V §22.4 Phase 2: the assistant's cross-run concept-taxonomy editing tools (ConceptGovernanceTools).

Reads always; every mutation is mode+approver gated and lands on the SAME append-only, reversible
governance ledger the /cross-run endpoints use. Never raises from execute (ToolProvider contract)."""
from __future__ import annotations

from looplab.engine.concept_registry import (load_concept_aliases, load_concept_splits, resolve_slug)
from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
from looplab.tools.concept_tools import ConceptGovernanceTools


def _seed_portfolio(mem, concepts):
    # require_existing=True checks the observed snapshot (concepts present in valid capsules).
    ConceptCapsuleStore(mem / "concept_capsules.jsonl").add(build_concept_capsule(
        run_id="r1", fingerprint=["k"], direction="max", concepts=concepts,
        concept_outcomes={c: 0.5 for c in concepts}))


def _auto(mem):
    return ConceptGovernanceTools(mem, mode="auto")     # auto: consequential edits apply without asking


def test_specs_read_only_in_plan_mutations_in_mutating_mode(tmp_path):
    plan = {s["function"]["name"] for s in ConceptGovernanceTools(tmp_path, mode="plan").specs()}
    assert plan == {"concept_taxonomy"}                                  # inspect only
    full = {s["function"]["name"] for s in ConceptGovernanceTools(tmp_path, mode="auto").specs()}
    assert full == {"concept_taxonomy", "concept_merge", "concept_purge", "concept_split",
                    "concept_edit_clear"}
    assert ConceptGovernanceTools(None).specs() == []                    # no memory_dir -> no tools


def test_merge_persists_to_ledger_and_shows_in_taxonomy(tmp_path):
    _seed_portfolio(tmp_path, ["loss/a", "loss/b"])
    t = _auto(tmp_path)
    out = t.execute("concept_merge", {"from_concept": "loss/a", "to_concept": "loss/b"})
    assert "merged" in out and "loss/a" in out and "loss/b" in out
    assert load_concept_aliases(tmp_path).get("loss/a") == "loss/b"      # persisted
    assert "loss/a -> loss/b" in t.execute("concept_taxonomy", {})


def test_purge_then_clear_reverts(tmp_path):
    _seed_portfolio(tmp_path, ["loss/junk", "loss/keep"])
    # purge is a HIGH (removal) verb -> it asks even in auto, so give an allowing approver.
    t = ConceptGovernanceTools(tmp_path, mode="auto", approver=lambda a: "allow_once")
    t.execute("concept_purge", {"concept": "loss/junk"})
    assert resolve_slug("loss/junk", load_concept_aliases(tmp_path)) is None   # tombstoned -> dropped
    assert "loss/junk" in t.execute("concept_taxonomy", {})              # shown under Purged
    t.execute("concept_edit_clear", {"concept": "loss/junk", "kind": "alias"})
    assert resolve_slug("loss/junk", load_concept_aliases(tmp_path)) == "loss/junk"   # live again


def test_split_persists(tmp_path):
    _seed_portfolio(tmp_path, ["data/aug", "data/aug-image", "data/aug-text"])
    t = _auto(tmp_path)
    rules = [{"to": "data/aug-image", "when_any": ["image", "vision"]},
             {"to": "data/aug-text", "when_any": ["text", "nlp"]}]
    out = t.execute("concept_split", {"from_concept": "data/aug", "rules": rules})
    assert "split" in out and "data/aug" in load_concept_splits(tmp_path)


def test_plan_mode_refuses_every_mutation(tmp_path):
    _seed_portfolio(tmp_path, ["loss/a", "loss/b"])
    t = ConceptGovernanceTools(tmp_path, mode="plan")
    out = t.execute("concept_merge", {"from_concept": "loss/a", "to_concept": "loss/b"})
    assert "plan mode" in out.lower()
    assert load_concept_aliases(tmp_path) == {}                          # nothing written


def test_default_mode_asks_and_approver_deny_blocks(tmp_path):
    _seed_portfolio(tmp_path, ["loss/a", "loss/b"])
    t = ConceptGovernanceTools(tmp_path, mode="default", approver=lambda a: "deny")
    out = t.execute("concept_merge", {"from_concept": "loss/a", "to_concept": "loss/b"})
    assert "declined" in out.lower()
    assert load_concept_aliases(tmp_path) == {}                          # blocked before the ledger write


def test_approver_allow_once_lets_default_mode_edit(tmp_path):
    _seed_portfolio(tmp_path, ["loss/a", "loss/b"])
    t = ConceptGovernanceTools(tmp_path, mode="default", approver=lambda a: "allow_once")
    t.execute("concept_merge", {"from_concept": "loss/a", "to_concept": "loss/b"})
    assert load_concept_aliases(tmp_path).get("loss/a") == "loss/b"


def test_purge_is_high_asks_even_in_auto_but_merge_proceeds(tmp_path):
    # REVIEW: purge tombstones a concept out of ALL portfolio views -> HIGH (asks even in auto), matching
    # the delete_* convention. Merge/split are reversible MODIFY verbs -> CONSEQUENTIAL (auto proceeds).
    _seed_portfolio(tmp_path, ["loss/a", "loss/b"])
    auto_no_approver = ConceptGovernanceTools(tmp_path, mode="auto")   # default_approver denies
    out = auto_no_approver.execute("concept_purge", {"concept": "loss/a"})
    assert "declined" in out.lower()                                   # HIGH -> asked -> denied
    from looplab.engine.concept_registry import resolve_slug
    assert resolve_slug("loss/a", load_concept_aliases(tmp_path)) == "loss/a"   # not purged
    auto_no_approver.execute("concept_merge", {"from_concept": "loss/a", "to_concept": "loss/b"})
    assert load_concept_aliases(tmp_path).get("loss/a") == "loss/b"    # CONSEQUENTIAL merge proceeded


def test_reapply_after_clear_takes_effect_no_false_success(tmp_path):
    # REVIEW (action_id): merge A->B, clear A, merge A->B again MUST leave A merged (not silently no-op).
    # Omitting the registry idempotency token makes each call append fresh (last-write-wins in effect).
    _seed_portfolio(tmp_path, ["loss/a", "loss/b"])
    t = _auto(tmp_path)
    t.execute("concept_merge", {"from_concept": "loss/a", "to_concept": "loss/b"})
    t.execute("concept_edit_clear", {"concept": "loss/a", "kind": "alias"})
    from looplab.engine.concept_registry import resolve_slug
    assert resolve_slug("loss/a", load_concept_aliases(tmp_path)) == "loss/a"   # cleared
    out = t.execute("concept_merge", {"from_concept": "loss/a", "to_concept": "loss/b"})
    assert "merged" in out
    assert load_concept_aliases(tmp_path).get("loss/a") == "loss/b"    # re-applied, not a false-success no-op


def test_never_raises_on_bad_input(tmp_path):
    t = _auto(tmp_path)
    assert isinstance(t.execute("concept_merge", {"from_concept": "", "to_concept": ""}), str)
    assert isinstance(t.execute("concept_merge", {"from_concept": "x", "to_concept": "x"}), str)  # self-link
    assert "does not exist" in t.execute("concept_merge", {"from_concept": "ghost", "to_concept": "loss/a"})
    assert isinstance(t.execute("concept_split", {"from_concept": "loss/a", "rules": []}), str)
    assert isinstance(t.execute("concept_edit_clear", {"concept": "a", "kind": "bogus"}), str)
    assert isinstance(t.execute("nonexistent", {}), str)
    assert isinstance(ConceptGovernanceTools(None).execute("concept_taxonomy", {}), str)


def test_empty_taxonomy_reads_cleanly(tmp_path):
    assert "no taxonomy edits yet" in ConceptGovernanceTools(tmp_path, mode="auto").execute(
        "concept_taxonomy", {})
