"""Guards for the judge bench (`looplab/judgebench/`).

Two jobs, and they are different guards:

1. **The committed dataset is DERIVED and must not be hand-edited.** It is 278 KB of gzipped
   evidence in the repo, so "never hand-edit it" needs something that enforces it on a machine with
   no `runs/` at all. `test_every_label_rederives` recomputes every label from the row's own stored
   facts through the production rule, so an edited label goes red offline.
2. **The bench must not lie about what it measured.** The label/verdict vocabularies stay closed,
   the two agreements stay in separate fields, `budget_exhausted` stays out of the accuracy
   denominator, and the corpus-limits paragraph travels with the artefact.

Most of these DRIVE the property over a synthetic run directory built here rather than pinning
source text, because the label rule is exactly the kind of thing a substring pin cannot check.
"""
from __future__ import annotations

import collections
import gzip
import hashlib
import json
import typing

import pytest

from looplab.judgebench import judge_corpus, score
from looplab.judgebench.judge_corpus import (
    CORPUS_LIMITS, DATASET_SCHEMA, DEFAULT_DATASET, LABELS, LABEL_BUDGET_EXHAUSTED,
    LABEL_PRODUCTIVE, LABEL_UNKNOWN, LABEL_WASTED, VERDICTS, build_dataset, extract_run,
    messages_of, read_dataset, rederive_label, render_prompt, write_dataset)


# ---------------------------------------------------------------- synthetic run (drives the rule)

_SYSTEM = "You are the ML engineer who wrote this training script."
_STAGE_CTX = ("This is the live log of pipeline stage 'train' (stage 2 of 3; the pipeline is "
              "mine -> train -> score). Judge THIS stage's output only.")


def _decision_spans(*, node_id, phase_span, start, digest, status, context="Optimizing metric 'm'."):
    """One monitor DECISION as the engine records it: a tool turn, then the `emit` that ends it."""
    messages = render_prompt({"system": _SYSTEM, "context": context, "stage_context": _STAGE_CTX,
                              "trajectory": "", "look_invitation": "", "digest": digest})
    common = {"kind": "generation", "name": "generation", "run_id": "synthetic",
              "trace_id": "t" * 32, "parent_id": phase_span, "status": "OK", "duration_s": 1.0}
    return [
        {**common, "span_id": phase_span + "a", "start": start,
         "attributes": {"phase": "train_monitor", "phase_span": phase_span, "node_id": node_id,
                        "generation": 0, "model": "synthetic-model", "input": messages,
                        "output": "[tool_calls: read_log]",
                        "tool_calls": [{"name": "read_log", "arguments": "{}"}]}},
        {**common, "span_id": phase_span + "b", "start": start + 1.0,
         "attributes": {"phase": "train_monitor", "phase_span": phase_span, "node_id": node_id,
                        "generation": 0, "model": "synthetic-model", "input": messages,
                        "output": "done",
                        "tool_calls": [{"name": "emit", "arguments": json.dumps(
                            {"status": status, "reason": "synthetic", "confidence": 0.9})}]}},
    ]


@pytest.fixture()
def synthetic_run(tmp_path):
    """A run holding one wasted node, one productive node and one timeout — the three label classes.

    `stage_finished` rows are written the way the engine writes them (all of an eval attempt's rows
    flushed together at the END of the attempt), because that burst is exactly what the attempt
    clustering has to cope with and a fixture that spread them out would test nothing.
    """
    run = tmp_path / "synthetic-run"
    run.mkdir()
    (run / "task.snapshot.json").write_text(json.dumps({"direction": "max"}), encoding="utf-8")

    spans = []
    spans += _decision_spans(node_id=1, phase_span="p1", start=1000.0, digest="loss 5.0\nloss 5.0\n",
                             status="broken")
    spans += _decision_spans(node_id=2, phase_span="p2", start=3000.0, digest="loss 2.0\nloss 1.0\n",
                             status="healthy")
    spans += _decision_spans(node_id=3, phase_span="p3", start=5000.0, digest="slow but moving\n",
                             status="healthy")
    (run / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")

    events = [
        # node 1: train ran to completion and the model it produced scored nothing -> wasted.
        {"seq": 1, "ts": 2000.0, "type": "stage_finished",
         "data": {"node_id": 1, "name": "train", "status": "ok", "exit_code": 0, "seconds": 900.0}},
        {"seq": 2, "ts": 2000.1, "type": "stage_finished",
         "data": {"node_id": 1, "name": "score", "status": "ok", "exit_code": 0, "seconds": 60.0}},
        {"seq": 3, "ts": 2001.0, "type": "node_evaluated",
         "data": {"node_id": 1, "metric": 0.0, "eval_seconds": 960.0}},
        # node 2: healthy all the way through, real metric -> productive.
        {"seq": 4, "ts": 4000.0, "type": "stage_finished",
         "data": {"node_id": 2, "name": "train", "status": "ok", "exit_code": 0, "seconds": 900.0}},
        {"seq": 5, "ts": 4001.0, "type": "node_evaluated",
         "data": {"node_id": 2, "metric": 0.8, "eval_seconds": 960.0}},
        # node 3: the stage ran out of budget -> budget_exhausted, NOT wasted.
        {"seq": 6, "ts": 6000.0, "type": "stage_finished",
         "data": {"node_id": 3, "name": "train", "status": "timeout", "exit_code": -9,
                  "seconds": 3600.0}},
        {"seq": 7, "ts": 6001.0, "type": "node_failed",
         "data": {"node_id": 3, "reason": "crash", "eval_seconds": 3600.0}},
    ]
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events),
                                      encoding="utf-8")
    return run


