"""Who may say WHAT FAILED: authenticated facts stay deterministic, readings of text go to a judge.

THE INCIDENT. `runs/e5small-dr-unified-v3` finished with three nodes, zero metrics and the engine's
systemic-failure stop. All three nodes died of `torch.OutOfMemoryError`, and all eight
`node_repaired` rows read `reason: crash`. `_failure_reason`'s `oom` branch recognises exactly ONE
signature — the KERNEL kill (`exit_code in (-9, 137)` and no `Traceback` in stderr) — and an
allocator OOM is its mirror image: it RAISES, prints a full traceback and exits 1, so every conjunct
is false. The Developer therefore got the generic crash directive ("diagnose the root cause")
instead of the memory-reduction one that exists one branch away and is exactly right, and two of its
repairs returned byte-identical files.

THE PRECEDENT is in `engine/triage.py`'s own header: two heuristics were deleted on 2026-08-05
because "a bound that depends on the TEXT QUALITY of a program's error output is not a bound" — one
of them ran 1,741 repairs because an identifier happened to be Cyrillic. The STOPPING decision moved
to the triage model then. The CLASSIFICATION did not, and it has now failed the same way.

THE LINE, and it is the whole of this file:

    * AUTHENTICATED facts — the two watchdog verdicts (`run_argv`'s out-of-band `signals`), the
      engine's own clock, the drift cross-reader's refusal, the stage-contract statuses `_run_stages`
      reports structurally, the engine-written `setup failed:` short-circuit — stay deterministic and
      OVERRIDE the model. The judge is not consulted about one and could not be read if it answered.
    * READINGS — `crash` / `oom` / `no_metric`, the three residual buckets inferred from the dead
      process's own text — go to the judge, over a CLOSED vocabulary. A reason outside it is refused
      and the engine keeps its own answer.

Measured over `runs/` (five modern runs, the two legacy ones excluded because their rows predate
`node_repaired.reason`): 61 classified failure rows — 20 from an authenticated fact, where this
change can move nothing at all, and 41 from the three readings, of which 26 were out-of-memory
failures recorded as `crash` and one carries no diagnosis anywhere in the record.

The mirror defect has its own file and this one must never buy anything from it:
`tests/test_watchdog_kill_is_not_an_oom.py` is the regression suite for a `diverged` read as an
`oom`, and the first test below is that file's property restated against a MODEL saying the wrong
word instead of a heuristic.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.core.models import FAILURE_REASONS
from looplab.engine import crash_repair as cr
from looplab.engine.metric_salvage import NEVER_SALVAGED_REASONS
from looplab.engine.triage import (AGENT_TRIAGE_ACTIONS, AUTHENTICATED_FAILURE_REASONS,
                                   DEFAULT_TRIAGE_ACTION, JUDGED_FAILURE_REASONS,
                                   REASON_SOURCE_ENGINE, REASON_SOURCE_TRIAGE, UNANSWERABLE_TRIAGE_ACTION,
                                   UNREADABLE_TRIAGE_ACTION, _failure_reason, _rule_triage,
                                   coerce_failure_reason, judged_failure_reason)
from looplab.engine.train_monitor import MONITOR_REPAIR_REASON
from looplab.runtime.sandbox import RunResult

_OOM = {"action": "repair", "failure_kind": "oom", "rationale": "torch.OutOfMemoryError in the log"}


# --------------------------------------------------------------------------------------------
# The override: an authenticated fact is not a model's to contradict
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fact", AUTHENTICATED_FAILURE_REASONS)
def test_no_judge_verdict_can_move_an_authenticated_classification(fact):
    """The one property that keeps this feature from re-creating the v6 node 5 incident from the
    other side. A model handed a diverged training and asked "what failed?" may well answer `oom` —
    the exit code is `-9` with no traceback, which is what an OOM looks like — and on v6 node 5 the
    Developer's own rationale did exactly that, quoting the health-check banner while it halved a
    batch size for the third time. The engine already KNOWS, out of band; so it does not ask."""
    reason, source = judged_failure_reason(fact, _OOM)
    assert reason == fact
    assert source == REASON_SOURCE_ENGINE


def test_the_watchdog_verdicts_specifically_are_on_the_authenticated_side():
    """Named rather than derived, because a well-meaning cleanup that moved either into the judged
    set would be the whole of `tests/test_watchdog_kill_is_not_an_oom.py` undone in one tuple."""
    assert "diverged" in AUTHENTICATED_FAILURE_REASONS
    assert "stalled" in AUTHENTICATED_FAILURE_REASONS
    assert "diverged" not in JUDGED_FAILURE_REASONS and "stalled" not in JUDGED_FAILURE_REASONS

    killed = RunResult(exit_code=-9, stdout="", stderr="", metric=None, timed_out=False,
                       diverged=True)
    assert judged_failure_reason(_failure_reason(killed), _OOM) == ("diverged", REASON_SOURCE_ENGINE)


# --------------------------------------------------------------------------------------------
# The reading: the judge answers, over a closed vocabulary
# --------------------------------------------------------------------------------------------

# VERBATIM from `runs/e5small-dr-unified-v3` node 0's `node_repaired.error_in` (attempt 1) — the
# whole of what the engine captured, and the reason a MARKER LIST is not the answer here. The node
# died of `torch.OutOfMemoryError`, which is in the stage log four times; `accelerate`/`torchrun`
# swallowed the child exception and re-raised this summary, so the captured stderr contains no
# allocator string at all. `_is_torch_oom` (2026-08-20, the other half of the same fix) scans exactly
# this text and correctly finds nothing in it. Nine of the corpus's 26 misclassified rows are this
# shape.
_V3_OPAQUE_TAIL = (
    "eback : To enable traceback see: https://pytorch.org/docs/stable/elastic/errors.html\n"
    "------------------------------------------------------------\n"
    "Root Cause (first observed failure):\n[0]:\n"
    "  time      : 2026-08-19_16:52:04\n  host      : jupyterhub-ml-azemlyak-test\n"
    "  rank      : 1 (local_rank: 1)\n  exitcode  : 1 (pid: 2482274) \n"
    "  error_file: <N/A>\n"
    "  traceback : To enable traceback see: https://pytorch.org/docs/stable/elastic/errors.html\n"
    "============================================================\n")


def test_the_v3_allocator_oom_is_reclassified_and_reaches_the_memory_directive():
    """The incident, end to end over the seam, on the bytes the engine actually captured.

    This is the case a better signature cannot reach, which is why it is the test that matters: the
    OOM is real, it is in the stage log, and it is not in `res.stderr` — the launcher ate it. Both
    deterministic rungs are therefore correct and useless here (`exit 1` with a traceback is not the
    kernel signature; no allocator marker is present), and only a reader that can OPEN THE LOG can
    answer. The triage judge has had exactly that since 2026-08-15 (`repair_log_tools`)."""
    v3 = RunResult(exit_code=1, stdout="", stderr=_V3_OPAQUE_TAIL, metric=None, timed_out=False)
    assert _failure_reason(v3) == "crash", (
        "the deterministic answer, including the allocator markers, on the real captured bytes")

    reason, source = judged_failure_reason(_failure_reason(v3), _OOM)
    assert (reason, source) == ("oom", REASON_SOURCE_TRIAGE)

    # …and that is the whole point of the reclassification: a different directive.
    generic = _Repairer()._repair_error_context("crash", "e")
    memory = _Repairer()._repair_error_context("oom", "e")
    assert generic != memory
    assert "LESS memory" in memory and "LESS memory" not in generic


def test_the_deterministic_marker_still_answers_the_shapes_it_can_see():
    """The control, and the boundary between the two rungs. Where the allocator DID name itself in
    the captured stderr, `_failure_reason` says `oom` on its own — so that row never becomes a
    judged one, the judge's answer about it is not read, and this seam adds nothing. Seventeen of the
    corpus's 26 rows are this shape."""
    named = RunResult(exit_code=1, stdout="",
                      stderr="Traceback (most recent call last):\n"
                             "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 1.12 GiB\n",
                      metric=None, timed_out=False)
    assert _failure_reason(named) == "oom"
    # And a judge that disagreed with the marker could still only pick from the same three: the
    # vocabulary is closed whether or not the deterministic rung got there first.
    assert judged_failure_reason("oom", {"action": "repair", "failure_kind": "diverged"})[0] == "oom"


