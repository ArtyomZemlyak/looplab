"""The two independent defects behind a live 3.5-hour, 2345-`node_repaired` inline-repair runaway
on ONE node (`runs/rubert-dr-0804`), and the guards that stop each.

Cause 1 — a varying identifier defeated the anti-stuck signature. `transformers` had been
auto-installed in a broken state, so every eval failed inside its lazy-import registry naming a
DIFFERENT symbol each time (`'ColPaliProcessor'`, `'MarkupLMProcessor'`, `'SplitModulelist'`, …).
`_normalize_error_sig` normalized numbers and paths but not quoted identifiers, so 2345 failures
minted 369 distinct signatures whose longest identical RUN was 2 — under a threshold of 4 the
consecutive counter reset on nearly every attempt and the guard never fired. With
`inline_repair_attempts = 0` (UNLIMITED, an operator decision) that guard is the ONLY bound.

Cause 2 — a failed repair CALL was treated as a repair. The Developer's repair request died with
`402 out of credits`, so it returned the in-band "(developer error: …)" sentinel; that string was
committed as the node's code by `node_repaired`, re-materialized, and re-evaluated. 2343 of the
2345 "repairs" were this. A provider/transport failure is not a code defect and must not drive the
code-repair loop.

These tests drive the REAL `_evaluate` repair loop (only the solution's source is scripted), so
they exercise the actual guards, not a model of them.
"""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import DEVELOPER_ERROR_PREFIX, Idea, NodeStatus
from looplab.engine.orchestrator import Engine, _normalize_error_sig
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"

# The 402 the run actually got back, verbatim in shape (OpenRouter, credits exhausted).
_402 = (f"{DEVELOPER_ERROR_PREFIX} LLM request to https://openrouter.ai/api/v1 failed: "
        "Error code: 402 - {'error': {'message': 'This request requires more credits, or fewer "
        "max_tokens. You requested up to 64000 tokens, but can only afford 7918.', 'code': 402}})")

# Distinct symbols the real registry walk reported, in first-seen order (374 distinct over 2330
# occurrences). Alphabetic on purpose: the pre-fix normalizer's digit rule did NOT merge them,
# which is exactly why every attempt minted a fresh signature.
_REAL_SYMBOLS = [
    "XLMRobertaModel", "NerPipeline", "ImageTextToTextPipeline", "AriaConfig", "TvpProcessor",
    "PeAudioVideoProcessor", "LayoutXLMProcessor", "PipedPipelineDataFormat", "MergeModulelist",
    "PaddleOCRVLProcessor", "GraniteSpeechProcessor", "ColPaliProcessor", "MarkupLMProcessor",
    "SplitModulelist", "KyutaiSpeechToTextProcessor", "AlignProcessor", "BrosProcessor",
]

_GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"


def _lazy_import_src(symbol: str) -> str:
    """A solution whose crash reproduces the broken-registry shape: same exception, same message
    template, same frame — only the quoted symbol differs."""
    return (f"raise ModuleNotFoundError(\"Could not import module '{symbol}'. "
            "Are this object's requirements defined correctly?\")\n")


class _ScriptedDev:
    """Developer stub: `sources` is the source each repair ships, in order. `sentinel_from` is the
    1-based repair call at which the provider dies and `repair` returns the 402 sentinel instead of
    code — a repair CALL that produced no repair."""

    def __init__(self, sources, sentinel_from=None, first=None):
        self.sources = list(sources)
        self.sentinel_from = sentinel_from
        self.first = first if first is not None else _lazy_import_src(_REAL_SYMBOLS[0])
        self.repair_calls = 0

    def implement(self, idea):
        return self.first

    def repair(self, idea, code, error):
        self.repair_calls += 1
        if self.sentinel_from is not None and self.repair_calls >= self.sentinel_from:
            return _402
        return self.sources.pop(0) if self.sources else _GOOD


class _Stub:
    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})