def _by_node(rows):
    return {r["provenance"]["node_id"]: r for r in rows}


# ---------------------------------------------------------------------------- the label rule

def test_label_rule_separates_the_three_outcome_classes(synthetic_run):
    """The rule's whole point: a stage that SUCCEEDED and produced a dead model is `wasted`, and a
    stage that ran out of budget is neither `wasted` nor `productive`."""
    rows = _by_node(extract_run(synthetic_run))
    assert rows[1]["label"]["label"] == LABEL_WASTED
    assert rows[1]["label"]["label_basis"] == "node_metric_degenerate"
    assert rows[1]["label"]["stage_status"] == "ok"          # stage status alone would say "fine"
    assert rows[2]["label"]["label"] == LABEL_PRODUCTIVE
    assert rows[3]["label"]["label"] == LABEL_BUDGET_EXHAUSTED
    assert rows[3]["label"]["label_basis"] == "stage_timeout"


def test_degenerate_is_relative_to_the_run_best_not_to_zero(synthetic_run):
    """`2e-05` is not `0.0` and is just as dead; `0.225` against a `0.728` best is neither."""
    rows = _by_node(extract_run(synthetic_run))
    best = rows[1]["label"]["run_best_metric"]
    assert best == pytest.approx(0.8)
    assert judge_corpus._is_degenerate(2e-05, best, "max") is True
    assert judge_corpus._is_degenerate(0.225, 0.728, "max") is False
    # A minimised metric inverts the whole rule and no run in the corpus exercises it, so the
    # honest answer is "undecidable" and not the silently-wrong "not degenerate".
    assert judge_corpus._is_degenerate(0.0, 0.8, "min") is None


def test_a_stage_that_worked_is_not_charged_for_a_later_stage_failing(tmp_path):
    """The attribution that removes the corpus's biggest spurious miss: eight `healthy` verdicts
    about `mine`, on nodes whose `train` crashed minutes later. `mine` was fine and the judge
    watching it could not see `train`."""
    run = tmp_path / "r"
    run.mkdir()
    (run / "task.snapshot.json").write_text('{"direction": "max"}', encoding="utf-8")
    stage_ctx = ("This is the live log of pipeline stage 'mine' (stage 1 of 3; the pipeline is "
                 "mine -> train -> score). Judge THIS stage's output only.")
    spans = _decision_spans(node_id=0, phase_span="q0", start=10.0, digest="mining 5/10\n",
                            status="healthy")
    for span in spans:
        span["attributes"]["input"] = render_prompt(
            {"system": _SYSTEM, "context": "c", "stage_context": stage_ctx, "trajectory": "",
             "look_invitation": "", "digest": "mining 5/10\n"})
    (run / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"seq": 1, "ts": 100.0, "type": "stage_finished",
         "data": {"node_id": 0, "name": "mine", "status": "ok", "exit_code": 0, "seconds": 80.0}},
        {"seq": 2, "ts": 100.1, "type": "stage_finished",
         "data": {"node_id": 0, "name": "train", "status": "fail", "exit_code": 1, "seconds": 6.0}},
        {"seq": 3, "ts": 101.0, "type": "node_failed", "data": {"node_id": 0, "reason": "crash"}},
    ]), encoding="utf-8")
    row = extract_run(run)[0]
    assert row["context"]["stage"] == "mine"
    assert row["label"]["label"] == LABEL_PRODUCTIVE
    assert row["label"]["label_basis"].startswith("stage_ok_node_failed_elsewhere")


def test_attempts_are_clustered_from_bursts_not_from_stage_windows(tmp_path):
    """`ts - seconds` is NOT a stage's window: the engine flushes every stage row of an eval attempt
    together at the end, so `mine`'s row can claim a start an hour before the burst while `train`'s
    claims one six seconds before it, both ending at the same instant. Placing a decision by that
    arithmetic put 32 of 168 decisions in the wrong attempt or none at all."""
    run = tmp_path / "r"
    run.mkdir()
    (run / "task.snapshot.json").write_text('{"direction": "max"}', encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"seq": 1, "ts": 5000.0, "type": "stage_finished",
         "data": {"node_id": 0, "name": "mine", "status": "ok", "seconds": 3600.0}},
        {"seq": 2, "ts": 5000.05, "type": "stage_finished",
         "data": {"node_id": 0, "name": "train", "status": "fail", "seconds": 6.0}},
        {"seq": 3, "ts": 9000.0, "type": "stage_finished",
         "data": {"node_id": 0, "name": "mine", "status": "reused", "seconds": 0.0}},
        {"seq": 4, "ts": 9000.05, "type": "stage_finished",
         "data": {"node_id": 0, "name": "train", "status": "ok", "seconds": 3000.0}},
    ]), encoding="utf-8")
    attempts = judge_corpus._stage_attempts(run / "events.jsonl")[0]
    assert [end for end, _ in attempts] == [5000.05, 9000.05]
    assert attempts[0][1]["train"][0] == "fail" and attempts[1][1]["train"][0] == "ok"
    # A decision at t=4000 sits INSIDE the first attempt even though `mine`'s claimed window
    # (1400..5000) and `train`'s (4994..5000) disagree about where it is.
    assert next(smap for end, smap in attempts if 4000.0 <= end)["train"][0] == "fail"


# ---------------------------------------------------------------- the committed dataset

