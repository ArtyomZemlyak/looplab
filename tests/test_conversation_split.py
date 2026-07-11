"""The chat trace splits the create_node WRAPPER into its real sub-stages (propose / implement / repair)
so each renders as its own coloured, collapsible band — the "Author node" container is not a stage the
reader cares about. Generations sitting directly under create_node (no sub-op) fall back to a single
create_node stage so nothing is dropped."""
from __future__ import annotations

from looplab.core.models import RunState
from looplab.events.traceview import build_conversation


def _gen(sid, parent, start, tid="T", nid=5):
    return {"span_id": sid, "parent_id": parent, "trace_id": tid, "name": "generation",
            "kind": "generation", "start": start,
            "attributes": {"node_id": nid, "input": [{"role": "user", "content": "q"}], "output": "a"}}


def _op(sid, parent, name, start, tid="T", nid=5):
    return {"span_id": sid, "parent_id": parent, "trace_id": tid, "name": name,
            "kind": "operation", "start": start, "attributes": {"node_id": nid}}


def test_create_node_splits_into_sub_stages():
    st = RunState(run_id="r", task_id="t")
    spans = [
        _op("root", None, "create_node", 0),
        _op("p", "root", "propose", 1), _gen("pg", "p", 2),
        _op("i", "root", "implement", 3), _gen("ig", "i", 4),
        _op("rp", "root", "repair", 5), _gen("rg", "rp", 6),
    ]
    labels = [s["label"] for s in build_conversation(st, spans, 5)["stages"]]
    assert labels == ["propose", "implement", "repair"]      # sub-stages, no "create_node" wrapper


def test_generation_directly_under_create_node_falls_back():
    st = RunState(run_id="r", task_id="t")
    spans = [_op("root", None, "create_node", 0), _gen("g", "root", 1)]   # no propose/implement wrapper
    labels = [s["label"] for s in build_conversation(st, spans, 5)["stages"]]
    assert labels == ["create_node"]                         # fallback — nothing dropped


def test_non_create_node_traces_stay_one_stage():
    st = RunState(run_id="r", task_id="t")
    spans = [
        _op("e", None, "evaluate", 0, tid="E"), _gen("eg", "e", 1, tid="E"),
        _op("f", None, "foresight_rank", 2, tid="F"), _gen("fg", "f", 3, tid="F"),
    ]
    labels = [s["label"] for s in build_conversation(st, spans, 5)["stages"]]
    assert labels == ["evaluate", "foresight_rank"]          # each already a meaningful stage


def _tool(sid, parent, name, start, attrs=None, tid="T", nid=5):
    return {"span_id": sid, "parent_id": parent, "trace_id": tid, "name": name,
            "kind": "tool", "start": start, "attributes": {"node_id": nid, **(attrs or {})}}


def test_live_stage_band_appears_from_its_anchor_before_close():
    # A training subprocess emits only a phase-stamped `stage_started` anchor while its `train` op is
    # still OPEN (not on disk). The Train band must appear LIVE from that anchor — with zero turns (the
    # anchor turn itself is suppressed as noise) — so the UI can render the running stage + its live log
    # instead of the band being dropped as "empty" until the stage closes.
    st = RunState(run_id="r", task_id="t")
    spans = [
        _op("e", None, "evaluate", 0, tid="E"),     # eval trace root; the `train` op ("trainop") is still open → absent
        _tool("a", "trainop", "stage_started", 1,
              {"phase": "train", "phase_span": "trainop", "stage": "train"}, tid="E"),
    ]
    bands = build_conversation(st, spans, 5)["stages"]
    train = [b for b in bands if b["label"] == "train"]
    assert train, "the live Train band must appear from its anchor before the stage op closes"
    assert train[0]["turns"] == []      # …and the anchor's own turn is suppressed (no empty stage_started row)
