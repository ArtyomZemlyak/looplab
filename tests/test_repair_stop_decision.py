"""What stops the inline-repair loop, after the 2026-08-05 redesign.

THE DECISION. Of the three ways to bound in-node repair — leave it unlimited, cap it at a fixed
count, or let a model decide — the loop now uses the model, with a cap as a backstop:

  * the crash-triage MODEL is consulted once per attempt (the call the loop already made) and is
    handed this node's whole repair history — what failed, what each fix claimed, which files it
    actually touched, how far the pipeline got. Its `abandon` verdict is the stop, and it means
    exactly "I no longer know how to fix this";
  * `inline_repair_attempts` is a HARD operator cap for when the judge is wrong in the expensive
    direction — and since 2026-08-06 it is charged against the DURABLE `node_repaired` rows, so a
    resume continues a node's repair chain rather than restarting it (`test_the_budget_and_the_history_survive_a_resume`);
  * a judge that produced no usable verdict is never read as permission to keep repairing — and the
    two ways that happens are two different verdicts, because they are two different facts:
    `unanswerable` (the TRANSPORT failed) stops the node AND pauses the run naming the provider,
    while `unreadable` (the model answered something outside the vocabulary) stops only the node.
    Both are re-asked once first: one non-answer is not a diagnosis.

WHAT IT REPLACES, and why. Two heuristics, both removed:

  1. an anti-stuck counter over a NORMALIZED error signature (`_normalize_error_sig`). It bounded
     the loop only for errors whose text it happened to normalize: it collapsed ASCII quoted
     identifiers and nothing else, so on this Russian-language repo the IDENTICAL registry-walk
     failure that terminalized after 3 repairs with an ASCII symbol ran 1741 repairs with no
     terminal when the symbol was Cyrillic. A whitespace-only stderr normalized to the empty string,
     which the guard treated as an unconditional exemption. Provider prose carrying a varying
     request id minted a fresh signature every attempt. A bound that depends on the text quality of
     a program's error output is not a bound.
  2. the environment/experiment `repair_class` apportionment (commit e0ec3a4d), which gave the loop
     a SECOND allowance for repairs that only reconciled code with installed libraries. A budget is
     about time and money — a re-eval costs the same whichever kind of mistake preceded it.

Every loop test here drives the REAL `_evaluate`; only the solution's source and the judge's verdict
are scripted, so they exercise the actual control flow.
"""
from __future__ import annotations

import ast
from pathlib import Path

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea, NodeStatus
from looplab.engine.orchestrator import Engine, _rule_triage
from looplab.engine.evaluate import (_UNLIMITED_REPAIR_CEILING, _durable_repair_ledger,
                                     _effective_repair_cap)
from looplab.engine.triage import (AGENT_TRIAGE_ACTIONS, DEFAULT_TRIAGE_ACTION,
                                   TRIAGE_ACTIONS, TRIAGE_TRANSPORT_FAILURE_KEY,
                                   UNANSWERABLE_TRIAGE_ACTION, UNREADABLE_TRIAGE_ACTION,
                                   _TRIAGE_REASK_LIMIT, coerce_triage_action,
                                   is_transport_failure_verdict, repair_artifact_defect)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime import deps
from looplab.runtime.deps import InstallResult
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree
# The verbatim evidence from the two live incidents, owned by the file that documents them.
from tests.test_repair_runaway_guard import (_DDP, _DDP_RATIONALE, _GOOD, _REAL_SYMBOLS, _SECOND_QUESTION,
                                             _SEQUENCE, _cyrillic_src, _emits, _lazy_import_src)

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


class _Judge:
    """A scripted crash-triage model. `script` maps a marker in the error text to a verdict;
    `default` answers everything else. Records what it was ASKED, which is half the point: the
    redesign's claim is that the judge decides on the repair HISTORY, so the history has to reach
    it."""

    def __init__(self, script=None, default=None):
        self.script = dict(script or {})
        self.default = dict(default or {"action": "repair", "rationale": "keep going"})
        self.calls: list[dict] = []

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def triage_crash(self, node, error, attempt, *, state=None, brief="", history="",
                     stages_passed=None, attempts_left=None):
        self.calls.append({"attempt": attempt, "error": error, "history": history,
                           "stages_passed": stages_passed, "attempts_left": attempts_left})
        for marker, verdict in self.script.items():
            if marker in error:
                return dict(verdict)
        return dict(self.default)


class _StopWhenCircling(_Judge):
    """The judge the design actually asks for: it reads the HISTORY and stops when the same failure
    survives the fixes that claimed to address it. Deliberately a crude reader — the point is that
    the evidence is sufficient, not that the rule is clever."""

    def triage_crash(self, node, error, attempt, *, state=None, brief="", history="",
                     stages_passed=None, attempts_left=None):
        super().triage_crash(node, error, attempt, state=state, brief=brief, history=history,
                             stages_passed=stages_passed, attempts_left=attempts_left)
        tail = " ".join(error.split())[-120:]
        if history.count(tail) >= 2:
            return {"action": "abandon",
                    "rationale": "the same failure has survived every fix — I no longer know what "
                                 "to change"}
        return {"action": "repair", "rationale": "the failure moved; next fix identified"}


class _ScriptedDev:
    def __init__(self, sources, first=None, cycle=False):
        self.sources = list(sources)
        self.first = first if first is not None else self.sources[0]
        self.cycle = cycle
        self.repair_calls = 0
        self.errors: list[str] = []

    def implement(self, idea):
        return self.first

    def repair(self, idea, code, error):
        self.errors.append(error)
        self.repair_calls += 1
        if self.sources:
            return self.sources.pop(0)
        return self.first if self.cycle else _GOOD


def _drive(tmp_path, dev, judge=None, *, wall=120, **kw):
    """Seed one node and run the REAL repair loop over it, skipping genesis/policy."""
    kw.setdefault("auto_install_deps", False)
    kw.setdefault("inline_repair", True)
    run_dir = tmp_path / "run"
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=judge or _Judge(), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": dev.implement(None)})

    async def _bounded() -> bool:
        # A hard wall so a REGRESSION fails the test instead of hanging CI: every case below ran
        # unbounded before this design, and the suite must observe that as a failure, not a timeout.
        with anyio.move_on_after(wall) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the inline-repair loop did not terminate"
    return list(EventStore(run_dir / "events.jsonl").read_all()), eng


def _repairs(evs):
    return [e for e in evs if e.type == "node_repaired" and e.data.get("node_id") == 0]


def _terminals(evs):
    return [e for e in evs if e.type in ("node_evaluated", "node_failed")
            and e.data.get("node_id") == 0]


