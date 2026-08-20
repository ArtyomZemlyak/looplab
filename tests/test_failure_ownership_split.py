"""WHO MAY SAY WHAT A FAILED EVAL FAILED OF — the ownership split, as a checkable rule.

This file replaces `test_triage_llm_failure_classifier.py` (2026-08-20, one day old), which guarded
the narrower half-measure: a judge allowed to RE-READ the three kinds `_failure_reason` inferred
from the dead process's text, with the text rules themselves left in place. Every property of that
file that survives is here, re-pointed; what changed is the rule underneath it, so the file is
renamed rather than edited — a test whose name still describes the old contract is how a suite comes
to speak a language production does not.

THE DEFECT, restated precisely, because "regex is bad" is not it. `_failure_reason` was the one
place in this tree where **text got the LAST WORD**: nothing downstream re-checks a `reason`, yet
the reason chooses the repair directive, gates the triage-driven install, gates metric salvage and
lands on the durable terminal a whole run is audited from. Everywhere else that text touches a
decision, this codebase had already converged on the opposite discipline and written it down —
`runtime/deps.py::triage_install_candidates` lets a traceback and a model's prose NOMINATE a
distribution ("Free rationale text alone can NEVER mint a candidate") and `is_present` DECIDES by
spawning the eval interpreter and asking `find_spec`. `crash_repair._prepare_env` records what the
version without that probe cost: a regex reduced `pytorch_lightning.utilities.cloud_io` to
`pytorch_lightning`, pip said "Requirement already satisfied" rc=0, and the engine wrote a
`deps_installed` receipt claiming the environment had just been fixed.

So the question this file asks of every reason is **"does an out-of-band channel exist?"** — not
"is the text trustworthy" and not "how confident is the classifier". `engine/failure_diagnosis.py`
holds the per-reason table and the measurements; what is HERE is the rule as assertions.

THE THREE PROPERTIES THAT MATTER, and each is one wrong line away from a real incident:

  1. THE ASYMMETRY. When the engine's own answer is ENGINE-FINAL, the diagnostician is not
     consulted at all. This is `test_watchdog_kill_is_not_an_oom.py`'s property restated against a
     MODEL saying the wrong word rather than a heuristic: a model handed a diverged training and
     asked "what failed?" may well answer `oom` — exit -9, no traceback, which is what an OOM looks
     like — and on v6 node 5 the Developer's own rationale did exactly that while it halved a batch
     size for the third time.
  2. NO DIAGNOSED REASON MAY MOVE A METRIC. Doc 36's line as a property: salvage is contained by
     ORDERING (`_salvage_eval_metric` runs on the deterministic answer, branches earlier) AND by
     vocabulary disjointness from `NEVER_SALVAGED_REASONS`. Both are asserted, because one of them
     is a fact about code order that a refactor can move.
  3. `unclassified` IS NOT A REGEX. When a WIRED diagnostician cannot answer, the row must say so
     rather than silently keeping a residual that reads identically to agreement.

WHY THE OVERLAP IS ONE MEMBER AND WHY IT IS NOT DISJOINTNESS. The brief that commissioned this
asked for "no engine-caused reason is agent-decidable, and vice versa". The first half is the
asymmetry above and is enforced. The second half is asked for exactly one member, `not_learning`,
and is REFUSED with its argument in `DIAGNOSED_ENGINE_FINAL_OVERLAP` — because disjointness
conflates a CAUSE vocabulary with a PRODUCER partition, and because without it this change cannot
fix its own motivating measurement (16 `runs/rubertlite-dense-retrieval` terminals whose real cause
is "the loss never moved" and whose label is a stage checker's `check_failed`). The exception is
registered, bounded to one name, and this file asserts it is EXACTLY that one.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from looplab.core.models import FAILURE_REASONS
from looplab.engine import crash_repair as cr
from looplab.engine.failure_diagnosis import (DIAGNOSABLE_ENGINE_REASONS,
                                              DIAGNOSED_ENGINE_FINAL_OVERLAP,
                                              DIAGNOSED_FAILURE_REASONS,
                                              DIAGNOSED_ONLY_REASONS,
                                              DIAGNOSIS_CODE_LOOK_TURNS,
                                              DIAGNOSIS_UNAVAILABLE_KEY, ENGINE_FINAL_REASONS,
                                              EVIDENCE_SOURCE_CODE, EVIDENCE_SOURCE_LOG,
                                              EVIDENCE_SOURCE_NONE, REASON_SOURCE_ENGINE,
                                              REASON_SOURCE_TRIAGE, REASON_SOURCE_UNDIAGNOSED,
                                              REASON_SOURCES, UNCLASSIFIED_REASON, coerce_evidence,
                                              coerce_failure_kind, diagnosed_failure_reason,
                                              evidence_citation_resolves)
from looplab.engine.metric_salvage import NEVER_SALVAGED_REASONS
from looplab.engine.triage import (UNANSWERABLE_TRIAGE_ACTION, UNREADABLE_TRIAGE_ACTION,
                                   _failure_reason, _rule_triage)
from looplab.engine.train_monitor import MONITOR_REPAIR_REASON
from looplab.runtime.sandbox import RunResult

_OOM = {"action": "repair", "failure_kind": "oom", "rationale": "torch.OutOfMemoryError in the log"}


def _dedented(fn) -> str:
    """A method's source, dedented so `ast.parse` accepts it."""
    return textwrap.dedent(inspect.getsource(fn))


