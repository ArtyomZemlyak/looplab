"""AGENTIC concept-taxonomy steward (§21.20.13/§22.4) — the LLM counterpart of the operator's manual
merge/split/purge. Pins: the LLM PROPOSES, deterministic guardrails validate (only in-vocabulary,
reversible), the apply path records through the SAME governance writes, and it degrades to an empty
curation (no client / bad output) so it never blocks or corrupts the graph.
"""
from __future__ import annotations

from looplab.engine.concept_steward import (
    apply_concept_curation, curation_is_empty, propose_concept_curation, steward_concepts,
)
from looplab.engine.concept_registry import load_concept_aliases, load_concept_splits
from looplab.engine.memory import build_concept_capsule, ConceptCapsuleStore, portfolio_concept_overview


class _Client:
    """A fake LLM: `complete_tool` returns a fixed curation dict; `complete_text` unused."""
    def __init__(self, curation):
        self._c = curation

    def complete_tool(self, messages, json_schema):
        return self._c

    def complete_text(self, messages):
        return "{}"


def _overview(*concepts_per_run):
    caps = [build_concept_capsule(run_id=f"r{i}", fingerprint=["k"], direction="max",
                                  concepts=list(cs), concept_outcomes={})
            for i, cs in enumerate(concepts_per_run)]
    return portfolio_concept_overview(caps), caps


def test_no_client_is_empty_curation():
    ov, _ = _overview(["a"], ["b"])
    assert curation_is_empty(propose_concept_curation(ov, None))


def test_llm_merge_proposal_is_validated_in_vocabulary():
    ov, _ = _overview(["data/hn"], ["data/hard-negative-mining"], ["loss/mnr"])
    client = _Client({"merges": [{"from_concept": "data/hn", "to_concept": "data/hard-negative-mining"},
                                 {"from_concept": "not-in-graph", "to_concept": "data/hard-negative-mining"}],
                      "splits": [], "purges": []})
    prop = propose_concept_curation(ov, client)
    # the in-vocabulary merge survives; the one referencing an unknown source is dropped (guardrail)
    assert len(prop["merges"]) == 1 and prop["merges"][0]["from_concept"] == "data/hn"


def test_llm_split_and_purge_validated():
    ov, _ = _overview(["data/augmentation"], ["junk"])
    client = _Client({"merges": [], "purges": ["junk", "unknown-noise"],
                      "splits": [{"from_concept": "data/augmentation",
                                  "rules": [{"to": "data/hard-neg", "when_any": ["hard"]},
                                            {"to": "data/augmentation", "when_any": ["x"]}],  # self-target dropped
                                  "default": "data/augmentation"}]})
    prop = propose_concept_curation(ov, client)
    assert prop["purges"] == [{"from_concept": "junk"}]              # unknown purge dropped
    assert len(prop["splits"]) == 1 and len(prop["splits"][0]["rules"]) == 1   # self-target rule dropped


def test_bad_output_degrades_to_empty():
    ov, _ = _overview(["a"], ["b"])

    class _Boom:
        def complete_tool(self, m, j):
            raise RuntimeError("model exploded")

        def complete_text(self, m):
            raise RuntimeError("no")

    assert curation_is_empty(propose_concept_curation(ov, _Boom()))   # never raises


def test_apply_records_through_governance_writes(tmp_path):
    curation = {"merges": [{"from_concept": "data/hn", "to_concept": "data/hard-neg"}],
                "splits": [{"from_concept": "data/aug", "rules": [{"to": "data/syn", "when_any": ["synonym"]}],
                            "default": "data/aug"}],
                "purges": [{"from_concept": "junk"}]}
    rc = apply_concept_curation(str(tmp_path), curation, by="steward")
    assert len(rc["applied"]) == 3 and not rc["skipped"]
    aliases = load_concept_aliases(str(tmp_path))
    assert aliases["data/hn"] == "data/hard-neg" and aliases["junk"] == "\x00purged"
    assert load_concept_splits(str(tmp_path))["data/aug"]["rules"][0]["to"] == "data/syn"


def test_apply_skips_invalid_without_sinking_the_batch(tmp_path):
    # a cycle-closing merge is rejected at record time; the OTHER merge still lands
    apply_concept_curation(str(tmp_path), {"merges": [{"from_concept": "a", "to_concept": "b"}]})
    rc = apply_concept_curation(str(tmp_path), {"merges": [
        {"from_concept": "b", "to_concept": "a"},          # cycle -> skipped
        {"from_concept": "c", "to_concept": "d"}]})         # valid -> applied
    assert any(s["from_concept"] == "b" for s in rc["skipped"])
    assert any(a["from_concept"] == "c" for a in rc["applied"])