# --------------------------------------------------------------- the verdict contract itself
def test_triage_verdict_vocabulary_has_one_spelling():
    """Registry (CLAUDE.md: duck-typed seams are registry-guarded). The agent's emit schema, the
    engine's coercion and the rule path must all speak `TRIAGE_ACTIONS` — a typo'd literal would
    silently turn a STOP into "keep going", which is invisible in a passing run and expensive in a
    real one."""
    import inspect

    from looplab.agents import unified_agent

    src = inspect.getsource(unified_agent.UnifiedAgent.triage_crash)
    assert '"enum": list(AGENT_TRIAGE_ACTIONS)' in src, (
        "the emit schema must read the enum from the registry, never re-spell it")
    assert set(TRIAGE_ACTIONS) == {"repair", "abandon", "reject_idea", "unanswerable", "unreadable"}
    # Both ENGINE verdicts are off the wire: a live model must not be able to claim its own
    # unreachability (and trip the run-level provider circuit breaker), nor to assert that the engine
    # could not read it.
    assert UNANSWERABLE_TRIAGE_ACTION not in AGENT_TRIAGE_ACTIONS
    assert UNREADABLE_TRIAGE_ACTION not in AGENT_TRIAGE_ACTIONS
    assert set(AGENT_TRIAGE_ACTIONS) < set(TRIAGE_ACTIONS)


def test_a_verdict_nobody_can_parse_is_not_permission_to_continue():
    """The fail-closed direction, at the coercion — and WHICH fail-closed verdict it produces.

    It defaulted to `repair` ("the cheap, safe action", only cheap if a repair is cheap: each one is
    a full re-eval plus two LLM calls). It then defaulted to `unanswerable`, which was the
    mirror-image error — that verdict accuses the PROVIDER and halts the run, and this branch is
    reached precisely when a provider answered. It is `unreadable`.

    The literal string "unanswerable" is rejected here like any other non-verdict, and THIS is the
    enforcement of "engine-minted only": `AGENT_TRIAGE_ACTIONS` merely documents it, and while the
    default WAS `unanswerable` the exclusion was self-defeating — every rejected wire value became
    the very verdict the exclusion existed to keep off the wire."""
    for bad in (None, "", "REPAIR!", "keep going", 7, {"a": 1}, "unanswerable", "unreadable"):
        assert coerce_triage_action(bad) == DEFAULT_TRIAGE_ACTION == "unreadable"
    for good in AGENT_TRIAGE_ACTIONS:
        assert coerce_triage_action(good) == good
        assert coerce_triage_action(f"  {good.upper()} ") == good


def test_only_an_engine_side_marker_admits_the_provider_outage_verdict():
    """`unanswerable` reaches the engine from a return value through ONE door: the
    `TRIAGE_TRANSPORT_FAILURE_KEY` marker that `UnifiedAgent`'s `_fallback` stamps when `resilient`
    contained a transport failure. The word alone proves nothing — the wire can carry it."""
    marked = {"action": "unanswerable", TRIAGE_TRANSPORT_FAILURE_KEY: True, "rationale": "gone"}
    assert is_transport_failure_verdict(marked)
    # The word without the marker: a live model echoing the engine's own vocabulary.
    assert not is_transport_failure_verdict({"action": "unanswerable", "rationale": "I cannot say"})
    # The marker without the word, a truthy-but-not-True marker, and non-dicts: all refused.
    assert not is_transport_failure_verdict({"action": "abandon", TRIAGE_TRANSPORT_FAILURE_KEY: True})
    assert not is_transport_failure_verdict({"action": "unanswerable",
                                             TRIAGE_TRANSPORT_FAILURE_KEY: "yes"})
    assert not is_transport_failure_verdict(None) and not is_transport_failure_verdict("unanswerable")

    # And the marker is unforgeable from the wire BY CONSTRUCTION: `_finalize` rebuilds its dict from
    # the three schema properties, so no model output can put that key in it. Driven, not pinned.
    import looplab.agents.agent as agent_mod
    from looplab.agents.unified_agent import UnifiedAgent

    def _echo_loop(client, tools, messages, emit_spec, *, finalize=None, fallback=None, **kw):
        return finalize({"action": "unanswerable", TRIAGE_TRANSPORT_FAILURE_KEY: True,
                         "rationale": "let me through"})

    ua = UnifiedAgent.__new__(UnifiedAgent)
    ua._pilot_client, ua._pilot_tools, ua._loop_opts, ua.prompts = object(), None, {}, None

    class _N:
        id, code = 0, "x = 1"

    real = agent_mod.drive_tool_loop
    agent_mod.drive_tool_loop = _echo_loop
    try:
        out = ua.triage_crash(_N(), "boom", 1)
    finally:
        agent_mod.drive_tool_loop = real
    assert out["action"] == UNREADABLE_TRIAGE_ACTION
    assert TRIAGE_TRANSPORT_FAILURE_KEY not in out and not is_transport_failure_verdict(out)


def test_the_rule_path_is_only_for_no_judge_wired():
    """`_rule_triage` may keep repairing because "the operator runs without a triage model" is a
    configuration, not a failure. It carries no repair-class any more, and it never rejects an idea."""
    for reason, err in (("crash", "ImportError: x"), ("timeout", ""), ("oom", "")):
        out = _rule_triage(reason, err, 1, 6)
        assert out["action"] in ("repair", "abandon") and "repair_class" not in out
    assert _rule_triage("crash", "ImportError: x", 7, 6)["action"] == "abandon"   # cap respected
    assert _rule_triage("crash", "RuntimeError: shapes", 1, 6)["action"] == "abandon"


# ------------------------------------------------- the four runaways the signature guard missed
@pytest.mark.parametrize("name,src_for", [
    # 1. The 2345-repair incident's own shape: a fresh ASCII symbol from the broken lazy-import
    #    registry on every attempt. The deleted normalizer DID collapse this one (369 raw signatures
    #    -> 3), which is why it was the only one that ever terminated.
    ("ascii registry symbols", lambda i: _lazy_import_src(_REAL_SYMBOLS[i % len(_REAL_SYMBOLS)])),
    # 2. The same failure with a CYRILLIC symbol — the headline. Identical control flow, and the
    #    normalizer absorbed nothing, so this ran 1741 repairs with no terminal inside a 100 s wall.
    ("cyrillic column names", _cyrillic_src),
    # 3. A whitespace-only stderr. Truthy, so it survived the `or` fallback, and normalized to the
    #    EMPTY signature — which never entered the ledger, so progress read True forever (1055
    #    repairs, no terminal).
    ("blank stderr", lambda i: _emits("  \n \t ")),
    # 4. Provider PROSE carrying a varying request id: the SyntaxError quotes the id, so every
    #    attempt minted a fresh signature (1568 repairs, no terminal). Constant prose was bounded.
    ("provider prose with a varying id",
     lambda i: f"Error: upstream 502 (request {i:08x}f{i:04x}); please retry\n"),
])
def test_every_unbounded_case_now_terminates(tmp_path, name, src_for):
    """The acceptance table. Each of these ran without a terminal until a wall clock cut it; each
    must now stop. The judge here is the crude history reader — it needs no per-case tuning, because
    it is reading the trajectory rather than matching the error text."""
    dev = _ScriptedDev([src_for(i) for i in range(1, 40)], first=src_for(0))
    evs, _ = _drive(tmp_path, dev, _StopWhenCircling(), inline_repair_attempts=12)

    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed", name
    assert len(_repairs(evs)) <= 12, name          # …and inside the operator's hard cap
    assert fold(evs).nodes[0].status is NodeStatus.failed


