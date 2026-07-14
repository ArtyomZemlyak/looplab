"""PART IV E3 — novelty-gate recall / paraphrase-leak diagnostic (§21.12)."""
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.novelty_recall import paraphrase_leaks


def _state(tmp_path, ideas, novelty_rejected=0):
    """Build a run: `ideas` = list of (theme, rationale) that BECAME nodes (passed the gate)."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    for i, (theme, rat) in enumerate(ideas):
        s.append("node_created", {"node_id": i, "parent_ids": [], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {}, "theme": theme,
                                           "rationale": rat}})
        s.append("node_evaluated", {"node_id": i, "metric": 0.8 + 0.001 * i})
    for j in range(novelty_rejected):
        s.append("novelty_rejected", {"node_id": 100 + j, "near_node": 0, "kind": "llm"})
    return fold(s.read_all())


def test_offline_reports_candidate_pairs_without_adjudication(tmp_path):
    st = _state(tmp_path, [
        ("dcl-rdrop", "decoupled contrastive loss with r-drop regularization"),
        ("dcl-rdrop-dup", "decoupled contrastive loss with r-drop regularization"),  # near-identical
        ("hard-neg-mining", "mine external hard negatives from the corpus with a cross-encoder")])
    r = paraphrase_leaks(st, client=None)
    assert r["n_nodes"] == 3
    assert r["adjudicated"] is False and r["leaks"] == []
    # the two near-identical DCL nodes should surface as a candidate pair; the distinct one should not pair
    assert (0, 1) in r["candidate_pairs"] or (1, 0) in r["candidate_pairs"]


def test_llm_adjudication_flags_a_leak_and_computes_recall(tmp_path, monkeypatch):
    import looplab.core.parse as parse_mod
    st = _state(tmp_path, [
        ("dcl-rdrop", "decoupled contrastive loss with r-drop"),
        ("dcl-rdrop-again", "decoupled contrastive loss with r-drop")], novelty_rejected=3)

    class _V:
        is_paraphrase = True
        reason = "same method, same modifier, only reworded"
    monkeypatch.setattr(parse_mod, "parse_structured", lambda *a, **k: _V())
    r = paraphrase_leaks(st, client=object())
    assert r["adjudicated"] is True
    assert len(r["leaks"]) >= 1                       # the paraphrase pair leaked through
    # recall = caught / (caught + leaked) = 3 / (3 + n_leaks)
    assert r["recall"] == round(3 / (3 + len(r["leaks"])), 3)


def test_degrades_when_no_pairs(tmp_path):
    st = _state(tmp_path, [("only-one", "a single lonely experiment")])
    r = paraphrase_leaks(st, client=object())
    assert r["candidate_pairs"] == [] and r["recall"] is None
