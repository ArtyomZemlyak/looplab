"""WHAT A FAILED EVAL LEAVES BEHIND FOR SOMEONE READING IT A WEEK LATER.

THE DEFECT, measured. `evaluate._eval_failure_text` produced ONE string —
`self._redact(res.stderr[-500:])` — and its own docstring names four consumers: the repair prompt,
`node_repaired.error_in`, the judge's history rows and the terminal's `error`. Replayed over the
committed 122-row `failure_triage.v1` corpus, **not one** of the preserved stderr tails contains a
torch allocator marker: `oom_marker_in_evidence` scores 0/16 as a label basis and
`allocator_message_in_stderr` 0/7. The cause is visible in the tails themselves — a tqdm bar
overwrites one line with `\\r` and pads it to the terminal width, so the last 500 characters of an
OOM'd training run are ~440 characters of bar and padding and the `torch.OutOfMemoryError` line sits
just above them. Measured on the preserved stage logs, the allocator marker sits 948 / 1,659 /
3,386 / 4,869 characters from the end of the four logs that carry an attributable one: 0 of 4 inside
a 500-character window.

**THE FIX IS NOT A WIDER WINDOW, AND THAT IS THE ARGUMENT THIS FILE HOLDS.** The bytes were never
lost: `sandbox._tee_drain` writes `<workdir>/<stage>.log` and nothing deletes it — 787 MB of them
across the eight preserved runs, including a run that finished long ago. What was lost is the
ACCOUNT. So the diagnostician — which by the time it answers has already read those logs, the config
and the code the eval ran, and had all of it discarded when the call returned — now writes down what
it found:

  * `reason_summary`, the causal statement with its numbers INLINE, which must work with nothing
    else in front of it; and
  * `reason_findings`, the trail behind it, each citation re-resolved by the engine inside the
    workdir fence and stamped `resolved`.

Both are additive, fold-ignored columns on `node_repaired` and `node_failed` (invariant #5).

THE FAILURE MODE THIS IS DESIGNED AGAINST, and every test below is pointed at one half of it: **a
summary is the agent's ACCOUNT, not the evidence.** If the diagnostician is wrong, its summary is
wrong in exactly the same way and no reader can tell from the summary alone. Hence the citations —
`quote` and `locator` are for CHECKING, `summary` and `means` are for READING. And hence the
opposite rule too: because a citation may die, the summary must stand alone, which is why the bar on
it is about CONTENT (the allocation size, the parameter, the stage, the exception type) and not
about length.

Every test here drives the REAL `_evaluate` or the REAL coercion. Only the solution's source and the
diagnostician's verdict are scripted.

EVERY ASSERTION BELOW WAS SHOWN TO FAIL, by mutating a throwaway copy of the tree (CLAUDE.md's own
rule — never the real one). Four of them were VACUOUS on the first pass and are rewritten here, which
is the reason this paragraph exists rather than a claim that they are all sound:

  * the redaction test masked by hand and never touched `_evaluate`'s wiring, so
    `coerce_diagnosis_summary(triage, None)` — the exact pre-fix defect — left it green. Split into a
    unit half and `test_the_engine_really_wires_its_redactor_into_the_durable_row`, which reads the
    bytes of `events.jsonl`;
  * the redact-BEFORE-cap test had no input that could tell the two orders apart. Almost none can:
    the only shape that separates them is a secret the cap cuts IN HALF, leaving a stub below
    `_PATTERNS`' sixteen-character minimum;
  * the attempt-boundary test used a chain whose second attempt was also diagnosed, so `_summary`
    was reassigned anyway and a missing reset was invisible. It now uses an attempt that
    terminalizes without reaching the diagnostician at all;
  * the primary-citation test used a verdict whose primary was repeated in `findings`, which makes
    "carry the primary" and "do not carry it" produce the same list.

Also driven red: dropping either column from either row, the record leaking into the repair prompt,
into the in-process judge history AND into the durable rebuild of it (two different builders),
dropping an unresolvable finding, unbounding the list, and teaching the fold to read a column
invariant #5 says it must ignore. Fourteen mutations, fourteen red.
"""
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea
from looplab.engine.evaluate import _JUDGE_ERROR_CHARS, _durable_repair_ledger
from looplab.engine.failure_diagnosis import (DIAGNOSIS_SUMMARY_CAP, EVIDENCE_QUOTE_CAP,
                                              EVIDENCE_SOURCE_LOG, EVIDENCE_SOURCE_NONE,
                                              FINDINGS_CAP, FINDING_MEANS_CAP,
                                              coerce_diagnosis_summary, coerce_evidence,
                                              coerce_findings, resolve_findings)
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"