def test_the_hard_cap_bounds_a_judge_that_never_says_stop(tmp_path):
    """The backstop, on its own. A judge that answers "repair" forever — wrong in the expensive
    direction, or a degraded endpoint that is answering but not thinking — is bounded EXACTLY by
    `inline_repair_attempts`, and the terminal says so rather than blaming the node."""
    for cap in (1, 5, 12):
        dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
        evs, _ = _drive(tmp_path / f"cap{cap}", dev, _Judge(), inline_repair_attempts=cap)
        assert len(_repairs(evs)) == cap
        terminal = _terminals(evs)
        assert len(terminal) == 1 and terminal[0].type == "node_failed"
        assert "hard limit" in terminal[0].data["triage_rationale"]


def test_zero_still_means_no_operator_cap_and_the_judge_still_stops_it(tmp_path):
    """`inline_repair_attempts = 0` keeps meaning NO OPERATOR CAP — a pre-existing run resumes with
    it (`LEGACY_CONFIG_SNAPSHOT_DEFAULTS`) — so the judge must still be what stops it, well before
    the engine's own ceiling. Under 0 the node terminates on the judge's verdict, because the judge
    is a real bound and not a decoration. (The ceiling behind it is
    `test_an_always_repair_judge_under_zero_now_terminates`, for when the judge is not.)"""
    dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    evs, _ = _drive(tmp_path, dev, _StopWhenCircling(), inline_repair_attempts=0)

    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    assert "no longer know" in terminal[0].data["triage_rationale"]


# ----------------------------------------------------- the case the budget exists to PROTECT
def test_the_six_migrations_still_reach_the_research_question(tmp_path):
    """The other direction, and the one a stop-happy design fails. `runs/rubert-dr-0805` node 0 had
    to walk six PyTorch-Lightning/transformers/accelerate migrations of a year-stale repo before it
    could ask its first real research question (a DDP `find_unused_parameters` modelling decision).
    The failure MOVES every attempt, so the history reader keeps going — and the node reaches the
    research question, answers it, and produces a metric."""
    tails = [t for _m, t, _r in _SEQUENCE] + [_DDP, _SECOND_QUESTION]
    dev = _ScriptedDev([_emits(t) for t in tails[1:]] + [_GOOD], first=_emits(tails[0]))
    judge = _StopWhenCircling()
    evs, _ = _drive(tmp_path, dev, judge, inline_repair_attempts=12)

    assert len(_repairs(evs)) == 8                       # exactly the chain the live run needed
    assert any("find_unused_parameters" in e for e in dev.errors)   # it REACHED the question …
    assert any("finished reduction" in e for e in dev.errors)       # … and the one behind it
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_evaluated"
    st = fold(evs)
    assert st.nodes[0].status is NodeStatus.evaluated and st.nodes[0].metric == 0.1


def test_the_judge_is_told_what_it_needs_to_tell_those_two_apart(tmp_path):
    """The evidence contract. The two tests above differ ONLY in whether the failure moves, so the
    judge cannot separate them from a single traceback — which is exactly what it used to get. It
    must receive the per-attempt history (what failed, what the fix claimed, what it changed, how
    deep the pipeline got) and the remaining hard budget."""
    tails = [t for _m, t, _r in _SEQUENCE]
    dev = _ScriptedDev([_emits(t) for t in tails[1:]] + [_GOOD], first=_emits(tails[0]))
    judge = _StopWhenCircling()
    _drive(tmp_path, dev, judge, inline_repair_attempts=12)

    first, later = judge.calls[0], judge.calls[-1]
    assert first["history"] == ""                     # nothing has been tried yet
    assert first["attempts_left"] == 12 and later["attempts_left"] < 12
    assert "WHAT HAS ALREADY BEEN TRIED" in later["history"]
    for marker in ("cloud_io", "init_empty_weights", "tensorboardX"):
        assert marker in later["history"], marker     # the TRAJECTORY, not just the last error
    assert "the fix claimed:" in later["history"] and "it changed:" in later["history"]
    assert later["stages_passed"] is not None


def test_a_node_going_in_circles_is_cut_off_but_a_moving_one_is_not(tmp_path):
    """Both directions in one place, with the same judge and the same cap — so the difference is the
    node's behaviour and nothing else."""
    circling = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    evs_c, _ = _drive(tmp_path / "circles", circling, _StopWhenCircling(), inline_repair_attempts=12)
    assert _terminals(evs_c)[0].type == "node_failed"
    assert len(_repairs(evs_c)) < 12                  # stopped by the JUDGE, before the cap

    moving = _ScriptedDev(
        [f"def {fn}():\n    raise AttributeError('no attribute on this object')\n{fn}()\n"
         for fn in ("alpha", "bravo", "charlie", "delta", "echo")] + [_GOOD],
        first="def seed():\n    raise AttributeError('no attribute on this object')\nseed()\n")
    evs_m, _ = _drive(tmp_path / "moving", moving, _StopWhenCircling(), inline_repair_attempts=12)
    assert _terminals(evs_m)[0].type == "node_evaluated"
    assert len(_repairs(evs_m)) == 6


# ------------------------------------------------------------------- fail-closed: no judge, no go
def test_a_judge_that_cannot_be_reached_stops_the_node_and_pauses_the_run(tmp_path):
    """THE case the redesign must not get wrong. A dead provider is how the 2345-repair incident
    began, and the judge runs on that same provider — so "the judge could not be reached" must never
    mean "keep repairing". It lands on the developer-crash circuit breaker: no repair, ONE terminal
    naming `developer_crash`, and ONE run-level pause the operator can `resume` from.

    This is the half that must SURVIVE the 2026-08-06 split: only a transport failure halts the run,
    and a transport failure still does."""

    class _DeadJudge(_Judge):
        def triage_crash(self, node, error, attempt, **kw):
            self.calls.append({"attempt": attempt})
            raise RuntimeError("Error code: 402 - out of credits")

    dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    judge = _DeadJudge()
    evs, _ = _drive(tmp_path, dev, judge, inline_repair_attempts=12)

    assert _repairs(evs) == []                       # nothing was repaired blind
    assert dev.repair_calls == 0
    # …but it was ASKED TWICE before the node was written off. One flapped socket must not cost a
    # whole node: before the re-ask, a single `ConnectionError` on attempt 1 ended the node with
    # `developer.repair` calls = 0, where the second ask would have healed it.
    assert len(judge.calls) == 1 + _TRIAGE_REASK_LIMIT
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].data["reason"] == "developer_crash"
    assert "402" in terminal[0].data["error"]
    pauses = [e for e in evs if e.type == "pause"]
    assert len(pauses) == 1 and pauses[0].data.get("node_id") is None
    assert "402" in pauses[0].data["reason"] and "resume" in pauses[0].data["reason"]
    assert fold(evs).paused is True


def test_the_re_ask_costs_nothing_when_the_judge_answers(tmp_path):
    """The other side of the re-ask: it must be reachable ONLY by a non-answer. "The stop decision is
    free" is the design's central cost claim — the loop already made exactly one triage call per
    attempt — so a healthy run must still make exactly one."""
    dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    judge = _Judge()
    evs, _ = _drive(tmp_path, dev, judge, inline_repair_attempts=4)
    assert len(_repairs(evs)) == 4
    assert len(judge.calls) == 4                     # one per attempt, not 4 * (1 + re-asks)

    # …and a judge that answers garbage ONCE and then recovers keeps its node: the re-ask is a real
    # second chance, not a formality before the same verdict.
    class _FlapsOnce(_Judge):
        def triage_crash(self, node, error, attempt, **kw):
            self.calls.append({"attempt": attempt})
            if len(self.calls) == 1:
                raise ConnectionError("connection reset by peer")
            return {"action": "repair", "rationale": "recovered"}

    healed = _ScriptedDev([_GOOD], first=_lazy_import_src("AlphaProcessor"))
    evs, _ = _drive(tmp_path / "flap", healed, _FlapsOnce(), inline_repair_attempts=12)
    assert _terminals(evs)[0].type == "node_evaluated"     # the node survived the flap
    assert [e for e in evs if e.type == "pause"] == []