def _payload_stamps(src: str) -> dict[str, set[str]]:
    """Every `"<key>": <expr>` the source really BUILDS, as `{key: {unparsed expr, …}}`.

    Two node shapes, because the engine writes its payloads both ways: `ast.Dict` pairs (the
    `node_repaired` literal, including the ones inside `**({...} if … else {})`) and
    `<target>[<key>] = <expr>` assignments (the `node_failed` path, which adds its evidence keys
    conditionally after the dict is built).

    A COMMENT PRODUCES NEITHER NODE, which is the whole point — `test_a_commented_out_stamp_is_seen`
    drives exactly that."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    out.setdefault(k.value, set()).add(ast.unparse(v))
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant)
                        and isinstance(tgt.slice.value, str)):
                    out.setdefault(tgt.slice.value, set()).add(ast.unparse(node.value))
    return out


def _tuple_assign_targets(src: str, func: str) -> list[tuple[str, ...]]:
    """The target tuples of every `a, b = <func>(...)` in the source.

    Asserted instead of the RENDERED line, whose spelling changes under any reformat — a guard that
    reddens on a line wrap is a guard people learn to edit rather than read."""
    found = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        called = node.value.func
        name = called.id if isinstance(called, ast.Name) else getattr(called, "attr", "")
        if name != func:
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Tuple):
                found.append(tuple(el.id for el in tgt.elts if isinstance(el, ast.Name)))
    return found


def _enum_sources(src: str) -> set[str]:
    """Every `"enum": <expr>` value in a JSON-schema dict, unparsed."""
    out = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "enum":
                    out.add(ast.unparse(v))
    return out


def _drop_stamp(src: str, key: str, value: str, *, expect: int) -> str:
    """Remove every `"<key>": <value>` PAIR from the code, leaving its text in a trailing comment.

    THE MUTATION THE AST REWRITE EXISTS TO SURVIVE, in the shape it really takes: the stamp stops
    being built and a comment quoting the line it replaced remains. That is CLAUDE.md's cheapest
    mutation, and it is exactly what a substring pin cannot see.

    IT REMOVES THE PAIR RATHER THAN COMMENTING THE LINE, because the engine writes stamps in two
    shapes and only one of them survives a line comment: the `node_failed` terminal packs
    `"reason_source": _reason_source, "engine_reason": _engine_reason}` with the closing brace on
    the same line, so commenting it makes the source unparseable and the driver would then fail for
    a reason that has nothing to do with the property. `expect` pins how many writers there are,
    checked rather than assumed, so a needle that silently matches a subset cannot pass — which is
    the mistake this helper was written twice to avoid."""
    pair = f'"{key}": {value}'
    lines = src.splitlines()
    hits = [i for i, ln in enumerate(lines) if pair in ln and not ln.lstrip().startswith("#")]
    assert len(hits) == expect, (
        f"{pair!r} matched {len(hits)} live lines, expected {expect} — re-derive the mutation "
        f"rather than loosening it; a partial mutation leaves this driver proving nothing")
    for i in hits:
        line = lines[i]
        indent = " " * (len(line) - len(line.lstrip()))
        if line.strip() in (pair + ",", pair):
            lines[i] = f"{indent}# {line.strip()}"
            continue
        for variant in (", " + pair, pair + ", ", pair + ",", pair):
            if variant in line:
                lines[i] = line.replace(variant, "", 1) + f"  # {pair}"
                break
        else:                                    # pragma: no cover - refuse rather than guess
            raise AssertionError(f"cannot remove {pair!r} from {line!r} without guessing")
    return "\n".join(lines)


def _comment_out(src: str, needle: str, *, expect: int = None) -> str:
    """Comment out EVERY live line containing `needle`, keeping its text in the comment.

    THE MUTATION THESE GUARDS EXIST TO SURVIVE, and the one CLAUDE.md names as the cheapest: delete
    the code, leave a comment holding the pinned literal.

    EVERY line and not the first, because a stamp written on more than one row is only really gone
    when all of them are — commenting one of two would leave the AST check correctly green and the
    driver would then be proving nothing. `expect` pins the count where the number is itself part of
    the property (the ownership rule is applied exactly once); it is checked rather than assumed,
    so this helper can never silently mutate nothing and pass."""
    lines = src.splitlines()
    hits = [i for i, ln in enumerate(lines)
            if needle in ln and not ln.lstrip().startswith("#")]
    assert hits, f"{needle!r} matched no live line; the mutation would be a no-op"
    if expect is not None:
        assert len(hits) == expect, f"{needle!r} matched {len(hits)} live lines, expected {expect}"
    for i in hits:
        indent = len(lines[i]) - len(lines[i].lstrip())
        lines[i] = " " * indent + "# " + lines[i].lstrip()
    return "\n".join(lines)


def _classifier_vocabulary() -> set[str]:
    """Every string literal `_failure_reason` can RETURN, resolved from real `ast.Return` nodes.

    AST and not line-splitting, which is what the file this replaces did (`line.split('return', 1)`
    over the source text). That reading is satisfied by a COMMENT carrying the word `return` and a
    literal, which is precisely the mutation CLAUDE.md's guard-test ladder names as the cheapest one
    — delete the code, leave a comment. Comments are not AST nodes.
    """
    tree = ast.parse(inspect.getsource(_failure_reason).lstrip())
    out = {node.value.value for node in ast.walk(tree)
           if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
           and isinstance(node.value.value, str)}
    assert out, "the classifier stopped returning string literals; re-derive this test"
    return out


# --------------------------------------------------------------------------------------------
# THE RULE ITSELF: the split partitions the classifier, and the one exception is registered
# --------------------------------------------------------------------------------------------

def test_the_split_partitions_everything_the_engine_can_produce():
    """A reason an ENGINE producer can emit that neither side names is a reason nobody has decided
    who OWNS — and that is not a cosmetic gap: `diagnosed_failure_reason`'s first branch returns any
    non-`DIAGNOSABLE` value unchanged, so an unregistered reason silently becomes engine-final and
    the diagnostician is never asked about a failure class nobody chose to keep.

    TWO ENGINE PRODUCERS, not one, and naming the second is what makes this derivable at all:
    `_failure_reason` reads a finished process, and the live training watchdog names
    `MONITOR_REPAIR_REASON` on a stage it killed MID-RUN. Nothing about that reason can come out of
    an exit code, so demanding it from the classifier would mean inventing a signal for a fact the
    engine already holds out of band."""
    engine_produced = _classifier_vocabulary() | {MONITOR_REPAIR_REASON}
    assert engine_produced == set(ENGINE_FINAL_REASONS) | set(DIAGNOSABLE_ENGINE_REASONS)
    assert set(ENGINE_FINAL_REASONS) & set(DIAGNOSABLE_ENGINE_REASONS) == set(), (
        "a reason the engine hands on as evidence cannot also be one it keeps as an answer")


def test_the_engine_can_no_longer_say_oom_at_all():
    """THE SHARPEST STATEMENT OF WHAT THIS CHANGE DID, and the one a reader is most likely to miss.

    Both of `_failure_reason`'s `oom` producers were TEXT RULES — the `-9/137 + no-Traceback` kernel
    signature and `_is_torch_oom`'s allocator marker list — so deleting them left the engine with no
    way to name an out-of-memory failure at all. `oom` is therefore not a deterministic answer under
    review; it is ANSWER-ONLY, a kind only the diagnostician can ever produce.

    That is exactly right for the one failure class where no out-of-band channel can exist: the
    engine did not cause the exit, the candidate's own allocator raised and died, nothing observed
    it, and device-level free memory is sampled after the process is gone and the allocation
    released. It is also the load-bearing consequence — a corpus row that used to read `oom` from a
    string match now reads `crash` unless something actually looked."""
    assert "oom" not in _classifier_vocabulary()
    assert "oom" not in DIAGNOSABLE_ENGINE_REASONS
    assert "oom" in DIAGNOSED_FAILURE_REASONS
    assert set(DIAGNOSED_ONLY_REASONS) == set(DIAGNOSED_FAILURE_REASONS) - set(
        DIAGNOSABLE_ENGINE_REASONS) == {"oom", "not_learning"}


def test_the_diagnosed_vocabulary_overlaps_engine_final_in_exactly_one_registered_place():
    """THE DEVIATION FROM THE BRIEF, pinned so it can only ever be argued and never drift.

    `not_learning` is both: the engine produces it when ITS OWN training watchdog killed a stage
    (`MONITOR_REPAIR_REASON`), and the diagnostician may ANSWER it about a failure nothing killed —
    a loss frozen in a log that a stage checker labelled `check_failed`. Those are one cause with
    two witnesses, and the record tells them apart through `reason_source`, not through the word.

    Any second member has to argue the three bullets in `DIAGNOSED_FAILURE_REASONS`' comment, so
    this asserts the set EXACTLY rather than asserting "the overlap is small"."""
    overlap = set(DIAGNOSED_FAILURE_REASONS) & set(ENGINE_FINAL_REASONS)
    assert overlap == set(DIAGNOSED_ENGINE_FINAL_OVERLAP) == {"not_learning"}
    assert MONITOR_REPAIR_REASON == "not_learning", (
        "the overlap is registered by VALUE; if the watchdog's reason is renamed this argument is "
        "about a different thing")


def test_the_registered_overlap_buys_nothing_privileged():
    """The three bullets that make the exception safe, as assertions rather than prose. A wrong
    `not_learning` must cost exactly what any other wrong kind costs — one repair round pointed at
    the objective instead of at the real bug."""
    # (a) it cannot suppress a metric the eval produced, nor admit one the trust gate refused.
    assert "not_learning" not in NEVER_SALVAGED_REASONS
    # (b) it buys no extra attempt: the rule path bounds it exactly as it bounds a timeout/oom.
    bounded = _rule_triage("not_learning", "", attempt=99, max_attempts=3)
    assert bounded["action"] == "abandon"
    # (c) it is selectable, so an operator who narrowed `inline_repair_reasons` still governs it.
    assert "not_learning" in FAILURE_REASONS


# --------------------------------------------------------------------------------------------
# THE ASYMMETRY: an engine-final fact is not a model's to contradict
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fact", ENGINE_FINAL_REASONS)
def test_no_diagnosis_can_move_an_engine_final_classification(fact):
    """The one property that keeps this feature from re-creating the v6 node 5 incident from the
    other side. The engine already KNOWS, out of band; so it does not ask. Note this holds for
    `not_learning` too — the registered overlap is about what the diagnostician may ANSWER, never
    about what it may OVERRIDE."""
    reason, source = diagnosed_failure_reason(fact, _OOM)
    assert reason == fact
    assert source == REASON_SOURCE_ENGINE


def test_the_three_watchdog_verdicts_specifically_are_engine_final():
    """Named rather than derived, because a well-meaning cleanup that moved one of these into the
    diagnosable set would be the whole of `tests/test_watchdog_kill_is_not_an_oom.py` undone in one
    tuple."""
    for kill in ("diverged", "stalled", "not_learning"):
        assert kill in ENGINE_FINAL_REASONS
        assert kill not in DIAGNOSABLE_ENGINE_REASONS

    killed = RunResult(exit_code=-9, stdout="", stderr="", metric=None, timed_out=False,
                       diverged=True)
    assert diagnosed_failure_reason(_failure_reason(killed), _OOM) == ("diverged",
                                                                      REASON_SOURCE_ENGINE)


def test_a_diagnostician_may_not_claim_the_engines_clock_fired():
    """`timeout` is the near-miss that proves the line is real rather than "whatever the model might
    usefully say". `rubertlite-dr-unified-v8` node 9 genuinely could not fit its budget — its own
    repair rationale says so — and the engine's clock did NOT fire; the stage checker caught it
    first. Answering `timeout` would be a model asserting an engine mechanism it cannot observe, and
    `timeout` is in `NEVER_SALVAGED_REASONS`, so admitting it would hand a model the power to
    suppress a metric."""
    assert "timeout" not in DIAGNOSED_FAILURE_REASONS
    verdict = {"action": "repair", "failure_kind": "timeout", "rationale": "ran out of budget"}
    reason, source = diagnosed_failure_reason("check_failed", verdict)
    assert reason == UNCLASSIFIED_REASON and source == REASON_SOURCE_UNDIAGNOSED, (
        "an out-of-vocabulary kind is a diagnostician that did not answer, not a licence")


# --------------------------------------------------------------------------------------------
# THE HOLE THIS CLOSED: a contract failure is evidence, not an answer
# --------------------------------------------------------------------------------------------

def test_check_failed_is_diagnosable_and_the_two_filesystem_contracts_are_not():
    """THE MEASURED ASYMMETRY, and the reason only ONE of the three stage-contract statuses moved.

    `check_failed` is written from `_call_stage_check`, i.e. from ANOTHER MODEL's reading of the
    candidate's own stdout — one model's prose is no more an authenticated fact than the candidate's
    stderr is. Measured: 21 such rows in `runs/`, of which 16 (`rubertlite-dense-retrieval`) are
    "Loss stagnant at 13.3 throughout epoch 19" and its siblings — every one of them
    `not_learning` — one is a run that could not fit its budget, and one held `RECALL@100: 0.697972`
    and was failed anyway.

    `needs_failed`/`expect_failed` come from the engine's own `stat` of a declared input/output.
    Measured: all 8 `expect_failed` rows in the corpus are the same real cause (the stage wrote its
    artifact where the manifest does not declare it) and the repair rationale AGREES with the label
    in 8 of 8. Zero mislabels, because a stat is not a reading. Moving them would widen the trusted
    set for no measured gain."""
    assert "check_failed" in DIAGNOSABLE_ENGINE_REASONS
    assert "needs_failed" in ENGINE_FINAL_REASONS and "expect_failed" in ENGINE_FINAL_REASONS

    contract = RunResult(exit_code=0, stdout="", stderr="stage 'train' failed verification: "
                         "Loss stagnant at 13.3 throughout epoch 19, indicating no learning "
                         "progress.", metric=None, timed_out=False,
                         stages=[{"name": "train", "status": "check_failed"}])
    assert _failure_reason(contract) == "check_failed"
    verdict = {"action": "repair", "failure_kind": "not_learning",
               "rationale": "the loss never left its initialization value"}
    assert diagnosed_failure_reason(_failure_reason(contract), verdict) == ("not_learning",
                                                                           REASON_SOURCE_TRIAGE)


@pytest.mark.parametrize("kind", DIAGNOSED_FAILURE_REASONS)
def test_every_member_of_the_closed_vocabulary_is_accepted(kind):
    verdict = {"action": "repair", "failure_kind": kind, "rationale": "r"}
    assert diagnosed_failure_reason("crash", verdict) == (kind, REASON_SOURCE_TRIAGE)


def test_the_source_says_triage_even_when_the_diagnostician_agrees():
    """A 2026-08-20 change from `judged_failure_reason`, which stamped `engine` on agreement. Under
    ownership-by-decision the diagnostician DID decide, and `engine_reason` on the same row is what
    lets a reader recover whether the two agreed — attributing agreement to the engine made a
    CONFIRMED diagnosis uncountable, which is the same blindness `unclassified` exists to end one
    branch over."""
    verdict = {"action": "repair", "failure_kind": "crash", "rationale": "a real bug"}
    assert diagnosed_failure_reason("crash", verdict) == ("crash", REASON_SOURCE_TRIAGE)


def test_the_vocabulary_is_normalized_the_way_the_action_vocabulary_is():
    """Case and surrounding space are the model's, not a verdict. Same rule as
    `coerce_triage_action`, so a judge that shouts is not punished for it."""
    assert coerce_failure_kind("  OOM  ", "crash") == "oom"
    assert coerce_failure_kind("gpu_melted", "crash") == "crash"


# --------------------------------------------------------------------------------------------
# THE FALLBACK IS NOT A REGEX: `unclassified`, and its four properties
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("verdict", [
    {"action": "repair", "rationale": "r"},                        # a seam that never emits a kind
    {"action": "repair", "failure_kind": "gpu_melted"},            # out of vocabulary
    {"action": "repair", "failure_kind": ""},                      # emitted empty
    {"action": UNANSWERABLE_TRIAGE_ACTION, "failure_kind": "oom"},  # the transport failed
    {"action": UNREADABLE_TRIAGE_ACTION, "failure_kind": "oom"},    # nobody could read the verdict
    {"action": "salvage_cause_fix", "failure_kind": "oom"},        # a marker, not a verdict
])
def test_a_wired_diagnostician_that_cannot_answer_records_that_it_could_not(verdict):
    """THE CHANGE FROM THE HALF-MEASURE, and the point of the whole third requirement. These used to
    fall back to the engine's residual, which made a diagnostician that FAILED write a row byte-
    identical to one that AGREED. `unclassified` + `undiagnosed` is what makes the unavailability
    countable in the record instead of papered over.

    The two engine verdicts are in this list because a call that could not produce a STOP decision
    has not produced a classification either — and because reading a `failure_kind` off a
    transport-failure fallback would be reading a field the engine itself wrote."""
    assert diagnosed_failure_reason("crash", verdict) == (UNCLASSIFIED_REASON,
                                                          REASON_SOURCE_UNDIAGNOSED)
    assert diagnosed_failure_reason("check_failed", verdict) == (UNCLASSIFIED_REASON,
                                                                 REASON_SOURCE_UNDIAGNOSED)


def test_unclassified_has_the_four_properties_that_make_it_safe_to_route():
    """Each is a separate way this value could have been a regression, and the third is the one a
    reviewer is most likely to get wrong in the generous direction."""
    # 1. it routes to a repair rather than throwing the node away, and to the BLIND bound.
    assert UNCLASSIFIED_REASON in FAILURE_REASONS
    from looplab.core.config import Settings
    assert UNCLASSIFIED_REASON in Settings().inline_repair_reasons
    repair = _rule_triage(UNCLASSIFIED_REASON, "", attempt=1, max_attempts=50)
    assert repair["action"] == "repair"
    # 2. it can never suppress a metric the eval really produced.
    assert UNCLASSIFIED_REASON not in NEVER_SALVAGED_REASONS
    # 3. it buys NO extra attempt — same blind bound as a `crash`, never the caller's full cap.
    from looplab.engine.triage import _RULE_BLIND_CRASH_ATTEMPTS
    over = _rule_triage(UNCLASSIFIED_REASON, "", attempt=_RULE_BLIND_CRASH_ATTEMPTS + 1,
                        max_attempts=50)
    assert over["action"] == "abandon"
    assert _rule_triage("crash", "", attempt=_RULE_BLIND_CRASH_ATTEMPTS + 1,
                        max_attempts=50)["action"] == "abandon"
    # 4. it is countable: its own reason_source, distinct from both the engine's and the judge's.
    assert REASON_SOURCE_UNDIAGNOSED not in (REASON_SOURCE_ENGINE, REASON_SOURCE_TRIAGE)
    assert set(REASON_SOURCES) == {REASON_SOURCE_ENGINE, REASON_SOURCE_TRIAGE,
                                   REASON_SOURCE_UNDIAGNOSED}


def test_no_diagnostician_wired_is_a_configuration_and_not_a_failure():
    """THE BRANCH THAT KEEPS EVERY OFFLINE RUN BYTE-IDENTICAL, and the reason the marker exists at
    all. `_rule_triage`'s verdict is shaped exactly like a model's — an `action` in
    `AGENT_TRIAGE_ACTIONS` with no `failure_kind` — so without `DIAGNOSIS_UNAVAILABLE_KEY` the rule
    above would mint `unclassified` on every toy, offline and `unified_agent=false` run.

    This is the same distinction `triage.py`'s verdict contract draws between `unanswerable` (the
    judge was there and the transport died) and this path (no judge was ever there)."""
    for reason in DIAGNOSABLE_ENGINE_REASONS:
        verdict = _rule_triage(reason, "ImportError: x", attempt=1, max_attempts=3)
        assert verdict.get(DIAGNOSIS_UNAVAILABLE_KEY) is True, (
            f"the no-judge path must mark itself for {reason!r} or it reads as a failed diagnosis")
        assert "failure_kind" not in verdict
        assert diagnosed_failure_reason(reason, verdict) == (reason, REASON_SOURCE_ENGINE)


def test_the_unavailable_marker_is_unforgeable_from_the_wire():
    """A model that emitted `diagnosis_unavailable: true` would be asserting that it does not exist.
    It cannot: `_ask_triage` REBUILDS an agent verdict from a fixed key list, so the marker is
    simply not carried — the same construction that makes `TRIAGE_TRANSPORT_FAILURE_KEY` safe."""
    from looplab.core.models import RunState

    class _Liar:
        def triage_crash(self, node, error, attempt, *, state=None, brief="", **kw):
            return {"action": "repair", "rationale": "r", DIAGNOSIS_UNAVAILABLE_KEY: True,
                    "failure_kind": "oom"}

    out = _EngineStub(_Liar())._triage_crash(RunState(), object(), "boom", 1, reason="crash")
    assert DIAGNOSIS_UNAVAILABLE_KEY not in out
    assert diagnosed_failure_reason("crash", out) == ("oom", REASON_SOURCE_TRIAGE)


@pytest.mark.parametrize("verdict", [None, "repair", 42, 0.5, object()])
def test_a_caller_with_no_verdict_object_at_all_keeps_the_engines_answer(verdict):
    """THE BOUNDARY BETWEEN "nobody was asked" AND "asked and could not answer", and it is drawn at
    `isinstance(verdict, dict)` on purpose.

    A NON-dict means the caller never reached a verdict object — no seam was invoked, nothing
    produced anything — which is the no-judge shape and must keep the engine's own answer, because
    otherwise a `None` from a caller that simply has no judge would mint `unclassified`. A DICT
    means a call happened and returned something, so a dict with no readable kind is a
    diagnostician that failed (covered by the test above) even when the dict is empty."""
    assert diagnosed_failure_reason("crash", verdict) == ("crash", REASON_SOURCE_ENGINE)


@pytest.mark.parametrize("verdict", [{}, {"failure_kind": "oom"}, {"rationale": "r"}])
def test_a_readable_object_with_no_verdict_in_it_is_a_failed_diagnosis(verdict):
    """The other side of the same boundary. Something answered and what came back is not a verdict,
    which is precisely the condition `unclassified` names."""
    assert diagnosed_failure_reason("crash", verdict) == (UNCLASSIFIED_REASON,
                                                          REASON_SOURCE_UNDIAGNOSED)


def test_the_rule_is_total_over_junk():
    """It runs on the eval loop's FAILURE path, where a raise costs the terminal being written."""
    for junk in (object(), [], 0.5, {"action": object()}, {"failure_kind": object()},
                 {"action": "repair", "failure_kind": ["oom"]}):
        reason, source = diagnosed_failure_reason("crash", junk)
        assert reason in set(FAILURE_REASONS)
        assert source in REASON_SOURCES