@pytest.mark.parametrize("kind", JUDGED_FAILURE_REASONS)
def test_every_member_of_the_closed_vocabulary_is_accepted(kind):
    verdict = {"action": "repair", "failure_kind": kind, "rationale": "r"}
    assert judged_failure_reason("crash", verdict)[0] == kind


@pytest.mark.parametrize("invented", ["gpu_melted", "diverged", "timeout", "drift", "setup",
                                      "expect_failed", "not_learning", "", "oom!", "out of memory", None, 7])
def test_a_reason_outside_the_vocabulary_is_refused_not_accepted(invented):
    """`Settings.inline_repair_reasons` SELECTS on this vocabulary, so an invented reason does not
    merely mislabel a row — it silently disables inline repair for that failure, with nothing red
    anywhere. That is the exact drift `tests/test_inline_repair_reason_coverage.py` exists for.

    Note what is in this list besides junk: four real `FAILURE_REASONS` members that are on the
    AUTHENTICATED side. A model may not reach them by writing them into `failure_kind` any more than
    by being asked about them — the vocabulary is closed in both directions."""
    verdict = {"action": "repair", "failure_kind": invented, "rationale": "r"}
    assert judged_failure_reason("crash", verdict) == ("crash", REASON_SOURCE_ENGINE)
    assert coerce_failure_reason(invented, "no_metric") == "no_metric"


