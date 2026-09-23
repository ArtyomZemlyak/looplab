"""A node whose declared new path never ran did not measure its idea: `inert_path`, metric withheld.

Measured 2026-09-23 on a MiniOneRec inference run. A node added a prefix-cached prefill behind a
self-check; the check died on an attribute transformers 5 removed, a fallback switched the path off,
and the node scored 1.004 with every list byte-identical to its parent -- recorded as an idea that
does not help, when the idea never executed. Both outcomes finish cleanly and print a number, so the
engine had no way to tell them apart until the node could SAY what running looks like.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import anyio

from looplab.adapters.toytask import ToyTask
from looplab.core.models import FAILURE_REASONS, Idea
from looplab.engine import activation
from looplab.engine.metric_salvage import NEVER_SALVAGED_REASONS
from looplab.engine.orchestrator import Engine
from looplab.engine.triage import _failure_reason
from looplab.events.eventstore import EventStore
from looplab.runtime.sandbox import RunResult, SubprocessSandbox
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


# ------------------------------------------------------------ the declaration

def test_markers_are_cleaned_and_bounded():
    assert activation.normalize_markers(["  prefix cache: ON ", "", 7, "prefix cache: ON"]) == [
        "prefix cache: ON"]
    assert activation.normalize_markers("one") == ["one"]
    assert activation.normalize_markers({"not": "a list"}) == []
    many = [f"m{i}" for i in range(20)]
    assert len(activation.normalize_markers(many)) == activation.MAX_MARKERS
    assert len(activation.normalize_markers(["x" * 999])[0]) == activation.MAX_MARKER_CHARS


def test_a_malformed_declaration_is_no_declaration(tmp_path):
    (tmp_path / activation.ACTIVATION_MANIFEST_NAME).write_text("{not json")
    assert activation.read_markers(tmp_path) == []
    (tmp_path / activation.ACTIVATION_MANIFEST_NAME).write_text(json.dumps({"markers": ["on"]}))
    assert activation.read_markers(tmp_path) == ["on"]
    assert activation.read_markers(tmp_path / "missing") == []


# ------------------------------------------------------------ the check

def test_a_marker_in_the_captured_streams_or_a_fresh_log_counts(tmp_path):
    assert activation.missing_markers(["ON"], texts=("warmup... ON\n",)) == []
    (tmp_path / "score.log").write_text("prefix cache: ON\n")
    assert activation.missing_markers(["prefix cache: ON"], texts=("",), workdir=tmp_path,
                                      since=time.time() - 60) == []
    assert activation.missing_markers(["never printed"], texts=("x",), workdir=tmp_path) == [
        "never printed"]


def test_a_log_left_by_an_earlier_attempt_does_not_vouch_for_this_one(tmp_path):
    stale = tmp_path / "score.log"
    stale.write_text("prefix cache: ON\n")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    assert activation.missing_markers(["prefix cache: ON"], texts=(), workdir=tmp_path,
                                      since=time.time()) == ["prefix cache: ON"]


def test_the_match_is_exact_and_case_sensitive():
    assert activation.missing_markers(["Cache: ON"], texts=("cache: on",)) == ["Cache: ON"]


# ------------------------------------------------------------ the reason

def test_inert_path_is_a_registered_reason_that_is_never_salvaged():
    assert "inert_path" in FAILURE_REASONS
    assert "inert_path" in NEVER_SALVAGED_REASONS


def test_the_classifier_names_it_off_the_engine_s_own_flag():
    res = RunResult(exit_code=0, stdout="", stderr="", metric=None, timed_out=False,
                    inert_path={"missing": ["ON"], "metric": 1.004})
    assert _failure_reason(res) == "inert_path"
    clean = RunResult(exit_code=0, stdout="", stderr="", metric=None, timed_out=False)
    assert _failure_reason(clean) != "inert_path"


# ------------------------------------------------------------ driven through the real engine

class _Dev:
    def __init__(self):
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.errors: list = []

    def implement(self, idea):
        return "print('unused')\n"

    def repair(self, idea, code, error):
        self.errors.append(error)
        return code


class _Researcher:
    """Repairs once, then gives up -- so the test sees exactly one repair and then the terminal."""

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def triage_crash(self, node, error, attempt, **kw):
        if attempt <= 1:
            return {"action": "repair", "rationale": "make the declared path run"}
        return {"action": "abandon", "rationale": "stop after one look"}


def _drive(tmp_path, *, prints: str, markers):
    """Seed one node whose files declare `markers`, and run the REAL `_evaluate` over a command that
    prints `prints` and then its metric."""
    run_dir = tmp_path / "run"
    dev = _Dev()
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=_Researcher(), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True)
    code = f"print({prints!r}); print('METRIC: 0.5')"
    eng._eval_spec = {"command": ["python", "-c", code], "cwd": ".",
                      "metric": {"kind": "stdout_regex", "pattern": "METRIC: ([0-9.]+)"},
                      "timeout": 120.0}
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    files = {}
    if markers is not None:
        files[activation.ACTIVATION_MANIFEST_NAME] = activation.manifest_text(markers)
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": "print('unused')\n", "files": files})

    async def _bounded() -> bool:
        with anyio.move_on_after(300) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the eval did not terminate"
    events = [e for e in EventStore(run_dir / "events.jsonl").read_all()
              if e.type in ("node_evaluated", "node_failed") and e.data.get("node_id") == 0]
    return events, dev


def test_a_declared_path_that_never_announced_itself_is_withheld_and_sent_to_repair(tmp_path):
    events, dev = _drive(tmp_path, prints="RECO_PREFIX_CACHED_PREFILL disabled: verification raised",
                         markers=["prefix cache: ON"])
    assert [e.type for e in events] == ["node_failed"], events
    assert events[0].data.get("reason") == "inert_path"
    assert dev.errors, "the node must reach repair, not a terminal verdict on its idea"
    assert "[inert_path]" in dev.errors[0] and "'prefix cache: ON'" in dev.errors[0]
    assert "0.5" in dev.errors[0], "the withheld number is named, so the repair knows what it measured"


def test_a_path_that_announced_itself_is_scored_as_usual(tmp_path):
    events, dev = _drive(tmp_path, prints="prefix cache: ON", markers=["prefix cache: ON"])
    assert [e.type for e in events] == ["node_evaluated"], events
    assert events[0].data.get("metric") == 0.5
    assert dev.errors == []


def test_a_node_that_declares_nothing_is_judged_exactly_as_before(tmp_path):
    events, _dev = _drive(tmp_path, prints="anything at all", markers=None)
    assert [e.type for e in events] == ["node_evaluated"]
    events, _dev = _drive(tmp_path / "empty", prints="anything at all", markers=[])
    assert [e.type for e in events] == ["node_evaluated"]


# ------------------------------------------------------------ the Developer declares it

def _build(monkeypatch, done_args, *, plan_steps=()):
    """A REAL `LLMRepoDeveloper.implement()` whose `done` carries `done_args`; returns its files."""
    import sys
    import looplab.agents.agent as agent_mod
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": list(plan_steps)})
        tools.execute("write_file", {"path": "solution.py", "content": "print('prefix cache: ON')\n"})
        return finalize(dict(done_args))

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task, plan_decompose=bool(plan_steps), plan_min_steps=2)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    return dev.last_files


def test_both_dones_offer_the_declaration():
    from looplab.adapters.repo_task import LLMRepoDeveloper
    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    for spec in (dev._emit_spec(), dev._repair_emit_spec()):
        props = spec["function"]["parameters"]["properties"]
        assert props["activation_markers"]["type"] == "array"
        assert "withheld" in props["activation_markers"]["description"]


def test_a_declared_marker_is_written_into_the_node_s_files(monkeypatch):
    files = _build(monkeypatch, {"summary": "s", "activation_markers": ["prefix cache: ON"]})
    assert json.loads(files[activation.ACTIVATION_MANIFEST_NAME]) == {"markers": ["prefix cache: ON"]}


def test_the_last_plan_step_declares_it_too(monkeypatch):
    files = _build(monkeypatch, {"summary": "s", "activation_markers": ["prefix cache: ON"]},
                   plan_steps=["write it", "wire it"])
    assert json.loads(files[activation.ACTIVATION_MANIFEST_NAME]) == {"markers": ["prefix cache: ON"]}


def test_no_declaration_writes_no_file(monkeypatch):
    assert activation.ACTIVATION_MANIFEST_NAME not in _build(monkeypatch, {"summary": "s"})