# --------------------------------------------------------------------------------------------
# THE TEXT RULES ARE GONE — negative pins, which stay substrings on purpose
# --------------------------------------------------------------------------------------------

def test_no_text_rule_survives_in_the_classifier():
    """NEGATIVE pins, and CLAUDE.md's guard-test ladder says these stay substrings deliberately:
    what must not come back is the TEXT, and a commented-out copy of a deleted rule is as much of a
    drift risk as a live one. Each literal below is the deleted rule's own spelling.

    Note what is NOT asserted: the classifier's source may still MENTION these in comments — the
    obituaries are load-bearing, they are what stops someone reinstating the marker list from the
    corpus win it really did produce. So the pins are on `_failure_reason`'s own body, resolved by
    AST, where a comment is not a node."""
    body = ast.parse(inspect.getsource(_failure_reason).lstrip())
    # STRIP THE DOCSTRING FIRST. It is a `Constant` like any other and it NAMES the deleted rules on
    # purpose — the obituaries are load-bearing, they are what stops someone reinstating the marker
    # list from the corpus win it really did produce. Without this the guard convicts its own
    # documentation, which is the mirror image of the satisfiable-by-a-comment failure it exists to
    # avoid.
    fn = body.body[0]
    assert isinstance(fn, ast.FunctionDef)
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    live = ast.dump(body)
    for gone in ("setup failed:", "Traceback", "OutOfMemoryError", "out of memory", "startswith"):
        assert gone not in live, f"{gone!r} is back in the classifier's executable body"
    # …and the two constants that held them are gone from the module entirely.
    src = Path(inspect.getfile(_failure_reason)).read_text()
    for name in ("def _is_torch_oom", "_TORCH_OOM_MARKERS: tuple", "_MECHANICAL_MARKERS = ("):
        assert name not in src, f"{name!r} was deleted on 2026-08-20 and must not return"