# ------------------------------------------------------------------ the failure this file is about
# The allocator line and the bar are the real shapes, condensed only in the number of renders. The
# padding is what does the damage and is not decoration: `runs/e5small-dr-unified-v2` node 0's
# durable 522-character `error_in` is `[failed stage: train]\n` plus the last frames of exactly this
# bar, and the `Tried to allocate` line it needs is entirely above the cut.
_ALLOCATOR_LINE = (
    "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 8.79 GiB. GPU 0 has a total "
    "capacity of 139.80 GiB of which 4.59 GiB is free. Process 251757 has 139.12 GiB memory in use.")
_TRACEBACK = ("Traceback (most recent call last):\n"
              '  File "solution.py", line 9, in <module>\n'
              "    train(batch_size=8192)\n"
              f"{_ALLOCATOR_LINE}\n")
# 520 characters of progress bar, which is what the 500-character tail is spent on.
_BAR = "\r  0%|          | 0/10545 [00:10<?, ?it/s]" + " " * 476 + "\n"

_ALLOCATION_SIZE = "8.79 GiB"          # the one fact a reader must be able to recover


def _oom_solution() -> str:
    """A solution that dies exactly as the corpus's OOM nodes die: the traceback on stderr, a stage
    log beside it on disk, and the tqdm bar last so it owns the tail."""
    return (
        "import sys, json, pathlib\n"
        f"_tb = {_TRACEBACK!r}\n"
        f"_bar = {_BAR!r}\n"
        "pathlib.Path('train.log').write_text('epoch 0 loss 8.85\\n' + _tb + _bar)\n"
        "sys.stderr.write(_tb)\n"
        "sys.stderr.write(_bar)\n"
        "sys.exit(1)\n")


class _Diagnostician:
    """A crash-triage double that answers the way the shipped prompt asks a real one to: a summary
    carrying the numbers inline, plus the trail it read them from.

    It RECORDS the `error` string it was handed, which is the other half of every test here — the
    claim is that the RECORD widened and the PROMPT did not, and only the recorded string can show
    the second half."""

    def __init__(self, verdict=None, action="abandon"):
        self.calls: list[dict] = []
        self.verdict = verdict

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def implement(self, idea):
        return _oom_solution()

    def repair(self, idea, code, error):
        self.calls.append({"role": "repair", "error": error})
        return _oom_solution()

    def triage_crash(self, node, error, attempt, *, state=None, brief="", **kw):
        # `history` is captured beside `error` because they are DIFFERENT strings with different
        # provenance — `error` is `_eval_failure_text`'s tail, `history` is rebuilt from the durable
        # `node_repaired` rows — and only one of them is at risk from a widened record.
        self.calls.append({"role": "triage", "error": error, "attempt": attempt,
                           "history": str(kw.get("history") or "")})
        if self.verdict is not None:
            return dict(self.verdict)
        return {
            "action": "abandon",
            "failure_kind": "oom",
            "rationale": "the allocator raised; halve the batch",
            "summary": (
                "The `train` stage died in epoch 0 with torch.OutOfMemoryError: it asked the CUDA "
                f"allocator for {_ALLOCATION_SIZE} on a 139.80 GiB card that had 4.59 GiB free, "
                "because batch_size is set to 8192 in solution.py line 9. Nothing else failed — "
                "the loss was 8.85 at the only step that ran."),
            "evidence_source": EVIDENCE_SOURCE_LOG,
            "evidence_locator": "train.log:2",
            "evidence_quote": _ALLOCATOR_LINE,
            "findings": [
                {"source": "log", "locator": "train.log:2", "quote": _ALLOCATOR_LINE,
                 "means": "the allocator refused an 8.79 GiB request with 4.59 GiB free"},
                {"source": "code", "locator": "solution.py:9", "quote": "train(batch_size=8192)",
                 "means": "the batch size that produced that request"},
            ],
        }

    def triage_repair(self, *a, **k):       # pragma: no cover - not reached in these cases
        return {"action": "abandon", "rationale": "n/a"}