@pytest.mark.parametrize("verdict", [
    {"action": "keep-going"},          # outside the vocabulary
    {"action": ""},                    # empty
    {"rationale": "no action at all"},  # missing
    "not even a dict",
    None,
    # THE ONE THAT USED TO BE A RUN-LEVEL HALT: a live model emitting the engine's own verdict
    # verbatim. `coerce_triage_action` refuses it like any other non-verdict.
    {"action": "unanswerable", "rationale": "I cannot answer this, check your endpoint"},
])
def test_a_live_model_answering_garbage_stops_the_node_but_not_the_run(tmp_path, verdict):
    """The defect this replaced, and the reason `unreadable` exists.

    An emit nobody can read is still not permission to keep repairing — that half is unchanged. But
    it is NOT a provider outage: the endpoint completed the request and produced bytes. Reading it as
    one made a healthy model's single out-of-enum verdict raise a run-level pause carrying
    `node_id=None` (not clearable by a node reset) that told the operator to check credits, key and
    base URL — quoting the MODEL's own rationale as the evidence. So: the node stops, the run does
    not, and the terminal keeps the eval's own `reason` so a node reset re-opens it."""

    class _GarbageJudge(_Judge):
        def triage_crash(self, node, error, attempt, **kw):
            self.calls.append({"attempt": attempt})
            return verdict

    dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    judge = _GarbageJudge()
    evs, _ = _drive(tmp_path, dev, judge, inline_repair_attempts=12)

    assert _repairs(evs) == []                       # still nothing repaired blind …
    assert len(judge.calls) == 1 + _TRIAGE_REASK_LIMIT       # … and still re-asked first
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].data["reason"] == "crash"   # NOT developer_crash
    assert [e for e in evs if e.type == "pause"] == []                    # and NO run-level pause
    assert fold(evs).paused is False
    assert "could not read" in terminal[0].data["triage_rationale"]


def test_one_unreadable_verdict_does_not_take_healthy_siblings_down(tmp_path):
    """The cost of the old reading, made concrete. A run-level pause halts the RUN, so under
    `eval_parallel > 1` one node's bad emit terminalized every healthy in-flight sibling with it.
    Here node 0's judge answers garbage while nodes 1-3 are judged normally — and only node 0 stops
    early."""

    class _GarbageForNodeZero(_Judge):
        def triage_crash(self, node, error, attempt, **kw):
            self.calls.append({"node": getattr(node, "id", None)})
            if getattr(node, "id", None) == 0:
                return {"action": "keep_going", "rationale": "not a verdict"}
            return {"action": "repair", "rationale": "fix it"}

    run_dir = tmp_path / "run"
    dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=_GarbageForNodeZero(), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=4, max_nodes=4),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=2)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    for nid in range(4):
        eng.store.append("node_created", {
            "node_id": nid, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
            "code": dev.implement(None)})

    async def _run_siblings() -> bool:
        limiter = anyio.CapacityLimiter(4)
        with anyio.move_on_after(120) as scope:
            async with anyio.create_task_group() as tg:
                for nid in range(4):
                    tg.start_soon(eng._evaluate, nid, limiter, None)
        return scope.cancelled_caught

    assert not anyio.run(_run_siblings), "the sibling evals did not terminate"
    evs = list(EventStore(run_dir / "events.jsonl").read_all())
    assert [e for e in evs if e.type == "pause"] == []
    assert not fold(evs).paused
    # Node 0 spent nothing; every healthy sibling still spent its full budget on its own node.
    repaired = {nid: len([e for e in evs if e.type == "node_repaired"
                          and e.data.get("node_id") == nid]) for nid in range(4)}
    assert repaired[0] == 0 and all(repaired[nid] == 2 for nid in (1, 2, 3)), repaired


def test_an_older_triage_crash_signature_is_not_read_as_a_dead_provider(tmp_path):
    """`triage_crash` is a DUCK-TYPED seam — any object wired as `researcher` may implement it — and
    this change added three keyword arguments to it. Passing them unconditionally makes an
    implementation written against the old signature raise TypeError, which the fail-closed handler
    would then read as a dead provider: it would stop the node AND pause the whole run. The call is
    narrowed to what the callee accepts, so an old signature simply keeps the historical prompt."""

    class _OldSignature:
        def __init__(self):
            self.calls = 0

        def propose(self, state, parent):
            return Idea(operator="x", params={"x": 1.0, "y": 1.0})

        def triage_crash(self, node, error, attempt, *, state=None, brief=""):
            self.calls += 1
            return {"action": "repair" if self.calls < 3 else "abandon", "rationale": "old-style"}

    judge = _OldSignature()
    dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    evs, _ = _drive(tmp_path, dev, judge, inline_repair_attempts=12)

    assert judge.calls == 3                          # it was really consulted, three times
    assert len(_repairs(evs)) == 2
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].data["reason"] == "crash"   # NOT developer_crash
    assert [e for e in evs if e.type == "pause"] == []                    # and no spurious pause

    # A `**kwargs` implementation gets the full evidence, as the new signature does.
    from looplab.engine.crash_repair import _accepted_kwargs
    assert _accepted_kwargs(lambda **kw: None, {"history": "h"}) == {"history": "h"}
    assert _accepted_kwargs(lambda a, b: None, {"history": "h"}) == {}
    assert _accepted_kwargs(len, {"history": "h"}) == {}                  # unintrospectable -> none


