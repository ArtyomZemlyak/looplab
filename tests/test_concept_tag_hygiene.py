"""`Settings.concept_tag_hygiene` — the concept taggers stop fragmenting their own vocabulary.

PROVENANCE: run `minionerec-lora-v1` (2026-10-05; 39 nodes, 73 concepts). Its grown vocabulary
carried one concept spelled three ways (`continual` / `continual-learning` / `continual_learning`),
hyperparameter VALUES as concepts (`optimization/lr/1e-3`, `lora/rank/r64`), tags for a method the
experiment only COMPARED against, five different tag sets for one experiment re-injected five times
with identical text — and 0 `concept_consolidation` events, because every rename waited for a
quiescent boundary an operator-queued run never reaches.

What is pinned here, each half driven through the real function with a fake client:
  * OFF is the historical prompt BYTE FOR BYTE — the golden digests below were taken from the tree
    before the flag existed (commit 08c3191c), not from this one;
  * ON, the rules are a separate trailing block, and task-agnostic;
  * the value-segment predicate as a table, its boundary (names that end in digits) included;
  * the mint-time cleanup, folding onto a known spelling, and its absence on replayed ids;
  * the model-free consolidation pre-pass, and that the model can neither re-decide nor undo it;
  * the pending-nodes gate letting ONLY those renames through mid-evaluation;
  * identical-description reuse making no call.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.engine.concept_cadence import ConceptCadenceMixin
from looplab.engine.options import EngineOptions
from looplab.engine.shared import concept_tag_hygiene
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search import concept_map, concept_tagging
from looplab.search.concept_graph import ConceptGraph
from looplab.search.concept_map import build_concept_map, consolidate_concepts, syntactic_renames
from looplab.search.concept_tagging import (_TAGGER_HYGIENE_RULES, concept_tag_hygiene_enabled,
                                            hygienic_concept_id, is_value_segment,
                                            strip_value_segments, tag_nodes_llm, tag_text_llm)
from tests.factories import make_engine


# ----------------------------------------------------------------------------------- fixtures
class _Client:
    """A recording fake: answers every structured call with `out` (or raises), keeps the messages."""

    def __init__(self, out=None, *, fail=False):
        self.out = out
        self.fail = fail
        self.messages = []

    def complete_tool(self, messages, json_schema, **_kw):
        self.messages.append(messages)
        if self.fail:
            raise RuntimeError("provider down")
        return self.out

    def complete_text(self, messages, **_kw):
        self.messages.append(messages)
        if self.fail:
            raise RuntimeError("provider down")
        return "not json"

    @property
    def calls(self) -> int:
        return len(self.messages)


def _graph() -> ConceptGraph:
    g = ConceptGraph(task_type="t")
    for cid in ("optimization/lr", "continual-learning/forgetting", "continual_learning",
                "training/epochs"):
        g.ensure(cid)
    return g


def _state(tmp_path, ideas):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "t", "goal": "g", "direction": "max"})
    for nid, idea in enumerate(ideas):
        s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", **idea}})
    return fold(s.read_all())


_ONE_IDEA = [{"params": {"lr": 0.001}, "theme": "lower lr", "rationale": "try a smaller step"}]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------- 1. OFF is the historical prompt, byte for byte
# Digests of the system prompts the PRE-FLAG tree (08c3191c) sends for exactly the fixtures above —
# derived there, so a change to the OFF path that also changed this file's own expectations is still
# caught. They move only when the shared prompt text moves, which is a contract change of its own.
_GOLDEN_NODE_SYSTEM = "c3ed180f6ce50f7ac31b55cecb72a18b05613d0412fd99a6c3216337d005c8f6"
_GOLDEN_TEXT_SYSTEM = "54aef3a4c7fd283231b7bb8beb2e6f2b5a936197126aa4d579f08c7b11c3e73e"
_GOLDEN_CONSOLIDATE_USER = "f8634112618c3fa6428c4e4b005255009ccd35c94d2148950d67ae3ad3d52124"


@pytest.mark.parametrize("kwargs", [{}, {"concept_tag_hygiene": False}])
def test_off_the_node_tagger_prompt_is_the_historical_bytes(tmp_path, kwargs):
    client = _Client({"concept_ids": ["optimization/lr"]})
    tag_nodes_llm(_state(tmp_path, _ONE_IDEA), _graph(), client, grow=True, **kwargs)
    assert _sha(client.messages[0][0]["content"]) == _GOLDEN_NODE_SYSTEM


@pytest.mark.parametrize("kwargs", [{}, {"concept_tag_hygiene": False}])
def test_off_the_item_tagger_prompt_is_the_historical_bytes(kwargs):
    client = _Client({"concept_ids": ["optimization/lr"]})
    tag_text_llm("a smaller learning rate", _graph(), client, **kwargs)
    assert _sha(client.messages[0][0]["content"]) == _GOLDEN_TEXT_SYSTEM


@pytest.mark.parametrize("kwargs", [{}, {"concept_tag_hygiene": False}])
def test_off_the_consolidator_sees_the_historical_vocabulary(kwargs):
    client = _Client({"merges": []})
    _, _, rename = consolidate_concepts(_graph(), {0: frozenset({"optimization/lr"})},
                                        client=client, **kwargs)
    assert _sha(client.messages[0][1]["content"]) == _GOLDEN_CONSOLIDATE_USER
    # …and decides nothing on its own: the spelling pair stays two concepts when OFF.
    assert rename == {}


def test_off_a_returned_id_is_minted_as_returned(tmp_path):
    graph = _graph()
    client = _Client({"concept_ids": ["optimization/lr/1e-3", "continual_learning/forgetting"]})
    tags = tag_nodes_llm(_state(tmp_path, _ONE_IDEA), graph, client, grow=True)
    assert tags[0] == {"optimization/lr/1e-3", "continual_learning/forgetting"}
    assert "optimization/lr/1e-3" in graph


# ------------------------------------------------------------------ 2. ON, the rules, as one block
def test_on_both_taggers_append_the_rules_after_the_historical_prompt(tmp_path):
    off, on = _Client({"concept_ids": []}), _Client({"concept_ids": []})
    state = _state(tmp_path, _ONE_IDEA)
    tag_nodes_llm(state, _graph(), off, grow=True)
    tag_nodes_llm(state, _graph(), on, grow=True, concept_tag_hygiene=True)
    assert on.messages[0][0]["content"] == off.messages[0][0]["content"] + _TAGGER_HYGIENE_RULES

    off, on = _Client({"concept_ids": []}), _Client({"concept_ids": []})
    tag_text_llm("x", _graph(), off)
    tag_text_llm("x", _graph(), on, concept_tag_hygiene=True)
    assert on.messages[0][0]["content"] == off.messages[0][0]["content"] + _TAGGER_HYGIENE_RULES


def test_the_rules_say_the_three_things_and_name_no_domain():
    rules = _TAGGER_HYGIENE_RULES
    assert "baseline" in rules and "trains nothing" in rules           # what it DOES
    assert "NEVER a hyperparameter VALUE" in rules                      # a knob, not its value
    assert "REUSE known ids VERBATIM" in rules and "`_` vs `-`" in rules  # no spelling variants
    # TASK-AGNOSTIC: no word of the run that motivated them leaks into every task's prompt.
    for word in ("lora", "sft", "continual", "recommend", "minionerec", "rank/r"):
        assert word not in rules.lower()


# ------------------------------------------------------------------ 3. what counts as a VALUE
@pytest.mark.parametrize("segment,is_value", [
    # numbers: integer, decimal, scientific, with a magnitude/multiplier
    ("2", True), ("16", True), ("0.05", True), (".5", True), ("1e-3", True), ("3e-4", True),
    ("1e-5", True), ("100k", True), ("4x", True), ("7b", True),
    # a closed knob mnemonic + a number
    ("r16", True), ("r64", True), ("lr1e-4", True), ("lr-3e-4", True), ("alpha32", True),
    ("bs64", True), ("seed7", True), ("epochs2", True), ("r16-alpha32", True), ("r16_alpha32", True),
    # THE BOUNDARY: a name that merely ends in digits is a method, model, norm, metric, precision
    ("bm25", False), ("e5", False), ("t5", False), ("l2", False), ("f1", False), ("gpt2", False),
    ("fp16", False), ("int8", False), ("b0", False), ("v2", False), ("top5", False),
    ("k8s", False), ("llama-7b", False),
    # words and fragments
    ("lr", False), ("rank", False), ("epochs", False), ("r", False), ("1e", False), ("", False),
])
def test_the_value_segment_table(segment, is_value):
    assert is_value_segment(segment) is is_value


@pytest.mark.parametrize("cid,stripped", [
    ("optimization/lr/1e-3", "optimization/lr"),
    ("lora/rank/r64", "lora/rank"),
    ("training/epochs/2", "training/epochs"),
    ("a/b/16/1e-3", "a/b"),               # every trailing value goes
    ("model/e5", "model/e5"),             # a name is kept
    ("a/r16/targets", "a/r16/targets"),   # a value in the MIDDLE qualifies a name; kept
    ("16", ""),                           # nothing but a value: no concept at all
])
def test_trailing_values_are_stripped(cid, stripped):
    assert strip_value_segments(cid) == stripped


# ------------------------------------------------------------- 4. the mint-time cleanup (ON)
@pytest.mark.parametrize("raw,minted", [
    ("optimization/lr/1e-3", "optimization/lr"),                       # value -> its known knob
    ("continual_learning/forgetting", "continual-learning/forgetting"),  # -> the known spelling
    ("continual-learning", "continual-learning"),                      # known as returned: kept
    ("eval/multi_dataset", "eval/multi-dataset"),                      # unknown: folded to `-`
    ("training/epochs/2", "training/epochs"),
    ("16", ""),
])
def test_hygienic_id_resolves_onto_known_spellings(raw, minted):
    assert hygienic_concept_id(raw, _graph()) == minted


def test_a_new_leaf_grows_under_the_known_parent_spelling():
    """A level is resolved one at a time, so a new leaf never re-spells the ancestor it grew under:
    with only `data_aug` known, `data-aug/mixup` is minted as `data_aug/mixup`, not as a second
    `data-aug` axis beside it."""
    graph = ConceptGraph(task_type="t")
    graph.ensure("data_aug/crop")
    assert hygienic_concept_id("data-aug/mixup", graph) == "data_aug/mixup"
    assert hygienic_concept_id("data-aug/new_family/x", graph) == "data_aug/new-family/x"


def test_on_the_node_tagger_mints_the_cleaned_ids(tmp_path):
    graph = _graph()
    client = _Client({"concept_ids": ["optimization/lr/1e-3", "continual_learning/forgetting",
                                      "training/epochs/2", "model/e5"]})
    modes: dict = {}
    tags = tag_nodes_llm(_state(tmp_path, _ONE_IDEA), graph, client, grow=True,
                         concept_tag_hygiene=True, producer_modes=modes)
    assert tags[0] == {"optimization/lr", "continual-learning/forgetting", "training/epochs",
                       "model/e5"}
    assert "optimization/lr/1e-3" not in graph and "continual_learning/forgetting" not in graph
    assert modes[0] == "llm"


def test_a_replayed_id_is_never_rewritten(tmp_path):
    """`known_tags` are the log's own facts: rewriting them at read time would make one log fold to
    a different vocabulary under a different flag."""
    graph = _graph()
    client = _Client({"concept_ids": []})
    tags = tag_nodes_llm(_state(tmp_path, _ONE_IDEA), graph, client, grow=True,
                         concept_tag_hygiene=True, known_tags={0: ["optimization/lr/1e-3"]})
    assert client.calls == 0
    assert tags[0] == {"optimization/lr/1e-3"} and "optimization/lr/1e-3" in graph


# ---------------------------------------------------------- 5. the model-free consolidation pass
_FRAGMENTED = ["continual", "continual-learning", "continual_learning",
               "continual-learning/forgetting", "continual_learning/forgetting",
               "continual_learning/replay", "continual_learning/replay/random_subset",
               "optimization", "optimization/lr", "optimization/lr/1e-3", "optimization/lr/3e-4",
               "lora", "lora/rank", "lora/rank/r64", "model", "model/e5"]


def test_syntactic_renames_fold_spellings_and_values_level_by_level():
    tags = {1: {"continual-learning/forgetting"}, 2: {"continual-learning/forgetting"},
            3: {"continual_learning"}, 4: {"optimization/lr/1e-3"}}
    assert syntactic_renames(_FRAGMENTED, tags) == {
        # the hyphenated parent has more uses in its subtree (2 vs 1), and its children follow it
        "continual_learning": "continual-learning",
        "continual_learning/forgetting": "continual-learning/forgetting",
        "continual_learning/replay": "continual-learning/replay",
        "continual_learning/replay/random_subset": "continual-learning/replay/random_subset",
        # value leaves collapse onto their knob
        "optimization/lr/1e-3": "optimization/lr",
        "optimization/lr/3e-4": "optimization/lr",
        "lora/rank/r64": "lora/rank",
    }   # `continual` is a different WORD — the model's to decide; `model/e5` is a name


def test_more_uses_win_and_a_tie_keeps_the_hyphen():
    assert syntactic_renames(["a_b", "a-b"], {1: {"a_b"}, 2: {"a_b"}, 3: {"a-b"}}) == {"a-b": "a_b"}
    assert syntactic_renames(["a_b", "a-b"], {}) == {"a_b": "a-b"}


def test_a_recorded_decision_is_never_re_decided():
    # `a_b` is a recorded canonical: it is the spelling, and nothing renames it.
    assert syntactic_renames(["a_b", "a-b"], {3: {"a-b"}}, decided={"a_b"}) == {"a-b": "a_b"}
    assert syntactic_renames(["x/lr/1e-3"], {}, decided={"x/lr/1e-3"}) == {}


def _vocab_graph():
    g = ConceptGraph(task_type="t")
    for cid in _FRAGMENTED:
        g.ensure(cid)
    return g


def test_on_consolidation_runs_the_pre_pass_and_hides_its_raws_from_the_model():
    client = _Client({"merges": [
        # the model tries to re-decide a pre-pass raw and to move a pre-pass canonical: both refused
        {"raw": "continual_learning", "canonical": "continual/other"},
        {"raw": "optimization/lr", "canonical": "optimization/learning-rate"},
        # …and its own merge of an id the pre-pass left alone still goes through
        {"raw": "model/e5", "canonical": "model/encoder"},
    ]})
    syntactic: dict = {}
    graph, tags, rename = consolidate_concepts(
        _vocab_graph(), {1: frozenset({"optimization/lr/1e-3", "continual-learning/forgetting"}),
                         2: frozenset({"continual_learning"})},
        client=client, concept_tag_hygiene=True, syntactic_out=syntactic)
    shown = client.messages[0][1]["content"]
    assert "- continual_learning " not in shown and "- optimization/lr/1e-3 " not in shown
    assert "- continual-learning " in shown
    assert rename["continual_learning"] == "continual-learning"
    assert rename["optimization/lr/1e-3"] == "optimization/lr"
    assert "optimization/lr" not in rename
    assert rename["model/e5"] == "model/encoder"
    assert syntactic == {k: v for k, v in rename.items() if k != "model/e5"}
    assert tags[1] == {"optimization/lr", "continual-learning/forgetting"}
    assert tags[2] == {"continual-learning"}
    assert "optimization/lr/1e-3" not in graph


def test_a_failed_model_step_keeps_the_syntactic_decisions():
    syntactic: dict = {}
    _, _, rename = consolidate_concepts(
        _vocab_graph(), {}, client=_Client(fail=True), concept_tag_hygiene=True,
        known_renames={"model": "models"}, syntactic_out=syntactic)
    assert rename["lora/rank/r64"] == "lora/rank" and rename["model"] == "models"
    assert syntactic["lora/rank/r64"] == "lora/rank" and "model" not in syntactic


# --------------------------------------------------- 6. the gate: syntactic renames while pending
def _busy(tmp_path, *, pending: int):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "t", "goal": "g", "direction": "max"})
    for nid in range(2 + pending):
        s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "theme": f"t{nid}"}})
        if nid < 2:
            s.append("node_evaluated", {"node_id": nid, "metric": 0.5})
    # node 0 is classifier-tagged, node 1 operator-tagged: only 0 may lend its tags by description
    s.append("node_concepts", {"node_id": 0, "concepts": ["loss/x"], "mode": "llm",
                               "at_vocab": 2, "at_pending": 0, "generation": 0})
    s.append("concept_tag_edited", {"node_id": 1, "concepts": ["loss/y"]})
    return s


class _Host(ConceptCadenceMixin):
    def __init__(self, store, hygiene):
        self.store = store
        self._concept_tag_hygiene = hygiene


def _recorded(tmp_path, monkeypatch, *, pending, hygiene):
    store = _busy(tmp_path / f"p{pending}-{hygiene}", pending=pending)
    state = fold(store.read_all())
    seen = {}
    graph = SimpleNamespace(concepts=lambda: [SimpleNamespace(id="loss/x")])

    def _fake_build(*_a, **kwargs):
        seen.update(kwargs)
        out = {"graph": graph, "mode": "llm", "raw_tags": {}, "raw_tag_modes": {},
               "consolidated": {"loss/dcl_x": "loss/dcl-x", "loss/dcl": "loss/decoupled"}}
        if kwargs.get("concept_tag_hygiene"):
            out["consolidated_syntactic"] = {"loss/dcl_x": "loss/dcl-x"}
        return out

    monkeypatch.setattr(concept_map, "build_concept_map", _fake_build)
    _Host(store, hygiene)._refresh_concept_tags(state, object(), "tool_call", None)
    rows = [e.data["rename"] for e in store.read_all() if e.type == "concept_consolidation"]
    return rows, seen


def test_while_pending_only_the_syntactic_renames_are_recorded(tmp_path, monkeypatch):
    rows, seen = _recorded(tmp_path, monkeypatch, pending=2, hygiene=True)
    assert rows == [{"loss/dcl_x": "loss/dcl-x"}]
    assert seen["concept_tag_hygiene"] is True
    # an operator's tags are never lent under the classifier's mode
    assert seen["reuse_known_ids"] == {0}


def test_while_pending_and_off_nothing_is_recorded_as_before(tmp_path, monkeypatch):
    rows, seen = _recorded(tmp_path, monkeypatch, pending=2, hygiene=False)
    assert rows == []
    assert "concept_tag_hygiene" not in seen and "reuse_known_ids" not in seen


@pytest.mark.parametrize("hygiene", [True, False])
def test_a_quiescent_pass_records_every_rename_either_way(tmp_path, monkeypatch, hygiene):
    rows, _ = _recorded(tmp_path, monkeypatch, pending=0, hygiene=hygiene)
    assert rows == [{"loss/dcl_x": "loss/dcl-x", "loss/dcl": "loss/decoupled"}]


# ------------------------------------------------- 7. identical description, identical tags, no call
_TWINS = [{"params": {"r": 64}, "theme": "bigger adapter", "rationale": "does capacity help"},
          {"params": {"lr": 0.1}, "theme": "other", "rationale": "something else"},
          {"params": {"r": 64}, "theme": "bigger adapter", "rationale": "does capacity help"},
          {"params": {"r": 64}, "theme": "bigger adapter", "rationale": "does capacity help"}]


def test_twins_in_one_pass_cost_one_call(tmp_path):
    client = _Client({"concept_ids": ["capacity/width"]})
    modes: dict = {}
    tags = tag_nodes_llm(_state(tmp_path, _TWINS), _graph(), client, grow=True,
                         concept_tag_hygiene=True, producer_modes=modes, max_workers=8)
    assert client.calls == 2                              # node 0 and node 1; 2 and 3 reuse 0
    assert tags[0] == tags[2] == tags[3] == {"capacity/width"}
    assert modes == {0: "llm", 1: "llm", 2: "llm", 3: "llm"}


def test_off_every_twin_is_asked(tmp_path):
    client = _Client({"concept_ids": ["capacity/width"]})
    tag_nodes_llm(_state(tmp_path, _TWINS), _graph(), client, grow=True)
    assert client.calls == 4


def test_a_twin_of_a_known_node_reuses_its_tags_and_only_a_named_one(tmp_path):
    state = _state(tmp_path, _TWINS)
    client = _Client({"concept_ids": ["capacity/other"]})
    tags = tag_nodes_llm(state, _graph(), client, grow=True, concept_tag_hygiene=True,
                         known_tags={0: ["capacity/width"], 1: ["x/y"]})
    assert client.calls == 0 and tags[2] == tags[3] == {"capacity/width"}
    # a known node the caller did not name as reusable lends nothing
    client = _Client({"concept_ids": ["capacity/other"]})
    tags = tag_nodes_llm(state, _graph(), client, grow=True, concept_tag_hygiene=True,
                         known_tags={0: ["capacity/width"], 1: ["x/y"]}, reuse_known_ids=set())
    assert client.calls == 1 and tags[2] == tags[3] == {"capacity/other"}


def test_a_failed_answer_is_not_copied(tmp_path):
    class _FailFirst(_Client):
        def complete_tool(self, messages, json_schema, **kw):
            self.messages.append(messages)
            if "node 0)" in messages[-1]["content"]:
                raise RuntimeError("transient")
            return {"concept_ids": ["capacity/width"]}

        def complete_text(self, messages, **kw):
            raise RuntimeError("transient")

    client = _FailFirst()
    modes: dict = {}
    tags = tag_nodes_llm(_state(tmp_path, _TWINS), _graph(), client, grow=True,
                         concept_tag_hygiene=True, producer_modes=modes)
    assert modes[0] == "offline-heuristic"
    # node 2 asked for itself (the failure lent nothing); node 3 then reused node 2's answer
    assert modes[2] == modes[3] == "llm" and tags[2] == tags[3] == {"capacity/width"}
    asked = [m[-1]["content"].split(")")[0] for m in client.messages]
    assert sum("node 3" in a for a in asked) == 0


# ------------------------------------------------------- 8. the flag, its readers and the forwards
def test_the_flag_is_on_for_new_runs_and_off_for_everything_older(tmp_path):
    assert Settings().concept_tag_hygiene is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["concept_tag_hygiene"] is False
    legacy = Settings().masked_snapshot()
    legacy.pop("concept_tag_hygiene")
    assert settings_from_snapshot(legacy).concept_tag_hygiene is False
    assert settings_from_snapshot(Settings().masked_snapshot()).concept_tag_hygiene is True
    assert EngineOptions().concept_tag_hygiene is False
    assert EngineOptions.from_settings(Settings()).concept_tag_hygiene is True
    assert make_engine(tmp_path / "bare")._concept_tag_hygiene is False
    assert make_engine(tmp_path / "on", concept_tag_hygiene=True)._concept_tag_hygiene is True
    # the two readers: the engine's, and every other caller's — both OFF when the field is absent
    assert concept_tag_hygiene(SimpleNamespace()) is False
    assert concept_tag_hygiene(SimpleNamespace(_concept_tag_hygiene=True)) is True
    assert concept_tag_hygiene_enabled(SimpleNamespace()) is False
    assert concept_tag_hygiene_enabled(Settings()) is True


def test_build_concept_map_forwards_only_when_on(tmp_path, monkeypatch):
    seen: list = []

    def _fake_tag(_state, _graph, _client, **kwargs):
        seen.append(("tag", kwargs))
        return {}

    def _fake_consolidate(graph, tags, **kwargs):
        seen.append(("consolidate", kwargs))
        if "syntactic_out" in kwargs:
            kwargs["syntactic_out"]["a_b"] = "a-b"
        return graph, tags, {}

    monkeypatch.setattr(concept_tagging, "tag_nodes_llm", _fake_tag)
    monkeypatch.setattr(concept_map, "consolidate_concepts", _fake_consolidate)
    monkeypatch.setattr(concept_map, "derive_reference_concepts", lambda *a, **k: [])
    state = _state(tmp_path, _ONE_IDEA)
    off = build_concept_map(state, client=object())
    assert "consolidated_syntactic" not in off
    assert all("concept_tag_hygiene" not in kw for _, kw in seen)
    seen.clear()
    on = build_concept_map(state, client=object(), concept_tag_hygiene=True, reuse_known_ids={3})
    assert on["consolidated_syntactic"] == {"a_b": "a-b"}
    assert dict(seen)["tag"]["reuse_known_ids"] == {3}
    assert dict(seen)["consolidate"]["concept_tag_hygiene"] is True


def test_the_proposal_tagger_forwards_only_when_on(monkeypatch):
    from looplab.core.models import Idea
    from looplab.search import graded_novelty

    seen: list = []
    monkeypatch.setattr(graded_novelty, "tag_text_llm",
                        lambda text, graph, client, **kw: seen.append(kw) or frozenset())
    graded_novelty.tag_idea_llm(Idea(operator="draft"), _graph(), object())
    graded_novelty.tag_idea_llm(Idea(operator="draft"), _graph(), object(),
                                concept_tag_hygiene=True)
    assert seen == [{"parser": "tool_call"}, {"parser": "tool_call", "concept_tag_hygiene": True}]