def _drive(tmp_path, dev, **kw):
    """Seed one node and run the REAL repair loop over it, skipping genesis/policy. Returns the
    engine's events. `inline_repair_attempts=0` (UNLIMITED) unless a test overrides it: the point
    of every loop test here is that the anti-stuck guard — not a count — is what bounds the loop."""
    kw.setdefault("auto_install_deps", False)      # the scripted crash is not a real missing lib
    kw.setdefault("inline_repair", True)
    kw.setdefault("inline_repair_attempts", 0)     # 0 = UNLIMITED (must stay meaning that)
    run_dir = tmp_path / "run"
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=_Stub(), developer=dev,
                 sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": dev.implement(None)})
    # A hard wall so a REGRESSION fails the test instead of hanging CI: the pre-fix loop was
    # unbounded, and this suite must be able to observe that as a failure, not a timeout.
    async def _bounded() -> bool:
        with anyio.move_on_after(120) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the inline-repair loop did not terminate"
    return list(EventStore(run_dir / "events.jsonl").read_all()), run_dir


def _repairs(evs):
    return [e for e in evs if e.type == "node_repaired" and e.data.get("node_id") == 0]


def _terminals(evs):
    return [e for e in evs if e.type in ("node_evaluated", "node_failed")
            and e.data.get("node_id") == 0]


# ------------------------------------------------------------------ cause 1: the signature itself
def test_lazy_import_registry_symbols_collapse_to_one_signature():
    """The exact failure mode: same exception, same message template, same frame, a different
    quoted symbol each time. Pre-fix these were N distinct signatures (369 over the real 2345
    failures, longest identical run 2), so the anti-stuck counter reset on nearly every attempt."""
    sigs = {_normalize_error_sig(
        '  File "/opt/conda/lib/python3.11/site-packages/transformers/utils/import_utils.py", '
        'line 2430, in __getattr__\n    raise ModuleNotFoundError(\n'
        f"ModuleNotFoundError: Could not import module '{sym}'. "
        "Are this object's requirements defined correctly?\n")
        for sym in _REAL_SYMBOLS}
    assert len(sigs) == 1, sigs

    # Dotted module paths from the same registry collapse with the bare symbols' TEMPLATE too —
    # they differ only in the quoted operand.
    dotted = _normalize_error_sig(
        "ModuleNotFoundError: Could not import module 'models.x_clip.processing_x_clip'. "
        "Are this object's requirements defined correctly?")
    bare = _normalize_error_sig(
        "ModuleNotFoundError: Could not import module 'ColPaliProcessor'. "
        "Are this object's requirements defined correctly?")
    assert dotted == bare


def test_signature_still_distinguishes_class_template_and_frame():
    """The other direction, which is what keeps a node that IS making progress alive. The identity
    of a failure is (exception class, message template, where it failed) — all three survive."""
    base = ("ModuleNotFoundError: Could not import module 'alpha'. "
            "Are this object's requirements defined correctly?")
    # 1. exception CLASS
    assert _normalize_error_sig(base) != _normalize_error_sig(
        base.replace("ModuleNotFoundError", "ImportError"))
    # 2. message TEMPLATE (same class, same quoted operand shape, different unquoted prose)
    assert _normalize_error_sig("ModuleNotFoundError: No module named 'alpha'") != \
        _normalize_error_sig("ModuleNotFoundError: Could not import module 'alpha'")
    # 3. WHERE it failed — a fix that moves the crash into another frame mints a new signature
    frame = 'File "/w/train.py", line 3, in {fn}\nAttributeError: object has no attribute'
    assert _normalize_error_sig(frame.format(fn="build_model")) != \
        _normalize_error_sig(frame.format(fn="main"))
    # 4. an unrelated failure stays unrelated (the pre-existing T10 property)
    assert _normalize_error_sig(base) != _normalize_error_sig("TypeError: unsupported operand")


def test_signature_leaves_non_identifier_quoted_text_alone():
    """Only identifier-shaped quoted operands are absorbed. Quoted PROSE is real message content —
    collapsing it would merge genuinely different failures (over-normalizing in the direction that
    abandons a working node)."""
    a = _normalize_error_sig("RuntimeError: expected 'a valid checkpoint dir' here")
    b = _normalize_error_sig("RuntimeError: expected 'a corrupted archive file' here")
    assert a != b
    # …while the identifier-shaped operand in the SAME message shape is absorbed.
    assert _normalize_error_sig("RuntimeError: expected 'checkpoint_dir' here") == \
        _normalize_error_sig("RuntimeError: expected 'archive_file' here")