def _drive(tmp_path, dev, **kw):
    """Seed one node and run the REAL eval + repair loop over it."""
    kw.setdefault("auto_install_deps", False)
    kw.setdefault("inline_repair", True)
    run_dir = tmp_path / "run"
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=dev, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": dev.implement(None)})

    async def _bounded() -> bool:
        with anyio.move_on_after(120) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the eval did not terminate"
    return list(EventStore(run_dir / "events.jsonl").read_all()), eng


def _terminal(evs):
    return next(e for e in evs if e.type in ("node_failed", "node_evaluated")
                and e.data.get("node_id") == 0)


# =================================================================================================
# THE PROPERTY, BOTH HALVES, DRIVEN
# =================================================================================================

def test_the_allocator_message_is_recoverable_from_the_record_and_the_prompt_did_not_grow(tmp_path):
    """BOTH HALVES OF THE CLAIM, on one real failure whose allocator line sits outside the last 500
    characters of stderr.

    Half one — RECOVERABLE. A reader holding only the terminal event can say what happened and
    because of what: the allocation size, the free memory, the parameter and its value are all in
    `reason_summary`, and `reason_findings` says where each came from.

    Half two — THE PROMPT DID NOT GROW. The string handed to the diagnostician and to
    `Developer.repair` is byte-identical to what `_eval_failure_text` has always produced, and the
    fact recovered above is provably NOT in it. That is what makes this a split rather than a
    widening: at ~8.8 provider calls per failure, a record that cost prompt tokens would be a trade,
    and this one is not.

    The negative control is inside the assertions rather than beside them: the same fact is asserted
    ABSENT from `error`/`error_in`, which is exactly what the shipped record carried before this
    change and all it would carry if the new columns were dropped."""
    dev = _Diagnostician()
    evs, _eng = _drive(tmp_path, dev)
    term = _terminal(evs)
    assert term.type == "node_failed"

    # --- half two: what the model was ASKED with ------------------------------------------------
    asked = [c for c in dev.calls if c["role"] == "triage"]
    assert asked, "the diagnostician was never consulted"
    prompt = asked[0]["error"]
    # BYTE-EXACT, not a length bound: the prompt is the tagged head `crash_repair` has always put on
    # it plus `_eval_failure_text`'s last 500 characters of stderr, and nothing else. A record column
    # leaking into it breaks this equality rather than merely widening a budget nobody watches.
    assert prompt == "[failure kind: crash]\n" + (_TRACEBACK + _BAR)[-500:]
    assert _ALLOCATION_SIZE not in prompt, (
        "the allocator line reached the PROMPT — this change is a record split, not a wider "
        "window, and a prompt that grew would be paying tokens per failure for it")
    assert "/10545 [" in prompt, "the 500-char tail is the progress bar, as measured on the corpus"
    # …and the repair prompt is the same string, so no consumer of it grew either.
    for call in dev.calls:
        assert _ALLOCATION_SIZE not in call["error"]

    # --- half one: what the RECORD holds ---------------------------------------------------------
    data = term.data
    assert _ALLOCATION_SIZE not in data["error"], (
        "the pre-change record could not carry this fact — if it can now, the test is measuring "
        "something other than the new columns")
    summary = data["reason_summary"]
    # THE BAR IS ABOUT CONTENT. Each of these is a fact a reader needs and cannot get anywhere else
    # once the run is gone; a summary that merely pointed at `train.log:2` would satisfy no line.
    for fact in (_ALLOCATION_SIZE, "4.59 GiB", "8192", "torch.OutOfMemoryError", "train"):
        assert fact in summary, f"the record does not name {fact!r}"

    # --- and the trail is checkable ---------------------------------------------------------------
    findings = data["reason_findings"]
    # The primary citation leads, and the model's own repeat of it did NOT spend a second slot —
    # a real diagnostician restates its decisive citation in the list as a matter of course.
    assert [f["source"] for f in findings] == ["log", "code"]
    assert [f["locator"] for f in findings] == ["train.log:2", "solution.py:9"]
    assert findings[0]["resolved"] is True and findings[1]["resolved"] is True, (
        "both cited files are really in the workdir and the engine re-resolved them")
    assert _ALLOCATOR_LINE in findings[0]["quote"]
    assert findings[1]["means"] == "the batch size that produced that request"


