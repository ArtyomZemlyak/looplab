"""THE REPAIR CONTEXT IS THE ENGINE'S OWN RECORD (review 2026-09-22, ENG2-14 / doc 50 ES2-05, and the
repair-context audit beside it) — `Settings.repair_context_record`.

The audit drove scripted failures through the REAL `Engine._evaluate` and captured exactly what
`Developer.repair` receives. Four places where that text contradicted the engine's record, or left
out what it holds:

  1. ES2-05. A `not_learning` / `diverged` the DIAGNOSTICIAN named over a `check_failed` stage was
     handed "LoopLab's live training watchdog KILLED this stage … check the specific thing the
     watchdog named above" — nothing was killed, and the account at the head of the text is the
     diagnostician's. 14 of the 122 rows of the triage corpus (`bench-out/cand.durable.jsonl`) are
     exactly this case: triage-sourced `not_learning` over `check_failed`, every one a `repair`.
  2. A process that exited non-zero with nothing on stderr (a SIGKILL, -9) or hit its deadline was
     described as "the command ran cleanly (exit 0) but printed NO parseable metric" — one line above
     the timeout directive that contradicts it.
  3. A clean exit with no metric that wrote ANYTHING to stderr lost the one sentence naming the key
     the eval reads: the Developer was handed a `UserWarning` and asked to fix it.
  4. The node's own earlier repairs — the rows the triage judge and the critic read every attempt —
     never reached the Developer: a three-repair chain got byte-identical text three times while the
     stuck contract asked whether "every fix you can think of has already been tried and failed".

ON fixes all four; OFF is the historical text BYTE FOR BYTE — proven against digests taken on the
tree before the change, both over a grid of the two text rules and over the Developer's whole
context in driven runs. Every case below drives the real loop or the real rule; none reads source
text except the one-reader census at the end, which reads the AST.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import anyio
import pytest

from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import DEVELOPER_STUCK_PREFIX, FAILURE_REASONS
from looplab.engine import crash_repair
from looplab.engine.crash_repair import (CrashRepairMixin, _DIAGNOSED_DIVERGED_LEAD,
                                         _DIAGNOSED_NOT_LEARNING_LEAD, _DIVERGED_FIX,
                                         _NOT_LEARNING_FIX, _WATCHDOG_DIVERGED_LEAD,
                                         _WATCHDOG_NOT_LEARNING_LEAD, _format_repair_log,
                                         developer_repair_history)
from looplab.engine.evaluate import EvaluateMixin, _durable_repair_ledger
from looplab.engine.failure_diagnosis import (REASON_SOURCES, engine_observed_facts,
                                              exit_signal_name, silent_exit_account)
from looplab.engine.options import EngineOptions
from looplab.engine.shared import repair_context_record
from looplab.events.eventstore import EventStore
from looplab.runtime.sandbox import RunResult
from tests._source_scan import called_names, iter_trees
from tests.test_repair_loop_golden import _Judge, _emits, _engine

GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"


class _RecDev:
    """Records the `error` of every repair call — exactly what the Developer is handed."""

    def __init__(self, first, answers=(), tail=None):
        self.first, self.answers, self.tail = first, list(answers), tail
        self.contexts: list[str] = []

    def implement(self, idea):
        return self.first

    def repair(self, idea, code, error):
        self.contexts.append(error)
        if self.answers:
            return self.answers.pop(0)
        return self.tail(len(self.contexts)) if callable(self.tail) else GOOD


def _drive(run_dir, dev, judge=None, *, fake=None, kill=None, **kw):
    """One seeded node through the real `_evaluate`, the product's `deep_repair`, and the scripted
    evaluator result / watchdog kill when given. The same construction the audit captured its
    pre-change contexts with, so the OFF digest below is a comparison with the tree before."""
    kw.setdefault("deep_repair", True)
    kw.setdefault("inline_repair", True)
    eng = _engine(run_dir, dev=dev, judge=judge or _Judge(), **kw)
    if kill is not None:
        # The live training watchdog, scripted: it fills the attempt's kill signal the way
        # `_monitor_training` does when it stops a stage it attributes to the implementation.
        eng._train_monitor = True
        eng._eval_spec = eng._eval_spec or {"command": ["python", "x.py"],
                                            "metric": {"key": "metric"}}

        async def fake_monitor(node_id, generation, workdir, cancel, ctx, kill_signal, *_a):
            kill_signal.update(kill)
        eng._monitor_training = fake_monitor
    if fake is not None:
        results = list(fake)

        def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
            return results.pop(0) if len(results) > 1 else results[0]
        eng._run_eval = fake_run_eval
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g",
                                     "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "seed"},
        "code": dev.implement(None)})

    async def _run():
        with anyio.move_on_after(300):
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
    anyio.run(_run)
    return dev.contexts, EventStore(Path(run_dir) / "events.jsonl").read_all()


_SUMMARY = ("The loss sat at exactly 2.3026 (= ln 10) for all 12 epochs while grad_norm stayed at "
            "0.0: `data.py:31` shuffles the labels independently of the inputs, so every batch "
            "pairs each image with a random label and the objective has nothing to descend on. ")


def _diagnosing(kind, summary):
    return _Judge({"action": "repair", "rationale": "fix the objective", "failure_kind": kind,
                   "evidence_source": "log", "evidence_locator": "train.log:12",
                   "evidence_quote": "loss=2.3026", "summary": summary})


_CHECK_FAILED = RunResult(
    exit_code=0, stdout="epoch 12 loss=2.3026\n", metric=None, timed_out=False,
    stderr="stage 'train' failed verification: the training loss did not decrease over 12 epochs",
    stages=[{"name": "train", "status": "check_failed", "exit_code": 0, "seconds": 1.0}],
    failed_stage="train")

# name -> (Developer factory, keyword arguments). The six the OFF digest was taken over.
_SCENARIOS = {
    "metric_key_missing_with_warning": (lambda: _RecDev(
        "import json, sys\nsys.stderr.write('UserWarning: lr scheduler stepped before optimizer\\n')\n"
        "print(json.dumps({'accuracy': 0.91}))\n"), {}),
    # SCRIPTED, not a real `os.kill(..., SIGKILL)`: Windows has no `signal.SIGKILL`, so the real kill
    # became an AttributeError traceback there (Windows run 53) and the scenario stopped being a
    # silent kill. The results are exactly what the real SIGKILL and the repaired run produced on
    # POSIX — the OFF digest below, pinned over the real kill, is unchanged by the swap.
    "killed_silently": (lambda: _RecDev("import os, signal\nprint('epoch 1 loss=0.9')\n"
                                        "os.kill(os.getpid(), signal.SIGKILL)\n"), dict(
        fake=[RunResult(exit_code=-9, stdout="epoch 1 loss=0.9\n", stderr="", metric=None,
                        timed_out=False),
              # …and the repaired attempt scores, as the real one did on `GOOD`.
              RunResult(exit_code=0, stdout='{"metric": 0.1}\n', stderr="", metric=0.1,
                        timed_out=False)])),
    "check_failed_diagnosed_not_learning": (lambda: _RecDev("print(1)\n"), dict(
        fake=[_CHECK_FAILED], judge=_diagnosing("not_learning", _SUMMARY))),
    "check_failed_diagnosed_diverged": (lambda: _RecDev("print(1)\n"), dict(
        fake=[_CHECK_FAILED], judge=_diagnosing("diverged", "loss became nan at step 311"))),
    "watchdog_not_learning_kill": (lambda: _RecDev("print(1)\n"), dict(
        fake=[RunResult(exit_code=-9, stdout="", stderr="", metric=None, timed_out=False,
                        stages=[{"name": "train", "status": "fail", "exit_code": -9,
                                 "seconds": 1.0}], failed_stage="train")],
        kill={"kill": True, "terminal_reason": "not_learning",
              "reason": "loss frozen at 2.3026 for 400 steps"})),
    "repeated_failure_chain": (lambda: _RecDev(
        _emits("KeyError: 'label'\n"),
        answers=[_emits("KeyError: 'label'\n") + "# tried renaming the column\n",
                 _emits("KeyError: 'label'\n") + "# tried renaming the column\n"],
        tail=lambda n: f"{DEVELOPER_STUCK_PREFIX} out of ideas)"),
        dict(inline_repair_attempts=4)),
}

# sha256 over the Developer's contexts in the six scenarios, captured on the tree BEFORE the flag
# existed (14 repair calls). OFF must reproduce it byte for byte.
_OFF_CONTEXTS_BEFORE = "6e442cb822d772d16eee2b14664dd8c4decd8e368ed532c6a5167ba0aedc1e50"


def _scenario(tmp_path, name, **extra):
    make_dev, kw = _SCENARIOS[name]
    kw = dict(kw)
    judge = kw.pop("judge", None)
    return _drive(tmp_path / name, make_dev(), judge, **{**kw, **extra})


# ------------------------------------------------------------------- OFF is the historical text

def test_off_the_developer_is_handed_what_it_was_handed_before(tmp_path):
    h = hashlib.sha256()
    calls = 0
    for name in _SCENARIOS:
        contexts, _events = _scenario(tmp_path, name)
        for ctx in contexts:
            # CRLF FOLDED, as the golden this module drives folds it: a real child's stderr on
            # Windows ends its lines `\r\n`, and the digest pins what the Developer was TOLD.
            ctx = ctx.replace("\r\n", "\n")
            h.update(name.encode() + b"\x00" + ctx.encode("utf-8") + b"\x00")
            calls += 1
    assert calls == 14 and h.hexdigest() == _OFF_CONTEXTS_BEFORE


class _Repairer(CrashRepairMixin):
    def __init__(self, deep, repo=None, record=None):
        self._deep_repair = deep
        self._repo_spec = repo
        self._eval_parallel = 2
        self._gpu_ids = [0, 1]
        if record is not None:
            self._repair_context_record = record


class _TextHost:
    def __init__(self, spec=None, record=None):
        self._eval_spec = spec
        if record is not None:
            self._repair_context_record = record

    def _redact(self, text):
        return text


# FROZEN at the reasons the two digests below were pinned against, and not `sorted(FAILURE_REASONS)`:
# this grid asserts that the OFF rung keeps the HISTORICAL bytes, and a reason added later has no
# historical bytes to keep -- enumerating the live registry made every new member a digest change
# that said nothing about the rule under test. `inert_path` (2026-09-23) is the first such member.
_PINNED_REASONS = ("check_failed", "check_false_positive", "crash", "diverged", "drift",
                   "expect_failed", "needs_failed", "no_metric", "not_learning", "oom",
                   "rules_violation", "setup", "stalled", "timeout", "unclassified")
_REASONS = sorted(_PINNED_REASONS) + ["idea_rejected", "developer_crash", "unknown_kind", ""]


def test_the_pinned_grid_names_every_reason_but_the_ones_added_after_it():
    assert set(FAILURE_REASONS) - set(_PINNED_REASONS) == {"inert_path"}
    assert set(_PINNED_REASONS) <= set(FAILURE_REASONS)


def _context_grid(record=None, **kw):
    out = []
    for deep in (False, True):
        for repo in (None, {"editables": ["x"]}):
            r = _Repairer(deep, repo, record)
            for reason in _REASONS:
                for error in ("", "boom", "stage 'train' failed verification: loss flat\n"):
                    for headline in ("", "ValueError: shapes do not align"):
                        for fence in ("", "[fence: refused read]"):
                            out.append(r._repair_error_context(
                                reason, error, headline=headline, fence_note=fence, **kw))
    return out


def _text_grid(record=None):
    out = []
    for spec in (None, {"metric": {"key": "auc", "kind": "stdout_json"}},
                 {"metric": {"key": "auc", "kind": "file"}}):
        host = _TextHost(spec, record)
        for stderr in ("", "  \n ", "Traceback...\nValueError: boom\n", "UserWarning: w\n" * 60):
            for exit_code, timed_out in ((0, False), (1, False), (-9, False), (-9, True),
                                         (137, False)):
                for drift in (None, {"primary": 1.0, "cross": 0.5}):
                    for failed in (None, "train"):
                        for metric in (None, 0.5):
                            out.append(EvaluateMixin._eval_failure_text(host, RunResult(
                                exit_code=exit_code, stdout="", stderr=stderr, metric=metric,
                                timed_out=timed_out, drift=drift, failed_stage=failed)))
    return out


def _digest(texts):
    h = hashlib.sha256()
    for text in texts:
        h.update(text.encode("utf-8") + b"\x00")
    return h.hexdigest()


# Both taken on the tree before the change (912 and 480 texts).
_CONTEXT_GRID_BEFORE = "821d5bf87a12c280d73ae72721b00003a45d6be4bb85913650091b8a6f948488"
_TEXT_GRID_BEFORE = "a10da6738a4036704042b84f87c8f22d0364197c0ac52617125f2611da5d2ffe"


@pytest.mark.parametrize("source", [None, *REASON_SOURCES])
@pytest.mark.parametrize("record", [None, False])
def test_off_both_text_rules_are_the_historical_bytes_whoever_named_the_reason(record, source):
    """An absent attribute (a stub that never ran `Engine.__init__`) and an explicit OFF alike, and
    whichever source the caller now passes — OFF ignores it."""
    assert _digest(_context_grid(record, reason_source=source)) == _CONTEXT_GRID_BEFORE
    assert _digest(_text_grid(record)) == _TEXT_GRID_BEFORE


# ------------------------------------------------------------------------- 1. who stopped it (ES2-05)

def _directive(ctx: str) -> str:
    """The directive: what follows the error text, up to the stuck contract."""
    return ctx.split("\nstage 'train' failed verification: the training loss did not decrease "
                     "over 12 epochs\n", 1)[1].split("\n\n[YOU MAY DECLINE.", 1)[0]


@pytest.mark.parametrize("name, kind", [("check_failed_diagnosed_not_learning", "not_learning"),
                                        ("check_failed_diagnosed_diverged", "diverged")])
def test_a_diagnosed_kind_is_never_told_a_watchdog_killed_it(tmp_path, name, kind):
    off, off_events = _scenario(tmp_path / "off", name)
    on, on_events = _scenario(tmp_path / "on", name, repair_context_record=True)
    lead = {"not_learning": (_WATCHDOG_NOT_LEARNING_LEAD, _DIAGNOSED_NOT_LEARNING_LEAD),
            "diverged": (_WATCHDOG_DIVERGED_LEAD, _DIAGNOSED_DIVERGED_LEAD)}[kind]
    fix = {"not_learning": _NOT_LEARNING_FIX, "diverged": _DIVERGED_FIX}[kind]
    # the durable row says who named it — the fact the sentence is keyed on
    repaired = [e.data for e in on_events if e.type == "node_repaired"]
    assert repaired and {r["reason"] for r in repaired} == {kind}
    assert {r["reason_source"] for r in repaired} == {"triage"}
    assert {r["engine_reason"] for r in repaired} == {"check_failed"}
    # the defect, on the OFF bytes
    assert _directive(off[0]) == lead[0] + fix and "KILLED this stage" in off[0]
    # ON: the diagnostician's account opens it, nothing claims a kill, and the FIX is the same words
    assert _directive(on[0]) == lead[1] + fix
    assert "KILLED" not in on[0] and "watchdog named" not in on[0]
    assert on[0].startswith(f"[failure kind: {kind}]\nThe failure diagnostician read this run's "
                            "logs and concluded:")


def test_the_watchdogs_own_kill_keeps_its_sentence_when_on(tmp_path):
    on, on_events = _scenario(tmp_path, "watchdog_not_learning_kill", repair_context_record=True)
    assert {e.data["reason_source"] for e in on_events if e.type == "node_repaired"} == {"engine"}
    assert _WATCHDOG_NOT_LEARNING_LEAD + _NOT_LEARNING_FIX in on[0]
    assert on[0].startswith("[failure kind: not_learning]\nThe live training watchdog stopped "
                            "this run: loss frozen at 2.3026 for 400 steps")


@pytest.mark.parametrize("source", [None, "engine", "undiagnosed", "declared"])
def test_only_the_diagnosticians_source_changes_the_sentence(source):
    r = _Repairer(False, record=True)
    for reason, lead, fix in (("not_learning", _WATCHDOG_NOT_LEARNING_LEAD, _NOT_LEARNING_FIX),
                              ("diverged", _WATCHDOG_DIVERGED_LEAD, _DIVERGED_FIX)):
        assert r._repair_error_context(reason, "e", reason_source=source) == (
            f"[failure kind: {reason}]\ne\n" + lead + fix)
    assert "No LoopLab watchdog" in r._repair_error_context("diverged", "e",
                                                            reason_source="triage")


def test_no_other_directive_moves_when_on():
    """The flag keys two directives on the source; every other reason renders the same bytes ON
    and OFF, whoever named it — the change is exactly the two sentences."""
    for source in (None, *REASON_SOURCES):
        on = _context_grid(True, reason_source=source)
        off = _context_grid(False, reason_source=source)
        moved = {(a, b) for a, b in zip(off, on) if a != b}
        if source != "triage":
            assert not moved
        else:
            assert moved and all(b.startswith(("[failure kind: not_learning]",
                                               "[failure kind: diverged]")) for _a, b in moved)


# --------------------------------------------------------------------- 2/3. how the process ended

def test_a_silent_kill_is_not_described_as_a_clean_exit(tmp_path):
    off, _ = _scenario(tmp_path / "off", "killed_silently")
    on, on_events = _scenario(tmp_path / "on", "killed_silently", repair_context_record=True)
    assert "exit=-9 timed_out=False no_metric — the command ran cleanly (exit 0)" in off[0]
    assert "ran cleanly" not in on[0] and "no_metric" not in on[0]
    assert ("exit=-9 timed_out=False — the process was killed by SIGKILL and wrote nothing to "
            "stderr, so there is no traceback to read.") in on[0]
    # the SAME account is the node's durable record and the judge's input, not only the prompt
    repaired = [e.data for e in on_events if e.type == "node_repaired"][0]
    assert "killed by SIGKILL" in repaired["error_in"]


def test_a_deadline_is_not_described_as_a_clean_exit(tmp_path):
    timed_out = RunResult(exit_code=-9, stdout="epoch 1 loss=0.9\n", stderr="", metric=None,
                          timed_out=True)
    on, _ = _drive(tmp_path / "on", _RecDev("print(1)\n"), fake=[timed_out],
                   repair_context_record=True)
    off, _ = _drive(tmp_path / "off", _RecDev("print(1)\n"), fake=[timed_out])
    assert "ran cleanly (exit 0)" in off[0] and "exceeded its evaluation time budget" in off[0]
    assert "ran cleanly" not in on[0] and "exceeded its evaluation time budget" in on[0]
    assert ("exit=-9 timed_out=True — the evaluation was stopped at its time budget before it "
            "printed a metric, and it wrote nothing to stderr.") in on[0]


def test_a_warning_on_stderr_no_longer_hides_the_key_the_eval_reads(tmp_path):
    off, _ = _scenario(tmp_path / "off", "metric_key_missing_with_warning")
    on, _ = _scenario(tmp_path / "on", "metric_key_missing_with_warning",
                      repair_context_record=True)
    hint = ("the command ran cleanly (exit 0) but printed NO parseable metric. The eval reads a "
            "stdout JSON line for key 'metric'")
    assert hint not in off[0] and "UserWarning" in off[0]
    assert hint in on[0]
    # in FRONT of the tail, which keeps its whole window
    assert on[0].index(hint) < on[0].index("UserWarning: lr scheduler stepped before optimizer")


def test_the_failure_text_rule_moves_only_the_cases_it_names():
    """Over the whole grid: ON differs from OFF only for a silent unclean exit (its account) and a
    clean, metric-less, stage-less, drift-less exit that wrote to stderr (the prepended hint)."""
    specs = (None, {"metric": {"key": "auc", "kind": "stdout_json"}})
    for spec in specs:
        for stderr in ("", "  \n ", "Traceback...\nValueError: boom\n"):
            for exit_code, timed_out in ((0, False), (1, False), (-9, False), (-9, True)):
                for drift in (None, {"primary": 1.0}):
                    for failed in (None, "train"):
                        for metric in (None, 0.5):
                            res = RunResult(exit_code=exit_code, stdout="", stderr=stderr,
                                            metric=metric, timed_out=timed_out, drift=drift,
                                            failed_stage=failed)
                            off = EvaluateMixin._eval_failure_text(_TextHost(spec, False), res)
                            on = EvaluateMixin._eval_failure_text(_TextHost(spec, True), res)
                            clean = exit_code == 0 and not timed_out
                            silent = not stderr.strip()
                            if silent and drift is None and not clean:
                                assert on != off and "ran cleanly" not in on
                                assert on.endswith(silent_exit_account(exit_code, timed_out))
                            elif (not silent and clean and drift is None and metric is None
                                  and failed is None):
                                assert on.startswith("[no_metric — the command ran cleanly") and (
                                    on.endswith(off))
                            else:
                                assert on == off


def test_the_silent_exit_account_states_only_what_the_exit_holds():
    assert silent_exit_account(-9, True).startswith("— the evaluation was stopped at its time")
    assert "killed by SIGKILL" in silent_exit_account(-9, False)
    assert "killed by SIGKILL" in silent_exit_account(137, False)      # the shell's spelling
    assert "killed by SIGSEGV" in silent_exit_account(-11, False)
    assert "non-zero exit" in silent_exit_account(1, False)
    assert "non-zero exit" in silent_exit_account(-64, False)          # a signal with no name here
    assert "non-zero exit" in silent_exit_account(None, False)         # total over junk
    for code, name in ((-9, "SIGKILL"), (137, "SIGKILL"), (-15, "SIGTERM"), (143, "SIGTERM"),
                       (1, None), (0, None), (128, None), (160, None), ("x", None)):
        assert exit_signal_name(code) == name
    assert not any(word in silent_exit_account(-9, False).lower() for word in ("oom", "memory")), (
        "the exit code names a signal, never its cause — that is the diagnostician's judgement")


def test_the_diagnosticians_account_of_an_exit_did_not_move():
    """`engine_observed_facts` now reads the shared signal table; its bytes are the historical ones."""
    res = RunResult(exit_code=-9, stdout="", stderr="", metric=None, timed_out=False)
    assert engine_observed_facts(res).startswith(
        "--- WHAT THE ENGINE ITSELF OBSERVED (facts, not readings) ---\nexit code -9 (killed by "
        "SIGKILL); the process wrote NOTHING to stderr.\n")
    res = RunResult(exit_code=3, stdout="", stderr="boom", metric=None, timed_out=False)
    assert "exit code 3; it wrote 4 characters to stderr." in engine_observed_facts(res)


# ---------------------------------------------------------------------------- 4. what was tried

def test_the_developer_sees_what_this_node_already_tried(tmp_path):
    on, events = _scenario(tmp_path / "on", "repeated_failure_chain", repair_context_record=True)
    off, _ = _scenario(tmp_path / "off", "repeated_failure_chain")
    assert len(on) == 3 and len(set(off)) == 1, "OFF: three repairs, one text"
    header = "--- WHAT HAS ALREADY BEEN TRIED ON THIS NODE (oldest first) ---"
    assert header not in on[0] and on[0] == off[0], "a first repair has nothing to show"
    _attempts, rows, _ = _durable_repair_ledger(events, 0, 0)
    for n in (1, 2):
        # the judge's own rendering of the durable rows written before repair n+1, AFTER the stuck
        # contract, so every byte the context carried before keeps its place
        assert on[n] == off[n] + "\n\n" + _format_repair_log(rows[:n])
        assert on[n].index("[YOU MAY DECLINE.") < on[n].index(header)
    assert "attempt 2: failed with — KeyError: 'label'" in on[2]
    assert "THE ENGINE COMPARED THE BYTES: this attempt changed no file at all" in on[2]


def test_the_history_block_is_the_judges_rendering_and_empty_renders_empty():
    rows = [{"attempt": 1, "error": "KeyError: 'x'", "fix": "rename", "changed": [],
             "stages_passed": 0, "verified": "inert", "unmet": []}]
    assert developer_repair_history([]) == developer_repair_history(None) == ""
    assert developer_repair_history(rows) == "\n\n" + crash_repair._format_repair_log(rows)


# --------------------------------------------------------------------------------- the switch

def test_on_for_new_runs_off_for_a_pre_field_snapshot_and_off_at_every_constructor():
    """MUTATION: drop the LEGACY row -> the pre-field snapshot reads ON."""
    assert Settings().repair_context_record is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["repair_context_record"] is False
    legacy = Settings().masked_snapshot()
    for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
        legacy.pop(field, None)
    legacy.pop("config_snapshot_schema", None)
    assert settings_from_snapshot(legacy).repair_context_record is False
    assert settings_from_snapshot(Settings().masked_snapshot()).repair_context_record is True
    assert EngineOptions().repair_context_record is False
    assert EngineOptions.from_settings(Settings()).repair_context_record is True


def test_the_settings_reach_the_repair_path_through_the_engine(tmp_path):
    """Settings -> EngineOptions -> the knob -> the one reader, on a real Engine, both ways."""
    on = _engine(tmp_path / "on", dev=_RecDev(GOOD), judge=_Judge(),
                 options=EngineOptions.from_settings(Settings()))
    off = _engine(tmp_path / "off", dev=_RecDev(GOOD), judge=_Judge())
    assert repair_context_record(on) is True and repair_context_record(off) is False
    assert repair_context_record(object()) is False, "a stub that never ran __init__ reads OFF"


def test_one_reader_and_the_three_sites_ask_it():
    """The attribute is read in ONE place (`shared.py::repair_context_record`) — by AST, so a
    comment is not a read — and the three sites that change text ask that reader."""
    readers = []
    for path, tree in iter_trees():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "_repair_context_record"
                    and isinstance(node.ctx, ast.Load)):
                readers.append(path.name)
            elif (isinstance(node, ast.Constant) and node.value == "_repair_context_record"):
                readers.append(path.name)
    assert readers == ["shared.py"], readers
    for fn in (CrashRepairMixin._repair_error_context, EvaluateMixin._eval_failure_text,
               EvaluateMixin._eval_apply_repair):
        assert called_names(fn).count("repair_context_record") == 1, fn.__qualname__
    assert "developer_repair_history" in called_names(EvaluateMixin._eval_apply_repair)
