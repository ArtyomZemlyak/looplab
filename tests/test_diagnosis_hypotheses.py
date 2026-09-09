"""The crash diagnostician may carry the OTHER explanations it considered (doc 52 row 32).

It answers one `failure_kind` with a findings trail behind it, so a wrong answer is
indistinguishable from a right one until the repair built on it fails — and this repo's own
classifier scores 88/118 on `failure_triage.v1`, i.e. roughly a quarter of its answers are wrong
while nothing records what else it weighed. SAGE's multi-hypothesis attribution — the alternatives,
each with its own severity, plus what would tell them apart — moved metrics-bearing outputs 42 → 92 %.

The properties under test are the ones that keep an alternative from becoming a second instruction:
it is bounded and redacted like every other durable model output, an unscored alternative is 0.0
rather than a coin flip, a `kind` outside the engine's own vocabulary is dropped, and nothing in the
repair path reads any of it.
"""
from __future__ import annotations

import pytest

from looplab.core.config import Settings
from looplab.engine.failure_diagnosis import (HYPOTHESES_CAP, coerce_hypotheses,
                                              hypotheses_enabled)


def test_the_alternatives_come_back_bounded_and_in_the_models_own_order():
    verdict = {"hypotheses": [
        {"cause": "the loader returned an empty split", "confidence": 0.4,
         "discriminator": "print len(train_ds) before the first epoch", "kind": "no_metric"},
        {"cause": "the checkpoint path was stale", "confidence": 0.9, "discriminator": "stat it"},
        {"cause": "third", "confidence": 0.1, "discriminator": "d"},
        {"cause": "fourth — past the cap", "confidence": 1.0, "discriminator": "d"}]}
    rows = coerce_hypotheses(verdict)
    assert len(rows) == HYPOTHESES_CAP == 3
    # NOT sorted by confidence: the diagnostician's own ranking of what it considered is information
    assert [row["cause"] for row in rows][:2] == ["the loader returned an empty split",
                                                  "the checkpoint path was stale"]
    assert rows[0]["kind"] == "no_metric" and rows[0]["confidence"] == pytest.approx(0.4)
    assert "kind" not in rows[1]          # none offered


def test_an_unscored_alternative_is_zero_and_not_a_coin_flip():
    """A reader deciding whether to act on an alternative must not be handed a number the model
    never produced."""
    rows = coerce_hypotheses({"hypotheses": [
        {"cause": "no confidence at all", "discriminator": "d"},
        {"cause": "a string confidence", "confidence": "high"},
        {"cause": "out of range", "confidence": 7.5},
        ]})
    assert [row["confidence"] for row in rows] == [0.0, 0.0, 1.0]


def test_a_kind_outside_the_engines_vocabulary_is_dropped_not_recorded():
    """The same refusal `coerce_failure_kind` makes: an alternative may not smuggle in a label the
    engine's own vocabulary does not contain, because a reader downstream would take it for a
    diagnosis."""
    rows = coerce_hypotheses({"hypotheses": [{"cause": "c", "kind": "the_model_made_this_up"}]})
    assert rows == [{"cause": "c", "confidence": 0.0, "discriminator": ""}]


def test_junk_and_duplicates_cost_a_row_not_the_record():
    assert coerce_hypotheses({}) == [] and coerce_hypotheses(None) == []
    assert coerce_hypotheses({"hypotheses": "not a list"}) == []
    rows = coerce_hypotheses({"hypotheses": [
        {"cause": "same"}, {"cause": "SAME"}, {"cause": ""}, "not a dict", {"cause": "other"}]})
    assert [row["cause"] for row in rows] == ["same", "other"]


def test_every_field_goes_through_redaction():
    secret = "AKIAIOSFODNN7EXAMPLE1234"
    seen = []

    def redact(text):
        seen.append(text)
        return text.replace(secret, "<redacted>")

    rows = coerce_hypotheses({"hypotheses": [{"cause": f"the key {secret} was read",
                                              "discriminator": f"grep {secret}"}]}, redact)
    assert secret not in rows[0]["cause"] and secret not in rows[0]["discriminator"]
    assert len(seen) >= 2


def test_the_flag_is_off_by_default_and_read_through_one_reader():
    assert Settings().diagnosis_hypotheses is False
    assert hypotheses_enabled(Settings()) is False
    assert hypotheses_enabled(Settings(diagnosis_hypotheses=True)) is True
    assert hypotheses_enabled(object()) is False        # a duck-typed stub means OFF


def test_the_schema_asks_for_them_only_when_the_flag_is_on():
    """OFF is not "the model may still answer" — it is the historical tool schema, byte for byte."""
    import inspect

    from looplab.agents.unified_agent import UnifiedAgent

    source = inspect.getsource(UnifiedAgent.triage_crash)
    assert 'if self._diagnosis_hypotheses:' in source
    assert '"hypotheses"' in source and "discriminator" in source
    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent._diagnosis_hypotheses = False
    assert agent._diagnosis_hypotheses is False