@pytest.fixture(scope="module")
def dataset():
    return read_dataset(DEFAULT_DATASET)


def test_every_label_rederives(dataset):
    """THE anti-hand-edit guard. Runs with no `runs/` present, so it holds in CI and in a
    `git archive HEAD` tree: every stored label is recomputed from that row's own stored facts
    through the production rule, and a label edited by hand no longer matches."""
    for row in dataset["rows"]:
        fresh = rederive_label(row)
        assert fresh["label"] == row["label"]["label"], row["case_id"]
        assert fresh["label_basis"] == row["label"]["label_basis"], row["case_id"]


def test_vocabularies_stay_closed(dataset):
    assert set(LABELS) == {LABEL_WASTED, LABEL_PRODUCTIVE, LABEL_BUDGET_EXHAUSTED, LABEL_UNKNOWN}
    for row in dataset["rows"]:
        assert row["label"]["label"] in LABELS
        assert row["recorded"]["status"] in VERDICTS or row["recorded"]["status"] is None
        assert row["schema"] == DATASET_SCHEMA
    assert len({r["case_id"] for r in dataset["rows"]}) == len(dataset["rows"])


def test_bench_verdicts_match_the_production_schema():
    """A registry guard, resolved from the real `Literal` rather than from a substring: if
    `TrainingVerdict.status` grows a fourth value the bench is silently no longer measuring the
    judge it claims to measure."""
    from looplab.engine.train_monitor import TrainingVerdict
    annotation = TrainingVerdict.model_fields["status"].annotation
    assert set(typing.get_args(annotation)) == set(VERDICTS)


def test_bench_stage_statuses_match_the_production_registry():
    """The same guard for the STAGE vocabulary, and it was missing while the copy was wrong.

    `judge_corpus`'s set predated `runtime/command_eval.py::STAGE_STATUSES` and carried `"error"` —
    which `_run_stages` mints nowhere — while omitting `needs_failed` and `env_unsupported`, which
    it does. Those two therefore fell through to `LABEL_UNKNOWN`/`stage_status_unknown` instead of
    `LABEL_WASTED`, dropping attempts the engine refused before spawning anything out of the
    early-stop bench's saveable-hours denominator.

    A copy is still the right shape here (see `VERDICTS`' own note: a bench that moves when
    production moves cannot detect that it moved). What a copy may not be is WRONG.
    """
    from looplab.judgebench.judge_corpus import (STAGE_FAILED_STATUSES, STAGE_OK_STATUSES,
                                                 STAGE_TIMEOUT_STATUSES)
    from looplab.runtime.command_eval import STAGE_STATUSES

    bench = STAGE_FAILED_STATUSES | STAGE_OK_STATUSES | STAGE_TIMEOUT_STATUSES
    assert bench == set(STAGE_STATUSES), (
        f"the bench classifies a different vocabulary than the engine mints: "
        f"only in bench {sorted(bench - set(STAGE_STATUSES))}, "
        f"only in production {sorted(set(STAGE_STATUSES) - bench)}")
    assert not (STAGE_FAILED_STATUSES & STAGE_OK_STATUSES), "the three classes must partition"
    assert not (STAGE_FAILED_STATUSES & STAGE_TIMEOUT_STATUSES)
    assert "timeout" not in STAGE_FAILED_STATUSES, (
        "`timeout` is deliberately its own label class — see the module docstring")


def test_the_bench_reads_the_SAME_TRACEBACK_LINE_production_does():
    """Two regexes for "the last exception line", drifted on three clauses.

    `judge_corpus._TERMINAL_EXCEPTION` sets the bench's `terminal_exception` label and ranks which
    tool reads get stored; `failure_diagnosis._HEADLINE_RE` decides what the Developer is SHOWN.
    Production widened for the indented-traceback case (a launcher indenting each child's traceback
    inside its own report block) and for any bracketed stream tag, and accepts `…Interrupt`; the
    bench anchored at the line start, allowed only `[rank\\d+]: `, and accepted `…Exit`. So the
    bench could not see the headline the diagnostician was shown on exactly the case production was
    widened for.

    Still a COPY, on `VERDICTS`' argument — a bench that moves when production moves cannot detect
    that it moved — so this asserts AGREEMENT rather than identity, and states the one deliberate
    difference.
    """
    from looplab.engine.failure_diagnosis import _HEADLINE_RE
    from looplab.judgebench.triage_corpus import _TERMINAL_EXCEPTION

    for line in ("ValueError: plain",
                 "    torch.OutOfMemoryError: CUDA out of memory",   # the indented traceback
                 "[rank0]: RuntimeError: ddp collective failed",     # the torchrun tag
                 "[worker-3]: KeyError: 'k'",                        # any bracketed tag
                 "KeyboardInterrupt: stopped"):
        assert bool(_TERMINAL_EXCEPTION.findall(line)) == bool(_HEADLINE_RE.findall(line)), line
        assert _TERMINAL_EXCEPTION.findall(line), f"and both must MATCH it: {line}"

    assert not _TERMINAL_EXCEPTION.findall("retrying after ValueError happened"), (
        "both stay ANCHORED: a line that merely mentions an error name is not a terminal line")
    assert not _HEADLINE_RE.findall("retrying after ValueError happened")

    # THE ONE DELIBERATE DIFFERENCE: `SystemExit: 2` is a definite crash for LABELLING and is not a
    # headline worth pushing to a Developer, so the bench keeps `Exit` and production does not.
    assert _TERMINAL_EXCEPTION.findall("SystemExit: 2")
    assert not _HEADLINE_RE.findall("SystemExit: 2")


