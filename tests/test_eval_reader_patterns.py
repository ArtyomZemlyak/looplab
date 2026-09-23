"""A regex metric reader that can never read anything is refused at SUBMIT (review 2026-09-22, RTA-06).

`stdout_regex` / `file_regex` read their number through `command_eval._regex_metric`, which turns
every malformed half of the spec into "no metric" on purpose (a node failure, never a crash of the
eval worker): no `pattern` (nor its tolerant `key` fallback), a non-string one, one that does not
compile, a `group` `int()` cannot read, a group the pattern does not have. The kind was the only
thing submit checked, so each of those specs started a run whose EVERY node failed `no_metric` with
nothing naming the cause — the `path` failure `tests/test_eval_reader_paths.py` closed, for the other
half of a regex reader. 83 of 83 corpus metrics are `stdout_regex`.

Same order as that file: each test reproduces the runtime behaviour through a REAL eval first — the
reader is unchanged and still abstains — and only then asserts the refusal, in every reader slot.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from looplab.adapters.repo_task import RepoTask, _PATHLESS_COST, _PATTERNLESS_COST
from looplab.runtime.command_eval import (READERS_REQUIRING_PATTERN, metric_spec_pattern_error,
                                          regex_pattern, run_command_eval)

_PROG = 'print("epoch 3 RECALL@100: 0.75")\nopen("train.log","w").write("val_loss=0.25\\n")\n'

# (name, spec, what the refusal must name). Every one of these read NO metric at runtime.
BROKEN = [
    ("no pattern", {"kind": "stdout_regex"}, "needs a `pattern`"),
    ("empty pattern", {"kind": "stdout_regex", "pattern": ""}, "needs a `pattern`"),
    ("non-string pattern", {"kind": "stdout_regex", "pattern": 7}, "needs a `pattern`"),
    ("does not compile", {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+"},
     "does not compile"),
    ("no such group", {"kind": "stdout_regex", "pattern": "RECALL@100: [0-9.]+"},
     "reads capture group 1, but"),
    ("group past the pattern", {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)",
                                "group": 2}, "reads capture group 2, but"),
    ("negative group", {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)", "group": -1},
     "reads capture group -1, but"),
    ("unreadable group", {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)", "group": "x"},
     "is not a capture-group number"),
    ("file reader, no pattern", {"kind": "file_regex", "path": "train.log"}, "needs a `pattern`"),
]
WORKING = [
    {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"},
    {"kind": "stdout_regex", "key": "RECALL@100: ([0-9.]+)"},          # the tolerant fallback
    {"kind": "stdout_regex", "pattern": "RECALL@(\\d+): ([0-9.]+)", "group": 2},
    {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)", "group": "1"},   # int("1")
    {"kind": "file_regex", "path": "train.log", "pattern": "val_loss=([0-9.]+)"},
]


def _task(**eval_kw) -> dict:
    return {"kind": "repo", "id": "t", "goal": "g", "direction": "max",
            "editable_path": "examples/repo_example", "edit_surface": ["*.json"],
            "eval": {"command": ["python", "ttrain.py"], "timeout": 60, **eval_kw}}


def _metric(tmp_path: Path, spec: dict):
    (tmp_path / "p.py").write_text(_PROG, encoding="utf-8")
    return run_command_eval([sys.executable, "p.py"], str(tmp_path), 60, spec).metric


@pytest.mark.parametrize("name,spec,named", BROKEN, ids=[b[0] for b in BROKEN])
def test_a_regex_reader_that_reads_nothing_is_refused_at_submit(tmp_path, name, spec, named):
    assert _metric(tmp_path, spec) is None, f"{name}: the runtime reader did read a metric"
    with pytest.raises(ValueError) as ei:
        RepoTask(**_task(metric=spec))
    msg = str(ei.value)
    assert "eval.metric" in msg and named in msg, msg
    assert _PATTERNLESS_COST["metric"] in msg, "the measured per-slot consequence is named"


@pytest.mark.parametrize("spec", WORKING)
def test_a_regex_reader_that_reads_still_validates_and_still_reads(tmp_path, spec):
    assert _metric(tmp_path, spec) in (0.75, 0.25)
    assert metric_spec_pattern_error(spec) is None
    RepoTask(**_task(metric=spec))


@pytest.mark.parametrize("slot", ["metrics", "constraints", "cross_check"])
def test_every_reader_slot_refuses_it_with_its_own_consequence(slot):
    broken = {"kind": "stdout_regex", "name": "lat"}
    field = {"metrics": {"metrics": {"lat": broken}},
             "constraints": {"constraints": [{**broken, "max": 1.0}]},
             "cross_check": {"cross_check": {**broken, "tolerance": 0.1}}}[slot]
    with pytest.raises(ValueError) as ei:
        RepoTask(**_task(metric={"kind": "stdout_regex", "pattern": "x: ([0-9.]+)"}, **field))
    assert _PATTERNLESS_COST[slot] in str(ei.value)


def test_the_consequences_are_the_path_ones_re_led_not_re_spelled():
    """One statement of each slot's symptom: a pathless and a patternless reader both return None,
    so the slot decides the symptom identically — only the missing half is named differently."""
    assert set(_PATTERNLESS_COST) == set(_PATHLESS_COST)
    for slot, text in _PATHLESS_COST.items():
        assert text.startswith("Without `path`")
        assert _PATTERNLESS_COST[slot] == text.replace("Without `path`",
                                                       "Without a usable `pattern`", 1)


def test_the_check_and_the_readers_resolve_the_pattern_one_way():
    """`key` stands in for a missing `pattern` in BOTH, so neither can accept what the other reads
    differently — and a non-regex reader is none of this rule's business."""
    assert regex_pattern({"key": "a(1)"}) == "a(1)" and regex_pattern({"pattern": "b(2)",
                                                                         "key": "a"}) == "b(2)"
    assert READERS_REQUIRING_PATTERN == {"stdout_regex", "file_regex"}
    assert metric_spec_pattern_error({"kind": "stdout_json", "key": "metric"}) is None
    assert metric_spec_pattern_error("not a spec") is None