def test_the_repair_path_does_not_read_the_alternatives():
    """The property that makes this safe to ship: the repair follows the ONE `failure_kind`. A
    second explanation is evidence for a human and for the next diagnosis, never a second
    instruction for this one."""
    import ast

    from tests._source_scan import iter_trees

    allowed = {
        # WHO MAY NAME THE ALTERNATIVES AT ALL, and in what role:
        "coerce_hypotheses": {"failure_diagnosis.py",   # defines it
                              "evaluate.py"},           # the one caller: coerce, then record
        "reason_hypotheses": {"evaluate.py",            # writes the key on the two failure rows
                              "types.py"},              # declares it in the payload contract
        "a._hypotheses": {"evaluate.py"},               # the attempt slot it lands in
    }
    # AST and not substrings, in BOTH directions: a comment naming the coercion (there are two,
    # deliberately, saying who owns the shape) must not fail this, and a read hidden in a comment
    # must not satisfy it. What is scanned is what executes — attributes, names and string keys.
    readers = []
    for path, tree in iter_trees():
        spoken = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                spoken.add("a._" + node.attr[1:] if node.attr.startswith("_") else node.attr)
            elif isinstance(node, ast.Name):
                spoken.add(node.id)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                spoken.add(node.value)
        for token, homes in allowed.items():
            if token in spoken and path.name not in homes:
                readers.append(f"{path.name}: {token}")
    assert not readers, (
        "something outside the producer reads the alternatives — the repair follows the ONE "
        f"failure_kind: {readers}")


def test_the_failure_rows_declare_the_key_they_can_carry():
    from looplab.events.types import EVENT_PAYLOAD_KEYS

    for etype in ("node_failed", "node_repaired"):
        assert "reason_hypotheses" in EVENT_PAYLOAD_KEYS[etype].keys, etype


def test_a_stale_alternative_never_reaches_the_next_attempts_row():
    """THE SLOT IS CLEARED WITH THE REST OF THE DIAGNOSIS. `reason_hypotheses` rides the durable
    row through the same `if a._x` test as `reason_summary` and `reason_findings`, so a set left
    over from attempt 3 is attempt 3's competing explanations printed on attempt 4's record — with
    nothing on the row to say they belong to a different failure. Driven on the helper both reset
    sites call, so the property holds for whichever site the chain takes."""
    from looplab.engine.evaluate import DIAGNOSIS_SLOTS, EvalAttempt, reset_diagnosis

    attempt = EvalAttempt(node_id=7)
    for slot in DIAGNOSIS_SLOTS:
        setattr(attempt, slot, [{"kind": "stale", "confidence": 0.9}])
    reset_diagnosis(attempt)
    assert [getattr(attempt, slot) for slot in DIAGNOSIS_SLOTS] == [None] * len(DIAGNOSIS_SLOTS)


def test_every_slot_a_failure_row_carries_is_in_the_registry():
    """THE OTHER DIRECTION, and the one the defect needed: a slot added at the WRITE site and at
    neither reset site is exactly how `_hypotheses` shipped stale. Re-derived by AST from the
    `reason_*` keys the two durable failure rows actually write off an `EvalAttempt` slot, so a
    sixth one cannot be added without either joining the registry or turning this red."""
    import ast

    from looplab.engine.evaluate import DIAGNOSIS_DEFAULTED, DIAGNOSIS_SLOTS
    from tests._source_scan import PKG

    tree = ast.parse((PKG / "engine" / "evaluate.py").read_text(encoding="utf-8"))
    written: set[str] = set()
    for node in ast.walk(tree):
        # `**({"reason_x": a._x} if a._x else {})` and `data["reason_x"] = a._x` alike: the key is
        # a `reason_`-prefixed constant and the value is one attribute read off the attempt.
        key = value = None
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and str(k.value).startswith("reason_"):
                    key, value = k.value, v
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Subscript)
              and isinstance(node.targets[0].slice, ast.Constant)
              and str(node.targets[0].slice.value).startswith("reason_")):
            key, value = node.targets[0].slice.value, node.value
        if key is None or not isinstance(value, ast.Attribute):
            continue
        if isinstance(value.value, ast.Name) and value.attr.startswith("_"):
            written.add(value.attr)
    assert written, "the scanner found no durable failure-row slot at all"
    # `_reason_source` is the one such slot that is REBOUND rather than cleared — a failure always
    # has an author, so its reset is a non-diagnostician source (`reason_source_for`: `engine`, or
    # `declared` for the reason the eval states), never None — and `DIAGNOSIS_DEFAULTED`
    # names it so the exception is written down instead of widening the rule.
    assert written <= set(DIAGNOSIS_SLOTS) | set(DIAGNOSIS_DEFAULTED), (
        f"a failure row carries "
        f"{sorted(written - set(DIAGNOSIS_SLOTS) - set(DIAGNOSIS_DEFAULTED))}, which no reset "
        "clears or rebinds")