def test_steward_end_to_end_apply(tmp_path):
    s = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    s.add(build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                                concepts=["data/hn"], concept_outcomes={}))
    s.add(build_concept_capsule(run_id="r2", fingerprint=["k"], direction="max",
                                concepts=["data/hard-negative-mining"], concept_outcomes={}))
    client = _Client({"merges": [{"from_concept": "data/hn", "to_concept": "data/hard-negative-mining"}],
                      "splits": [], "purges": []})
    out = steward_concepts(str(tmp_path), client, apply=True)
    assert out["receipt"]["applied"] and load_concept_aliases(str(tmp_path))["data/hn"]
    # the overview now collapses the two under the canonical concept (the steward's merge took effect)
    ov = portfolio_concept_overview(s.all(), aliases=load_concept_aliases(str(tmp_path)))
    assert ov["n_concepts"] == 1


# --------------------------------------------------------------------------- #
# Engine wiring — store_concept_curation at finalize (gated, decoupled, best-effort)
# --------------------------------------------------------------------------- #

def _fake_engine_with_client(memory_dir, client, *, on=True, auto=False):
    from types import SimpleNamespace
    return SimpleNamespace(
        memory_dir=str(memory_dir),
        _cross_run_curation=on, _cross_run_curation_auto=auto,
        researcher=SimpleNamespace(client=client, inner=None, fallback=None), developer=None)


def _seed_two_mergeable(mem):
    s = ConceptCapsuleStore(mem / "concept_capsules.jsonl")
    s.add(build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                                concepts=["data/hn"], concept_outcomes={}))
    s.add(build_concept_capsule(run_id="r2", fingerprint=["k"], direction="max",
                                concepts=["data/hard-negative-mining"], concept_outcomes={}))


def test_finalize_curation_off_is_noop(tmp_path):
    from looplab.engine.lessons import LessonMemory
    from looplab.core.models import RunState
    mem = tmp_path / "mem"; mem.mkdir()
    _seed_two_mergeable(mem)
    eng = _fake_engine_with_client(mem, _Client({"merges": [], "splits": [], "purges": []}), on=False)
    LessonMemory(eng).store_concept_curation(RunState(run_id="r", task_id="t"))
    assert not (mem / "concept_curation_log.jsonl").exists()   # flag off -> nothing happens


def test_finalize_curation_logs_but_does_not_apply_by_default(tmp_path):
    from looplab.engine.lessons import LessonMemory
    from looplab.core.models import RunState
    mem = tmp_path / "mem"; mem.mkdir()
    _seed_two_mergeable(mem)
    client = _Client({"merges": [{"from_concept": "data/hn", "to_concept": "data/hard-negative-mining"}],
                      "splits": [], "purges": []})
    LessonMemory(_fake_engine_with_client(mem, client, on=True, auto=False)).store_concept_curation(
        RunState(run_id="r", task_id="t"))
    assert (mem / "concept_curation_log.jsonl").exists()       # proposal LOGGED for operator
    assert not (mem / "concept_aliases.jsonl").exists()        # but NOT applied (default = ratify path)


def test_finalize_curation_auto_applies(tmp_path):
    from looplab.engine.lessons import LessonMemory
    from looplab.core.models import RunState
    mem = tmp_path / "mem"; mem.mkdir()
    _seed_two_mergeable(mem)
    client = _Client({"merges": [{"from_concept": "data/hn", "to_concept": "data/hard-negative-mining"}],
                      "splits": [], "purges": []})
    LessonMemory(_fake_engine_with_client(mem, client, on=True, auto=True)).store_concept_curation(
        RunState(run_id="r", task_id="t"))
    assert load_concept_aliases(str(mem))["data/hn"] == "data/hard-negative-mining"   # auto-applied


def test_finalize_curation_toy_backend_is_noop(tmp_path):
    from looplab.engine.lessons import LessonMemory
    from looplab.core.models import RunState
    mem = tmp_path / "mem"; mem.mkdir()
    _seed_two_mergeable(mem)
    # no client (toy backend) -> reflect_client None -> steward degrades to empty, nothing logged
    eng = _fake_engine_with_client(mem, None, on=True)
    LessonMemory(eng).store_concept_curation(RunState(run_id="r", task_id="t"))
    assert not (mem / "concept_curation_log.jsonl").exists()


def test_cli_concept_steward(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from looplab.cli import app
    import looplab.cli as cli
    s = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    s.add(build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                                concepts=["data/hn"], concept_outcomes={}))
    s.add(build_concept_capsule(run_id="r2", fingerprint=["k"], direction="max",
                                concepts=["data/hard-negative-mining"], concept_outcomes={}))
    monkeypatch.setattr(cli, "make_llm_client", lambda *a, **k: _Client(
        {"merges": [{"from_concept": "data/hn", "to_concept": "data/hard-negative-mining"}],
         "splits": [], "purges": []}))
    r = CliRunner().invoke(app, ["concept-steward", str(tmp_path)])
    assert r.exit_code == 0 and "merge  'data/hn'" in r.stdout and "dry run" in r.stdout
    r2 = CliRunner().invoke(app, ["concept-steward", str(tmp_path), "--apply"])
    assert r2.exit_code == 0 and "applied 1" in r2.stdout
    assert load_concept_aliases(str(tmp_path))["data/hn"] == "data/hard-negative-mining"