def test_bench_non_eval_reasons_are_reasons_something_MINTS():
    """A label that can never match is a bucket that stays empty while reading as coverage.

    Both `cancelled` and `idea_rejected_pre_eval` were dead: nothing writes either, and the real
    word is `idea_rejected`. Resolved from `ENGINE_TERMINAL_REASONS` rather than by grepping for the
    string, so a spelling that exists only in prose cannot satisfy this."""
    from looplab.core.models import ENGINE_TERMINAL_REASONS
    from looplab.judgebench.judge_corpus import NON_EVAL_FAILURE_REASONS

    unknown = sorted(set(NON_EVAL_FAILURE_REASONS) - set(ENGINE_TERMINAL_REASONS))
    assert not unknown, f"the bench classifies terminal reason(s) nothing mints: {unknown}"


def test_prompt_splits_round_trip_exactly(dataset):
    """The replay seam. If the split and the render are not inverses, a "changed prompt" replay is
    measuring a prompt nobody can reconstruct.

    **This test asserted `render_prompt(row["prompt"]) == messages_of(row)` until 2026-08-20, and
    that is `f(x) == f(x)`**: `messages_of` returns `render_prompt(row["prompt"])` for any row with
    no `messages` key, and the assertion two lines above pins that ALL 450 rows have none. It could
    not fail for any input the committed corpus can contain, so the property it names had never been
    tested. The original recorded messages are deliberately not stored (156 KB of byte-identical
    copy), so the falsifiable form is the ROUND TRIP through the production splitter: re-render each
    row's ingredients and split the result again, and every ingredient must come back byte-identical
    with `prompt_split_exact` still true. That fails the moment either half's block order, separator
    or header drifts from the other — which is the drift that would silently re-point the seam.
    """
    exact = 0
    for row in dataset["rows"]:
        if not row["prompt"]["prompt_split_exact"]:
            assert row.get("messages"), row["case_id"]     # inexact rows MUST keep the original
            continue
        exact += 1
        assert "messages" not in row, row["case_id"]       # and exact rows must not duplicate it
        # A NEW ingredient is additive with a reader-side default, exactly as an event field is
        # (CLAUDE.md invariant 5): `contract` shipped 2026-08-20 and no preserved row carries it, so
        # the re-split legitimately gains that key with an EMPTY value. Every key the row does carry
        # must still come back byte-identical, and a new key that came back NON-empty would mean the
        # splitter had carved an ingredient out of one of them — which is the drift this guards.
        resplit = judge_corpus._split_prompt(render_prompt(row["prompt"]))
        assert {k: resplit[k] for k in row["prompt"]} == row["prompt"], row["case_id"]
        assert not any(resplit[k] for k in resplit if k not in row["prompt"]), row["case_id"]
        assert messages_of(row) == render_prompt(row["prompt"]), row["case_id"]
        assert row["prompt"]["digest"], row["case_id"]
    assert exact == len(dataset["rows"])


def test_the_split_is_not_vacuously_exact(dataset):
    """`prompt_split_exact` must be capable of being FALSE, and `messages_of` must honour it.

    The row above asserts the flag is true 450 times; without this, a splitter that hard-coded
    `True` would satisfy it. Driven with a message the ingredients provably cannot reproduce.
    """
    # A well-formed TWO-message prompt whose head does not round-trip: the blocks are emitted with
    # a trailing blank line and this one has none, so re-rendering adds it. Deliberately not a
    # malformed message — `_split_prompt` returns False from an early guard for those, which never
    # reaches the re-join, and a control that stops short of the line it is about proves nothing.
    lossy = [{"role": "system", "content": "S"},
             {"role": "user", "content": "CTX" + judge_corpus._TAIL_HEADER + "loss: 1.0\n"
              + judge_corpus._TAIL_FOOTER}]
    inexact = judge_corpus._split_prompt(lossy)
    assert render_prompt(inexact) != lossy, "the control must actually be a lossy split"
    assert inexact["prompt_split_exact"] is False
    # ...and a row carrying that flag is replayed from the STORED messages, never re-rendered.
    stored = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    row = {"prompt": dict(inexact), "messages": stored}
    assert messages_of(row) == stored


def test_the_replay_seam_still_renders_the_ENGINES_prompt():
    """The round trip above is self-consistent; this is what stops it being self-consistent about
    the wrong prompt. `render_prompt` must reproduce what `_training_verdict` actually sends, or the
    bench replays a message the judge has never been asked.
    """
    from looplab.engine.train_monitor import TrainingMonitorMixin

    sent = []

    class _Client:
        def complete_tool(self, messages, schema):
            sent.append(messages)
            return {"status": "healthy", "reason": "r", "confidence": 0.5}

    class _Host(TrainingMonitorMixin):
        class developer:
            client = _Client()

    parts = {"system": "SYS", "context": "CTX", "stage_context": "STAGE", "trajectory": "TRAJ",
             "look_invitation": "LOOK", "digest": "loss: 1.0\n"}
    _Host()._training_verdict(parts["digest"], parts["context"], parts["stage_context"],
                              parts["trajectory"], tools=None)
    engine_messages = sent[-1]
    # `_training_verdict` splices `_MONITOR_SYSTEM` and `_LOOK_INVITATION` itself, so compare with
    # the ingredients it actually used — what must match is the ASSEMBLY, not those two constants.
    from looplab.engine.train_monitor import _MONITOR_SYSTEM
    assert render_prompt({**parts, "system": _MONITOR_SYSTEM, "look_invitation": ""}) == \
        engine_messages


