"""The Developer's outputs are a RETURN VALUE, and the paid calls that produce them leave the loop
(doc 52 row 12; the three markers `developer-output-has-no-immutable-envelope`,
`repair-path-holds-the-engine-loop`, `serial-node-build-holds-the-loop`).

What held the loop, measured: ZERO ticks during a triage/repair whose median is 116-276 s (one case
88.3 min), and a dead node that waited 62 minutes for its terminal while both H200s idled because the
loop was inside a serial build. What stopped the offload: the Developer left its outputs on the
SHARED instance (`DEVELOPER_OUTPUT_ATTRS`) and the engine read them back afterwards — safe only
because the freeze let no sibling run in between. So the envelope came first and the offloads after.

What this file drives, in the order the risk runs:
  1. THE ENVELOPE — its field set IS the registry plus `code`, it is immutable, and its capture is
     total over junk;
  2. THE LOCK — two calls on ONE shared instance from two threads cannot interleave their outputs:
     each caller gets exactly what its own call produced;
  3. THE LOOP, repair path — the engine's own tick counter keeps advancing while a blocking
     `repair` runs, and the node is still repaired;
  4. THE LOOP, serial build — the same counter advances while a blocking `implement` runs, and the
     nodes are still created;
  5. the wiring, by AST: every build site and the three repair-path calls leave through the
     offload helpers, and no engine site reads a side channel off the shared instance any more.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import textwrap
import threading
import time
from types import MappingProxyType, SimpleNamespace

import anyio
import pytest

from looplab.agents.roles import DEVELOPER_OUTPUT_ATTRS, DeveloperResult, developer_call_lock
from looplab.core.models import Idea
from looplab.engine.node_build import NodeBuildMixin
from tests._source_scan import function_tree


# ------------------------------------------------------------------------------- 1. THE ENVELOPE
def test_the_envelope_is_the_registry_plus_code_and_is_immutable():
    fields = {f.name for f in dataclasses.fields(DeveloperResult)}
    assert fields == set(DEVELOPER_OUTPUT_ATTRS) | {"code"}, (
        "a registry member with no envelope field is a side channel the engine can no longer read; "
        "an envelope field with no registry member is a channel no Developer produces")
    result = NodeBuildMixin._capture_developer_result(
        SimpleNamespace(last_files={"a.py": "x"}, last_deleted=["b.py"], last_footprint={"gpus": 2},
                        last_rollback_stage=" train ", last_budget_exhausted="time", last_edit_calls=3,
                        last_report="rep", last_seed="seed", last_run="run", last_patch={"ok": True}),
        "code")
    assert result.code == "code" and result.last_files == {"a.py": "x"}
    assert result.last_deleted == ("b.py",) and result.last_footprint == {"gpus": 2}
    assert result.last_rollback_stage == "train" and result.last_budget_exhausted == "time"
    assert result.last_edit_calls == 3 and result.last_patch == {"ok": True}
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.code = "other"
    with pytest.raises(TypeError):
        result.last_files["c.py"] = "y"                      # a read-only mapping
    assert isinstance(result.last_files, MappingProxyType)


def test_the_capture_is_total_over_junk_and_over_a_bare_stub():
    junk = SimpleNamespace(last_files="not a dict", last_deleted="x", last_footprint="f",
                           last_edit_calls="many", last_budget_exhausted="t" * 80)
    result = NodeBuildMixin._capture_developer_result(junk, None)
    assert result.code is None and result.last_files == {} and result.last_deleted == ()
    assert result.last_footprint == "f" and result.last_edit_calls == 0
    assert len(result.last_budget_exhausted) == 32
    bare = NodeBuildMixin._capture_developer_result(SimpleNamespace(), "c")
    assert bare == DeveloperResult(code="c")
    failed = DeveloperResult.failed("(developer error: boom)")
    assert failed.code.startswith("(developer error") and failed.last_files == {}


# ----------------------------------------------------------------------------------- 2. THE LOCK
class _Shared:
    """A Developer whose outputs land on the INSTANCE, called from two threads at once."""

    def __init__(self):
        self.busy = 0
        self.overlap = False

    def implement(self, tag):
        self.busy += 1
        if self.busy > 1:
            self.overlap = True
        self.last_files = {f"{tag}.py": tag}
        time.sleep(0.05)                                 # long enough for the other thread to arrive
        self.busy -= 1
        return tag


def test_two_offloaded_calls_on_one_instance_cannot_clobber_each_other():
    dev = _Shared()
    host = NodeBuildMixin.__new__(NodeBuildMixin)
    got = {}

    def _call(tag):
        got[tag] = host._run_developer(dev, dev.implement, tag)

    threads = [threading.Thread(target=_call, args=(tag,)) for tag in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not dev.overlap, "the per-instance lock must serialise the calls"
    for tag in ("a", "b", "c"):
        assert got[tag].code == tag and got[tag].last_files == {f"{tag}.py": tag}, got[tag]
    assert developer_call_lock(dev) is developer_call_lock(dev)
    assert developer_call_lock(dev) is not developer_call_lock(_Shared())
    assert developer_call_lock(SimpleNamespace()) is not None            # weakref-able
    assert developer_call_lock(object()) is not None                     # and the id fallback


# ------------------------------------------------------------------------- 3. THE LOOP (repair)
_BAD = "import definitely_not_a_real_module_zzz\n"
_GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"


class _Ticks:
    """The loop's own counter, observed FROM INSIDE the blocking call (a count taken from the start
    of the test cannot discriminate: a frozen loop still shows the ticks it accumulated before)."""

    def __init__(self):
        self.n = 0
        self.observed = {}
        self.release = threading.Event()

    async def run(self):
        while not self.release.is_set():
            self.n += 1
            await anyio.sleep(0.005)

    def watch(self, label, seconds=0.15):
        self.observed[f"{label}:before"] = self.n
        time.sleep(seconds)
        self.observed[f"{label}:after"] = self.n

    def advanced(self, label) -> bool:
        return self.observed.get(f"{label}:after", 0) > self.observed.get(f"{label}:before", 0)


class _Researcher:
    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})


class _CrashThenSlowFix:
    def __init__(self, ticks):
        self.ticks = ticks
        self.repairs = 0

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.repairs += 1
        self.ticks.watch("repair")
        self.last_files = {"solution.py": _GOOD}
        return _GOOD


def _toy_engine(tmp_path, developer, **kw):
    from pathlib import Path

    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    kw.setdefault("auto_install_deps", False)
    return Engine(tmp_path / "run", task=task, researcher=_Researcher(), developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2), **kw)


@pytest.mark.anyio
async def test_the_engine_keeps_turning_while_a_repair_is_in_flight(tmp_path):
    ticks = _Ticks()
    dev = _CrashThenSlowFix(ticks)
    engine = _toy_engine(tmp_path, dev)
    async with anyio.create_task_group() as tg:
        tg.start_soon(ticks.run)
        with anyio.fail_after(120):
            state = await engine.run()
        ticks.release.set()
    assert dev.repairs >= 1, "the fixture must reach the repair path"
    assert ticks.advanced("repair"), (
        f"the loop must keep turning while the Developer repairs: {ticks.observed} — a frozen loop "
        "is what left a dead node without a terminal for 62 minutes")
    # The repair still landed: the node was fixed in place and scored.
    assert any(n.metric is not None for n in state.nodes.values())
    rows = [e for e in engine.store.read_all() if e.type == "node_repaired"]
    assert rows and rows[0].data.get("files", {}).get("solution.py") == _GOOD, (
        "the envelope's files must be what the durable row carries")


# ------------------------------------------------------------------- 4. THE LOOP (serial build)
class _SlowBuilder:
    def __init__(self, ticks):
        self.ticks = ticks
        self.builds = 0

    def implement(self, idea):
        self.builds += 1
        self.ticks.watch("build")
        self.last_files = {"solution.py": _GOOD}
        return _GOOD


@pytest.mark.anyio
async def test_the_engine_keeps_turning_while_a_serial_build_is_in_flight(tmp_path):
    ticks = _Ticks()
    dev = _SlowBuilder(ticks)
    engine = _toy_engine(tmp_path, dev)
    async with anyio.create_task_group() as tg:
        tg.start_soon(ticks.run)
        with anyio.fail_after(120):
            state = await engine.run()
        ticks.release.set()
    assert dev.builds >= 1
    assert ticks.advanced("build"), (
        f"the loop must keep turning while the Developer builds: {ticks.observed}")
    assert len(state.nodes) >= 1 and all(n.files.get("solution.py") == _GOOD
                                         for n in state.nodes.values())


# -------------------------------------------------------------------------------- 5. THE WIRING
def _calls(fn):
    return [n for n in ast.walk(function_tree(fn)) if isinstance(n, ast.Call)]


def _attr_calls(fn, name):
    return [c for c in _calls(fn) if getattr(c.func, "attr", None) == name]


def test_every_build_site_leaves_the_loop_through_the_offload_helper():
    from looplab.engine.orchestrator import Engine

    creates = _attr_calls(Engine._handle_create_actions, "_create_node")
    assert not creates, "the serial lane must not build on the loop thread"
    offloads = _attr_calls(Engine._handle_create_actions, "_offload_node_build")
    assert len(offloads) == 2, "both serial sites (card-reserved and plain) go through the helper"
    # the helper itself wraps the real build and rides the proposal pool
    tree = function_tree(Engine._offload_build)
    assert any(getattr(c.func, "attr", None) == "run_sync" for c in ast.walk(tree)
               if isinstance(c, ast.Call))
    assert any(isinstance(n, ast.Name) and n.id == "proposal_limiter" for n in ast.walk(tree))
    assert any(isinstance(n, ast.Attribute) and n.attr == "_create_node"
               for n in ast.walk(function_tree(Engine._offload_node_build)))


def test_the_three_repair_path_calls_leave_through_the_sink_helper():
    from looplab.engine.evaluate import EvaluateMixin

    src = inspect.getsource(EvaluateMixin._evaluate)
    tree = ast.parse(textwrap.dedent(src))
    offloaded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "_offload_under_proposal_sink":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr in (
                        "_triage_crash", "_repair_result", "_repair_critic"):
                    offloaded.add(inner.attr)
    assert offloaded == {"_triage_crash", "_repair_result", "_repair_critic"}, offloaded
    # ... and none of the three is called directly on the loop thread any more
    direct = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and getattr(n.func, "attr", None) in ("_triage_crash", "_repair", "_repair_result",
                                                     "_repair_critic")]
    assert direct == [], [ast.unparse(d)[:60] for d in direct]


def test_no_engine_site_reads_a_side_channel_off_the_shared_instance():
    """The capture is the ONE reader. A `getattr(self.developer, "last_…")` anywhere else in the
    engine is the shared-instance gap coming back under a different name."""
    from tests._source_scan import iter_trees
    from pathlib import Path

    engine_dir = Path(__file__).resolve().parents[1] / "looplab" / "engine"
    offenders = []
    for path, tree in iter_trees(engine_dir):
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getattr"
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                    and str(node.args[1].value) in ("last_files", "last_deleted",
                                                     "last_rollback_stage", "last_budget_exhausted",
                                                     "last_edit_calls")):
                continue
            target = node.args[0]
            if isinstance(target, ast.Attribute) and target.attr == "developer":
                offenders.append(f"{path.name}:{node.lineno}: {ast.unparse(node)[:80]}")
    assert offenders == [], "\n".join(offenders)
