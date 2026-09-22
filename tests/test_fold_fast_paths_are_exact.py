"""The fold's three fast paths are EXACT, not approximately equal (review 2026-09-22, EVT-04a).

The review measured the fold O(n^2) over a run and named three quick wins, each of which removes
work without changing a single folded byte:

* `replay._on_llm_usage` re-ran `_clean_llm_totals` over the WHOLE accumulated ledger and over a
  full copy of its own payload on every row; the ledger it writes is already clean, so it is now
  sanitized once (`_FoldCtx.llm_cost_clean`) and each row contributes only its six columns;
* `replay._on_literature_retrieved` rebuilt the set of folded paper ids from the whole
  `st.literature` list on every row; the set now lives on the fold ctx, beside its one writer;
* `card_ledger._fold_merged_cards` deep-copied EVERY card as soon as one alias existed; a
  single-member group now reuses its card (`card_ledger._merge_group_base`).

"Behaviour-identical" is a claim, so it is DRIVEN: each fast path is folded against the VERBATIM
implementation it replaced (reinstalled through the same seams — the `_HANDLERS` table and the
`_merge_group_base` module global) over synthetic logs aimed at every branch it touches, over the
incremental `FoldCursor` at every prefix, and over the golden run log.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.core.models import Event, hypothesis_id
from looplab.events import card_ledger, replay
from looplab.events.eventstore import EventStore
from looplab.events.replay import FoldCursor, fold
from looplab.events.types import EV_LITERATURE_RETRIEVED, EV_LLM_USAGE

DATA = Path(__file__).resolve().parent / "data"


# ------------------------------------------------------ the implementations replaced, VERBATIM
def _reference_on_llm_usage(st, e, d, ctx):
    usage_id = d.get("usage_id")
    if isinstance(usage_id, str) and usage_id:
        if usage_id in ctx.llm_usage_ids:
            ctx.llm_usage_seen = True
            return
        ctx.llm_usage_ids.add(usage_id)
    base = replay._clean_llm_totals(st.llm_cost)
    delta = replay._clean_llm_totals(d)
    delta["priced_calls"] = replay._row_priced_calls(d, delta)
    base["cost"] = min(replay._MAX_LLM_COST, float(base["cost"]) + float(delta["cost"]))
    for key in ("calls", "priced_calls", "prompt_tokens", "completion_tokens", "total_tokens"):
        base[key] = min(replay._MAX_LLM_COUNTER, int(base[key]) + int(delta[key]))
    st.llm_cost = base
    ctx.llm_usage_seen = True


def _reference_on_literature_retrieved(st, e, d, ctx):
    from looplab.core.advisory_payloads import sanitize_literature_items
    seen = {row.get("id") for row in st.literature if isinstance(row, dict)}
    at_node = d.get("at_node") if type(d.get("at_node")) is int else None
    for item in sanitize_literature_items(d.get("items"), env=replay._FOLD_REDACTION_ENV):
        if item["id"] not in seen:
            seen.add(item["id"])
            st.literature.append({**item, "at_node": at_node})


def _reference_merge_group_base(card, members):
    return card.model_copy(deep=True)


def _dump(state) -> dict:
    """The whole folded state, plus the two projections `model_dump` excludes or nests."""
    return {"state": state.model_dump_json(), "llm_cost": state.llm_cost,
            "literature": state.literature,
            "cards": {cid: c.model_dump(mode="json") for cid, c in state.cards.items()}}


def _fast_and_reference(events, monkeypatch) -> tuple[dict, dict]:
    fast = _dump(fold(events))
    monkeypatch.setitem(replay._HANDLERS, EV_LLM_USAGE, _reference_on_llm_usage)
    monkeypatch.setitem(replay._HANDLERS, EV_LITERATURE_RETRIEVED,
                        _reference_on_literature_retrieved)
    monkeypatch.setattr(card_ledger, "_merge_group_base", _reference_merge_group_base)
    reference = _dump(fold(events))
    monkeypatch.undo()
    return fast, reference


def _events(rows) -> list[Event]:
    head = [("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})]
    return [Event(seq=i, ts=float(i + 1), type=t, data=d)
            for i, (t, d) in enumerate(head + list(rows))]


def _lit(n: int, title: str) -> dict:
    return {"id": "lit-" + format(n, "024x"), "title": title, "abstract_sha256": "b" * 64,
            "abstract_chars": 3, "query": "q", "tool": "arxiv_search"}


def _node(nid: int, statement: str, card_id: str | None = None, metric: float = 0.5):
    idea = {"operator": "draft", "params": {"x": nid}, "hypothesis": statement}
    if card_id is not None:
        idea["card_id"] = card_id
    return [("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                              "idea": idea, "code": "", "files": {}, "generation": 0}),
            ("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric,
                                "eval_seconds": 1.0, "violations": [], "trials": [],
                                "extra_metrics": {}, "stdout_tail": ""})]


# ------------------------------------------------------------------------------ the logs
USAGE_LOGS = {
    "usage-only": [
        ("llm_usage", {"usage_id": "u1", "calls": 1, "priced_calls": 1, "cost": 0.25,
                       "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}),
        ("llm_usage", {"usage_id": "u1", "calls": 1, "cost": 0.25}),          # a retried append
        ("llm_usage", {"calls": 2, "cost": 0.5}),           # legacy: no id, priced from cost
        ("llm_usage", {"calls": 2, "cost": 0.0}),           # legacy: unpriced
    ],
    "legacy-summary-base-then-deltas": [
        ("llm_cost", {"cost": 1.5, "calls": 3, "prompt_tokens": 10, "note": "legacy summary",
                      "by_role": {"developer": 2}}),
        ("llm_cost", {"cost": 2.0, "calls": 4, "priced_calls": 1}),       # later summary wins
        ("llm_usage", {"usage_id": "u1", "calls": 1, "cost": 0.25}),
        ("llm_cost", {"cost": 99.0, "calls": 99}),                        # ignored once deltas run
        ("llm_usage", {"usage_id": "u2", "calls": 3, "cost": 0.75, "priced_calls": 2}),
    ],
    "hostile-values": [
        ("llm_usage", {"usage_id": "u1", "calls": True, "cost": float("nan"),
                       "prompt_tokens": -5, "completion_tokens": "7", "total_tokens": 10 ** 30,
                       "extra": "never added"}),
        ("llm_usage", {"usage_id": "u2", "cost": float("inf"), "calls": 1}),
        ("llm_usage", {"usage_id": "u3", "cost": 1e308, "calls": 1}),
        ("llm_usage", {"usage_id": "u4", "cost": 1e308, "calls": 1}),      # the cost cap
        ("llm_usage", {"usage_id": "u5", "cost": -3.0, "calls": 2 ** 62}),
        ("llm_usage", {"usage_id": "u6", "cost": 0.1, "calls": 2 ** 62}),  # the counter cap
        ("llm_usage", {"usage_id": 7, "calls": 1, "cost": 0.1}),            # a non-str id
    ],
}

LITERATURE_LOG = [
    ("literature_retrieved", {"at_node": 0, "items": [_lit(1, "A"), _lit(2, "B"),
                                                      _lit(1, "A again")]}),
    ("literature_retrieved", {"at_node": 1, "items": [_lit(2, "B"), _lit(3, "C")]}),
    ("literature_retrieved", {"at_node": "x", "items": [_lit(4, "D"), {"id": "bad"}, "junk",
                                                        _lit(5, "")]}),
    ("literature_retrieved", {"items": "not a list"}),
    ("research_completed", {"memo": {"summary": "s", "literature": [_lit(6, "E")]}}),
    ("literature_retrieved", {"at_node": 2, "items": [_lit(6, "E"), _lit(3, "C again")]}),
]

MERGE_LOG = [
    ("card_added", {"id": "card-a", "statement": "direction A"}),
    ("card_added", {"id": "card-b", "statement": "direction B"}),
    ("card_added", {"id": "card-solo", "statement": "a card merged into a ghost"}),
    ("card_added", {"id": "card-lonely", "statement": "a card no merge touches"}),
    *_node(1, "direction A", "card-a", 0.4),
    *_node(2, "direction B", "card-b", 0.6),
    *_node(3, "a card merged into a ghost", "card-solo", 0.5),
    *_node(4, "a card no merge touches", "card-lonely", 0.7),
    *_node(5, "a legacy hypothesis one", None, 0.3),
    *_node(6, "a legacy hypothesis two", None, 0.8),
    *_node(7, "a legacy hypothesis three", None, 0.2),
    ("card_merged", {"canonical": "card-b", "aliases": ["card-a"], "statement": "A or B"}),
    # a single-member group whose canonical is NOT materialized: the one member is RENAMED
    ("card_merged", {"canonical": "card-ghost", "aliases": ["card-solo"]}),
    ("hypothesis_merged", {"canonical": hypothesis_id("a legacy hypothesis one"),
                           "aliases": [hypothesis_id("a legacy hypothesis two")],
                           "statement": "one and two"}),
    ("card_dropped", {"id": "card-lonely", "reason": "operator stop", "dropped_by": "operator"}),
]


# ------------------------------------------------------------------------------ the checks
@pytest.mark.parametrize("name", sorted(USAGE_LOGS))
def test_the_llm_ledger_is_folded_to_the_same_bytes(name, monkeypatch):
    """MUTATION: start `ctx.llm_cost_clean` True — skipping the one sanitizing pass the FIRST row
    needs — and a log whose first ledger row is a delta has no clean ledger to add to."""
    fast, reference = _fast_and_reference(_events(USAGE_LOGS[name]), monkeypatch)
    assert fast == reference
    assert fast["llm_cost"], "precondition: the ledger was written"


def test_the_literature_record_is_folded_to_the_same_bytes(monkeypatch):
    fast, reference = _fast_and_reference(_events(LITERATURE_LOG), monkeypatch)
    assert fast == reference
    assert [row["title"] for row in fast["literature"]] == ["A", "B", "C", "D", "E"]


def test_the_card_merge_fold_is_the_same_bytes_with_and_without_the_copy(monkeypatch):
    """The copy is the ONLY thing the fast path removes, so the two folds must agree byte for byte
    — and the singleton's merge WORK must still happen on the reused card: renamed onto the
    canonical id, its evidence and aliases rewritten. MUTATION: short-circuit a single-member group
    past that work (`folded[tid] = card; continue`) and the renamed singleton keeps the id
    `card-solo` under the key `card-ghost`."""
    fast, reference = _fast_and_reference(_events(MERGE_LOG), monkeypatch)
    assert fast == reference
    cards = fast["cards"]
    assert "card-b" in cards and "card-a" not in cards, "precondition: the two-card merge ran"
    assert cards["card-b"]["evidence"] == [1, 2] and cards["card-b"]["aliases"] == ["card-a"]
    ghost = cards.get("card-ghost")
    assert ghost is not None and "card-solo" not in cards, "…and the renaming singleton"
    assert ghost["id"] == "card-ghost" and ghost["aliases"] == ["card-solo"]
    assert ghost["evidence"] == [3]
    legacy = cards[hypothesis_id("a legacy hypothesis one")]
    assert legacy["evidence"] == [5, 6]
    assert cards["card-lonely"]["status"] == "dropped"


def test_every_incremental_snapshot_matches_the_reference_fold_of_its_prefix(monkeypatch):
    """The ctx-held state (`llm_cost_clean`, `literature_ids`) persists across `FoldCursor`
    extends and must stay in step with the accumulator exactly as a fresh fold would."""
    rows = (USAGE_LOGS["legacy-summary-base-then-deltas"] + LITERATURE_LOG
            + USAGE_LOGS["usage-only"])
    events = _events(rows)
    cursor = FoldCursor()
    snapshots = []
    for event in events:
        cursor.extend([event])
        snapshots.append(_dump(cursor.snapshot()))
    monkeypatch.setitem(replay._HANDLERS, EV_LLM_USAGE, _reference_on_llm_usage)
    monkeypatch.setitem(replay._HANDLERS, EV_LITERATURE_RETRIEVED,
                        _reference_on_literature_retrieved)
    for index, snapshot in enumerate(snapshots):
        assert snapshot == _dump(fold(events[:index + 1])), f"prefix {index + 1}"


def test_the_golden_run_log_folds_to_the_same_bytes(monkeypatch):
    events = EventStore(DATA / "golden_run_events.jsonl").read_all()
    assert events, "golden log missing/empty"
    fast, reference = _fast_and_reference(events, monkeypatch)
    assert fast == reference