def test_the_agent_fallbacks_fail_closed_and_do_it_DIFFERENTLY():
    """The same rule one layer down, where the transport failure is actually observed — DRIVEN, not
    pinned. Both of `UnifiedAgent.triage_crash`'s degradation paths answered "attempt repair"; they
    then both answered `unanswerable`, which collapsed the one distinction the layer exists to make.
    `_finalize` is reached BY an answer and `_fallback` INSTEAD of one, so only the second is
    evidence about the provider.

    (This used to be `body.count("DEFAULT_TRIAGE_ACTION") >= 2` over the source text, which the
    comment on the very branch it guards would satisfy on its own — CLAUDE.md's "a guard test must
    not be satisfiable by a COMMENT". Both properties are now behavioural.)"""
    import inspect

    import looplab.agents.agent as agent_mod
    from looplab.agents import unified_agent
    from looplab.agents.tool_loop import resilient
    from looplab.agents.unified_agent import UnifiedAgent

    # NEGATIVE pin (CLAUDE.md keeps these as substrings on purpose): what must not come back is the
    # TEXT of a degradation path answering "repair".
    body = inspect.getsource(unified_agent.UnifiedAgent.triage_crash).split("def _finalize", 1)[1]
    assert 'action = "repair"' not in body and '"action": "repair"' not in body, (
        "a degradation path still answers 'repair' — that is the reading that let a dead provider "
        "keep the repair loop at full speed")

    ua = UnifiedAgent.__new__(UnifiedAgent)
    ua._pilot_client, ua._pilot_tools, ua._loop_opts, ua.prompts = object(), None, {}, None

    class _N:
        id, code = 0, "x = 1"

    def _with_loop(loop):
        real = agent_mod.drive_tool_loop
        agent_mod.drive_tool_loop = loop
        try:
            return ua.triage_crash(_N(), "boom", 1)
        finally:
            agent_mod.drive_tool_loop = real

    # THE MODEL ANSWERED, out of enum -> `unreadable`, carrying no provider accusation.
    confused = _with_loop(lambda c, t, m, spec, *, finalize=None, fallback=None, **kw:
                          finalize({"action": "keep_going", "rationale": "just fix the typo"}))
    assert confused["action"] == UNREADABLE_TRIAGE_ACTION
    assert not is_transport_failure_verdict(confused)

    # NOBODY ANSWERED — `resilient` contains the transport error and hands over to `_fallback`.
    def _dead(client, tools, messages, emit_spec, *, finalize=None, fallback=None, **kw):
        def _boom():
            raise ConnectionError("connection reset by peer")
        return resilient(_boom, lambda: fallback(messages))

    outage = _with_loop(_dead)
    assert outage["action"] == UNANSWERABLE_TRIAGE_ACTION
    assert is_transport_failure_verdict(outage), (
        "the transport fallback must stamp the engine-side marker, or the engine reads a real "
        "outage as an ordinary unreadable answer and never pauses the run")


# ------------------------------------------------------- the ledger is the LOG's, not the process's
def test_the_budget_and_the_history_survive_a_resume(tmp_path):
    """INVARIANT #3, and the defect that broke it. `attempt`, the judge's `repair_log` and the
    unparseable-answer counter were plain `_evaluate` locals initialized to 0/[] , and nothing folds
    them (`replay.py::_on_node_repaired` mutates code and files only). So every re-entry — `looplab
    resume`, a crashed process, and above all the operator-resumed PAUSE this design prescribes as
    the recovery from a dead judge — restarted the hard budget at zero and showed the judge an empty
    history for a node that already had durable repairs. A flapping provider therefore looped
    pause -> resume -> fresh budget forever.

    Driven the way it actually happens: a SECOND `Engine` over the same run directory, which is what
    `looplab resume` builds."""
    cap = 4
    run_dir = tmp_path / "run"

    def _engine(judge, dev):
        return Engine(run_dir, task=ToyTask.load(TASK), researcher=judge, developer=dev,
                      sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                      auto_install_deps=False, inline_repair=True, inline_repair_attempts=cap)

    class _StopAfter(_ScriptedDev):
        """Ends the process's eval mid-loop, exactly as a kill does: the node is left PENDING with
        durable `node_repaired` rows and no terminal."""

        def __init__(self, n):
            super().__init__([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
            self.n = n

        def repair(self, idea, code, error):
            if self.repair_calls >= self.n:
                raise _Kill()
            return super().repair(idea, code, error)

    class _Kill(BaseException):
        """A BaseException so `_evaluate`'s `except Exception` containment cannot absorb it."""

    dev = _StopAfter(2)
    eng = _engine(_Judge(), dev)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": dev.implement(None)})

    async def _first() -> None:
        await eng._evaluate(0, anyio.CapacityLimiter(1), None)

    with pytest.raises(_Kill):
        anyio.run(_first)
    mid = list(EventStore(run_dir / "events.jsonl").read_all())
    assert len(_repairs(mid)) == 2 and _terminals(mid) == []      # pending, two durable repairs
    assert fold(mid).nodes[0].status is NodeStatus.pending

    # A fresh process: new Engine, new judge, new developer, same run directory.
    judge2 = _Judge()
    dev2 = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    eng2 = _engine(judge2, dev2)

    async def _resume() -> bool:
        with anyio.move_on_after(120) as scope:
            await eng2._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_resume), "the resumed repair loop did not terminate"
    evs = list(EventStore(run_dir / "events.jsonl").read_all())

    # THE BUDGET CONTINUED. 4 total across both processes, not 2 + 4; the attempt numbers are one
    # monotone chain, which is what makes them a ledger rather than two overlapping ones.
    assert len(_repairs(evs)) == cap
    assert [e.data["attempt"] for e in _repairs(evs)] == [1, 2, 3, 4]
    assert dev2.repair_calls == 2                    # the resume only spent what was left
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    assert "hard limit" in terminal[0].data["triage_rationale"]

    # THE HISTORY CONTINUED. The judge in the FRESH process was shown the earlier process's
    # attempts — it is handed the trajectory precisely so it can tell "moving" from "circling", and
    # it saw none of it before this fix.
    assert judge2.calls, "the resumed loop never consulted the judge"
    first_after_resume = judge2.calls[0]
    assert "attempt 1:" in first_after_resume["history"], first_after_resume["history"]
    assert "attempt 2:" in first_after_resume["history"]
    assert first_after_resume["attempts_left"] == 2          # not `cap`
    # …and the rows carry the evidence columns, from the DURABLE event rather than a lost local.
    assert "it changed:" in first_after_resume["history"]
    assert "not recorded" not in first_after_resume["history"]


def test_the_durable_ledger_is_generation_scoped_and_reads_old_rows(tmp_path):
    """The ledger's own truth table. A `node_reset` opens a new lifecycle whose budget genuinely
    starts fresh, so an abandoned generation's rows must not charge it — while a row written before
    the evidence columns existed must still COUNT, and must be distinguishable from one that changed
    nothing (`_format_repair_log` renders the two differently, and "changed nothing twice" is
    exactly what a circling chain looks like)."""
    from looplab.engine.crash_repair import _format_repair_log
    from looplab.events.eventstore import Event

    def _ev(**data):
        return Event(v=1, seq=0, ts=0.0, type="node_repaired", data=data)

    rows = [
        _ev(node_id=0, generation=0, attempt=1, error_in="boom one", rationale="fix A",
            changed=["a.py"], stages_passed=1, unparseable_repairs=0),
        _ev(node_id=0, generation=0, attempt=2, error_in="boom two", rationale="fix B",
            changed=[], stages_passed=2, unparseable_repairs=2),
        _ev(node_id=1, generation=0, attempt=9, error_in="another node", rationale="x"),
        _ev(node_id=0, generation=1, attempt=1, error_in="a newer lifecycle", rationale="y"),
        _ev(node_id=0, attempt=3, error_in="boom three", rationale="fix C"),   # unstamped (old log)
    ]
    n, hist, unparseable = _durable_repair_ledger(rows, 0, 0)
    assert n == 3                                    # this node, this generation, incl. the unstamped
    assert [r["attempt"] for r in hist] == [1, 2, 3]
    assert unparseable == 2                          # the max, not the last
    # A RESET LIFECYCLE STARTS FRESH: generation 0's rows do not charge generation 1's budget.
    assert [r["error"] for r in _durable_repair_ledger(rows[:4], 0, 1)[1]] == ["a newer lifecycle"]
    # …and an UNSTAMPED row binds to whichever lifecycle is asking, exactly as
    # `replay._generation_matches` treats it: a log written before generations were stamped has no
    # way to say which one it belongs to, and charging it to the current one is the safe direction
    # (it can only shorten a budget, never extend it past the cap).
    assert _durable_repair_ledger(rows, 0, 1)[0] == 3
    assert _durable_repair_ledger(rows, 7, 0) == (0, [], 0)  # a node with no repairs
    assert _durable_repair_ledger([], 0, 0) == (0, [], 0)
    # A stamp that is PRESENT but invalid (an explicit null) is rejected, not read as unstamped —
    # `replay._event_generation` treats it the same way, and the fold would drop such a row.
    assert _durable_repair_ledger([_ev(node_id=0, generation=None, attempt=1)], 0, 0)[0] == 0

    rendered = _format_repair_log(hist)
    assert "it changed: a.py" in rendered
    assert "it changed: nothing" in rendered                       # the empty list …
    assert "it changed: (not recorded" in rendered                 # … is NOT the missing column


def test_zero_means_no_operator_cap_and_gets_the_engine_ceiling():
    """WHAT AN OPERATOR WITH `inline_repair_attempts: 0` GETS, stated as a truth table.

    `0` is what 38 of the 46 preserved run snapshots carry, INCLUDING `runs/rubert-dr-0804` — the
    2345-repair incident this whole redesign was built for. It kept meaning "unbounded", and
    unbounded was measured to be exactly nothing: an always-`repair` judge under 0 ran 795 repairs /
    796 full evals in 45 s with no terminal, i.e. on its OWN snapshot the incident was still not
    prevented. `0` still means no OPERATOR cap (the grandfathering in
    `core/config.py::LEGACY_CONFIG_SNAPSHOT_DEFAULTS` stands and nobody silently acquires a 12
    mid-run) — but the engine keeps a ceiling of its own behind it."""
    assert _effective_repair_cap(0) == _UNLIMITED_REPAIR_CEILING
    assert _UNLIMITED_REPAIR_CEILING > 12, "the ceiling must not silently re-cap an uncapped run"
    for n in (1, 5, 8, 12, 99):
        assert _effective_repair_cap(n) == n         # an explicit cap is never widened or narrowed


def test_an_always_repair_judge_under_zero_now_terminates(tmp_path):
    """The 2345-repair shape, on a snapshot that says `0`: an alive judge that always says "repair"
    and a node that never stops crashing. It must terminalize, and the terminal must not read as if
    the operator had chosen 50."""
    dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    evs, _ = _drive(tmp_path, dev, _Judge(), inline_repair_attempts=0, wall=300)

    assert len(_repairs(evs)) == _UNLIMITED_REPAIR_CEILING
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    why = terminal[0].data["triage_rationale"]
    assert "absolute ceiling" in why and "inline_repair_attempts is 0" in why
    assert "hard limit" not in why, "a run with no operator cap must not be told it hit one"


def test_a_repair_that_is_not_a_program_is_a_provider_failure_not_a_fix():
    """`core/models.py::is_developer_error` recognises only LoopLab's own sentinel, whose single
    producer is `adapters/repo_developer.py`. A provider answering with prose produces no sentinel,
    and the prose was committed as the node's code. `repair_artifact_defect` is the engine's own
    check, and it separates the two cases that need different treatment."""
    # Provably not a program: it can never print a metric, whoever wrote it.
    for text in ("", "   \n", "# rate limit exceeded, retry later\n",
                 '"""Service temporarily unavailable — the provider is out of credits."""\n',
                 "pass\n"):
        assert repair_artifact_defect(text) == "no_code", text
    # Not Python at all — AMBIGUOUS (a truncated generation looks identical), so it is bounded
    # rather than terminal.
    for text in ("Error: upstream 502 Bad Gateway (request 8f2a91)\n", "def f(:\n", "\x00\n"):
        assert repair_artifact_defect(text) == "unparseable", text
    # A real repair, including one that only defines things.
    for text in (_GOOD, "import json\n\n\ndef f():\n    return 1\n", "x = 1\n"):
        assert repair_artifact_defect(text) == "", text


def test_prose_that_parses_names_the_provider_instead_of_no_metric(tmp_path):
    """The recurrence this closes. Prose that happens to PARSE — a comment-only or docstring-only
    answer — exits 0 with no metric, so the node terminalized as `no_metric` and told the operator
    "the command printed no metric" about a provider that was dead, raising no pause."""

    class _ProseDev(_ScriptedDev):
        def repair(self, idea, code, error):
            self.repair_calls += 1
            return '"""Service temporarily unavailable — upstream is out of credits."""\n'

    dev = _ProseDev([], first=_lazy_import_src("AlphaProcessor"))
    evs, _ = _drive(tmp_path, dev, _Judge(), inline_repair_attempts=12)

    assert _repairs(evs) == []                       # it never counted as a repair
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].data["reason"] == "developer_crash"
    assert "no executable code" in terminal[0].data["error"]
    assert len([e for e in evs if e.type == "pause"]) == 1