def test_setup_is_engine_final_through_a_flag_and_not_through_a_prefix():
    """THE ANTI-PATTERN IN ITS PUREST FORM, fixed in the direction that does not spend safety.

    `run_command_eval` KNEW: it had just read `rc` from the setup step it launched. It then threw
    that knowledge into `stderr` as twelve characters and the classifier read it back. And it was
    not free — `setup` is in `NEVER_SALVAGED_REASONS`, so a candidate whose training script happened
    to begin its stderr with that literal had a metric it really produced SUPPRESSED.

    The fix is NOT to move `setup` to a model. It is to stop discarding the fact. So this drives
    both directions: the flag decides, and the forgeable prefix no longer does."""
    real = RunResult(exit_code=1, stdout="", stderr="setup failed:\nno module named torch",
                     metric=None, timed_out=False, setup_failed=True)
    assert _failure_reason(real) == "setup"

    forged = RunResult(exit_code=1, stdout="", stderr="setup failed:\n(the candidate wrote this)",
                       metric=None, timed_out=False, setup_failed=False)
    assert _failure_reason(forged) == "crash", (
        "the candidate's own stderr must not be able to claim the engine's setup step failed")

    assert "setup" in ENGINE_FINAL_REASONS and "setup" in NEVER_SALVAGED_REASONS