def test_the_record_is_readable_with_the_run_deleted(tmp_path):
    """SELF-SUFFICIENCY, driven the only way it can be: destroy everything the citations point at
    and read the row anyway.

    This is what "the summary must stand alone" MEANS operationally, and it is why no digest, no
    gone-vs-changed discriminator and no pruning policy was built — those all try to keep the link
    alive, and the operator's ruling is that if a link dies, it dies. What must survive is the
    account."""
    dev = _Diagnostician()
    evs, eng = _drive(tmp_path, dev)
    row = json.loads(json.dumps(_terminal(evs).data))     # the durable bytes and nothing else

    import shutil
    shutil.rmtree(tmp_path / "run" / "nodes", ignore_errors=True)
    assert not (tmp_path / "run" / "nodes").exists()

    # A reader with the row and no filesystem still gets the whole causal statement.
    assert _ALLOCATION_SIZE in row["reason_summary"] and "8192" in row["reason_summary"]
    assert "because" in row["reason_summary"].lower(), (
        "the summary owes a CAUSAL statement, not a list of observations")
    # The citations are now dead, and that costs the reader nothing the summary already gave them.
    # Note WHICH dead answer this is: with the workdir gone there is nothing to resolve AGAINST, so
    # every citation reads `None` — "unchecked", the existing three-answer vocabulary's own word —
    # rather than False, which would claim the engine looked and the file was not there. Neither is
    # a failure of anything, and that is the whole point: the account above does not depend on it.
    dead = resolve_findings(row["reason_findings"], tmp_path / "run" / "nodes" / "node_0")
    # Counted, because `[] == [] * 0` holds and a resolver that returned nothing would pass it.
    assert len(dead) == len(row["reason_findings"]) == 2
    assert [f["resolved"] for f in dead] == [None, None]
    assert dead == [{**f, "resolved": None} for f in row["reason_findings"]], (
        "a dead citation must lose its resolution stamp and NOTHING else")


# =================================================================================================
# A CITATION THAT DOES NOT RESOLVE IS MARKED AND KEPT
# =================================================================================================

def test_an_unresolvable_citation_is_marked_and_never_dropped(tmp_path):
    """Three answers and no deletions. Dropping a bad citation would hide the one thing a reader
    most needs to know about an account they cannot otherwise check; silently keeping it unmarked
    would let an invented citation read exactly like a real one."""
    (tmp_path / "train.log").write_text("boom\n")
    findings = coerce_findings({"findings": [
        {"source": "log", "locator": "train.log:2", "quote": "boom", "means": "it is there"},
        {"source": "log", "locator": "invented.log:9", "quote": "nope", "means": "it is not"},
        {"source": "error", "quote": "from the tail", "means": "nothing filesystem-shaped"},
        {"source": "none", "quote": "", "means": "the log stops mid-epoch with no exception"},
    ]})
    out = resolve_findings(findings, tmp_path)

    assert len(out) == 4, "a finding is never dropped, whatever its citation does"
    assert [f["resolved"] for f in out] == [True, False, None, None]
    # …and the FINDING survives its dead citation intact — the text is the valuable part.
    assert out[1]["quote"] == "nope" and out[1]["means"] == "it is not"
    assert out[3]["means"].startswith("the log stops")


def test_a_citation_that_escapes_the_workdir_is_refused_and_marked_rather_than_read(tmp_path):
    """The locator is MODEL-AUTHORED TEXT reaching a filesystem call, and widening one citation to
    a list widened that surface too. Same fence, same three answers, one rule — `resolve_findings` CALLS `evidence_citation_resolves` rather than restating it."""
    outside = tmp_path.parent / "elsewhere.txt"
    outside.write_text("x")
    workdir = tmp_path / "wd"
    workdir.mkdir()
    for locator in ("../elsewhere.txt", str(outside), "a/../../elsewhere.txt"):
        out = resolve_findings(
            coerce_findings({"findings": [{"source": "log", "locator": locator, "quote": "q"}]}),
            workdir)
        assert out and out[0]["resolved"] is False, locator


