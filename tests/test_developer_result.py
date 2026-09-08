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
from looplab.engine.audit import AuditMixin
from looplab.engine.node_build import NodeBuildMixin
from tests._source_scan import function_tree
from tests.factories import make_engine


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
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    kw.setdefault("auto_install_deps", False)
    return make_engine(tmp_path / "run", task=task, researcher=_Researcher(),
                       developer=developer, n_seeds=1, max_nodes=2, **kw)


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
    # …the helper leaves the loop and rides the proposal pool, and does so THROUGH THE SINK. It
    # called `run_sync` directly until 2026-09-07, which is how `_create_node`'s novelty appends
    # (`_prepare_node_idea` -> `_apply_novelty_gate` -> `_append_proposal_event`) came to land from
    # a worker thread — FOLDED, authority-bearing rows, the breach the other two offloaded lanes
    # were each fixed for. `run_sync` is now one hop down inside
    # `novelty.py::_offload_under_proposal_sink`; asserting it HERE again would re-admit the bare
    # form, so what is pinned is the seam that buffers those intents and publishes them on the main
    # task. `tests/test_offload_lane_writes_no_folded_events.py` drives the property itself.
    tree = function_tree(Engine._offload_build)
    assert any(getattr(c.func, "attr", None) == "_offload_under_proposal_sink"
               for c in ast.walk(tree) if isinstance(c, ast.Call)), (
        "the serial build lane must leave the loop under the proposal sink, not through a bare "
        "to_thread — its build reaches `_append_proposal_event`")
    assert any(isinstance(n, ast.Name) and n.id == "proposal_limiter" for n in ast.walk(tree))
    assert any(isinstance(n, ast.Attribute) and n.attr == "_create_node"
               for n in ast.walk(function_tree(Engine._offload_node_build)))


def test_the_three_repair_path_calls_leave_through_the_sink_helper():
    from tests._source_scan import eval_attempt_tree

    tree = eval_attempt_tree()     # the driver AND its phases: the three calls live in two of them
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


def test_every_registered_side_channel_is_actually_READ_into_the_envelope():
    """The field-set pin cannot see a field nobody reads, and `last_budget_facts` proved it.

    That field was added to `DEVELOPER_OUTPUT_ATTRS` and to `DeveloperResult`, and
    `_capture_developer_result` was not extended — so a session cut off by its money ceiling wrote
    the dict onto the instance and the envelope reported `None`, with every existing guard green.
    Every engine site is being migrated off instance reads onto this envelope, so the first consumer
    to move would have recorded "the session was not cut off".

    DRIVEN, not pinned: a Developer carrying a DISTINCT sentinel per registered attribute must come
    back with every one of them, which no amount of listing the names can fake.

    MUTATION: drop any `getattr(developer, "<attr>", …)` line -> that channel reads as its falsy
    default and this test names it.
    """
    from looplab.agents.roles import DEVELOPER_OUTPUT_ATTRS
    from looplab.engine.node_build import NodeBuildMixin

    # A value per attribute that survives its own coercion, so "captured" and "defaulted" differ.
    sentinels = {
        "last_files": {"solver.py": "x = 1\n"},
        "last_deleted": ["gone.py"],
        "last_footprint": {"gpus": 2},
        "last_report": "a report",
        "last_seed": "a seed",
        "last_run": "a run",
        "last_patch": {"ok": True},
        "last_rollback_stage": "train",
        "last_budget_exhausted": "cost",
        "last_budget_facts": {"kind": "cost", "seconds": 42.0, "detail": "ceiling"},
        "last_edit_calls": 7,
    }
    assert set(sentinels) == set(DEVELOPER_OUTPUT_ATTRS), (
        "a side channel was registered or removed; give it a distinct sentinel here so this test "
        "can tell a captured value from a defaulted one")

    dev = type("_Dev", (), dict(sentinels))()
    envelope = NodeBuildMixin._capture_developer_result(dev, "code")
    unread = [name for name in DEVELOPER_OUTPUT_ATTRS
              if not getattr(envelope, name) and sentinels[name]]
    assert not unread, f"registered but never read into the envelope: {unread}"