# ------------------------------------------------------------------ cause 1: the loop it bounds
def test_varying_symbol_no_longer_defeats_the_stuck_guard(tmp_path):
    """The reproduction, with a WORKING provider: a fresh symbol on every eval, unlimited attempts.
    Pre-fix this ran until something else stopped it (2345 repairs / 3.5 h on the real run); the
    guard must now abandon at `inline_repair_stuck_repeat`."""
    dev = _ScriptedDev([_lazy_import_src(s) for s in _REAL_SYMBOLS[1:]])
    evs, _ = _drive(tmp_path, dev, inline_repair_stuck_repeat=4)

    # 4 evals: the 4th sighting of the signature trips the guard, so 3 repairs were spent.
    assert len(_repairs(evs)) == 3, [e.data.get("attempt") for e in _repairs(evs)]
    assert dev.repair_calls == 3
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    assert terminal[0].data["triage_action"] == "abandon"
    assert "stuck" in terminal[0].data["triage_rationale"]
    assert fold(evs).nodes[0].status is NodeStatus.failed


def test_alternating_errors_trip_the_guard_a_streak_counter_would_miss(tmp_path):
    """The guard counts each signature's recurrences over the node, not a consecutive streak. An
    oscillating repair (fix A breaks B, fix B breaks A) never produces two identical signatures in a
    row, so a streak counter resets to 1 forever — the same shape of hole as cause 1."""
    a = _lazy_import_src("AlphaProcessor")                    # ModuleNotFoundError
    b = "raise AttributeError('no attribute on this object')\n"   # a different mechanical class
    dev = _ScriptedDev([b, a, b, a, b, a], first=a)        # A B A B … , never twice in a row
    evs, _ = _drive(tmp_path, dev, inline_repair_stuck_repeat=4)

    # A is seen at evals 1,3,5,7 -> its 4th sighting trips the guard: 6 repairs, never 4 in a row.
    assert len(_repairs(evs)) == 6
    terminal = _terminals(evs)
    assert len(terminal) == 1 and "stuck" in terminal[0].data["triage_rationale"]


def test_unlimited_attempts_still_means_unlimited(tmp_path):
    """`inline_repair_attempts = 0` must keep meaning UNLIMITED. A node whose error genuinely moves
    every attempt gets MORE repairs than `inline_repair_stuck_repeat` — the fix bounds the loop
    through the guard, it does not quietly reinstate a count."""
    # The crash MOVES every attempt (a different frame each time — word names, not digits, which
    # the numeric rule would collapse), so every signature is genuinely new.
    moving = [f"def {fn}():\n    raise AttributeError('no attribute on this object')\n{fn}()\n"
              for fn in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")] + [_GOOD]
    dev = _ScriptedDev(moving)
    evs, _ = _drive(tmp_path, dev, inline_repair_stuck_repeat=4)

    assert len(_repairs(evs)) == 7 > 4          # well past the stuck threshold: never capped
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_evaluated"
    assert fold(evs).nodes[0].status is NodeStatus.evaluated


def test_a_genuinely_moving_crash_is_still_repaired_to_success(tmp_path):
    """Do not weaken repair for the case it exists for: the error text legitimately changes as the
    agent makes progress (different class, different frame), and the node must still be fixed."""
    dev = _ScriptedDev([
        "def build():\n    raise TypeError(\"__init__() got an unexpected keyword 'hidden'\")\n"
        "build()\n",
        "def main():\n    raise AttributeError(\"object has no attribute 'fit'\")\nmain()\n",
        _GOOD,
    ])
    evs, _ = _drive(tmp_path, dev, inline_repair_stuck_repeat=4)

    assert len(_repairs(evs)) == 3
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_evaluated"
    st = fold(evs)
    assert st.nodes[0].status is NodeStatus.evaluated and st.nodes[0].code == _GOOD


# ------------------------------------------------------------------ cause 2: a failed repair CALL
def test_failed_repair_call_is_not_a_repair(tmp_path):
    """The 402. The repair CALL failed, so no repaired code exists: nothing may be recorded as a
    repair, nothing may reach disk, and the node terminalizes naming the provider failure."""
    dev = _ScriptedDev([], sentinel_from=1)
    evs, run_dir = _drive(tmp_path, dev, inline_repair_stuck_repeat=4)

    assert dev.repair_calls == 1                       # asked once, then stopped asking
    assert _repairs(evs) == []                         # a failed CALL is not an attempt
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].type == "node_failed"
    assert terminal[0].data["reason"] == "developer_crash"
    assert terminal[0].data["triage_action"] == "abandon"
    assert "402" in terminal[0].data["error"]          # the operator sees WHY, on the node too

    # The sentinel never became the node's code …
    node = fold(evs).nodes[0]
    assert not node.code.startswith(DEVELOPER_ERROR_PREFIX)
    assert node.error_reason == "developer_crash"
    # … and never reached the workdir as if it were a fix.
    for path in (run_dir / "nodes").rglob("*"):
        if path.is_file():
            assert DEVELOPER_ERROR_PREFIX not in path.read_text(encoding="utf-8", errors="replace")