def test_the_vocabulary_is_normalized_the_way_the_action_vocabulary_is():
    """Case and surrounding space are the model's, not a verdict. Same rule as
    `coerce_triage_action`, so a judge that shouts is not punished for it."""
    assert coerce_failure_reason("  OOM  ", "crash") == "oom"


# --------------------------------------------------------------------------------------------
# The fallback: every way the judge can fail lands back on today's behaviour
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("verdict", [
    None, "repair", 42, {},
    {"action": "repair", "rationale": "r"},                      # a seam that never emits the field
    {"failure_kind": "oom"},                                     # a kind with no verdict at all
    {"action": UNANSWERABLE_TRIAGE_ACTION, "failure_kind": "oom"},   # the transport failed
    {"action": UNREADABLE_TRIAGE_ACTION, "failure_kind": "oom"},     # nobody could read the verdict
    {"action": "salvage_cause_fix", "failure_kind": "oom"},       # not a verdict at all
])
def test_an_unusable_verdict_keeps_the_engines_own_classification(verdict):
    """BYTE-FOR-BYTE TODAY'S BEHAVIOUR, and the list is the point: there is no path here that ends
    in a classification nobody chose. The two engine verdicts are in it because a call that could
    not produce a STOP decision has not produced a classification either — and because reading a
    `failure_kind` off a transport-failure fallback would be reading a field the engine itself
    wrote."""
    assert judged_failure_reason("crash", verdict) == ("crash", REASON_SOURCE_ENGINE)
    assert judged_failure_reason("no_metric", verdict) == ("no_metric", REASON_SOURCE_ENGINE)


def test_the_no_judge_path_is_untouched():
    """`_rule_triage` is what runs when no triage model is WIRED — a configuration, not a failure.
    It emits no `failure_kind`, so its verdicts fall through the same refusal as any other absent
    one and the deterministic classification stands."""
    for reason in ("crash", "oom", "no_metric"):
        verdict = _rule_triage(reason, "ImportError: x", attempt=1, max_attempts=3)
        assert "failure_kind" not in verdict
        assert judged_failure_reason(reason, verdict) == (reason, REASON_SOURCE_ENGINE)


# --------------------------------------------------------------------------------------------
# The registry invariants — what makes the judge safe for the RECORD
# --------------------------------------------------------------------------------------------

def test_a_judged_reason_can_never_move_a_metric():
    """DOC 36's LINE, as a checkable property rather than an intention. `reason` is not only a
    "what happens next" input: `metric_salvage.NEVER_SALVAGED_REASONS` reads it, and salvage decides
    whether a number the eval produced enters the RECORD and (under `metric_salvage="select"`)
    becomes selectable. Every member of that set is on the AUTHENTICATED side and no member of the
    judged vocabulary is in it, so a judge's answer cannot move a node into the refusal — nor, the
    direction that actually costs something, out of it."""
    assert set(JUDGED_FAILURE_REASONS) & set(NEVER_SALVAGED_REASONS) == set()
    assert set(NEVER_SALVAGED_REASONS) <= set(AUTHENTICATED_FAILURE_REASONS)


def test_the_two_buckets_partition_the_classifier_exactly():
    """Derived from `_failure_reason`'s OWN source, like the coverage test one file over: a reason
    the classifier can produce and neither bucket names is a reason nobody has decided who owns."""
    source = inspect.getsource(_failure_reason)
    returned = {line.split('return', 1)[1].split('#')[0].strip().strip('"\'')
                for line in source.splitlines() if line.strip().startswith('return ')}
    assert returned, "the classifier stopped returning literals; re-derive this test"
    assert returned == set(AUTHENTICATED_FAILURE_REASONS) | set(JUDGED_FAILURE_REASONS)
    assert set(AUTHENTICATED_FAILURE_REASONS) & set(JUDGED_FAILURE_REASONS) == set()