def test_a_byte_range_locator_still_names_its_file(tmp_path):
    """`path:startbyte-endbyte` is the shape the prompt asks a tool-using role for, and a resolver
    that only understood `path:line` would report every one of them as an invented citation.

    The falsifying input is the third case: a colon followed by something that is NOT a location
    must stay part of the PATH, or the fence resolves a different file than the model named."""
    (tmp_path / "train.log").write_text("x")
    (tmp_path / "odd:name.txt").write_text("x")
    for locator, expected in (("train.log:426", True), ("train.log:8290-33089", True),
                              ("odd:name.txt", True), ("train.log:8290-nope", False)):
        out = resolve_findings(
            coerce_findings({"findings": [{"source": "log", "locator": locator, "quote": "q"}]}),
            tmp_path)
        assert out[0]["resolved"] is expected, locator


# =================================================================================================
# REDACTION — the eighth persisted output channel
# =================================================================================================

def test_the_summary_and_every_finding_go_through_the_redactor(tmp_path):
    """`evidence_quote` is by its own schema description "the one line that settles it, quoted" —
    bytes a model copied out of a stage log — and it landed on the durable row UNREDACTED. That is
    the same defect the C2 sweep closed for `node_failed.triage_rationale` one field over, and
    widening the record from one quote to a summary plus six findings widens exactly that surface.

    Measured over the 257 preserved stage/console logs: a 500-character window carries 0 redaction
    masks, and a wider read carries 3 at 8 KB, 36 at 16 KB (a real `password`) and 384 at 64 KB.
    This role now reads with TOOLS, so its quotable window is the whole file."""
    from looplab.engine.audit import AuditMixin

    class _Host:
        _redact_output = True
        _redact = AuditMixin._redact

    redact = lambda text: _Host._redact(_Host(), text)     # noqa: E731 - the production funnel
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    verdict = {
        "summary": f"the loader authenticated with {secret} and then died",
        "evidence_source": "log", "evidence_locator": f"train.log#{secret}",
        "evidence_quote": f"api_key={secret}",
        "findings": [{"source": "code", "locator": "cfg.py:3", "quote": f"TOKEN = '{secret}'",
                      "means": f"the key {secret} is committed"}],
    }
    summary = coerce_diagnosis_summary(verdict, redact)
    evidence = coerce_evidence(verdict, redact)
    findings = coerce_findings(verdict, redact)

    for blob in [summary, json.dumps(evidence), json.dumps(findings)]:
        assert secret not in blob, "a model restating what it read is a laundering channel"
    assert "sk-***" in summary, "…and the redactor really ran, rather than the text going missing"
    # `all(...)` over a sequence nobody counted is true of an empty one, and "redaction emptied the
    # record" is exactly the failure that would empty it. Count first.
    assert len(findings) == 2 and summary
    assert all(f["quote"] or f["means"] for f in findings), "redaction must not empty the record"


def test_the_engine_really_wires_its_redactor_into_the_durable_row(tmp_path):
    """THE WIRING, and it is a separate test because the one above cannot see it: passing `redact`
    by hand proves the coercion can mask, not that `_evaluate` asks it to. The mutation that makes
    this fail is one character — `coerce_diagnosis_summary(triage, None)` — and it leaves every
    unit-level redaction test green while restoring the exact defect the C2 sweep closed one field
    over. Driven end to end, asserted on the bytes in `events.jsonl`."""
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    dev = _Diagnostician(verdict={
        "action": "abandon", "rationale": "done",
        "summary": f"the loader read {secret} out of the environment and then died",
        "evidence_source": "log", "evidence_locator": "train.log:2",
        "evidence_quote": f"Authorization: Bearer {secret}",
        "findings": [{"source": "log", "locator": "train.log:2", "quote": f"api_key={secret}",
                      "means": f"the committed key is {secret}"}]})
    _evs, _eng = _drive(tmp_path, dev)
    raw = (tmp_path / "run" / "events.jsonl").read_text()
    assert secret not in raw, (
        "the secret reached events.jsonl — and from there the trace, the UI and every export")
    assert "sk-***" in raw, "…and it was MASKED rather than the whole column going missing"