def test_the_declared_contract_is_its_OWN_ingredient_not_part_of_trajectory():
    """The contract block (shipped 2026-08-20) sits between the stage identity and the trajectory.

    It needs its own name because `llm_candidate(overrides=...)` benches a prompt change by
    REPLACING one ingredient: if the split folded the contract into `trajectory`, every trajectory
    A/B would silently also delete the contract and the number would be about a prompt nobody
    proposed. Driven end to end through the real `_training_verdict`, with a negative control — the
    same message WITHOUT a contract must split to `contract == ""` and re-render byte for byte, so a
    splitter that always claimed one would go red here too.
    """
    from looplab.engine.train_monitor import TrainingMonitorMixin, _MONITOR_SYSTEM
    from looplab.judgebench.judge_corpus import CONTRACT_PREFIX, _split_prompt

    sent = []

    class _Client:
        def complete_tool(self, messages, schema):
            sent.append(messages)
            return {"status": "healthy", "reason": "r", "confidence": 0.5}

    class _Host(TrainingMonitorMixin):
        class developer:
            client = _Client()

    # The REAL producer, not a hand-written string: what must hold is that the block the engine
    # actually emits is the one the splitter can name, and both HALVES of the declaration have to
    # produce it — a header that appeared only when an `assert` was present would leave a
    # files-only stage's contract silently folded into `trajectory`, which is the exact drift this
    # ingredient exists to prevent and which no round-trip assertion can see.
    from looplab.engine.train_monitor import StageDeclaration, stage_contract_context
    for declaration in (StageDeclaration(assertion="all 15 epochs completed"),
                        StageDeclaration(files=("x/final/model.safetensors",)),
                        StageDeclaration(assertion="all 15 epochs completed",
                                         files=("x/final/model.safetensors",))):
        assert stage_contract_context(declaration, "").startswith(CONTRACT_PREFIX)
    contract = stage_contract_context(
        StageDeclaration(assertion="all 15 epochs completed", files=("x/final/model.safetensors",)),
        "")
    _Host()._training_verdict("loss: 1.0\n", "CTX",
                              "This is the live log of pipeline stage 'train' (stage 1 of 2; "
                              "the pipeline is train -> score).",
                              "TRAJ", None, contract_text=contract)
    parts = _split_prompt(sent[-1])
    assert parts["prompt_split_exact"] is True
    assert parts["contract"] == contract
    assert parts["trajectory"] == "TRAJ"           # the contract did NOT leak into it
    assert render_prompt(parts) == sent[-1]

    _Host()._training_verdict("loss: 1.0\n", "CTX",
                              "This is the live log of pipeline stage 'train' (stage 1 of 2; "
                              "the pipeline is train -> score).",
                              "TRAJ", None)
    bare = _split_prompt(sent[-1])
    assert bare["prompt_split_exact"] is True
    assert bare["contract"] == ""
    assert bare["trajectory"] == "TRAJ"
    assert render_prompt(bare) == sent[-1]
    # ...and the two messages differ by exactly the block, which is what ADDITIVE has to mean here.
    assert sent[-2][1]["content"].replace(contract + "\n\n", "") == sent[-1][1]["content"]


def test_corpus_limits_travel_with_the_artifact(dataset):
    """A caveat that lives only in a doc is a caveat nobody reading the number sees."""
    assert dataset["header"]["limits"] == CORPUS_LIMITS
    report = score.score_dataset(dataset["rows"][:5], score.recorded_candidate)
    text = score.format_report(report)
    assert CORPUS_LIMITS in text
    assert text.index(CORPUS_LIMITS) < text.index("accuracy")


# ---------------------------------------------------------------- redaction (driven, not pinned)

