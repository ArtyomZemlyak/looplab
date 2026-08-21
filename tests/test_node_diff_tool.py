"""WHAT ACTUALLY DIFFERS BETWEEN TWO NODES — the tool, and the one property it must never lose.

WHY IT EXISTS (the operator's own framing, 2026-08-21)
------------------------------------------------------
An agent proposing the next experiment is handed a table of `node -> metric` and reasons about the
numbers without being able to see what separates the rows. It then "improves" a parameter that was
never the difference, or re-proposes a difference already tried. The instruction was to fix this in
the TOOLING rather than by adding a sentence to a prompt — "дать ему инструмент, который тебе diff
вернёт" — so that the answer is a fact the agent can ask for instead of an instruction it may skip.

THE PROPERTY UNDER TEST
-----------------------
**An empty diff and a missing diff must never render the same.** "These two nodes are identical in
`train.py`" and "neither node's `train.py` is in the record" are opposite facts. A tool that returns
"" for both teaches an agent that nothing changed, which is precisely the failure it was built to
prevent — and it is the same rule `comparability_notice` follows by refusing to be silent on
`UNKNOWN`, because silence beside a number is read as assent.

Half of these tests are therefore about ABSENCE, not about diffing. The other half are driven over
`runs/rubertlite-dr-unified-v8`, folded through the real `fold`, because the thing under test is a
read over fields other modules write: a fixture that invents `{"files": {...}}` would still pass if
the fold renamed one tomorrow, and the tool would be silently empty in production.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.tools import node_diff as nd
from looplab.tools.node_diff import NO_DIFFERENCE, NOT_RECOVERABLE, NodeDiffTools

_RUN = Path("/home/jovyan/data/looplab/runs/rubertlite-dr-unified-v8/events.jsonl")


@pytest.fixture(scope="module")
def real_state():
    """The v8 run, folded exactly as the engine folds it. Skipped rather than faked when the run is
    not on this box — a fabricated stand-in would silently weaken every assertion below."""
    if not _RUN.exists():
        pytest.skip("the v8 run is not on this machine")
    return fold(EventStore(str(_RUN)).read_all())


@pytest.fixture()
def tools(real_state):
    t = NodeDiffTools()
    t.bind_state(real_state)
    return t


# ------------------------------------------------------------------ absence is not identity
class _N:
    """The narrowest node this module reads."""

    def __init__(self, nid, files=None, params=None, metric=None, provenance=None):
        self.id, self.files, self.metric, self.metric_provenance = nid, files or {}, metric, provenance
        self.idea = type("I", (), {"params": params or {}})()
        self.attempt = 0
        self.status = "ok"


class _S:
    def __init__(self, *nodes):
        self.nodes = {n.id: n for n in nodes}


def _rec(state, nid):
    return nd.node_record(state, nid)


def test_two_nodes_with_no_files_are_not_reported_as_identical():
    """THE headline property. Both sides empty means the record cannot answer, and saying
    "no difference" there is a false statement an agent will act on."""
    state = _S(_N(1), _N(2))
    out = nd.diff_files(_rec(state, 1), _rec(state, 2))
    assert NOT_RECOVERABLE in out[0] and NO_DIFFERENCE not in "".join(out)


def test_one_side_missing_says_so_and_says_which_side():
    state = _S(_N(1, files={"train.py": "x = 1\n"}), _N(2))
    out = "\n".join(nd.diff_files(_rec(state, 1), _rec(state, 2)))
    assert NOT_RECOVERABLE in out and "node 2" in out
    assert "NOT 'no difference'" in out


def test_two_identical_file_sets_say_they_were_compared_and_how_many():
    """The other half of the same property: a real "no difference" must be VISIBLY a measurement,
    with the count of what was compared, so it cannot be confused with an unanswered question."""
    files = {"train.py": "x = 1\n", "cfg.yaml": "a: 1\n"}
    state = _S(_N(1, files=dict(files)), _N(2, files=dict(files)))
    out = "\n".join(nd.diff_files(_rec(state, 1), _rec(state, 2)))
    assert NO_DIFFERENCE in out and "compared 2 experiment files" in out


def test_a_node_that_does_not_exist_is_not_a_node_with_no_differences(tools):
    out = tools.execute("diff_nodes", {"left": 0, "right": 4242})
    assert "no node 4242" in out and "NOT 'no difference'" in out
    assert nd.node_record(_S(_N(1)), 99) is None


def test_absent_applied_params_never_read_as_agreement():
    """The v8-era record has no `applied_params` at all. The tool must say the proposal MAY NOT be
    what ran, because that record is exactly the one that put batch 8192 into a task goal on the
    strength of a champion that ran 512."""
    state = _S(_N(1, params={"train.batch": 8192.0}), _N(2, params={"train.batch": 4096.0}))
    out = "\n".join(nd.diff_params(_rec(state, 1), _rec(state, 2)))
    assert f"applied params: {NOT_RECOVERABLE}" in out
    assert "may not be what ran" in out
    # …and the proposal is still shown, because it is the only thing the record has.
    assert "train.batch" in out and "8192.0" in out and "4096.0" in out


def test_a_node_with_no_metric_is_not_a_node_with_a_zero():
    state = _S(_N(1, metric=0.8), _N(2))
    out = "\n".join(nd.diff_metrics(_rec(state, 1), _rec(state, 2)))
    assert "produced NO metric" in out and "delta: not computed" in out
    assert "+0" not in out and "-0" not in out


# ------------------------------------------------------------------ the comparability join
def _prov(key, authority="declared"):
    return {"comparability": {"keys": {authority: key}}}


def test_a_delta_is_never_printed_bare_when_the_two_are_not_known_to_be_comparable():
    """`comparability_notice` is deliberately non-empty for UNKNOWN as well as DIFFERENT. This
    section inherits that: a number and "may these be ranked" belong on the same line."""
    state = _S(_N(1, metric=0.70), _N(2, metric=0.76))
    out = "\n".join(nd.diff_metrics(_rec(state, 1), _rec(state, 2)))
    assert "+0.060000" in out and "COMPARABILITY UNKNOWN" in out

    differ = _S(_N(1, metric=0.70, provenance=_prov("aaa")),
                _N(2, metric=0.76, provenance=_prov("bbb")))
    out = "\n".join(nd.diff_metrics(_rec(differ, 1), _rec(differ, 2)))
    assert "NOT COMPARABLE" in out


def test_the_only_silent_delta_is_the_one_measured_on_one_scale():
    same = _S(_N(1, metric=0.70, provenance=_prov("aaa")),
              _N(2, metric=0.76, provenance=_prov("aaa")))
    out = "\n".join(nd.diff_metrics(_rec(same, 1), _rec(same, 2)))
    assert "+0.060000" in out and "same evaluation" in out
    assert "UNKNOWN" not in out and "NOT COMPARABLE" not in out


# ------------------------------------------------------------------ bounds that announce themselves
def test_a_truncated_file_diff_says_it_was_truncated():
    """A bounded view that does not say it is bounded reads as the whole record."""
    left = {"train.py": "\n".join(f"line {i}" for i in range(600))}
    right = {"train.py": "\n".join(f"LINE {i}" for i in range(600))}
    out = "\n".join(nd.diff_files(_S(_N(1, files=left), _N(2, files=right)).nodes[1] and
                                  _rec(_S(_N(1, files=left), _N(2, files=right)), 1),
                                  _rec(_S(_N(1, files=left), _N(2, files=right)), 2)))
    assert "more diff lines in train.py, not shown" in out


def test_a_truncated_answer_says_so_and_says_how_to_get_the_rest():
    left = {f"f{i}.py": f"a = {i}\n" for i in range(40)}
    right = {f"f{i}.py": f"a = {i + 1}\n" for i in range(40)}
    state = _S(_N(1, files=left), _N(2, files=right))
    out = nd.render_diff(_rec(state, 1), _rec(state, 2), max_answer=900)
    assert "ANSWER TRUNCATED" in out and "characters not shown" in out
    assert 'section="code"' in out


def test_engine_bookkeeping_is_excluded_from_the_diff_and_the_exclusion_is_counted():
    """A raw recursive diff of two real node workdirs returns `__pycache__/*.pyc`, `train.log` and
    `.looplab-manifest` before it reaches one line of experiment source. Excluding them is right;
    excluding them silently is not — the count is what stops "9 of 9 files differ" from being read
    as "the whole workdir differs"."""
    left = {"train.py": "a\n", "train.log": "one\n", "x/__pycache__/m.pyc": "\x00",
            ".looplab-manifest": "abc"}
    right = {"train.py": "b\n", "train.log": "two\n", "x/__pycache__/m.pyc": "\x01",
             ".looplab-manifest": "def"}
    out = "\n".join(nd.diff_files(_rec(_S(_N(1, files=left), _N(2, files=right)), 1),
                                  _rec(_S(_N(1, files=left), _N(2, files=right)), 2)))
    assert "1 of 1 experiment files differ" in out and ": train.py" in out
    assert "3 engine/output files not compared" in out
    assert "train.log" not in out and ".pyc" not in out


# ------------------------------------------------------------------ the refusal surface
def test_the_provider_offers_no_way_to_name_a_path(tools):
    """Read-only BY CONSTRUCTION, not by policy. There is no path argument, so there is nothing for
    a `..` to escape from — and this test is what keeps it that way when someone adds a convenience
    argument later."""
    props = tools.specs()[0]["function"]["parameters"]["properties"]
    assert set(props) == {"left", "right", "section"}
    assert not any(k in json.dumps(props).lower() for k in ("path", "file", "glob", "dir"))
    assert "no tool named" in tools.execute("read_file", {"path": "/etc/passwd"})


def test_the_refusals_name_what_exists(tools):
    assert "does not differ from" in tools.execute("diff_nodes", {"left": 3, "right": 3})
    assert "name two node ids" in tools.execute("diff_nodes", {"left": "x", "right": 3})
    assert "no section named" in tools.execute("diff_nodes",
                                               {"left": 0, "right": 3, "section": "secrets"})
    # `bool` is an `int` subclass — `int(True) == 1` must not silently answer about node 1.
    assert "name two node ids" in tools.execute("diff_nodes", {"left": True, "right": 3})


def test_an_unbound_provider_answers_rather_than_raising(real_state):
    """Providers soft-fail: a tool call before `bind_state`, or against a half-built state, is "no
    nodes yet", never an exception out of `execute` that ends the agent's turn."""
    t = NodeDiffTools()
    assert "no node" in t.execute("diff_nodes", {"left": 0, "right": 1})
    assert t.specs()[0]["function"]["name"] == "diff_nodes"
    t.bind_state(object())          # a state with no `.nodes`
    assert "no node" in t.execute("diff_nodes", {"left": 0, "right": 1})