def test_a_repair_call_that_raises_becomes_the_same_sentinel(tmp_path):
    """`agents/roles.py::LLMDeveloper.repair` calls `complete_text` uncaught and
    `ValidatingDeveloper._attempt_loop` does not catch either, and `_evaluate` was the one `_repair`
    call site with no handler above it — so a 401/402 on a NON-repo task escaped the eval entirely:
    no terminal, no pause, and on the serial path it took the whole run down."""

    class _RaisingDev(_ScriptedDev):
        def repair(self, idea, code, error):
            self.repair_calls += 1
            raise RuntimeError("LLM request failed: Error code: 402 - out of credits")

    dev = _RaisingDev([], first=_lazy_import_src("AlphaProcessor"))
    evs, _ = _drive(tmp_path, dev, _Judge(), inline_repair_attempts=12)

    assert dev.repair_calls == 1                     # asked once, then stopped asking
    assert _repairs(evs) == []
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].data["reason"] == "developer_crash"
    assert "402" in terminal[0].data["error"]
    assert len([e for e in evs if e.type == "pause"]) == 1


def test_repeated_non_python_answers_become_a_provider_verdict(tmp_path):
    """The ambiguous half, bounded. One truncated generation must still be repairable — abandoning a
    node on it would be a regression — but a provider that keeps answering with prose is a provider
    failure. Counted DIRECTLY, because the SyntaxError it produces carries a varying request id and
    so looks new every time."""

    class _TruncatingDev(_ScriptedDev):
        def __init__(self, n_bad):
            super().__init__([], first=_lazy_import_src("AlphaProcessor"))
            self.n_bad = n_bad

        def repair(self, idea, code, error):
            self.repair_calls += 1
            if self.repair_calls <= self.n_bad:
                return f"Error: upstream 502 (request {self.repair_calls:08x}ab); retry\n"
            return _GOOD

    # ONE truncation recovers: the node is repaired again and reaches a metric.
    evs, _ = _drive(tmp_path / "one", _TruncatingDev(1), _Judge(), inline_repair_attempts=12)
    assert _terminals(evs)[0].type == "node_evaluated"

    # A provider that never returns code stops, naming the provider, and pauses the run.
    evs, _ = _drive(tmp_path / "many", _TruncatingDev(99), _Judge(), inline_repair_attempts=12)
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].data["reason"] == "developer_crash"
    assert "not valid Python" in terminal[0].data["error"]
    assert len([e for e in evs if e.type == "pause"]) == 1