def test_redaction_runs_before_the_cap_so_masking_cannot_be_truncated_away(tmp_path):
    """The 2026-08-14 C2 ordering ruling ("redact BEFORE the cut, so masking can never be truncated
    away"), restated on the new fields.

    THE FALSIFYING INPUT IS THE POINT AND IS NOT OBVIOUS. Almost every arrangement of a secret and a
    cap passes under BOTH orders — put the secret before the cut and both mask it, put it after and
    both drop it — so a test built from one of those proves nothing. What separates them is a secret
    the cap CUTS IN HALF: `_PATTERNS` matches `sk-` plus at least sixteen characters, so
    cap-then-mask hands the redactor a twelve-character stub it no longer recognises and the
    fragment lands on the durable row verbatim. Mask-then-cap never sees the stub at all."""
    from looplab.engine.audit import AuditMixin

    class _Host:
        _redact_output = True
        _redact = AuditMixin._redact

    redact = lambda text: _Host._redact(_Host(), text)     # noqa: E731
    # Positioned so the cap falls INSIDE the secret: 1185 + len("sk-") + 12 == the cap.
    tail = "Z" * 40
    text = "A" * (DIAGNOSIS_SUMMARY_CAP - 15) + "sk-" + tail
    out = coerce_diagnosis_summary({"summary": text}, redact)
    assert len(out) <= DIAGNOSIS_SUMMARY_CAP
    assert "Z" not in out, (
        "a cap applied BEFORE the screen leaves a truncated credential the shape rule no longer "
        "recognises — a recognisable fragment on the durable row, which is the leak the C2 "
        "ordering ruling exists to close")
    assert out.endswith("sk-***"), "…and the whole secret really was masked, not merely cut off"


# =================================================================================================
# ADDITIVE FOLD, READER-SIDE DEFAULTS (invariant #5)
# =================================================================================================

def test_the_new_columns_are_fold_ignored_and_an_old_row_folds_identically(tmp_path):
    """Invariant #5, driven both ways: adding the columns changes no folded state, and a row written
    before they existed folds exactly as it did.

    The falsifying input is the pair — if the fold read either column, the two `model_dump`s would
    differ; if a reader defaulted an ABSENT column to something, the legacy row would gain state."""
    def _log(path, extra):
        s = EventStore(path)
        s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
        s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": ""},
                                  "code": "print(1)"})
        s.append("node_repaired", {"node_id": 0, "attempt": 1, "code": "print(2)",
                                   "error_in": "boom", **extra})
        s.append("node_failed", {"node_id": 0, "error": "boom", "reason": "oom", **extra})
        return fold(EventStore(path).read_all())

    rich = {"reason_summary": "it asked for 8.79 GiB and had 4.59 GiB",
            "reason_findings": [{"source": "log", "locator": "train.log:2", "quote": "boom",
                                 "means": "there", "resolved": True}]}
    with_cols = _log(tmp_path / "a.jsonl", rich)
    legacy = _log(tmp_path / "b.jsonl", {})
    assert with_cols.model_dump() == legacy.model_dump(), (
        "the fold read a column it must ignore — these rows are for readers, not for state")


def test_a_row_the_diagnostician_never_wrote_omits_the_columns_entirely(tmp_path):
    """"Nobody was asked" and "asked and wrote nothing" must not be the same durable row — the same
    distinction `reason_evidence` already keeps, and the reason both are OMITTED rather than written
    empty."""
    dev = _Diagnostician(verdict={"action": "abandon", "rationale": "no idea"})
    evs, _ = _drive(tmp_path, dev)
    data = _terminal(evs).data
    assert "reason_summary" not in data and "reason_findings" not in data
    # …and the row is otherwise complete, so the absence is about the diagnostician and not a crash.
    assert data["error"] and data["reason"]


# =================================================================================================
# THE ATTEMPT BOUNDARY
# =================================================================================================