def test_failed_repair_call_does_not_consume_an_attempt_or_reset_the_guard(tmp_path):
    """A real repair happens first, then the provider dies. The failed CALL must not be counted as
    the second attempt (the pre-fix loop counted 2343 of them) — the terminal names the provider,
    not the stuck guard, and exactly ONE `node_repaired` exists."""
    dev = _ScriptedDev([_lazy_import_src("ColPaliProcessor")], sentinel_from=2)
    evs, _ = _drive(tmp_path, dev, inline_repair_stuck_repeat=4)

    repairs = _repairs(evs)
    assert len(repairs) == 1 and repairs[0].data["attempt"] == 1
    assert dev.repair_calls == 2                       # the 2nd call returned the sentinel
    terminal = _terminals(evs)
    assert len(terminal) == 1
    assert terminal[0].data["reason"] == "developer_crash"
    assert "provider" in terminal[0].data["triage_rationale"]


def test_failed_repair_call_surfaces_as_a_run_level_pause(tmp_path):
    """The operator must learn "your endpoint is out of credits" from ONE pause reason, not by
    reading thousands of repair events. Mirrors the build path's PAUSE-on-first-developer_crash
    circuit-breaker: every other node reaches the same dead endpoint."""
    dev = _ScriptedDev([], sentinel_from=1)
    evs, _ = _drive(tmp_path, dev)

    pauses = [e for e in evs if e.type == "pause"]
    assert len(pauses) == 1
    reason = pauses[0].data["reason"]
    assert "402" in reason and "openrouter" in reason.lower()
    assert pauses[0].data.get("node_id") is None       # run-level: the fix is the provider, not
    assert fold(evs).paused is True                    # this node, so a node reset must not clear it


def test_no_second_auto_pause_when_the_run_is_already_halting(tmp_path, monkeypatch):
    """The auto-pause re-checks `_run_halt_intent` exactly as `confirm_phase.py::_pace_confirm_
    refusal` does, so a run that is already paused / finished / stopping — or a batch of siblings
    all hitting the same dead endpoint — does not collect a redundant pause each time. The node's
    own terminal still lands, naming the provider failure."""
    dev = _ScriptedDev([], sentinel_from=1)
    monkeypatch.setattr(Engine, "_run_halt_intent", lambda self: True)   # already halting
    evs, _ = _drive(tmp_path, dev)

    assert [e for e in evs if e.type == "pause"] == []
    terminal = _terminals(evs)
    assert len(terminal) == 1 and terminal[0].data["reason"] == "developer_crash"


# ------------------------------------------------------------------ replay safety of both paths
@pytest.mark.parametrize("sentinel_from", [None, 1])
def test_one_terminal_and_an_order_tolerant_fold(tmp_path, sentinel_from):
    """Invariants #2 and #5 across both new exits: exactly one terminal per node, and the run-level
    auto-pause folds as a monotone latch — splicing it to the end of the log changes nothing."""
    dev = _ScriptedDev([_lazy_import_src(s) for s in _REAL_SYMBOLS[1:]],
                       sentinel_from=sentinel_from)
    evs, _ = _drive(tmp_path, dev, inline_repair_stuck_repeat=4)

    assert len(_terminals(evs)) == 1
    before = fold(evs)
    pauses = [e for e in evs if e.type == "pause"]
    spliced = [e for e in evs if e.type != "pause"] + pauses      # move any pause to the very end
    after = fold(spliced)
    assert (after.paused, after.nodes[0].status, after.nodes[0].error_reason,
            after.total_eval_seconds) == (
        before.paused, before.nodes[0].status, before.nodes[0].error_reason,
        before.total_eval_seconds)
    # Folding twice is byte-stable on the fields the loop writes (no I/O, no hidden state).
    again = fold(evs)
    assert (again.nodes[0].status, again.nodes[0].code, again.nodes[0].error_reason) == (
        before.nodes[0].status, before.nodes[0].code, before.nodes[0].error_reason)