def test_only_one_pause_across_concurrent_sibling_evals(tmp_path):
    """Every sibling reaches the same dead endpoint at the same time. The run-level pause is
    deduplicated by the already-halting re-check under `_write_lock`, so the operator gets ONE
    diagnosis, not one per in-flight eval."""

    class _RaisingDev(_ScriptedDev):
        def repair(self, idea, code, error):
            raise RuntimeError("Error code: 402 - out of credits")

    run_dir = tmp_path / "run"
    dev = _RaisingDev([], first=_lazy_import_src("AlphaProcessor"))
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=_Judge(), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=4, max_nodes=4),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=12)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    for nid in range(4):
        eng.store.append("node_created", {
            "node_id": nid, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
            "code": dev.implement(None)})

    async def _run_siblings():
        limiter = anyio.CapacityLimiter(4)
        with anyio.move_on_after(120) as scope:
            async with anyio.create_task_group() as tg:
                for nid in range(4):
                    tg.start_soon(eng._evaluate, nid, limiter, None)
        return scope.cancelled_caught

    assert not anyio.run(_run_siblings), "the sibling evals did not terminate"
    evs = list(EventStore(run_dir / "events.jsonl").read_all())
    assert len([e for e in evs if e.type == "pause"]) == 1
    terminals = [e for e in evs if e.type in ("node_evaluated", "node_failed")]
    assert len(terminals) == 4                       # invariant #2: exactly one per node
    assert all(e.data["reason"] == "developer_crash" for e in terminals)


# ---------------------------------------------------------------------- replay safety
def test_one_terminal_and_a_fold_that_ignores_the_dropped_field(tmp_path):
    """Invariants #2 and #5. `repair_class` was ADDITIVE on `node_repaired` and the fold never read
    it, so a log written while the two-ledger split shipped folds identically now that the field is
    gone — an existing `runs/` directory stays replayable."""
    tails = [t for _m, t, _r in _SEQUENCE]
    dev = _ScriptedDev([_emits(t) for t in tails[1:]] + [_GOOD], first=_emits(tails[0]))
    evs, _ = _drive(tmp_path, dev, _StopWhenCircling(), inline_repair_attempts=12)

    assert len(_terminals(evs)) == 1
    before = fold(evs)
    assert all("repair_class" not in e.data for e in _repairs(evs))    # no vestigial field
    for e in _repairs(evs):                          # …and an OLD log that HAS it folds the same
        e.data["repair_class"] = "environment"
    assert fold(evs).model_dump() == before.model_dump()


# ------------------------------------------- the missing library the traceback never names
def test_a_dependency_degraded_into_a_nameerror_is_installed_not_hand_patched(tmp_path,
                                                                              monkeypatch):
    """Unchanged by the redesign and still load-bearing: `transformers` guards `init_empty_weights`
    behind `is_accelerate_available()`, so the absent distribution's name appears NOWHERE in the
    exception. The agent named it, the engine installs it and re-runs WITHOUT spending an attempt."""
    installed: list[str] = []
    flag = tmp_path / "accelerate.installed"

    def fake_install(package, *, python=None, timeout=None):
        installed.append(package)
        flag.write_text("ok")
        return InstallResult(package=package, ok=True, returncode=0)

    monkeypatch.setattr(deps, "is_present", lambda m, **kw: m != "accelerate")
    _T2, _R2 = _SEQUENCE[1][1], _SEQUENCE[1][2]
    dev = _ScriptedDev([], first=_emits(_T2, installed_flag=flag))
    judge = _Judge({"init_empty_weights": {"action": "repair", "rationale": _R2,
                                           "missing_dependency": "accelerate"}})
    evs, _ = _drive(tmp_path, dev, judge, auto_install_deps=True, dep_installer=fake_install,
                    inline_repair_attempts=12)

    assert installed == ["accelerate"]
    dep_events = [e for e in evs if e.type == "deps_installed"]
    assert len(dep_events) == 1 and dep_events[0].data["source"] == "triage"
    assert dev.repair_calls == 0 and _repairs(evs) == []   # an install is not a repair
    assert fold(evs).nodes[0].status is NodeStatus.evaluated


def test_install_candidates_are_jointly_evidenced():
    """The pure contract, on the live evidence, with the ONE-SIDED join closed.

    The rationale used to need only to ECHO one identifier the traceback points at, after which the
    structured `missing_dependency` field was trusted whole — and `transformers` appears on every
    frame of the real accelerate traceback, so an honest rationale satisfied that by itself.
    Verified through the engine seam, `missing_dependency="tensorflow, jax, prophet, fastai"` on
    that traceback had the engine attempt exactly those four heavyweight installs into the SHARED
    eval interpreter. Path A now also requires the rationale to NAME the distribution, and to name
    only one — the degraded-dependency shape is "library L guards symbol S behind
    `is_X_available()`", a single X; a list is a guess, not a diagnosis."""
    _T1, _R1 = _SEQUENCE[0][1], _SEQUENCE[0][2]
    _T2, _R2 = _SEQUENCE[1][1], _SEQUENCE[1][2]
    _T4, _R4 = _SEQUENCE[3][1], _SEQUENCE[3][2]
    _T5 = _SEQUENCE[4][1]
    # A: the structured field supplies a name the traceback CANNOT contain, and the rationale both
    # describes this traceback and names that distribution.
    assert deps.triage_install_candidates("accelerate", _R2, _T2) == ["accelerate", "transformers"]
    # …the shopping list buys nothing: none of the four appear in the (honest) rationale, and it
    # names more than one distribution.
    assert deps.triage_install_candidates("tensorflow, jax, prophet, fastai", _R2, _T2) == [
        "transformers"]                              # only what the traceback itself names, via B
    # …nor does a rationale that is not about this traceback.
    assert deps.triage_install_candidates("accelerate", "the learning rate looks too high", _T2) == []
    # B: the traceback and the rationale name it independently (no structured field at all).
    assert deps.triage_install_candidates("", _R4, _T4) == ["tensorboard", "tensorboardX"]
    # Free prose can never mint a candidate by itself.
    assert deps.triage_install_candidates("", _R1, _T1) == ["pytorch_lightning"]
    # Shape gate: a failure that is not unresolved-name shaped installs nothing, whatever is claimed.
    assert deps.triage_install_candidates("torch", "install torch", _DDP) == []
    assert deps.triage_install_candidates("torch", "install torch", _T5) == []
    # Allowlist gate: an off-list name is a code bug (a typo'd or local module), never an install.
    assert deps.triage_install_candidates("my_local_helper_zzz", _R2, _T2) == ["transformers"]


def test_is_present_probes_the_interpreter_and_fails_closed():
    """The last condition, and the one no agent can influence: only what is provably ABSENT is
    installed. Any doubt answers 'present', so the failure mode is a missed install, never a
    surprise one."""
    assert deps.is_present("sys") and deps.is_present("json")
    assert not deps.is_present("definitely_not_a_real_module_zzz")
    assert deps.is_present("")                       # not a name -> no install
    assert deps.is_present("not an identifier!")
    assert deps.is_present("json", python="/nonexistent/python-zzz")   # launch failure -> present


