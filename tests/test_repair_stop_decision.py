"""What stops the inline-repair loop, after the 2026-08-05 redesign.

THE DECISION. Of the three ways to bound in-node repair — leave it unlimited, cap it at a fixed
count, or let a model decide — the loop now uses the model, with a cap as a backstop:

  * the crash-triage MODEL is consulted once per attempt (the call the loop already made) and is
    handed this node's whole repair history — what failed, what each fix claimed, which files it
    actually touched, how far the pipeline got. Its `abandon` verdict is the stop, and it means
    exactly "I no longer know how to fix this";
  * `inline_repair_attempts` is a HARD operator cap for when the judge is wrong in the expensive
    direction;
  * anything that means the judge could not ANSWER — a dead endpoint, a refusal, an emit nobody can
    parse — is `unanswerable`, which stops the node AND pauses the run naming the provider. It is
    never read as permission to keep repairing.

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
from looplab.engine.triage import (AGENT_TRIAGE_ACTIONS, DEFAULT_TRIAGE_ACTION, TRIAGE_ACTIONS,
                                   coerce_triage_action, repair_artifact_defect)
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
    assert set(TRIAGE_ACTIONS) == {"repair", "abandon", "reject_idea", "unanswerable"}
    # `unanswerable` is ENGINE-minted: a live model must not be able to claim its own
    # unreachability and trip the run-level provider circuit breaker.
    assert "unanswerable" not in AGENT_TRIAGE_ACTIONS
    assert set(AGENT_TRIAGE_ACTIONS) < set(TRIAGE_ACTIONS)


def test_a_verdict_nobody_can_parse_is_not_permission_to_continue():
    """The fail-closed direction, at the coercion. This defaulted to `repair` — "the cheap, safe
    action" — which is only cheap if a repair is cheap: each one is a full re-eval plus two LLM
    calls."""
    for bad in (None, "", "REPAIR!", "keep going", 7, {"a": 1}, "unanswerable"):
        assert coerce_triage_action(bad) == DEFAULT_TRIAGE_ACTION == "unanswerable"
    for good in AGENT_TRIAGE_ACTIONS:
        assert coerce_triage_action(good) == good
        assert coerce_triage_action(f"  {good.upper()} ") == good


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


def test_zero_still_means_unlimited_for_a_resumed_run(tmp_path):
    """`inline_repair_attempts = 0` keeps meaning UNLIMITED — a pre-existing run resumes with it
    (`LEGACY_CONFIG_SNAPSHOT_DEFAULTS`), so the judge must be the only thing stopping it. Under 0
    the node still terminates, because the judge is a real bound and not a decoration."""
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
def test_a_judge_that_cannot_answer_stops_the_node_and_pauses_the_run(tmp_path):
    """THE case the redesign must not get wrong. A dead provider is how the 2345-repair incident
    began, and the judge runs on that same provider — so "the judge did not answer" must never mean
    "keep repairing". It lands on the developer-crash circuit breaker: no repair, ONE terminal
    naming `developer_crash`, and ONE run-level pause the operator can `resume` from."""

    class _DeadJudge(_Judge):
        def triage_crash(self, node, error, attempt, **kw):
            raise RuntimeError("Error code: 402 - out of credits")

    dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    evs, _ = _drive(tmp_path, dev, _DeadJudge(), inline_repair_attempts=12)

    assert _repairs(evs) == []                       # nothing was repaired blind
    assert dev.repair_calls == 0
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].data["reason"] == "developer_crash"
    assert "402" in terminal[0].data["error"]
    pauses = [e for e in evs if e.type == "pause"]
    assert len(pauses) == 1 and pauses[0].data.get("node_id") is None
    assert "402" in pauses[0].data["reason"] and "resume" in pauses[0].data["reason"]
    assert fold(evs).paused is True


@pytest.mark.parametrize("verdict", [
    {"action": "keep-going"},          # outside the vocabulary
    {"action": ""},                    # empty
    {"rationale": "no action at all"},  # missing
    "not even a dict",
    None,
])
def test_a_malformed_verdict_is_unanswerable_not_repair(tmp_path, verdict):
    """A live-but-confused model is the same fail-closed case as a dead one: an emit nobody can read
    is not a verdict. (Tonight's watchdog verification found the mirror-image bug — an unparseable
    verdict silently treated as transparent — so this is not a hypothetical shape.)"""

    class _GarbageJudge(_Judge):
        def triage_crash(self, node, error, attempt, **kw):
            return verdict

    dev = _ScriptedDev([], first=_lazy_import_src("AlphaProcessor"), cycle=True)
    evs, _ = _drive(tmp_path, dev, _GarbageJudge(), inline_repair_attempts=12)

    assert _repairs(evs) == []
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].data["reason"] == "developer_crash"
    assert len([e for e in evs if e.type == "pause"]) == 1


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


def test_the_agent_fallbacks_fail_closed_too():
    """The same rule one layer down, where the transport failure is actually observed. Both of
    `UnifiedAgent.triage_crash`'s degradation paths answered "attempt repair"; they now answer the
    engine's fail-closed action, so a dead endpoint cannot drive the loop with no model in it."""
    import inspect

    from looplab.agents import unified_agent

    src = inspect.getsource(unified_agent.UnifiedAgent.triage_crash)
    body = src.split("def _finalize", 1)[1]
    assert 'action = "repair"' not in body and '"action": "repair"' not in body, (
        "a degradation path still answers 'repair' — that is the reading that let a dead provider "
        "keep the repair loop at full speed")
    assert body.count("DEFAULT_TRIAGE_ACTION") >= 2      # the malformed emit AND the transport path


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
