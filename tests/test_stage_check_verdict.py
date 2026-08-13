"""The inter-stage checker's VERDICT contract (doc 36 / doc 39 site #1).

What is being held: a stage dies only when the checker NAMES a physical failure from a closed
vocabulary. Prose, a quality comparison, an out-of-enum kind and an "I cannot tell" are all
`inconclusive`, and inconclusive does not fail anything.

Why it is worth a file: `stage_finished.status == "check_failed"` discarded 46.6 GPU-hours across 21
stages in `runs/` — more than every crash, timeout and artifact-contract failure put together, and
43 % of the 109 h that ever produced a usable metric. Two of the concerns that did it are replayed
below verbatim from the corpus.

These drive the property end-to-end through a REAL `run_command_eval` wherever the property is about
what the pipeline does, rather than pinning the source: the thing that must not regress is whether
the next stage RAN.
"""
from __future__ import annotations

import sys

import pytest

from looplab.engine.eval_stages import parse_stage_check_reply
from looplab.runtime.command_eval import (STAGE_CHECK_HARD_KINDS, STAGE_CHECK_INCONCLUSIVE,
                                          STAGE_CHECK_INCONCLUSIVE_KEY, STAGE_CHECK_UNSTRUCTURED,
                                          StageCheckVerdict, _stage_check_outcome,
                                          coerce_stage_check_kind, run_command_eval)

_M = {"kind": "stdout_json", "key": "metric"}

# Verbatim from `runs/`, with the stage-seconds each one discarded. These are the answers the old
# `startswith("OK")` rule read as "kill the node".
CORPUS_CONCERNS = [
    # rubertlite-dense-retrieval node 21 — the QUALITY comparison the prompt has banned twice.
    "Loss is not decreasing (stuck around 14.6), suggesting poor learning; validation recall (0.79) "
    "is below previous best (0.8491).",
    # rubertlite-dr-unified-v4 nodes 1/4/8 — an ABSENCE of evidence.
    "No output provided for stage 'prep', cannot confirm success.",
    # rubert-dr-0807 node 3 — 14.9 h. A metric judgement dressed as a mechanism claim.
    "FAIL: validation recall@100 is 0.0 (all thresholds 0.0) — model produced no meaningful "
    "retrieval, indicating a broken pipeline.",
]


# --------------------------------------------------------------------------------------------
# The rule, as a truth table
# --------------------------------------------------------------------------------------------

def test_ok_passes_and_an_empty_reply_passes():
    assert parse_stage_check_reply("OK", declared=False) is None
    assert parse_stage_check_reply("  ok  ", declared=False) is None
    assert parse_stage_check_reply("OK — checkpoint saved, loss fell", declared=False) is None
    assert parse_stage_check_reply("", declared=False) is None
    assert parse_stage_check_reply(None, declared=False) is None


@pytest.mark.parametrize("kind", [k for k in STAGE_CHECK_HARD_KINDS
                                  if k not in (STAGE_CHECK_UNSTRUCTURED,
                                               "declared_condition_violated")])
def test_every_named_physical_failure_still_kills_the_stage(kind):
    """The gate must keep working. A NaN loss is why this checker exists."""
    v = parse_stage_check_reply(f"FAIL {kind}: the log shows it", declared=False)
    assert v is not None and v.kind == kind
    assert _stage_check_outcome(v)[0] == kind != STAGE_CHECK_INCONCLUSIVE


@pytest.mark.parametrize("reply", CORPUS_CONCERNS)
def test_the_answers_that_burned_the_corpus_are_now_inconclusive(reply):
    """Not one of the three names a member of the vocabulary. The third opens with the literal word
    FAIL and still does not: `FAIL:` carries no kind, and "recall@100 is 0.0" is a statement about
    the RESULT, which is the search's job downstream and never this checker's."""
    v = parse_stage_check_reply(reply, declared=False)
    assert v is not None, "the doubt is still recorded"
    assert v.kind == STAGE_CHECK_INCONCLUSIVE
    assert v.concern, "and it still says what it saw"


def test_an_out_of_enum_kind_does_not_borrow_the_vocabulary_authority():
    for bad in ("FAIL bad_metric: below best", "FAIL quality: mediocre", "FAIL: something",
                "FAIL", "the stage looks wrong to me"):
        assert parse_stage_check_reply(bad, declared=False).kind == STAGE_CHECK_INCONCLUSIVE


def test_a_declared_condition_may_only_be_invoked_when_one_was_declared():
    """A checker may not invoke a contract that does not exist — an undeclared 'condition' is exactly
    what the banned comparison against a previous best looks like from the inside."""
    said = "FAIL declared_condition_violated: declares 90% coverage, log reports 9,364 of 764,676"
    assert parse_stage_check_reply(said, declared=True).kind == "declared_condition_violated"
    assert parse_stage_check_reply(said, declared=False).kind == STAGE_CHECK_INCONCLUSIVE


def test_the_model_cannot_mint_the_back_compat_kind():
    """`unstructured` is HARD and is minted only by `_stage_check_outcome` for a legacy string
    return. A model that emits the literal word must not inherit that branch's authority."""
    assert coerce_stage_check_kind(STAGE_CHECK_UNSTRUCTURED) == STAGE_CHECK_INCONCLUSIVE
    assert parse_stage_check_reply(f"FAIL {STAGE_CHECK_UNSTRUCTURED}: x",
                                   declared=True).kind == STAGE_CHECK_INCONCLUSIVE
    # …while the legacy shape itself is untouched.
    assert _stage_check_outcome("a bare concern string") == (STAGE_CHECK_UNSTRUCTURED,
                                                             "a bare concern string")
    assert _stage_check_outcome(None) == ("", "")
    assert _stage_check_outcome(StageCheckVerdict("crash", "")) == ("", ""), \
        "a verdict with nothing to say is not a failure"