def test_the_triage_driven_install_is_gated_on_a_crash_like_its_sibling():
    """`inline_repair_reasons` also admits `timeout` and `oom`, whose `err` is whatever the killed
    process last wrote. The traceback-driven env-prep round has always been gated on
    `reason == "crash"`; its triage-driven sibling was not, so a training run killed at the deadline
    after logging an early import warning could drive a pip install into the shared interpreter."""
    import inspect

    from looplab.engine import evaluate as ev

    src = inspect.getsource(ev.EvaluateMixin._evaluate)
    gates = [line for line in src.splitlines() if "_auto_install_deps" in line]
    assert len(gates) == 2 and all('reason == "crash"' in line for line in gates), gates


def test_the_two_ledger_apportionment_is_gone():
    """The redesign, asserted rather than described. A vestigial `repair_class` left in any of the
    three sites would be a second, silent budget dimension — and the engine emitting one the agent
    no longer fills is exactly the kind of always-fail-closed field that reads as working."""
    import inspect

    from looplab.agents import unified_agent
    from looplab.engine import crash_repair, evaluate, triage

    for mod in (triage, crash_repair, evaluate, unified_agent):
        # CODE, not prose: the modules deliberately DOCUMENT the removal, and a comment saying
        # "`repair_class` is gone" must not read as the field still being there.
        tree = ast.parse(inspect.getsource(mod).lstrip())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        literals = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and "\n" not in n.value}
        for gone in ("repair_class", "REPAIR_CLASSES", "coerce_repair_class",
                     "DEFAULT_REPAIR_CLASS", "_environment_failure", "_normalize_error_sig"):
            assert gone not in names | literals, f"{mod.__name__} still speaks {gone}"
    assert not hasattr(triage, "_environment_failure")
    assert not hasattr(triage, "_normalize_error_sig")


def test_the_other_two_per_node_budgets_are_the_log_s_too():
    """`dep_rounds` and `full_retrains` were the last process-local bounds in the attempt loop.

    Same defect as the repair budget above, and the same reason it matters: a bound a resume refunds
    is not a bound. `_MAX_DEP_ROUNDS` exists so an offline or misnamed package cannot loop forever,
    and `inline_repair_retrain_cap` guards GPU HOURS — which is exactly the budget a flapping
    provider's pause -> resume cycle must not hand back.

    Both are read straight off the log, generation-keyed exactly as `replay._generation_matches`
    keys everything else: an ABSENT stamp binds to whichever lifecycle is asking (a legacy log
    cannot say), a PRESENT-but-different one is rejected, and an explicit null is invalid rather
    than unstamped.
    """
    from looplab.engine.evaluate import _durable_dep_rounds, _durable_full_retrains
    from looplab.events.eventstore import Event

    def _dep(**data):
        return Event(v=1, seq=0, ts=0.0, type="deps_installed", data=data)

    def _rt(**data):
        return Event(v=1, seq=0, ts=0.0, type="full_retrain_charged", data=data)

    deps_rows = [
        _dep(node_id=0, generation=0, packages=["numpy"], round=1),
        _dep(node_id=0, generation=0, packages=["scipy"], round=2),
        _dep(node_id=1, generation=0, packages=["torch"], round=7),   # another node
        _dep(node_id=0, generation=1, packages=["pandas"], round=1),  # a newer lifecycle
    ]
    assert _durable_dep_rounds(deps_rows, 0, 0) == 2
    assert _durable_dep_rounds(deps_rows, 0, 1) == 1     # the reset lifecycle starts fresh
    assert _durable_dep_rounds(deps_rows, 5, 0) == 0     # a node that installed nothing
    assert _durable_dep_rounds([], 0, 0) == 0
    # `max`, not a count: a crash between the append and the `continue` can duplicate the row, and a
    # duplicate must not inflate a budget. The round number is the authority.
    assert _durable_dep_rounds([deps_rows[0], deps_rows[0], deps_rows[1]], 0, 0) == 2
    assert _durable_dep_rounds([_dep(node_id=0, generation=None, round=3)], 0, 0) == 0

    retrain_rows = [
        _rt(node_id=0, generation=0, spent=1, attempt=2),
        _rt(node_id=0, generation=0, spent=2, attempt=4),
        _rt(node_id=0, generation=1, spent=1, attempt=1),
    ]
    assert _durable_full_retrains(retrain_rows, 0, 0) == 2
    assert _durable_full_retrains(retrain_rows, 0, 1) == 1
    assert _durable_full_retrains(retrain_rows, 9, 0) == 0
    assert _durable_full_retrains([], 0, 0) == 0
    assert _durable_full_retrains([_rt(node_id=0, generation=None, spent=5)], 0, 0) == 0
    # A log written before the event existed contributes 0 — the honest reading. Inventing a charge
    # for an old log would abandon nodes that never spent any compute.
    assert _durable_full_retrains([
        Event(v=1, seq=0, ts=0.0, type="node_repaired",
              data={"node_id": 0, "generation": 0, "attempt": 1})], 0, 0) == 0


def test_the_retrain_charge_cannot_ride_on_node_repaired():
    """Why `full_retrain_charged` is its own event, stated as a property rather than a comment.

    `node_repaired` is appended BEFORE the loop asks `_repair_forces_full_retrain`, so a field on
    that event would always carry the count as of the PREVIOUS repair — and a resume would refund
    the most recent re-train, the single charge this cap exists to hold. This pins the ordering that
    makes the separate event necessary; if someone reorders the loop so the append follows the
    decision, this test is where they will find out that the event can then be folded back in.
    """
    import inspect
    from looplab.engine.orchestrator import Engine

    src = inspect.getsource(Engine._evaluate)
    append_at = src.index("EV_NODE_REPAIRED, repair_payload")
    decide_at = src.index("_repair_forces_full_retrain(res, next_start)")
    assert append_at < decide_at, (
        "the node_repaired append no longer precedes the re-train decision — the charge can now "
        "ride on that event as an additive field, and EV_FULL_RETRAIN_CHARGED can retire")
    charge_at = src.index("EV_FULL_RETRAIN_CHARGED")
    assert decide_at < charge_at, "the charge must be appended after the decision that makes it"


def test_the_attempt_loop_actually_seeds_from_the_durable_ledgers():
    """The functions above are useless unless the LOOP calls them, and testing them in isolation
    does not check that — measured: reverting both seeds to `= 0` left every assertion above green.

    AST, not a substring: `called_names` resolves real `ast.Call` nodes, so a commented-out call
    does not satisfy it. This is tier 3 by CLAUDE.md's ladder and it is honest about what it proves —
    that the call is PRESENT in `_evaluate`, not that it executed. Reaching the seeding behaviourally
    needs a real sandboxed evaluation that fails in exactly the right way and is then resumed by a
    second Engine over the same run dir; the three ledger readers are pure and fully driven above,
    so what is left uncovered is the wiring, which is what this pins.
    """
    from tests._source_scan import called_names
    from looplab.engine.orchestrator import Engine

    called = called_names(Engine._evaluate)
    for fn in ("_durable_repair_ledger", "_durable_dep_rounds", "_durable_full_retrains"):
        assert fn in called, (
            f"{fn} is no longer called by the attempt loop — that budget silently went back to being "
            "process-local, and a resume refunds it")