def test_an_allocator_that_raises_is_now_a_question_and_not_an_answer():
    """The v3 incident, and the honest statement of what changed. All three nodes of
    `runs/e5small-dr-unified-v3` died of `torch.OutOfMemoryError`, which RAISES — full traceback,
    exit 1 — so every conjunct of the old kernel signature was false and all eight `node_repaired`
    rows read `crash`.

    The marker list that briefly replaced that signature resolved all 26 such rows in the corpus,
    and that win was real. It is still deleted, because it was text with the LAST WORD: a host
    `MemoryError`, an OOM re-raised inside another library's exception, and the torchrun `Root
    Cause ... exitcode: 1` block that 9 of those same 26 rows ARE, are all invisible to it, and each
    one is another literal and another incident. `crash` is now the honest structural residual and
    the diagnostician answers what it died of."""
    raised = RunResult(exit_code=1, stdout="",
                       stderr="Traceback (most recent call last):\n"
                              "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 1.12 GiB\n",
                       metric=None, timed_out=False)
    assert _failure_reason(raised) == "crash"
    assert "crash" in DIAGNOSABLE_ENGINE_REASONS

    reason, source = diagnosed_failure_reason(_failure_reason(raised), _OOM)
    assert (reason, source) == ("oom", REASON_SOURCE_TRIAGE)

    # …and that is the whole point of the reclassification: a different directive reaches the
    # Developer.
    generic = _Repairer()._repair_error_context("crash", "e")
    memory = _Repairer()._repair_error_context("oom", "e")
    assert generic != memory
    assert "LESS memory" in memory and "LESS memory" not in generic


def test_the_kernel_oom_kill_is_also_a_question_now():
    """The other half of the deleted signature, driven so the change is not only asserted about
    source text. A cgroup SIGKILL is exit -9 with no traceback — which is ALSO what both watchdog
    kills look like, which is why reading it was a conflation and not merely a guess."""
    killed = RunResult(exit_code=-9, stdout="", stderr="", metric=None, timed_out=False)
    assert _failure_reason(killed) == "crash"


# --------------------------------------------------------------------------------------------
# THE EVIDENCE: the diagnostician's `is_present`, as far as that analogy really goes
# --------------------------------------------------------------------------------------------

def test_a_citation_into_the_workdir_resolves_and_one_that_is_not_there_does_not(tmp_path):
    """THE CHECK THAT MAKES A WRONG ANSWER AUDITABLE. It does not verify the CONCLUSION — no
    out-of-band probe of a failure KIND exists, and every candidate is either the text rule just
    deleted or a fact already known to be false of the v3 case — it verifies that the thing the
    verdict says it read is a thing that is there."""
    (tmp_path / "train.py").write_text("loss = 0.0\n")
    ok = coerce_evidence({"evidence_source": "code", "evidence_locator": "train.py:1",
                          "evidence_quote": "loss = 0.0"})
    assert ok == {"source": EVIDENCE_SOURCE_CODE, "locator": "train.py:1", "quote": "loss = 0.0"}
    assert evidence_citation_resolves(ok, tmp_path) is True

    missing = coerce_evidence({"evidence_source": "code", "evidence_locator": "nope.py"})
    assert evidence_citation_resolves(missing, tmp_path) is False


def test_a_citation_that_escapes_the_workdir_is_refused_rather_than_read(tmp_path):
    """The locator is MODEL-AUTHORED TEXT reaching a filesystem call. It is resolved against the
    workdir root and refused if it escapes — `..`, an absolute path, a symlink out. A refusal
    answers False (it did not resolve), never a raise and never a read outside the fence."""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("x")
    for locator in ("../secret.txt", str(outside), "a/../../secret.txt"):
        ev = coerce_evidence({"evidence_source": "code", "evidence_locator": locator})
        assert evidence_citation_resolves(ev, tmp_path) is False, locator