# --------------------------------------------------------------------------------------------
# The effect, driven through a real pipeline
# --------------------------------------------------------------------------------------------

def _pipeline(tmp_path):
    (tmp_path / "prep.py").write_text("open('o.pkl','w').write('x')\n", encoding="utf-8")
    (tmp_path / "train.py").write_text(
        "import json; open('ran','w').write('1'); print(json.dumps({'metric': 0.9}))\n",
        encoding="utf-8")
    return [{"name": "prep", "command": [sys.executable, "prep.py"], "check": True},
            {"name": "train", "command": [sys.executable, "train.py"]}]


@pytest.mark.parametrize("reply", CORPUS_CONCERNS)
def test_an_inconclusive_checker_no_longer_discards_the_pipeline(tmp_path, reply):
    """THE PROPERTY, and the reason it is not a source pin: what must not regress is that `train`
    RAN and the run kept its metric. A quiet `prep` stage that wrote its artifact is not a failure
    because a model could not narrate it."""
    stages = _pipeline(tmp_path)
    res = run_command_eval([sys.executable, "train.py"], str(tmp_path), 60, _M, stages=stages,
                           check_fn=lambda s, t, expect="": parse_stage_check_reply(
                               reply, declared=bool(expect)))
    assert (tmp_path / "ran").exists(), "the next stage never ran"
    assert res.failed_stage is None
    assert res.metric == pytest.approx(0.9)
    assert [s["status"] for s in res.stages] == ["ok", "ok"]
    # The doubt is RECORDED, under a key of its own — `concern` means "this is why the stage FAILED"
    # to the repair loop and to metric_salvage, and this stage did not fail.
    assert res.stages[0][STAGE_CHECK_INCONCLUSIVE_KEY]
    assert "concern" not in res.stages[0]


def test_a_named_physical_failure_still_stops_the_pipeline(tmp_path):
    """The other half. Weakening the gate would be the expensive mistake, so it is driven too."""
    stages = _pipeline(tmp_path)
    res = run_command_eval([sys.executable, "train.py"], str(tmp_path), 60, _M, stages=stages,
                           check_fn=lambda s, t, expect="": parse_stage_check_reply(
                               "FAIL nan_or_inf_loss: loss=nan from step 40 onward", declared=False))
    assert not (tmp_path / "ran").exists(), "train ran on a stage the checker condemned"
    assert res.failed_stage == "prep"
    assert res.metric is None
    assert res.stages[0]["status"] == "check_failed"
    assert "nan_or_inf_loss" in res.stages[0]["concern"]


def test_a_legacy_string_checker_keeps_its_gate(tmp_path):
    """`check_fn` is a duck-typed callback with doubles across the suite and a library entry point
    behind it. Downgrading a bare string to advisory would silently retire the gate for every caller
    that never migrated — so the string path stays byte-for-byte what it was."""
    stages = _pipeline(tmp_path)
    res = run_command_eval([sys.executable, "train.py"], str(tmp_path), 60, _M, stages=stages,
                           check_fn=lambda s, t, expect="": "no checkpoint was written")
    assert res.failed_stage == "prep"
    assert res.stages[0]["status"] == "check_failed"
    assert not (tmp_path / "ran").exists()


# --------------------------------------------------------------------------------------------
# The RECORD side, which must not move at all
# --------------------------------------------------------------------------------------------

def test_an_inconclusive_stage_still_vetoes_salvage():
    """doc 36 corollary 2, driven: the DOING moves and the RECORDING does not.

    Before the vocabulary, a doubtful checker answer ended the pipeline at that stage, so such a
    node could never reach salvage. Now it can — so the doubt has to veto here instead, or widening
    the action space would have quietly widened the trusted set too.

    Note the doubt is on the FIRST row: an inconclusive stage is no longer terminal, so it is
    generally upstream of the row `status` describes, which is why the veto is a scan.
    """
    from looplab.engine.metric_salvage import (VETO_STAGE_KEYS, _stage_carries_veto_key,
                                               salvage_condition)

    class _Res:
        exit_code = 0
        timed_out = False
        stalled = False

        def __init__(self, stages):
            self.stages = stages

    clean = [{"name": "prep", "status": "ok"}, {"name": "train", "status": "fail"}]
    assert salvage_condition(_Res(clean), "crash") == "stage_failed"

    doubted = [{"name": "prep", "status": "ok", "check_inconclusive": "could not tell"},
               {"name": "train", "status": "fail"}]
    assert salvage_condition(_Res(doubted), "crash") is None, \
        "a stage nobody could vouch for contributed a number to the record"

    # …and the predicate itself, so the rule has a truth table rather than one scenario.
    assert _stage_carries_veto_key(doubted) is True
    assert _stage_carries_veto_key(clean) is False
    assert _stage_carries_veto_key([]) is False
    assert _stage_carries_veto_key(["not a dict"]) is False
    assert _stage_carries_veto_key([{"check_inconclusive": ""}]) is False, "an empty doubt is no doubt"
    assert VETO_STAGE_KEYS == {STAGE_CHECK_INCONCLUSIVE_KEY}