def test_the_judge_adds_no_producer_to_the_registry():
    """The judge RE-READS a reading; it never invents a reason the classifier could not already
    produce. That is what keeps `test_inline_repair_reason_coverage`'s derivation true — the set of
    reasons anything in the engine can emit is unchanged by this whole feature, so every one of them
    is still selectable by `inline_repair_reasons`."""
    assert set(JUDGED_FAILURE_REASONS) <= set(FAILURE_REASONS)
    assert set(AUTHENTICATED_FAILURE_REASONS) <= set(FAILURE_REASONS)
    # The live watchdog's reason is the one producer that is neither: it names a fault MID-RUN, on a
    # stage the engine killed, so no exit code describes it and no judge is asked about it here.
    assert MONITOR_REPAIR_REASON not in JUDGED_FAILURE_REASONS
    assert (set(AUTHENTICATED_FAILURE_REASONS) | set(JUDGED_FAILURE_REASONS)
            | {MONITOR_REPAIR_REASON}) == set(FAILURE_REASONS)


def test_the_emit_schema_reads_the_vocabulary_from_the_registry():
    """The same rule `action`'s enum follows and for the same reason: a re-spelled literal in the
    agent would be a typo the engine's `inline_repair_reasons` gate keys on. Source-pinned, because
    the schema is built inside a method that needs a live client to reach."""
    from looplab.agents import unified_agent

    src = inspect.getsource(unified_agent.UnifiedAgent.triage_crash)
    assert '"enum": list(JUDGED_FAILURE_REASONS)' in src
    assert '"enum": ["crash"' not in src and "'crash', 'oom'" not in src


# --------------------------------------------------------------------------------------------
# Through the real seam
# --------------------------------------------------------------------------------------------

class _EngineStub(cr.CrashRepairMixin):
    """The mixin with only what `_triage_crash` reads."""

    def __init__(self, researcher):
        self.researcher = researcher
        self._inline_repair_attempts = 3

        class _T:
            def span(self, *a, **k):
                class _S:
                    def __enter__(self_inner): return self_inner
                    def __exit__(self_inner, *a): return False
                    def set(self_inner, *a): pass
                return _S()
        self.tracer = _T()


class _Repairer(cr.CrashRepairMixin):
    """The mixin under its own directive rule (same shape as the watchdog file's)."""

    def __init__(self):
        self._deep_repair = False
        self._repo_spec = None
        self._eval_parallel = 1
        self._gpu_ids = None


def test_the_kind_survives_the_engines_intake_and_junk_beside_it_does_not():
    """Driven through `_triage_crash`, because the seam is duck-typed and the field has to travel
    the whole way. The overreaching keys are the trust line: a judge that has just read the log may
    say which of THREE kinds it was and may not say what the metric is."""
    from looplab.core.models import RunState

    class _Judge:
        def triage_crash(self, node, error, attempt, *, state=None, brief="", **kw):
            return {"action": "repair", "failure_kind": "OOM", "rationale": "allocator raised",
                    "reason": "ok", "metric": 0.99, "selectable": True}

    out = _EngineStub(_Judge())._triage_crash(RunState(), object(), "boom", 1, reason="crash")
    assert set(out) == {"action", "failure_kind", "rationale", "missing_dependency"}
    assert judged_failure_reason("crash", out) == ("oom", REASON_SOURCE_TRIAGE)


def test_an_older_duck_typed_seam_still_classifies_exactly_as_before():
    """A `triage_crash` implementation written before this field existed — every test double in the
    tree, and any researcher an operator wired themselves — must not be read as saying anything."""
    from looplab.core.models import RunState

    class _Old:
        def triage_crash(self, node, error, attempt, *, state=None, brief="", **kw):
            return {"action": "repair", "rationale": "fix it"}

    out = _EngineStub(_Old())._triage_crash(RunState(), object(), "boom", 1, reason="crash")
    assert out["failure_kind"] == ""
    assert judged_failure_reason("crash", out) == ("crash", REASON_SOURCE_ENGINE)


def test_the_engine_loop_stamps_who_chose_the_reason():
    """The RECORD half of doc 36. `reason` lands on `node_repaired` and on the `node_failed`
    terminal, so once a model may choose it the row has to say so — and it has to keep the
    engine's own answer beside it, or the authenticated classification is destroyed by the very
    change that made it optional. Source-pinned at the two append sites."""
    from looplab.engine import evaluate as ev

    src = inspect.getsource(ev.EvaluateMixin._evaluate)
    assert '"reason_source": _reason_source' in src
    assert '"engine_reason": _engine_reason' in src
    assert "reason, _reason_source = judged_failure_reason(reason, triage)" in src