# ------------------------------------------------------------------ against the real run
def test_the_files_shown_are_the_ones_the_node_LAST_ran(real_state):
    """A node repaired five times produced its metric from the FIFTH write. v8 node 3 was repaired
    five times and ended with SEVEN files against the five it was created with; showing the
    created set beside the metric would attribute the number to code that did not produce it."""
    created = {}
    last_repair = {}
    for line in _RUN.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        d = e.get("data") or {}
        if not isinstance(d.get("files"), dict):
            continue
        if e["type"] == "node_created":
            created.setdefault(d.get("node_id"), set(d["files"]))
        elif e["type"] == "node_repaired":
            last_repair[d.get("node_id")] = set(d["files"])
    assert len(created[3]) == 5 and len(last_repair[3]) == 7      # the record, re-derived here
    assert set(nd.node_record(real_state, 3)["files"]) == last_repair[3]


def test_the_real_champion_pair_reports_a_gain_it_cannot_certify(real_state):
    """v8 node 3 (0.762048) against node 0 (0.736689). Every part of the answer is a fact about the
    record rather than about the tool: nine proposal coordinates move, nothing says what RAN, and
    the two numbers are not known to be on one scale."""
    t = NodeDiffTools()
    t.bind_state(real_state)
    out = t.execute("diff_nodes", {"left": 0, "right": 3, "section": "params"})
    assert "9 of 22 coordinates differ" in out
    assert "loss.rdrop_alpha" in out and "loss.dcl" in out
    assert f"applied params: {NOT_RECOVERABLE}" in out

    metric = t.execute("diff_nodes", {"left": 0, "right": 3, "section": "metric"})
    assert "0.736689" in metric and "0.762048" in metric and "+0.025359" in metric
    assert "COMPARABILITY UNKNOWN" in metric


def test_the_code_section_finds_the_change_a_metric_would_be_attributed_to(real_state):
    t = NodeDiffTools()
    t.bind_state(real_state)
    out = t.execute("diff_nodes", {"left": 0, "right": 3, "section": "code"})
    assert "experiment files differ" in out
    assert "vectorsearch/training/loss.py" in out
    assert "run_name" in out                    # a real hunk body, not just a file list


# ------------------------------------------------------------------ the wiring
def test_the_tool_reaches_the_agents_that_propose_and_judge():
    """Wired in `_shared_providers`, the ONE assembly the Researcher, the Strategist and the unified
    pilot all build from — asserted by driving the assembly, not by grepping for the import."""
    from looplab.agents.factory import _shared_providers

    class _T:
        pass

    settings = type("S", (), {"researcher_tools": True, "cross_run_tools": False,
                              "all_runs_tools": False, "cross_run_read_tools": False,
                              "memory_dir": None, "knowledge_dir": None})()
    for kwargs in ({}, {"core_only": True}, {"role": "strategist"}):
        providers = _shared_providers(_T(), settings, None, **kwargs)
        names = {s["function"]["name"] for p in providers for s in p.specs()}
        assert "diff_nodes" in names, kwargs