def test_a_secret_in_a_recorded_log_is_masked_by_the_shared_rule(synthetic_run):
    """Driven, because the preserved corpus happens to contain no credential shape at all — a
    fixpoint check over it would pass whether or not the extractor redacts anything."""
    spans = [json.loads(line) for line in
             (synthetic_run / "spans.jsonl").read_text(encoding="utf-8").splitlines()]
    leak = "api_key=AKIAIOSFODNN7EXAMPLEKEY"
    for span in spans:
        if span["attributes"]["phase_span"] == "p1":
            span["attributes"]["input"][1]["content"] += "\n" + leak
    (synthetic_run / "spans.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    row = _by_node(extract_run(synthetic_run))[1]
    blob = json.dumps(row)
    assert "AKIAIOSFODNN7EXAMPLEKEY" not in blob
    assert "api_key" in blob                                # the field name survives; the value does not


def test_stored_text_is_a_fixpoint_of_the_shared_redactor(dataset):
    """Applied ONCE, with the same rule persisted tails already use — so re-applying changes
    nothing. This is what says the committed file went through the screen rather than around it."""
    from looplab.core.redact import redact_output_tail
    for row in dataset["rows"][:80]:
        for text in (row["prompt"]["digest"], row["prompt"]["context"], row["recorded"]["reason"]):
            assert redact_output_tail(text, entropy=True) == text, row["case_id"]


# ---------------------------------------------------------------- the scorer

def _always(status):
    return lambda row: status


def test_the_two_agreements_are_never_merged(dataset):
    """Accuracy and churn answer different questions. A candidate that says `broken` every time
    catches every wasted decision AND stops every productive one — high on one, ruinous on the
    other — and no field may average them away."""
    rows = dataset["rows"]
    report = score.score_dataset(rows, _always("broken"))
    assert report.false_stop > 0 and report.missed_stop == 0
    assert report.true_continue == 0
    assert report.recorded_agreement_rate is not None
    assert report.recorded_agreement_rate < 1.0
    assert not any("combined" in f or "overall" in f for f in vars(report))
    fields = set(vars(report))
    assert {"false_stop", "missed_stop", "recorded_agreement"} <= fields


def test_no_label_means_no_accuracy_number_not_a_zero():
    """For a judge whose verdict has no recoverable outcome — the novelty gate rejects an idea, so
    the idea is never run — the honest answer is `None`. A 0.0 would be charted as "always wrong"."""
    unlabelled = [{"case_id": "x", "label": {"label": LABEL_UNKNOWN},
                   "recorded": {"status": "healthy"}, "context": {}, "provenance": {}}]
    report = score.score_dataset(unlabelled, score.recorded_candidate)
    assert report.label_accuracy is None
    assert report.label_coverage == 0
    assert report.recorded_agreement_rate == 1.0          # consistency still measurable, and it is
    assert "NOT accuracy" in score.format_report(report)   # labelled as what it is


def test_budget_exhausted_never_enters_the_accuracy_denominator(dataset):
    """60 of 450 decisions watched a stage that timed out. The compute was wasted, but the judge's
    own system prompt tells it a slow-but-progressing run is `watch`, so charging those as missed
    stops would penalise it for obeying its instructions — and would move the headline."""
    timeouts = [r for r in dataset["rows"] if r["label"]["label"] == LABEL_BUDGET_EXHAUSTED]
    assert timeouts, "the corpus must still exercise this class"
    report = score.score_dataset(timeouts, score.recorded_candidate)
    assert report.label_coverage == 0
    assert (report.true_stop, report.missed_stop, report.false_stop, report.true_continue) == (
        0, 0, 0, 0)
    assert report.by_label[LABEL_BUDGET_EXHAUSTED] == len(timeouts)
    assert LABEL_BUDGET_EXHAUSTED not in score.PRIMARY_LABELS


def test_an_unanswered_row_is_not_scored_as_healthy(dataset):
    """A candidate that fails to parse has NOT said the run is fine."""
    report = score.score_dataset(dataset["rows"][:10], lambda row: None)
    assert report.answered == 0 and len(report.unanswered) == 10
    assert report.label_coverage == 0 and report.recorded_coverage == 0


def test_attempts_are_the_unit_not_nodes(dataset):
    """Keyed by node, a node whose train failed three times and then worked collapses into one
    `wasted` row covering every decision including the ones that watched the attempt that worked."""
    attempts = score.per_attempt_report(dataset["rows"], score.recorded_candidate)
    keys = [k for k in attempts if k.startswith("e5small-dr-unified-v2:n1:")]
    assert len({attempts[k]["label"] for k in keys}) == 2, keys
    totals = score.attempt_totals(attempts)
    assert totals["wasted_attempts"] + totals["productive_attempts"] == len(attempts)


# ---------------------------------------------------------------- the incumbent's pinned baseline

def test_the_incumbent_baseline_is_what_a_candidate_is_read_against(dataset):
    """These are the numbers every candidate is compared to, so they are pinned deliberately.

    A change here is NOT a test to update — it means the corpus moved, and a bench whose baseline
    moved silently is a bench that will be believed while measuring something else. Re-derive the
    dataset, re-argue the numbers, then edit this.
    """
    report = score.score_dataset(dataset["rows"], score.recorded_candidate)
    assert report.rows == 450
    assert report.label_coverage == 354
    assert (report.true_stop, report.false_stop, report.missed_stop, report.true_continue) == (
        53, 5, 101, 195)
    assert report.recorded_agreement_rate == 1.0     # the incumbent replaying itself, by definition
    totals = score.attempt_totals(score.per_attempt_report(dataset["rows"],
                                                           score.recorded_candidate))
    assert (totals["wasted_caught"], totals["wasted_attempts"]) == (7, 27)
    assert (totals["productive_falsely_stopped"], totals["productive_attempts"]) == (3, 49)


# The committed artefact's UNCOMPRESSED bytes. A tripwire, and it is honest about being one: an
# editor can update this line as easily as they can edit the file. What it buys is that the edit
# CANNOT BE INVISIBLE — a 278 KB binary changing on its own reads as a rebuild, a binary changing
# beside this constant reads as what it is — and that it costs nothing to check on a machine with no
# `runs/`. It is deliberately over the DECOMPRESSED bytes: gzip output is not stable across zlib
# versions, and a pin that goes red on a library upgrade is a pin people learn to overwrite.
#
# It is the WEAKEST of the three anti-hand-edit guards and is listed last for that reason. The real
# ones are `test_every_label_rederives` (every label recomputed from the row's own facts through the
# production rule) and `test_the_committed_corpus_is_internally_derived` below (every field the
# label rule does NOT read, re-derived from the extractor's own construction rules).
_CORPUS_SHA256 = "7ec0d709337ca5d9fa2b835881710dd0962268295e7d17b78b0d3b4fbbabd5d5"
_CORPUS_BYTES = 3_976_740


def test_the_committed_corpus_is_internally_derived(dataset):
    """THE guard that replaces an always-skipping one, and it runs everywhere.

    Until 2026-08-20 the only check on how this 278 KB committed binary was DERIVED was
    `test_the_dataset_regenerates_from_the_runs_it_names`, which needs `runs/` — gitignored, and
    `LOOPLAB_BENCH_RUNS` set nowhere — so **it had never once executed**, and nothing in CI could
    tell a legitimately derived corpus from a hand-edited one. A skip that is unreachable in CI is
    not a guard.

    What CAN be checked without the source runs is that every row still satisfies the JOINS the
    extractor built it from. These are not one constant somebody can bump: each is a different field
    re-derived from a different other field, so a hand edit has to forge all of them consistently.

    * `case_id` is `<run>:n<node_id>:<phase_span>` — edit the provenance and the key stops matching;
    * `system_prompt_sha256` is the sha of the row's own stored `system` — edit the prompt and it
      breaks, which is the field the replay seam is entirely about;
    * `llm_calls` is the length of `span_ids` — the collapse from 3,950 spans to 450 decisions is
      the corpus's central claim and this is the arithmetic behind it;
    * `tools_available` is `bool(look_invitation)`, the join the whole tools A/B rests on;
    * rows are sorted by `(run, ts, case_id)` and the header's per-run counts are the real ones.
    """
    rows = dataset["rows"]
    assert len(rows) == dataset["header"]["rows"] == 450
    seen_ts = None
    for row in rows:
        prov, case = row["provenance"], row["case_id"]
        assert case == "%s:n%s:%s" % (prov["run"], prov["node_id"], prov["phase_span"]), case
        assert prov["system_prompt_sha256"] == hashlib.sha256(
            (row["prompt"]["system"] or "").encode("utf-8")).hexdigest()[:16], case
        assert len(prov["span_ids"]) == prov["llm_calls"] >= 1, case
        assert bool(prov["tools_available"]) is bool(row["prompt"]["look_invitation"]), case
        key = (prov["run"], prov["ts"], case)
        assert seen_ts is None or key >= seen_ts, case      # the extractor's own final sort
        seen_ts = key
    counted = collections.Counter(r["provenance"]["run"] for r in rows)
    assert {s["run"]: s["rows"] for s in dataset["header"]["sources"]} == dict(counted)
    # and the header cannot claim a judge/schema the rows do not carry
    assert {r["judge"] for r in rows} == {dataset["header"]["judge"]}
    assert {r["schema"] for r in rows} == {DATASET_SCHEMA}


def test_the_committed_artifact_has_not_been_edited_in_place():
    """The tripwire (see `_CORPUS_SHA256`). Runs with no `runs/`; a rebuild updates both lines."""
    raw = gzip.open(DEFAULT_DATASET, "rb").read()
    assert len(raw) == _CORPUS_BYTES
    assert hashlib.sha256(raw).hexdigest() == _CORPUS_SHA256, (
        "the committed corpus changed. If you REBUILT it (`python -m looplab.judgebench extract`), "
        "update _CORPUS_SHA256/_CORPUS_BYTES in the same commit and re-argue every pinned baseline "
        "in this file. If you did not, something edited a derived artefact in place.")


def test_the_dataset_regenerates_from_the_runs_it_names(tmp_path, dataset):
    """The strongest half, and the one that needs the source corpus: on a machine that HAS the runs,
    rebuilding the exact sources the header names must reproduce the file byte for byte.

    It SKIPS without them, and the skip is not the guarantee — that is what the two tests above are
    for. Point `LOOPLAB_BENCH_RUNS` at a directory holding the runs the header names to run it.
    """
    import os
    import pathlib
    # The corpus is not in the repository, and in an agent worktree it is not beside the tests
    # either, so the location is overridable rather than assumed.
    root = pathlib.Path(os.environ.get("LOOPLAB_BENCH_RUNS")
                        or pathlib.Path(__file__).resolve().parents[1] / "runs")
    sources = [root / s["run"] for s in dataset["header"]["sources"]]
    missing = [p.name for p in sources if not (p / "spans.jsonl").exists()]
    if missing:
        pytest.skip("runs/ not present (%s) — this half needs the source corpus; the derivation "
                    "and tripwire guards above run without it" % ", ".join(missing))
    rebuilt = write_dataset(build_dataset(sources), tmp_path / "rebuilt.jsonl.gz")
    assert gzip.open(rebuilt, "rb").read() == gzip.open(DEFAULT_DATASET, "rb").read()


# ------------------------------------------------- the GATE: a verdict is not an intervention

def test_the_gate_is_opt_in_and_moves_nothing_when_absent(dataset):
    """Every number above is an UNGATED number and must stay one. The gate changes what a stop
    COUNTS AS, so a gate that leaked into the default would silently re-target every A/B."""
    rows = dataset["rows"]
    plain = score.score_dataset(rows, score.recorded_candidate)
    assert plain.gate is None
    assert (plain.true_stop, plain.false_stop, plain.missed_stop, plain.true_continue) == (
        53, 5, 101, 195)
    assert "GATED" not in score.format_report(plain)


def test_the_engine_would_not_have_made_any_of_the_five_false_stops(dataset):
    """THE reason the gate exists. All five decisions that called `broken` on a run which finished
    fine sit at confidence 0.62-0.75, below the shipped `train_monitor_kill_confidence` of 0.8 —
    so the number the bench headlines is a property of the VERDICT and not of the engine, and a
    prompt change read against it is being scored on the expensive side against the wrong target.

    Pinned like the baseline it qualifies: a change here means the corpus moved, not that a test
    needs updating.
    """
    rows = dataset["rows"]
    gated = score.score_dataset(rows, score.recorded_candidate, gate=score.Gate())
    assert gated.false_stop == 0
    assert gated.true_stop == 49            # the bar costs four of the 53 true stops
    assert gated.missed_stop == 105
    totals = score.attempt_totals(
        score.per_attempt_report(rows, score.recorded_candidate, score.Gate()))
    assert (totals["wasted_caught"], totals["wasted_attempts"]) == (6, 27)
    assert totals["productive_falsely_stopped"] == 0
    assert "GATED" in score.format_report(gated)


def test_the_measured_trajectory_veto_is_inert_on_this_corpus(dataset):
    """A conjunct that changes no number here, stated rather than assumed.

    144 of 144 rows carrying a measured `descending` curve were judged `healthy`, so the veto never
    meets a `broken` to refuse — and the four v6 false stops it reads as its motivating case carry
    no measured trajectory at all, because `LossTrajectoryTracker` postdates those runs. The
    confidence bar is what would have stopped them.
    """
    rows = dataset["rows"]
    vetoed = [r for r in rows if score.trajectory_vetoes(r)]
    assert vetoed, "the corpus must still exercise a descending measured curve"
    assert {r["recorded"]["status"] for r in vetoed} == {"healthy"}
    with_veto = score.score_dataset(rows, score.recorded_candidate, gate=score.Gate())
    without = score.score_dataset(rows, score.recorded_candidate,
                                  gate=score.Gate(trajectory_veto=False))
    assert (with_veto.true_stop, with_veto.false_stop) == (without.true_stop, without.false_stop)


def test_a_gate_refuses_an_answer_it_cannot_weigh_rather_than_scoring_it_as_calm(dataset):
    """A candidate that says `broken` and reports no confidence has not said the run is safe.

    Scoring it as "did not stop" would give a candidate a perfect false-stop record for withholding
    the one field the gate weighs — the same defect the `unanswered` branch exists to stop one field
    over, where an out-of-vocabulary answer used to score exactly like `healthy`.
    """
    rows = dataset["rows"][:40]
    with pytest.raises(ValueError, match="no confidence"):
        score.score_dataset(rows, _always("broken"), gate=score.Gate())
    # ...but a bare non-stop answer needs no confidence, because no conjunct can bind on it.
    calm = score.score_dataset(rows, _always("healthy"), gate=score.Gate())
    assert calm.answered == len(rows) and calm.false_stop == 0


def test_the_gate_reads_the_engines_own_confidence_rule(dataset):
    """`_normalize_monitor_confidence`, not `float(x) >= t`. 19 of the 450 recorded confidences are
    STRINGS the model emitted (`'0.9'`), and a non-finite one must fail closed rather than compare
    True — `min(1.0, nan)` is 1.0 in Python, which is how that trap is spelled in the engine."""
    from looplab.engine.train_monitor import _normalize_monitor_confidence

    row = {"case_id": "x", "prompt": {}}
    gate = score.Gate()
    assert gate.stops(row, "broken", "0.9") is True          # the string form the model emits
    assert gate.stops(row, "broken", float("nan")) is False  # never authority
    assert gate.stops(row, "broken", 0.79) is False
    assert gate.stops(row, "watch", 0.99) is False           # `watch` is not a stop at any bar
    assert _normalize_monitor_confidence("0.9") == (0.9, True)
    strings = [r for r in dataset["rows"]
               if isinstance(r["recorded"]["confidence"], str)]
    assert len(strings) == 19, "the corpus must still exercise the string form"


def test_the_live_arm_asks_over_the_same_evidence_and_answers_what_a_gate_needs():
    """`score.llm_candidate` is the paid arm, driven here with a fake client so its PLUMBING is a
    red test rather than something only a spend can check.

    Two properties, and both were load-bearing when the arm was first run for real (450 calls,
    docs/guide/judge-bench.md): an `overrides` swap must re-render from the row's own stored
    ingredients rather than replaying the original message — which is what makes "the same rows
    with the tool affordance removed" a real A/B — and the answer must carry the `confidence` and
    the `fault`, because one paid pass has to serve the ungated score, the gated one, and the only
    measurement of `fault` this corpus can ever supply.
    """
    seen = []

    class _Client:
        def complete_tool(self, messages, schema):
            seen.append(messages)
            return {"status": "broken", "fault": "implementation", "confidence": 0.91,
                    "reason": "the objective cannot descend as written"}

    row = {"case_id": "c", "prompt": {"system": "S", "context": "C", "stage_context": "SC",
                                      "trajectory": "", "look_invitation": "LOOK",
                                      "digest": "loss: 1.0\n", "prompt_split_exact": True}}
    plain = score.llm_candidate(_Client())(row)
    assert plain == {"status": "broken", "confidence": 0.91, "fault": "implementation"}
    assert "LOOK" in seen[-1][1]["content"]

    stripped = score.llm_candidate(_Client(), overrides={"look_invitation": ""})(row)
    assert stripped["status"] == "broken"
    assert "LOOK" not in seen[-1][1]["content"]          # the affordance really left the prompt
    assert "loss: 1.0" in seen[-1][1]["content"]         # over the SAME recorded evidence

    # ...and a row that did not split exactly is refused rather than answered from the original.
    unsplit = dict(row, prompt=dict(row["prompt"], prompt_split_exact=False))
    assert score.llm_candidate(_Client(), overrides={"system": "X"})(unsplit) is None

    # the answer is gate-ready without a second pass
    assert score.Gate().stops(row, plain["status"], plain["confidence"]) is True