def test_one_attempts_account_never_rides_on_anothers_row(tmp_path):
    """A stale summary is strictly worse than an absent one: it is a confident, readable account of
    a DIFFERENT failure, and a reader cannot tell it from a correct one — which is the one property
    the summary is trusted for.

    THE INPUT THAT MAKES THIS FAIL is the whole difficulty, and the obvious chain does not: when the
    second attempt IS diagnosed, `_summary` is reassigned anyway and a missing reset is invisible.
    It has to be an attempt that terminalizes WITHOUT reaching the triage call, and the engine has
    several — the redone-work floor, the attempt cap, and the one used here, a failure whose reason
    the operator excluded from `inline_repair_reasons`. All three `break` above the diagnostician.

    So: attempt one crashes (in the repairable set) and is diagnosed with a summary; its repair
    produces a clean exit with no metric (`no_metric`, excluded here), which breaks out before
    anything can be asked. The terminal is attempt two's and owes attempt one's account nothing."""
    class _Chain(_Diagnostician):
        def repair(self, idea, code, error):
            self.calls.append({"role": "repair", "error": error})
            return "print('no metric here')\n"          # exits 0, no parseable metric

        def triage_crash(self, node, error, attempt, *, state=None, brief="", **kw):
            self.calls.append({"role": "triage", "error": error, "attempt": attempt})
            return {"action": "repair", "rationale": "halve the batch",
                    "summary": "attempt one: the allocator refused 8.79 GiB"}

    dev = _Chain()
    evs, _ = _drive(tmp_path, dev, inline_repair_attempts=3,
                    inline_repair_reasons=("crash",))
    repaired = [e for e in evs if e.type == "node_repaired"]
    assert len(repaired) == 1, "exactly one attempt should have been diagnosed"
    assert "attempt one" in repaired[0].data["reason_summary"]
    assert len([c for c in dev.calls if c["role"] == "triage"]) == 1, (
        "the second attempt must terminalize WITHOUT a verdict, or the reset is untestable")
    term = _terminal(evs).data
    assert term["reason"] == "no_metric"
    assert "reason_summary" not in term and "reason_findings" not in term, (
        "attempt one's account rode onto attempt two's row — a confident, readable, WRONG record")


# =================================================================================================
# BOUNDS AND TOTALITY — this runs on the failure path, where a raise costs the terminal
# =================================================================================================

def test_the_findings_list_is_bounded_and_the_strings_are_capped():
    """The row-size budget, stated in `FINDINGS_CAP`'s own comment: six findings at 3 x 300 is
    ~5.6 KB worst case, +2.3 % over the 138 failure-bearing rows in the preserved runs. Extra
    findings are dropped from the END — the first citations are the ones the model reached for."""
    over = [{"source": "log", "locator": f"f{n}.log", "quote": "q" * 900, "means": "m" * 900}
            for n in range(FINDINGS_CAP + 5)]
    out = coerce_findings({"findings": over})
    assert len(out) == FINDINGS_CAP
    assert [f["locator"] for f in out] == [f"f{n}.log" for n in range(FINDINGS_CAP)]
    assert all(len(f["quote"]) == EVIDENCE_QUOTE_CAP for f in out)
    assert all(len(f["means"]) == FINDING_MEANS_CAP for f in out)
    assert len(coerce_diagnosis_summary({"summary": "s" * 9000})) == DIAGNOSIS_SUMMARY_CAP


@pytest.mark.parametrize("junk", [None, 42, "findings", [], {"findings": "not a list"},
                                  {"findings": [None, 7, "x"]},
                                  {"findings": [{"source": object()}]},
                                  {"summary": {"a": 1}}, {"summary": object()}])
def test_the_coercions_are_total_over_junk(junk, tmp_path):
    """Same standard as the rule they ride beside: this runs on the eval loop's failure path, where
    a raise costs the terminal being written (invariant #2)."""
    findings = coerce_findings(junk)
    assert isinstance(findings, list)
    assert isinstance(coerce_diagnosis_summary(junk), str)
    for f in resolve_findings(findings, tmp_path):
        assert f["resolved"] in (True, False, None)