def test_three_answers_and_not_two_because_uncited_and_unresolvable_are_different_facts(tmp_path):
    """A durable row that conflated "it cited nothing" with "it cited something that is not there"
    could not be counted, and counting is the whole reason the field exists."""
    assert evidence_citation_resolves(coerce_evidence({}), tmp_path) is None
    assert evidence_citation_resolves(
        coerce_evidence({"evidence_source": "error", "evidence_quote": "boom"}), tmp_path) is None
    # a source naming a place with nothing to point at is not a citation
    assert coerce_evidence({"evidence_source": "log"})["source"] == EVIDENCE_SOURCE_NONE


def test_the_evidence_coercion_is_total_over_junk(tmp_path):
    """Same standard as the rule it rides beside: this runs on the failure path."""
    for junk in (None, 42, "code", [], {"evidence_source": object()},
                 {"evidence_locator": {"a": 1}}, {"evidence_source": "code",
                                                  "evidence_locator": "a\x00b"}):
        ev = coerce_evidence(junk)
        assert set(ev) == {"source", "locator", "quote"}
        assert ev["source"] in (EVIDENCE_SOURCE_CODE, EVIDENCE_SOURCE_LOG, "error",
                                EVIDENCE_SOURCE_NONE)
        assert evidence_citation_resolves(ev, tmp_path) in (True, False, None)


def test_the_check_records_and_never_refuses():
    """A COST DECISION, pinned so it is not quietly promoted to a gate. A refusal would demote an
    uncited-but-correct diagnosis to `unclassified`, i.e. LOSE it, and nobody has measured how often
    a live model mis-formats a citation. So the verdict stands on its own and the citation lands
    beside it as a countable column."""
    uncited = {"action": "repair", "failure_kind": "oom", "rationale": "the allocator raised"}
    assert diagnosed_failure_reason("crash", uncited) == ("oom", REASON_SOURCE_TRIAGE)
    assert coerce_evidence(uncited)["source"] == EVIDENCE_SOURCE_NONE


# --------------------------------------------------------------------------------------------
# THE REGISTRY INVARIANTS — what makes the diagnostician safe for the RECORD
# --------------------------------------------------------------------------------------------

def test_a_diagnosed_reason_can_never_move_a_metric():
    """DOC 36's LINE, as a checkable property rather than an intention, and stated TWICE on purpose
    because the two guarantees are independent:

      * VOCABULARY — every member of `NEVER_SALVAGED_REASONS` is engine-final and no diagnosed kind
        is in it, so a verdict cannot move a node into the salvage refusal nor, the direction that
        actually costs something, out of it.
      * ORDERING — `_salvage_eval_metric` is called on the DETERMINISTIC reason, branches before the
        triage call, so no diagnosed reason has ever reached the gate whatever it said. That one is
        a fact about code order, which is why it is pinned separately.
    """
    assert set(DIAGNOSED_FAILURE_REASONS) & set(NEVER_SALVAGED_REASONS) == set()
    assert set(NEVER_SALVAGED_REASONS) <= set(ENGINE_FINAL_REASONS)
    assert UNCLASSIFIED_REASON not in NEVER_SALVAGED_REASONS

    from looplab.engine import evaluate as ev
    src = inspect.getsource(ev.EvaluateMixin._evaluate)
    assert src.index("self._salvage_eval_metric(res, reason, workdir, _t0)") < src.index(
        "reason, _reason_source = diagnosed_failure_reason(reason, triage)"), (
        "salvage must be decided on the engine's own answer, above the diagnostician")


def test_producible_and_selectable_are_still_the_same_set_across_all_four_producers():
    """The classifier is no longer the only producer of a `reason`, and the third one is new. Every
    producible reason must stay selectable by `inline_repair_reasons`, which is what
    `tests/test_inline_repair_reason_coverage.py` guards from the other side."""
    everything = (_classifier_vocabulary()          # producer 1: the structural classifier
                  | {MONITOR_REPAIR_REASON}          # producer 2: the live training watchdog's kill
                  | set(DIAGNOSED_FAILURE_REASONS)   # producer 3: the diagnostician
                  | {UNCLASSIFIED_REASON})           # producer 4: the engine, on a failed diagnosis
    assert set(DIAGNOSED_FAILURE_REASONS) <= set(FAILURE_REASONS)
    assert set(ENGINE_FINAL_REASONS) <= set(FAILURE_REASONS)
    assert UNCLASSIFIED_REASON in set(FAILURE_REASONS)
    assert set(FAILURE_REASONS) == everything, (
        "producible <=> selectable: a reason no producer emits is dead vocabulary, and one no "
        "setting can select is a failure class that silently stops being repairable")
    # `oom` is in the registry ONLY because producer 3 exists — nothing else can emit it any more.
    assert "oom" not in (_classifier_vocabulary() | {MONITOR_REPAIR_REASON, UNCLASSIFIED_REASON})


def test_the_emit_schema_reads_the_vocabulary_from_the_registry():
    """The same rule `action`'s enum follows and for the same reason: a re-spelled literal in the
    agent would be a typo the engine's `inline_repair_reasons` gate keys on. Source-derived, because
    the schema is built inside a method that needs a live client to reach.

    BY AST, NOT SUBSTRING (2026-08-20). The property is "the enum is a CALL over the registry name",
    and that is a node — `ast.Call(func=Name('list'), args=[Name(<registry>)])`. A substring pin
    could not tell that from a comment quoting it, and the thing it must catch is precisely someone
    re-spelling the vocabulary as a literal list."""
    from looplab.agents import unified_agent

    src = _dedented(unified_agent.UnifiedAgent.triage_crash)
    enums = _enum_sources(src)
    assert "list(DIAGNOSED_FAILURE_REASONS)" in enums
    assert "list(EVIDENCE_SOURCES)" in enums
    assert "list(AGENT_TRIAGE_ACTIONS)" in enums, "the action enum follows the same rule"
    # …and NONE of them is a re-spelled literal. Derived from the nodes, so it holds however the
    # literal is spelled or wrapped.
    for value in enums:
        assert value.startswith("list("), f"an enum was re-spelled rather than read: {value}"
    # The NEGATIVE pins stay substrings on purpose (CLAUDE.md): what must not come back is the TEXT,
    # and a commented-out copy is as much of a drift risk as a live one.
    assert '"enum": ["crash"' not in src and "'crash', 'oom'" not in src


def test_the_code_scouts_turn_grant_agrees_with_its_own_derivation():
    """`agents` may not import `engine` at module scope, so `_REPAIR_LOOK_TURNS` is a LITERAL and
    this is the red test that stands in for the single definition. 4 is the log tools' historical
    grant; the extra 3 is `train_monitor._MONITOR_LOOK_TURNS`' own 6 -> 9 argument applied to the
    identical toolset one role over."""
    from looplab.agents.unified_agent import UnifiedAgent
    from looplab.engine import train_monitor as tm

    assert UnifiedAgent._REPAIR_LOOK_TURNS == 4 + DIAGNOSIS_CODE_LOOK_TURNS
    assert UnifiedAgent._REPAIR_LOOK_TURNS < tm._MONITOR_LOOK_TURNS, (
        "still below the watchdog's WHOLE budget: this one is additive over the pilot's")