def test_the_clear_is_inside_the_locked_window_and_not_at_the_call_site():
    """A build's own footprint survives a sibling call on the SHARED instance, both directions.

    `_reset_developer_footprint` used to be called by the five build sites themselves, before the
    Developer call — an UNLOCKED write to a possibly-shared instance. That was safe only while every
    such site ran on the loop thread; once the serial build, the fork's build and the node-reset
    rebuild moved off it (`orchestrator.py::_offload_build`, 2026-09-06), the write could land inside
    another caller's locked window. Driven here rather than pinned, because the defect is a
    schedule: the call site is one `with` away from looking correct in either arrangement.
    """
    class _Dev:
        """Sets its footprint DURING the call, as a real Developer does."""
        def __init__(self):
            self.last_footprint = None
            self.last_files = {}

        def implement(self, _idea):
            self.last_footprint = {"gpus": 4}
            time.sleep(0.15)                       # the paid call's window
            return "code"

        def repair(self, _idea, _code, _err):
            self.last_footprint = {"gpus": 1}      # a sibling leaving its own value behind
            return "repaired"

    engine = NodeBuildMixin.__new__(type("_E", (NodeBuildMixin,), {}))
    dev = _Dev()

    # (1) A sibling call that starts INSIDE this one's window must not reach its outputs.
    captured: dict = {}

    def _build():
        captured["res"] = NodeBuildMixin._run_developer(engine, dev, dev.implement, {"i": 1})

    def _sibling():
        time.sleep(0.05)
        NodeBuildMixin._run_developer(engine, dev, dev.repair, {"i": 2}, "code", "err")

    threads = [threading.Thread(target=_build), threading.Thread(target=_sibling)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert captured["res"].last_footprint == {"gpus": 4}, (
        "a concurrent call cleared or overwrote this build's footprint: the clear must happen "
        "inside the same `developer_call_lock` window as the call and the capture")

    # (2) The clear still HAPPENS: a Developer that omits the optional output reads as "no
    # estimate" and never inherits its predecessor's — the leak the clear exists to prevent.
    class _Omits(_Dev):
        def implement(self, _idea):
            return "code"                          # sets nothing

    stale = _Omits()
    stale.last_footprint = {"gpus": 8}             # a predecessor's leftover on the instance
    assert NodeBuildMixin._run_developer(
        engine, stale, stale.implement, {"i": 3}).last_footprint is None


def test_the_state_BIND_is_inside_the_locked_window_too(tmp_path):
    """The clear's unlocked sibling, missed when the clear moved in.

    `_implement_result` and `_repair_result` each called `bind_state(state)` on the shared Developer
    ABOVE `_run_developer`'s lock. `bind_state` is a plain write (`repo_developer` stores
    `self._memory_state = state`), so with two offloaded calls on one instance worker A could bind
    its fold, block on the lock, and then run its build against the fold worker B bound while A was
    waiting — the Developer's memory and cross-run providers answering about a different lifecycle
    than the node being built. Same class as the footprint clear, same window, one method apart.

    Driven, not pinned: the bind is one `with` away from looking correct in either arrangement.
    """
    class _Dev:
        """Records the state that was bound WHEN THE CALL RAN, which is the only one that matters."""
        def __init__(self):
            self.bound = None
            self.saw: dict = {}
            self.last_footprint, self.last_files = None, {}

        def bind_state(self, state, parent=None):
            self.bound = state

        def implement(self, idea):
            time.sleep(0.15)                       # the paid call's window
            self.saw[idea["i"]] = self.bound       # what THIS call was working against
            return "code"

        def repair(self, idea, _code, _err):
            self.saw[idea["i"]] = self.bound
            return "repaired"

    engine = NodeBuildMixin.__new__(type("_E", (NodeBuildMixin,), {}))
    dev = _Dev()
    state_a, state_b = {"fold": "A"}, {"fold": "B"}

    def _build():
        NodeBuildMixin._run_developer(engine, dev, dev.implement, {"i": 1}, bind_to=state_a)

    def _sibling():
        time.sleep(0.05)                           # lands inside the build's window
        NodeBuildMixin._run_developer(engine, dev, dev.repair, {"i": 2}, "code", "err",
                                      bind_to=state_b)

    threads = [threading.Thread(target=_build), threading.Thread(target=_sibling)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert dev.saw[1] is state_a, (
        "a concurrent call re-bound the Developer inside this build's window: the bind must happen "
        "inside the same `developer_call_lock` window as the call and the capture")
    assert dev.saw[2] is state_b

    # …and a caller that asks for NO bind is distinguishable from one that binds None, which is
    # what `_repair_result` does when it was given no state.
    quiet = _Dev()
    quiet.bound = state_a
    NodeBuildMixin._run_developer(engine, quiet, quiet.implement, {"i": 3})
    assert quiet.saw[3] is state_a, "an omitted bind must not overwrite what was already bound"
    NodeBuildMixin._run_developer(engine, quiet, quiet.implement, {"i": 4}, bind_to=None)
    assert quiet.saw[4] is None, "binding None is a bind, not an omission"


def test_the_agent_report_is_emitted_from_the_envelope_not_the_shared_instance():
    """`DeveloperResult.last_report` was captured under the lock and then read by nobody.

    `_emit_agent_report` read `last_report` off the ACTIVE developer, and it runs AFTER
    `developer_call_lock` has been released. Between a worker returning from `_run_developer` and
    reaching that line, a sibling's `_discard_node_build_telemetry` does `setattr(current,
    'last_report', None)` — also unlocked — so this node emits no `agent_validated` row at all; with
    the opposite interleaving it emits the sibling's report against this node's id. Both are silent,
    which is what makes the ADR-7 audit trail wrong or missing with nothing red.

    The instance is CLOBBERED here between the call and the emit, which is the schedule stated as a
    fixture: with the envelope's copy the row still describes the call that produced it.
    """
    class _Report:
        def __init__(self, name):
            self.name = name

        def summary(self):
            return {"report": self.name}

    class _Dev:
        def __init__(self):
            self.last_report, self.last_files, self.last_footprint = None, {}, None

        def implement(self, _idea):
            self.last_report = _Report("mine")
            return "code"

    appended: list = []

    class _Engine(NodeBuildMixin, AuditMixin):
        store = SimpleNamespace(append=lambda t, d: appended.append((t, d)))

    engine = _Engine.__new__(_Engine)
    dev = _Dev()
    built = NodeBuildMixin._run_developer(engine, dev, dev.implement, {"i": 1})

    dev.last_report = None            # a sibling's `_discard_node_build_telemetry`, unlocked
    engine._emit_agent_report(7, 0, developer=dev, report=built.last_report)
    assert [d["report"] for _t, d in appended] == ["mine"], appended
    assert appended[0][1]["node_id"] == 7

    # …and the other interleaving: the sibling left ITS report behind, and the envelope wins.
    appended.clear()
    dev.last_report = _Report("a sibling's")
    engine._emit_agent_report(7, 0, developer=dev, report=built.last_report)
    assert [d["report"] for _t, d in appended] == ["mine"], appended

    # An OMITTED report still falls back to the instance — the paths that made no fresh Developer
    # call (a pre-coded producer commit) have no envelope to hand over.
    appended.clear()
    engine._emit_agent_report(7, 0, developer=dev)
    assert [d["report"] for _t, d in appended] == ["a sibling's"], appended
    # …but a build whose Developer genuinely reported NOTHING must not fall back to it.
    appended.clear()
    engine._emit_agent_report(7, 0, developer=dev, report=None)
    assert appended == [], appended


def test_a_best_of_n_audit_row_describes_the_candidate_that_SHIPPED():
    """The eleventh registered member, and the one a `setattr` could not reach.

    `WrapsDeveloper.last_report` reads THROUGH to the inner developer — correct for a single-shot
    wrapper, wrong for best-of-N: N candidates each overwrite the inner's report, so the read-through
    describes candidate N while `last_files`, the footprint and the other eight describe the one
    that was PICKED. `_emit_agent_report` writes that value to the durable `agent_validated` row and
    `_capture_developer_result` copies the same read-through into the envelope, so a best-of-N
    node's ADR-7 audit trail described a candidate that did not ship, beside files that did.

    A property cannot be assigned, so the per-candidate snapshot loop raised `AttributeError` on
    exactly this member and swallowed it — the mixture was invisible. It goes through the wrapper's
    own `last_report` property now, and the single-shot paths still read through.
    """
    from looplab.search.best_of_n import BestOfNDeveloper

    class _Inner:
        def __init__(self):
            self.last_files, self.last_deleted, self.last_footprint = {}, [], None
            self.last_report, self.calls = "before-any-build", 0

        def implement(self, _idea):
            self.calls += 1
            self.last_report = f"report-{self.calls}"
            # Candidate 1 scores better, so candidate 2 is the one that must NOT be reported.
            return "def solve():\n    return 1\n" if self.calls == 1 else "x"

        def repair(self, _idea, _code, _err):
            self.last_report = "repair-report"
            return "fixed"

    inner = _Inner()
    dev = BestOfNDeveloper(inner, n=2, listwise=False, foresight=False)
    shipped = dev.implement(object())

    assert shipped.startswith("def solve"), "fixture: candidate 1 must be the pick"
    assert inner.last_report == "report-2", "fixture: the inner must have moved on"
    assert dev.last_report == "report-1", "the audit row describes a candidate that did not ship"
    assert NodeBuildMixin._capture_developer_result(dev, shipped).last_report == "report-1"

    # A repair is single-shot: the pick is dropped and the read-through applies again, or a repair
    # would report the previous BUILD's chosen candidate.
    dev.repair(object(), "code", "err")
    assert dev.last_report == "repair-report"

    # …and n == 1 is a transparent pass-through, as it has always been.
    solo_inner = _Inner()
    solo = BestOfNDeveloper(solo_inner, n=1, listwise=False, foresight=False)
    solo.implement(object())
    solo_inner.last_report = "moved on"     # …and it is a READ-THROUGH, not a stored copy
    assert solo.last_report == "moved on"


def test_the_discard_can_still_clear_a_best_of_n_report():
    """A read-only property silently disarmed `_discard_node_build_telemetry`.

    The discard clears an abandoned build's channels with
    `if hasattr(current, attr): setattr(current, attr, None); break`, inside a blanket
    `except (AttributeError, TypeError): pass`. Against a property with NO setter that `setattr`
    raises, the handler swallows it, the `break` never runs, and the walk nulls the INNER developer
    instead — which was sufficient while `last_report` read through to that inner, and stopped being
    sufficient the moment the wrapper began answering from its own chosen-candidate slot.

    So the fix that made the audit row name the shipped candidate re-opened the failure the discard
    exists to prevent: an abandoned build's report stands on the instance, and the one
    `_emit_agent_report` still on the instance read (`speculation.py`'s producer emit) stamps it on
    the NEXT node. Driven, and A/B'd against the parent commit — the discard worked before, and did
    not after.

    MUTATION: delete the `@last_report.setter` -> this is red.
    """
    from looplab.search.best_of_n import BestOfNDeveloper

    class _Inner:
        def __init__(self):
            self.last_files, self.last_deleted, self.last_footprint = {}, [], None
            self.last_report, self.calls = "before-any-build", 0

        def implement(self, _idea):
            self.calls += 1
            self.last_report = f"cand-{self.calls}"
            return "def solve():\n    return 1\n" if self.calls == 1 else "x"

    inner = _Inner()
    dev = BestOfNDeveloper(inner, n=2, listwise=False, foresight=False)
    dev.implement(object())
    assert dev.last_report == "cand-1"

    dev.last_report = None                 # exactly what the discard does, on the wrapper it reaches
    assert dev.last_report is None, (
        "the abandoned build's report survived the discard and will be stamped on the next node")
    # …and it does NOT fall back to the inner's, or the discard would be a no-op by another route.
    assert inner.last_report == "cand-2"

    # A later build still gets its own pick, so the setter is a clear and not a freeze.
    dev.implement(object())
    assert dev.last_report == "cand-3"


def test_no_build_site_clears_the_footprint_on_its_own():
    """The walk has ONE caller, and it is the one holding the lock.

    An AST scan and not a substring: the failure this guards is a site RE-ADDING the unlocked call,
    which reads as ordinary care at the call site. `_run_developer` is allowed to call it; nothing
    else in the engine is.
    """
    import looplab.engine.ablation
    import looplab.engine.node_build
    import looplab.engine.orchestrator
    import looplab.engine.speculation

    offenders = []
    for module in (looplab.engine.orchestrator, looplab.engine.speculation,
                   looplab.engine.ablation, looplab.engine.node_build):
        tree = ast.parse(inspect.getsource(module))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if func.name == "_run_developer":
                continue                            # the one legal caller
            for node in ast.walk(func):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "_reset_developer_footprint"):
                    offenders.append(f"{module.__name__}::{func.name}:{node.lineno}")
    assert not offenders, (
        "clearing the footprint outside `_run_developer` is an unlocked write to a possibly-shared "
        f"Developer — the clear belongs in the locked window: {offenders}")