def test_a_finding_that_points_nowhere_and_says_nothing_is_not_a_finding():
    """The one thing that IS dropped, and the boundary is deliberate: an entry with no citation, no
    quote and no reading is padding, while `means` ALONE is a real observation ("the log stops
    mid-epoch with no exception") and is kept, stamped `resolved: None`."""
    out = coerce_findings({"findings": [
        {"source": "none", "locator": "", "quote": "", "means": ""},
        {"source": "log", "locator": "", "quote": "", "means": "it stops with no exception"},
    ]})
    assert len(out) == 1 and out[0]["means"] == "it stops with no exception"
    assert out[0]["source"] == EVIDENCE_SOURCE_NONE, "a source with nothing to point at is not one"


def test_the_primary_citation_leads_the_list_and_is_not_duplicated():
    """A reader iterating `reason_findings` must never also have to remember `reason_evidence`.

    Two cases, because only the FIRST can fail: a model that DID repeat its primary citation makes
    the two implementations agree, so a test written only on that shape passes whether or not the
    primary is carried at all."""
    # (a) the primary is NOT among the findings — it must still lead.
    solo = coerce_findings({
        "evidence_source": "log", "evidence_locator": "train.log:2", "evidence_quote": "boom",
        "findings": [{"source": "code", "locator": "a.py:1", "quote": "x", "means": "new"}]})
    assert [f["locator"] for f in solo] == ["train.log:2", "a.py:1"]
    assert solo[0]["means"] == "", "the primary contract has no `means`, and none is invented"
    # (b) …and a model that repeats it does not spend a second slot on it.
    dup = coerce_findings({
        "evidence_source": "log", "evidence_locator": "train.log:2", "evidence_quote": "boom",
        "findings": [{"source": "log", "locator": "train.log:2", "quote": "boom", "means": "dup"},
                     {"source": "code", "locator": "a.py:1", "quote": "x", "means": "new"}]})
    assert [f["locator"] for f in dup] == ["train.log:2", "a.py:1"]


def test_the_judge_history_still_reads_the_bounded_error_and_not_the_summary(tmp_path):
    """WHAT MUST NOT HAVE HAPPENED, on the third consumer `_eval_failure_text` names. The judge's
    history rows are a PROMPT — `_JUDGE_ERROR_CHARS` bounds each row's `error` for exactly that
    reason — and they are rebuilt from `node_repaired`, which now carries a 1,200-character summary
    beside the 500-character error. A history that started shipping the summary would be this
    design's cost paid on every subsequent attempt of every repair chain, which is the trade it
    exists to avoid.

    The `history` kwarg and not the `error` argument: they are two different strings and only the
    first is rebuilt from the durable row. Asserting on `error` would pass whatever happened."""
    marker = "S" * DIAGNOSIS_SUMMARY_CAP
    dev = _Diagnostician(verdict={"action": "repair", "rationale": "again", "summary": marker})
    evs, _ = _drive(tmp_path, dev, inline_repair_attempts=2)
    seen = [c for c in dev.calls if c["role"] == "triage"]
    assert len(seen) >= 2, "a second attempt is needed for a history to exist at all"
    assert any(c["history"] for c in seen), "…and the history has to be non-empty to be tested"
    # The summary really is on the durable row this history is rebuilt from — otherwise the
    # assertion below is about a string that was never at risk.
    assert any(marker[:50] in (e.data.get("reason_summary") or "")
               for e in evs if e.type == "node_repaired")
    for call in seen:
        assert "S" * (_JUDGE_ERROR_CHARS + 1) not in call["history"], (
            "the record column reached the judge's PROMPT")
        assert "S" * (_JUDGE_ERROR_CHARS + 1) not in call["error"]

    # AND THE RESUMED HALF, driven separately, because it is a DIFFERENT builder: the loop above
    # renders the history from loop locals, while a process that resumed rebuilds it from these very
    # rows. The engine's own comment requires the two to carry the same columns; a leak that existed
    # in only one of them would show a chain judged before a resume and after it different prompts.
    _attempts, rows, _unparseable = _durable_repair_ledger(evs, 0, 0)
    assert rows, "the durable rebuild found no repair rows to render"
    assert all(len(r["error"]) <= _JUDGE_ERROR_CHARS for r in rows)
    assert not any("S" * (_JUDGE_ERROR_CHARS + 1) in json.dumps(r) for r in rows), (
        "the resumed judge's history carries the record column the in-process one does not")