def test_the_engine_loop_stamps_who_chose_the_reason_and_what_it_read():
    """The RECORD half of doc 36. `reason` lands on `node_repaired` and on the `node_failed`
    terminal, so once a model may choose it the row has to say so — and it has to keep the engine's
    own answer beside it, or the structural classification is destroyed by the very change that made
    it optional. Additive with reader-side defaults (invariant #5): the evidence keys are OMITTED
    when nobody was asked, so an old row folds unchanged.

    BY AST, NOT SUBSTRING (2026-08-20), and this test is why the rule is worth stating twice. It
    asserted five rendered lines over `inspect.getsource`, and `evaluate.py` names `engine_reason`
    fourteen times — mostly in prose. A future comment quoting the line it replaced would have kept
    all five green over a loop that stamps nothing. What is asserted now is the KEY -> VALUE
    MAPPING the source really builds, which is an `ast.Dict` pair or a `Subscript` assignment and is
    unforgeable by prose: a comment produces neither node.
    `test_a_commented_out_stamp_is_seen_by_the_ast_check_and_missed_by_a_substring` drives it."""
    from looplab.engine import evaluate as ev

    stamps = _payload_stamps(_dedented(ev.EvaluateMixin._evaluate))
    # WHO chose the reason, and WHAT the engine independently held — the two columns that keep the
    # durable rows honest now that a model may pick `reason`.
    assert "_reason_source" in stamps.get("reason_source", set())
    assert "_engine_reason" in stamps.get("engine_reason", set())
    # WHERE it looked, and whether the citation resolved. Both are written on BOTH terminals, and
    # the `node_failed` path adds them by subscript assignment rather than in the literal.
    assert "_evidence" in stamps.get("reason_evidence", set())
    assert "_evidence_resolved" in stamps.get("reason_evidence_resolved", set())
    # …and `reason` itself is the loop variable, never a literal, on the rows that carry a source.
    assert "reason" in stamps.get("reason", set())

    # THE RULE IS APPLIED, as a call whose two targets are the pair. Asserted as an `ast.Assign`
    # rather than as the rendered line, whose spelling changes under any reformat.
    targets = _tuple_assign_targets(_dedented(ev.EvaluateMixin._evaluate),
                                    "diagnosed_failure_reason")
    assert targets == [("reason", "_reason_source")], (
        f"the ownership rule must be applied exactly once, to both columns; found {targets}")


def test_a_commented_out_stamp_is_seen_by_the_ast_check_and_missed_by_a_substring():
    """THE DRIVER, and without it the rewrite above could still be vacuous and nobody would know.

    It performs the exact mutation CLAUDE.md names as the cheapest one — delete the code, leave a
    comment carrying the pinned literal — and asserts BOTH halves of why the rewrite was needed:
    the AST check sees the loss, and the substring pin it replaced does not. The second assertion is
    the one that matters, because it is the evidence that this was a real defect rather than a
    stylistic preference.

    Six separate mechanisms in this repo were found shipping a vacuous green on 2026-08-20. This
    file guards the change that removed text-matching from the engine, so a text-matching guard here
    would have been the seventh."""
    from looplab.engine import evaluate as ev

    src = _dedented(ev.EvaluateMixin._evaluate)
    assert "_engine_reason" in _payload_stamps(src).get("engine_reason", set()), "precondition"

    # BOTH rows that carry it — `node_repaired` and the `node_failed` terminal — because a stamp is
    # only gone when every writer of it is, and a half-mutation would leave this driver proving
    # nothing while looking like it passed.
    # ALL THREE WRITERS — `node_repaired`, the repair-log row the F8 critic reads, and the
    # `node_failed` terminal — because a stamp is only gone when every writer of it is, and a
    # partial mutation would leave this driver looking like it passed while proving nothing. The
    # count is pinned for exactly that reason: the first draft of this test used a needle with a
    # trailing comma, silently hit two of the three, and the check stayed correctly green.
    mutated = _drop_stamp(src, "engine_reason", "_engine_reason", expect=3)
    # the AST check REDDENS: the mapping is gone because a comment is not a node…
    assert "_engine_reason" not in _payload_stamps(mutated).get("engine_reason", set()), (
        "the AST check cannot see a stamp that stopped being built — it is vacuous")
    # …while the substring pin this replaced stays GREEN over the very same mutation.
    assert '"engine_reason": _engine_reason' in mutated, (
        "if the old pin also reddened here, the rewrite bought nothing and this test should say so")

    # The same, for the rule application itself.
    assert _tuple_assign_targets(src, "diagnosed_failure_reason") == [("reason", "_reason_source")]
    gone = _comment_out(src, "reason, _reason_source = diagnosed_failure_reason(reason, triage)",
                        expect=1)
    assert _tuple_assign_targets(gone, "diagnosed_failure_reason") == [], (
        "the rule could be deleted without this guard noticing")
    assert "reason, _reason_source = diagnosed_failure_reason(reason, triage)" in gone

    # And for the enum: the realistic drift there is a RE-SPELLED literal, not a deletion.
    from looplab.agents import unified_agent
    schema = _dedented(unified_agent.UnifiedAgent.triage_crash)
    respelled = schema.replace("list(DIAGNOSED_FAILURE_REASONS)",
                               '["crash", "oom", "no_metric", "check_failed", "not_learning"]', 1)
    assert "list(DIAGNOSED_FAILURE_REASONS)" not in _enum_sources(respelled), (
        "a vocabulary re-spelled as a literal must redden — that is the typo the registry exists for")


def test_the_diagnostician_gets_the_code_the_eval_actually_ran():
    """The tools half of the requirement, and the ROOT is the whole of it: `diagnosis_tools` is
    rooted at the node WORKDIR, not at the editable source. The Developer's own scouts are rooted at
    the source, which is a different filesystem from the one the eval saw — the distinction that
    already cost `runs/rubert-dr-0807` node 2, whose stage died on a missing
    `<workdir>/looplab_eval.py` while the repair session's `read_file` cheerfully returned it."""
    from looplab.engine import failure_diagnosis as fd
    from looplab.tools.reposcout import RepoScoutTools

    class _Off:
        _repair_log_tools = False

    class _On:
        _repair_log_tools = True

    assert fd.diagnosis_code_tools(_Off(), "/tmp") is None, "gated on the operator's own switch"

    import tempfile
    with tempfile.TemporaryDirectory() as wd:
        tools = fd.diagnosis_code_tools(_On(), wd)
        assert isinstance(tools, RepoScoutTools)
        # Rooted THERE and nowhere else. `_roots` is private and read here deliberately: the ROOT is
        # the whole safety argument, and asserting it through a public read would only prove what a
        # tool CALL returned, not what the fence is.
        assert [str(Path(r).resolve()) for r in tools._roots] == [str(Path(wd).resolve())]
    assert fd.diagnosis_code_tools(_On(), "/nonexistent/nope") is None


