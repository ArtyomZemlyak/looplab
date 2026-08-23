"""The Developer can look a lesson UP, and an in-node repair becomes one.

Two halves of the same measured gap on `runs/e5small-dr-unified-v4`, 2026-08-23.

READ SIDE. Across 10,455 tool calls, `search_lessons` fired TEN times — 9 in `propose`, 1 in
`deep_research`, ZERO in `card_build` / `plan` / `stages` / `inline_repair`. Not a preference: the
tool lives in `MemoryTools`, `MemoryTools` is composed in `agents/factory._shared_providers`, and
`repo_developer` assembles its own toolset. The role that WRITES THE CODE could read what the prior
renderer pushed at it and nothing else.

WRITE SIDE. The shared store held 23 lessons tagged `researcher`, 10 untagged and ZERO tagged
`developer` — because `pr["kind"] == "debug"` is the only thing mapped to that role, and a debug
pair needs a FAILED PARENT with a succeeding child. A star of improvements hanging off a WINNING
champion never produces that shape. Meanwhile nodes 3, 6, 8 and 9 each repaired the same mine-stage
failure in place, and none of it became a lesson.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from looplab.tools.memory_tools import MemoryTools


def _names(tool) -> list[str]:
    return [(s.get("function") or s).get("name") for s in tool.specs()]


# ---------------------------------------------------------------- read side: the role-scoped tool

def test_the_developer_gets_the_lessons_ledger_and_not_the_meta_notes(tmp_path):
    """Mirrors the line `_render_role_prior` already draws: meta-notes are research-flavoured and
    that renderer withholds them from the Developer. Offering them through a TOOL would reopen a
    deliberate decision sideways instead of overturning it."""
    (tmp_path / "lessons.jsonl").write_text("", encoding="utf-8")
    assert _names(MemoryTools(str(tmp_path), role="developer")) == ["search_lessons"]
    assert _names(MemoryTools(str(tmp_path), role="researcher")) == ["search_lessons", "recall_notes"]


def test_meta_notes_are_refused_and_not_merely_hidden(tmp_path):
    """`specs` and `execute` are two different promises. A name that never appeared in this role's
    spec list can still arrive from a replayed transcript or a merged toolset."""
    (tmp_path / "meta_notes.jsonl").write_text("", encoding="utf-8")
    out = MemoryTools(str(tmp_path), role="developer").execute("recall_notes", {"query": "x"})
    assert "not available to this role" in out


def _store(tmp_path, rows):
    import json
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return MemoryTools(str(tmp_path), role="developer")


def test_a_lesson_for_the_OTHER_role_stays_out_and_an_untagged_one_is_shared(tmp_path):
    """The same predicate the prior renderer uses, so the pull and the push cannot disagree about
    what a role may know. The row that cost three nodes is UNTAGGED — if untagged were filtered the
    fix would deliver nothing."""
    tool = _store(tmp_path, [
        {"statement": "widget alpha is for researchers only", "role": "researcher"},
        {"statement": "widget beta is a developer fix", "role": "developer"},
        {"statement": "widget gamma is untagged and therefore shared"},
    ])
    out = tool.execute("search_lessons", {"query": "widget"})
    assert "beta" in out and "gamma" in out
    assert "alpha" not in out, out


def test_the_local_role_spellings_match_the_canonical_ones():
    """`tools` avoids importing `engine` at module scope, so the two names are spelled twice. This
    is what makes that duplication checked rather than trusted."""
    from looplab.engine.lessons_priors import LESSON_ROLE_DEVELOPER, LESSON_ROLE_RESEARCHER
    from looplab.tools import memory_tools as mt
    assert mt._ROLE_DEVELOPER == LESSON_ROLE_DEVELOPER
    assert mt._ROLE_RESEARCHER == LESSON_ROLE_RESEARCHER


def test_the_developer_toolset_actually_carries_it(tmp_path):
    """THE WIRING. Every assertion above would stay green with the provider never composed — which
    is exactly the state this fixes."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper

    dev = object.__new__(LLMRepoDeveloper)
    dev._cross_run_read_tools = True
    dev._cross_run_memory_dir = str(tmp_path)
    dev._memory_state = None
    dev._editables = []
    dev._probe = False
    (tmp_path / "lessons.jsonl").write_text("", encoding="utf-8")

    dev._probe_repo_spec = None
    dev._probe_timeout_s = 60.0
    extra = dev._scout_tools(None)
    offered = set()
    for provider in extra:
        for spec in getattr(provider, "specs", lambda: [])():
            offered.add((spec.get("function") or spec).get("name"))
    assert "search_lessons" in offered, sorted(offered)
    assert "recall_notes" not in offered, "meta-notes must not reach the Developer"


# --------------------------------------------------------- write side: an in-node repair is a pair

def _node(nid, *, metric=None, repairs=0, parents=(), status=None, failed_stage=None):
    from looplab.core.models import NodeStatus
    return SimpleNamespace(id=nid, metric=metric, repairs=repairs, parent_ids=list(parents),
                           tombstoned=False, status=status or NodeStatus.evaluated,
                           failed_stage=failed_stage, error_reason=None,
                           idea=SimpleNamespace(params={}, rationale="r"), code="")


def _state(nodes, direction="max"):
    # `trust_gate="audit"` and empty violation maps: `select_comparison_pairs` runs the same
    # unreliable-metric predicate the selector does, and under `audit` nothing is excluded.
    return SimpleNamespace(nodes={n.id: n for n in nodes}, direction=direction,
                           aborted_nodes=set(), task_id="t", run_id="r",
                           trust_gate="audit", reward_hacks=[], holdout_fraction=0.0)


def test_a_node_repaired_in_place_becomes_a_developer_pair():
    """THE PROPERTY. No failed parent exists — the node failed a stage, fixed itself, then scored —
    and that is precisely 'what code change fixed a crash'."""
    from looplab.engine.memory import select_comparison_pairs

    st = _state([_node(0, metric=0.5, repairs=2, failed_stage="mine")])
    pairs = select_comparison_pairs(st, k=5)
    assert [(p["kind"], p["a"], p["b"]) for p in pairs] == [("debug", 0, 0)]


def test_a_node_that_never_needed_repair_produces_no_such_pair():
    """The other direction, so the rule is 'was repaired', not 'exists'."""
    from looplab.engine.memory import select_comparison_pairs

    st = _state([_node(0, metric=0.5, repairs=0)])
    assert select_comparison_pairs(st, k=5) == []


def test_the_star_lineage_that_produced_zero_developer_lessons_now_produces_them():
    """The shape of the live run: everything improves from a WINNING champion, so no child ever has
    a failed parent and the old rule yielded nothing for this role."""
    from looplab.engine.memory import select_comparison_pairs

    champion = _node(3, metric=0.79, repairs=1, failed_stage="train")
    child = _node(6, metric=0.78, repairs=1, parents=[3], failed_stage="mine")
    pairs = select_comparison_pairs(_state([champion, child]), k=10)
    kinds = [(p["kind"], p["a"], p["b"]) for p in pairs]
    assert ("debug", 3, 3) in kinds and ("debug", 6, 6) in kinds
    assert any(k == "solution" for k, _, _ in kinds), "the science pair must survive alongside"