def test_the_engine_states_what_it_observed_and_never_the_conclusion():
    """THE `setup` LESSON ONE RUNG ALONG, and the reason deleting the kernel-OOM rule did not make
    the engine dumber.

    `evaluate._eval_failure_text` surfaces `exit=` ONLY in its blank-stderr fallback, so a cgroup
    OOM-kill that leaves a "Killed" line handed the diagnostician that one word and nothing else — an
    engine-held fact, discarded, left to be re-inferred from the candidate's own text, which is
    exactly what this module exists to stop. `engine_observed_facts` hands it over.

    THE LINE IT MUST NOT CROSS is stating the CONCLUSION. "This looks like an OOM" is the deleted
    rule wearing a prompt: it would put the engine's authority behind a judgement it has no way to
    make, and the judgement is the thing being delegated. So the block may say `exit code -9 (killed
    by SIGKILL)` and may not say `oom` or `memory`.

    IT MAY, AND MUST, SAY WHAT IT EXCLUDED. `exit -9, no output` was a bad RULE because both watchdog
    tree-kills produce byte-identical evidence — the v6 node 5 conflation. Those never reach a
    diagnostician (engine-final), so naming that exclusion is what makes the remaining inference
    sound rather than a hint."""
    from looplab.engine.failure_diagnosis import engine_observed_facts

    kill = engine_observed_facts(RunResult(exit_code=-9, stdout="", stderr="", metric=None,
                                           timed_out=False))
    assert "exit code -9" in kill and "SIGKILL" in kill
    assert "wrote NOTHING to stderr" in kill
    assert "No watchdog of ours claimed this run" in kill
    # the conclusion stays the diagnostician's
    for verdict_word in ("oom", "memory", "out of memory", "reduce"):
        assert verdict_word not in kill.lower(), (
            f"{verdict_word!r} makes this a hint, not a fact — that is the deleted rule in a prompt")

    # 128+N is the shell's spelling of the same signal, and its stderr is NON-empty, which is the
    # shape `_eval_failure_text` never surfaced an exit code for at all.
    shell = engine_observed_facts(RunResult(exit_code=137, stdout="", stderr="Killed", metric=None,
                                            timed_out=False))
    assert "exit code 137" in shell and "SIGKILL" in shell

    # An ordinary failure is described without inventing a signal.
    plain = engine_observed_facts(RunResult(exit_code=1, stdout="", stderr="ValueError: x",
                                            metric=None, timed_out=False))
    assert "exit code 1" in plain and "killed by" not in plain

    # Total over junk, like every other function on this path.
    for junk in (None, object(), 42, "res"):
        assert isinstance(engine_observed_facts(junk), str)


def test_the_engine_facts_reach_the_prompt_and_an_older_seam_survives_them():
    """The seam half. `triage_crash` is DUCK-TYPED, so a new keyword passed unconditionally to an
    implementation written against the old signature raises TypeError — which the fail-closed
    handler reads as a dead provider and turns into a stopped node PLUS a run-level pause. That is
    the worst way for a signature change to land, and `_accepted_kwargs` is what stops it."""
    from looplab.core.models import RunState

    seen = {}

    class _New:
        def triage_crash(self, node, error, attempt, *, state=None, brief="", engine_facts="", **kw):
            seen["facts"] = engine_facts
            return {"action": "repair", "failure_kind": "oom", "rationale": "cgroup kill"}

    out = _EngineStub(_New())._triage_crash(RunState(), object(), "boom", 1, reason="crash",
                                            engine_facts="--- OBSERVED ---\nexit code -9\n")
    assert "exit code -9" in seen["facts"], "the block never reached the seam"
    assert diagnosed_failure_reason("crash", out) == ("oom", REASON_SOURCE_TRIAGE)

    class _Old:
        def triage_crash(self, node, error, attempt, *, state=None, brief=""):
            return {"action": "repair", "rationale": "old-style"}

    old = _EngineStub(_Old())._triage_crash(RunState(), object(), "boom", 1, reason="crash",
                                            engine_facts="--- OBSERVED ---\nexit code -9\n")
    assert old["action"] == "repair", (
        "an older seam must not be read as a dead provider because a keyword was added")


def test_the_log_tools_win_a_name_collision_with_the_code_tools():
    """`CompositeTools` de-dups by tool NAME with the first provider winning, and the logs go first
    deliberately: `read_log` is the only reader that knows this attempt's byte floor, so a general
    file reader shadowing it would let a repairer diagnosing attempt N read attempt N-1's curve as
    its own. Pinned as ORDER at the one place the composition happens."""
    from looplab.engine import failure_diagnosis as fd

    src = inspect.getsource(fd.diagnosis_tools)
    tree = ast.parse(src.lstrip())
    calls = [n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert calls.index("repair_log_tools") < calls.index("diagnosis_code_tools")


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


def test_the_kind_and_its_evidence_survive_the_engines_intake_and_junk_beside_them_does_not():
    """Driven through `_triage_crash`, because the seam is duck-typed and the fields have to travel
    the whole way. The overreaching keys are the trust line: a diagnostician that has just read the
    log may say which of five kinds it was and may not say what the metric is."""
    from looplab.core.models import RunState

    class _Judge:
        def triage_crash(self, node, error, attempt, *, state=None, brief="", **kw):
            return {"action": "repair", "failure_kind": "OOM", "rationale": "allocator raised",
                    "evidence_source": "log", "evidence_locator": "train.log",
                    "evidence_quote": "CUDA out of memory",
                    "reason": "ok", "metric": 0.99, "selectable": True}

    out = _EngineStub(_Judge())._triage_crash(RunState(), object(), "boom", 1, reason="crash")
    assert set(out) == {"action", "failure_kind", "rationale", "missing_dependency",
                        "evidence_source", "evidence_locator", "evidence_quote"}
    assert diagnosed_failure_reason("crash", out) == ("oom", REASON_SOURCE_TRIAGE)
    assert coerce_evidence(out) == {"source": EVIDENCE_SOURCE_LOG, "locator": "train.log",
                                    "quote": "CUDA out of memory"}


def test_an_older_duck_typed_seam_is_a_diagnostician_that_did_not_answer():
    """A `triage_crash` implementation written before these fields existed — every test double in
    the tree, and any researcher an operator wired themselves.

    NOTE THE DELIBERATE BEHAVIOUR CHANGE from the half-measure, which read this as "keep the
    engine's answer". A wired seam that returns no kind IS a diagnostician that was asked and said
    nothing, and the record must be able to count that. The no-judge path is a different branch and
    is covered above — it carries the engine-side marker precisely so the two do not collapse."""
    from looplab.core.models import RunState

    class _Old:
        def triage_crash(self, node, error, attempt, *, state=None, brief="", **kw):
            return {"action": "repair", "rationale": "fix it"}

    out = _EngineStub(_Old())._triage_crash(RunState(), object(), "boom", 1, reason="crash")
    assert out["failure_kind"] == ""
    assert diagnosed_failure_reason("crash", out) == (UNCLASSIFIED_REASON,
                                                      REASON_SOURCE_UNDIAGNOSED)
    # …but an ENGINE-FINAL reason is still untouched by any of it.
    assert diagnosed_failure_reason("timeout", out) == ("timeout", REASON_SOURCE_ENGINE)
