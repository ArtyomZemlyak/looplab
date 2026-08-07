"""The per-stage SUCCESS CONTRACT and the stage ROLLBACK — the two halves of "make a stage's
success mean something".

THE INCIDENT BOTH EXIST FOR, because every test below is calibrated against it and not against a
hypothetical. On the live run `runs/rubert-dr-0807` a model-invented `mine` stage (mine hard
negatives, declared `check: true`, running before `train`) crashed once, was repaired, and then:

    unique queries: 764676, unique docs: 30761
    saved 9364 queries with hard negatives -> ./hard_negs.pkl        <- exit 0

It succeeded by its own definition: it wrote the file and exited 0. It had produced hard negatives
for 9,364 of 764,676 queries — 1.2% — and `train` was then handed `--n_hard_negatives 4` as if every
query had them. Nothing objected, because LoopLab checked exactly two things: the exit code, and an
LLM sanity read of the log tail whose prompt correctly FORBIDS quality judgements.

So the tests are grouped by which half of that they attack:
  * `expect.files` — the technical half. Would NOT have caught this one (the file existed and was
    non-empty), and is tested here anyway because it catches the STALE artifact, which the persistent
    repair workdir makes plausible on every repaired node.
  * `expect.assert` — the half that would. Declared in the MANIFEST, not the script, so it is
    declarable in both repo-task modes: the Developer-authored scorer and the operator-PROTECTED one.
  * `_rollback_start` — the Developer's way to say "the stage that broke is not the stage that is
    wrong", with the five rungs that stop it re-running an expensive stage on a wrong guess.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from looplab.runtime.command_eval import (MAX_STAGE_EXPECT_FILES, STAGE_EXPECT_KEYS,
                                          _call_stage_check, materialized_stages,
                                          run_command_eval, validate_stages,
                                          verify_stage_artifacts)

_M = {"kind": "stdout_json", "key": "metric"}


# --------------------------------------------------------------------------------------------
# expect: AUTHORING (the shared validator both ends of the manifest handshake go through)
# --------------------------------------------------------------------------------------------

def test_a_well_formed_expect_survives_the_shared_validator_and_the_manifest_round_trip():
    """`expect` has to reach the RUNNER, and it crosses two boundaries to get there: the
    `declare_stages` tool validates it at authoring time and `materialized_stages` re-reads it out of
    `looplab_stages.json` at consume time. Both go through `validate_stages`, so a contract one side
    records must be the contract the other enforces — the alternative is a stage advertising a
    condition nothing checks, which is the defect this field exists to end, one level up."""
    stage = {"name": "mine", "command": ["python", "mine_hard_negs.py"], "timeout": 14400.0,
             "check": True,
             "expect": {"files": ["./hard_negs.pkl"],
                        "assert": "hard negatives mined for at least 90% of the 764676 queries"}}
    clean, err = validate_stages([stage])
    assert err is None
    # `./` is stripped, so the runner's `_confined` join and the declaration agree on one spelling.
    assert clean[0]["expect"] == {
        "files": ["hard_negs.pkl"],
        "assert": "hard negatives mined for at least 90% of the 764676 queries"}
    # and it survives the JSON manifest the Developer actually writes
    round_tripped = materialized_stages(json.loads(json.dumps({"stages": clean})))
    assert round_tripped[0]["expect"] == clean[0]["expect"]


@pytest.mark.parametrize("expect, needle", [
    ({"fles": ["x.pkl"]}, "unknown key"),
    ({"files": ["/etc/passwd"]}, "must be RELATIVE"),
    ({"files": ["../../escape.pkl"]}, "escapes the eval workdir"),
    ({"files": "hard_negs.pkl"}, "must be a list"),
    ({"files": [f"f{i}.pkl" for i in range(MAX_STAGE_EXPECT_FILES + 1)]}, "at most"),
    ({"assert": 17}, "must be a one-line string"),
    ({}, "declares nothing"),
    ("hard_negs.pkl", "must be an object"),
])
def test_a_malformed_expect_is_refused_with_a_reason_the_declarer_can_act_on(expect, needle):
    """Every refusal names the fix, because these strings are bounced straight back to the model by
    `declare_stages`' validate hook and a rejection it cannot act on costs a whole authoring round.

    The UNKNOWN-KEY case is the one that is a judgement call rather than hygiene: a dropped `"asserts"`
    typo would leave the stage looking, to anyone auditing the manifest, like it carries a contract
    while nothing enforces it. Silently ignoring it re-creates the original defect one field over."""
    clean, err = validate_stages([{"name": "s", "command": ["a"], "expect": expect}])
    assert clean is None
    assert needle in err, err


def test_the_expect_key_set_is_closed_and_the_validator_is_what_closes_it():
    """The registry and its enforcement are the same fact here — an added key that nothing reads is
    exactly what the closed set prevents, so the set must be what the refusal is derived from."""
    for key in STAGE_EXPECT_KEYS:
        clean, err = validate_stages([{"name": "s", "command": ["a"],
                                       "expect": {key: ["f.pkl"] if key == "files" else "one line"}}])
        assert err is None, f"{key} is in the registry but the validator refuses it: {err}"
        assert key in clean[0]["expect"]


def test_a_stage_without_expect_is_byte_identical_to_what_it_always_was():
    """Additive by construction: every existing manifest, every operator `cmd.stages`, and every
    golden replay must produce the exact same clean stage dict as before the field existed."""
    clean, err = validate_stages([{"name": "train", "command": ["python", "train.py"],
                                   "timeout": 60.0, "check": True}])
    assert err is None
    assert clean == [{"name": "train", "command": ["python", "train.py"],
                      "timeout": 60.0, "check": True}]
    assert "expect" not in clean[0]


# --------------------------------------------------------------------------------------------
# expect.files: VERIFICATION, driven through a real pipeline
# --------------------------------------------------------------------------------------------

def _prep_stage(tmp_path, body: str, expect: dict):
    (tmp_path / "prep.py").write_text(body, encoding="utf-8")
    (tmp_path / "score.py").write_text(
        "import json; print(json.dumps({'metric': 1.0}))\n", encoding="utf-8")
    return [{"name": "prep", "command": [sys.executable, "prep.py"], "expect": expect},
            {"name": "score", "command": [sys.executable, "score.py"]}]


def test_a_stage_that_exits_0_without_producing_its_declared_artifact_fails_the_pipeline(tmp_path):
    """THE PROPERTY, in its simplest form: exit 0 is not a claim anyone has to accept.

    Driven end-to-end through `run_command_eval` rather than by calling the verifier, because what
    has to hold is that the PIPELINE stops — the next stage must not run on the missing input, the
    failure must be attributed to `prep` (which is what routes the repair at the right file), and no
    metric may be read. A verifier that returns the right string while the pipeline runs on would
    pass a unit test and change nothing."""
    stages = _prep_stage(tmp_path, "print('mined everything, honest')\n",
                         {"files": ["hard_negs.pkl"]})
    res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 30, _M, stages=stages)
    assert res.failed_stage == "prep"
    assert res.metric is None, "a stage that produced nothing still reached the metric read"
    assert [s["name"] for s in res.stages] == ["prep"], "the next stage ran on a missing input"
    assert res.stages[0]["status"] == "expect_failed"
    assert "hard_negs.pkl" in res.stderr and "did NOT produce" in res.stderr


def test_an_empty_declared_artifact_fails_the_stage_even_though_it_exists(tmp_path):
    """0 bytes passes an existence check and carries nothing — the shape a writer that crashed after
    `open(p,"wb")` and had its exception swallowed leaves behind. This is why the BACKLOG's
    existence-only sketch is not the whole of the technical half."""
    stages = _prep_stage(tmp_path, "open('hard_negs.pkl','wb')\n", {"files": ["hard_negs.pkl"]})
    res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 30, _M, stages=stages)
    assert res.failed_stage == "prep"
    assert "EMPTY" in res.stderr


def test_a_stale_artifact_from_an_earlier_attempt_fails_the_stage(tmp_path):
    """THE ONE THAT MATTERS ON A REPAIRED NODE. The workdir deliberately persists across repair
    attempts so a completed `train` can be reused — which is exactly what makes a leftover artifact
    plausible here. A stage that "succeeds" without rewriting its output hands the next stage a
    previous attempt's (or a foreign experiment's) file, and `_safe_reuse_start` cannot see it:
    that predicate bounds the stages the engine SKIPS, never the ones it RE-RUNS.

    Driven with a real prior file and a real stage that does not touch it, not by faking an mtime."""
    stale = tmp_path / "hard_negs.pkl"
    stale.write_text("a previous attempt's negatives", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    stages = _prep_stage(tmp_path, "print('reusing the existing negatives, saves time')\n",
                         {"files": ["hard_negs.pkl"]})
    res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 30, _M, stages=stages)
    assert res.failed_stage == "prep"
    assert "NOT written by this run" in res.stderr


def test_a_stage_that_really_produces_its_declared_artifact_passes_and_the_pipeline_continues(tmp_path):
    """The other direction, which is the one a false-positive contract would break: a stage doing its
    job must be invisible to all of this. Includes a declared DIRECTORY (a checkpoint dir, a
    `save_pretrained` output), which is the shape a files-only reading would fail."""
    stages = _prep_stage(
        tmp_path,
        "import os\n"
        "open('hard_negs.pkl','w').write('negatives')\n"
        "os.makedirs('ckpt', exist_ok=True)\n"
        "open('ckpt/model.pt','w').write('weights')\n",
        {"files": ["hard_negs.pkl", "ckpt"]})
    res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 30, _M, stages=stages)
    assert res.failed_stage is None
    assert res.metric == 1.0
    assert [s["status"] for s in res.stages] == ["ok", "ok"]


def test_an_empty_declared_directory_fails_the_stage(tmp_path):
    """A `makedirs` that ran before the code that fills it leaves a directory that exists and holds
    nothing — the directory-shaped twin of the 0-byte file."""
    stages = _prep_stage(tmp_path, "import os; os.makedirs('ckpt', exist_ok=True)\n",
                         {"files": ["ckpt"]})
    res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 30, _M, stages=stages)
    assert res.failed_stage == "prep"
    assert "EMPTY" in res.stderr


def test_a_declared_artifact_that_escapes_the_workdir_is_refused_at_verification_too(tmp_path):
    """`_validate_expect` refuses an escaping path at AUTHORING time, but the manifest is not the only
    way a path escapes: a candidate can plant a symlink out of the sandbox at eval time, which no
    static check can see. The verifier re-decides containment against the real workdir with
    `_confined`, so the runner never stats a file outside the node."""
    outside = tmp_path.parent / "outside.pkl"
    outside.write_text("not mine", encoding="utf-8")
    wd = tmp_path / "wd"
    wd.mkdir()
    try:
        (wd / "linked.pkl").symlink_to(outside)
    except (OSError, NotImplementedError):            # pragma: no cover - symlink-less filesystem
        pytest.skip("symlinks unavailable here")
    problem = verify_stage_artifacts({"files": ["linked.pkl"]}, str(wd), None, stage="prep")
    assert problem and "does not resolve inside the eval workdir" in problem


def test_freshness_is_measured_from_the_stage_start_not_the_eval_start(tmp_path):
    """A stage that re-runs on attempt 3 must rewrite its artifact on attempt 3. Anchoring freshness
    at the EVAL start would let the artifact an EARLIER stage of the SAME eval wrote satisfy a later
    stage's declaration, which is the exact confusion the contract is meant to remove."""
    p = tmp_path / "out.pkl"
    p.write_text("x", encoding="utf-8")
    st = p.stat().st_mtime
    assert verify_stage_artifacts({"files": ["out.pkl"]}, str(tmp_path), st - 60) is None
    problem = verify_stage_artifacts({"files": ["out.pkl"]}, str(tmp_path), st + 600)
    assert problem and "NOT written by this run" in problem
    # No start time => the freshness rung degrades away and the rest still holds. An operator path
    # with no clock must not lose existence checking too.
    assert verify_stage_artifacts({"files": ["out.pkl"]}, str(tmp_path), None) is None


# --------------------------------------------------------------------------------------------
# expect.assert: the half that sees 1.2%
# --------------------------------------------------------------------------------------------

def test_a_declared_assertion_reaches_the_checker_and_can_fail_a_stage_that_exited_0(tmp_path):
    """THE 1.2% CASE, reconstructed. `mine` prints its coverage, exits 0, and writes a real non-empty
    file — so the exit code, the existing sanity checker and the whole technical half of the contract
    all pass it. The only thing that can object is a checker holding the stage's OWN declared
    condition, and this drives exactly that: the checker sees the declared sentence next to the log
    and fails the pipeline before `train` is handed the 1.2%."""
    seen: list = []

    def checker(stage, tail, expect=""):
        seen.append((stage, expect, tail))
        if expect and "9364" in tail:
            return "declared >=90% coverage; the log reports 9364 of 764676 (1.2%)"
        return None

    (tmp_path / "mine.py").write_text(
        "open('hard_negs.pkl','w').write('negs')\n"
        "print('unique queries: 764676, unique docs: 30761')\n"
        "print('saved 9364 queries with hard negatives -> ./hard_negs.pkl')\n", encoding="utf-8")
    (tmp_path / "train.py").write_text(
        "import json; print(json.dumps({'metric': 0.9}))\n", encoding="utf-8")
    stages = [{"name": "mine", "command": [sys.executable, "mine.py"],
               "expect": {"files": ["hard_negs.pkl"],
                          "assert": "hard negatives for at least 90% of the 764676 queries"}},
              {"name": "train", "command": [sys.executable, "train.py"]}]
    res = run_command_eval([sys.executable, "train.py"], str(tmp_path), 30, _M,
                           stages=stages, check_fn=checker)
    assert res.failed_stage == "mine", "the stage that manufactured 1.2% of the signal passed"
    assert res.metric is None
    assert [s["name"] for s in res.stages] == ["mine"], "train ran on the 1.2%"
    assert res.stages[0]["status"] == "check_failed"
    assert seen and seen[0][1].startswith("hard negatives for at least 90%")


def test_a_declared_assertion_reaches_the_checker_without_check_true(tmp_path):
    """A stage that states its own success condition must be HELD to it. Requiring `check: true`
    alongside would mean a declared contract is silently never enforced — the original defect, one
    field over — so the declaration itself opens the gate."""
    calls: list = []
    (tmp_path / "prep.py").write_text("open('o.pkl','w').write('x')\nprint('done')\n", encoding="utf-8")
    (tmp_path / "score.py").write_text("import json; print(json.dumps({'metric': 1}))\n",
                                       encoding="utf-8")
    stages = [{"name": "prep", "command": [sys.executable, "prep.py"],
               "expect": {"files": ["o.pkl"], "assert": "every row prepared"}},
              {"name": "score", "command": [sys.executable, "score.py"]}]
    assert "check" not in validate_stages(stages)[0][0]
    run_command_eval([sys.executable, "score.py"], str(tmp_path), 30, _M, stages=stages,
                     check_fn=lambda s, t, expect="": calls.append((s, expect)) or None)
    assert calls == [("prep", "every row prepared")]


def test_a_stage_with_only_check_true_still_reaches_the_checker_with_no_contract(tmp_path):
    """The historical path, unchanged: `check: true` alone is still the contract-FREE sanity read
    whose prompt forbids quality judgements. Narrowing that ban is opt-in per stage, via a declared
    condition — never something a stage acquires by upgrading."""
    calls: list = []
    (tmp_path / "prep.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "score.py").write_text("import json; print(json.dumps({'metric': 1}))\n",
                                       encoding="utf-8")
    stages = [{"name": "prep", "command": [sys.executable, "prep.py"], "check": True},
              {"name": "score", "command": [sys.executable, "score.py"]}]
    run_command_eval([sys.executable, "score.py"], str(tmp_path), 30, _M, stages=stages,
                     check_fn=lambda s, t, expect="": calls.append((s, expect)) or None)
    assert calls == [("prep", "")]


def test_a_two_argument_checker_is_still_called_with_two_arguments():
    """NON-VACUITY OF THE ARITY INSPECTION. `check_fn` is a duck-typed callback with doubles across
    the suite and an operator-replaceable factory behind it, and the call site sits inside an
    `except Exception` that SWALLOWS the failure and returns None. So calling three-arg
    unconditionally would not go red — every two-arg checker would go on being called and silently
    never fire again, which is the "silent narrowing" shape rather than a broken test.

    Driven by handing `_call_stage_check` a callable that would RAISE on a third argument."""
    got: list = []

    def two_arg(stage, tail):
        got.append((stage, tail))
        return None

    assert _call_stage_check(two_arg, "prep", "log", "a declared condition") is None
    assert got == [("prep", "log")]

    def three_arg(stage, tail, expect):
        got.append(expect)
        return "concern"

    assert _call_stage_check(three_arg, "prep", "log", "a declared condition") == "concern"
    assert got[-1] == "a declared condition"

    def kwarg_only(stage, tail, *, expect=""):
        got.append(("kw", expect))
        return None

    _call_stage_check(kwarg_only, "prep", "log", "cond")
    assert got[-1] == ("kw", "cond")

    # No declaration => the two-arg call, whoever the callee is: a stage using only `check: true`
    # reaches the checker exactly as it always did.
    def strict_two(stage, tail):
        got.append("strict")
        return None

    assert _call_stage_check(strict_two, "prep", "log", "") is None
    assert got[-1] == "strict"


def test_the_checker_prompt_carries_the_declared_condition_and_narrows_the_ban_only_then():
    """The checker's quality-judgement ban is load-bearing (it once failed the run's BEST model), so
    the narrowing has to be visible in the bytes and has to be CONDITIONAL. Driven against a real
    client that records the messages, both with and without a declaration."""
    from looplab.engine.eval_stages import EvalStagesMixin

    class _Client:
        def __init__(self):
            self.seen = []

        def complete_text(self, msgs, **kw):
            self.seen.append(msgs)
            return "OK"

    class _Eng(EvalStagesMixin):
        _eval_spec = {"metric": {"kind": "stdout_json", "key": "recall@100"}}

        def __init__(self, client):
            self._c = client

        def _reflect_client(self):
            return self._c

        def _idea_text(self, idea):
            return "mine harder negatives"

    c = _Client()
    check = _Eng(c)._stage_check_fn(None)
    assert check("mine", "saved 9364 of 764676", "at least 90% coverage") is None
    system, user = c.seen[-1][0]["content"], c.seen[-1][1]["content"]
    assert EvalStagesMixin.STAGE_CONTRACT_CLAUSE in system
    assert "DECLARED CONDITION for stage 'mine': at least 90% coverage" in user
    # The ban itself must SURVIVE the narrowing — the clause separates the two questions, it does
    # not relax the first one.
    assert "Do NOT judge result QUALITY or RANKING" in system

    check("mine", "saved 9364 of 764676")
    system_plain, user_plain = c.seen[-1][0]["content"], c.seen[-1][1]["content"]
    assert EvalStagesMixin.STAGE_CONTRACT_CLAUSE not in system_plain
    assert "DECLARED CONDITION" not in user_plain


# --------------------------------------------------------------------------------------------
# ROLLBACK
# --------------------------------------------------------------------------------------------

def _rollback_engine(tmp_path, protected=()):
    """A bare mixin instance over a real workdir: `_rollback_start` reads only `_repo_spec` and the
    filesystem, which is the point of it being a stated rule rather than a branch inside the attempt
    loop — the truth table below is reachable without driving an evaluation."""
    from looplab.engine.eval_stages import EvalStagesMixin

    class _Eng(EvalStagesMixin):
        _repo_spec = {"protected_names": list(protected)}

    (tmp_path / "mine.py").write_text("import helpers\n", encoding="utf-8")
    (tmp_path / "helpers.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "train.py").write_text("pass\n", encoding="utf-8")
    return _Eng()


_STAGES = [{"name": "mine", "command": ["python", "mine.py"]},
           {"name": "train", "command": ["python", "train.py"]}]


def test_rollback_is_accepted_when_the_repair_changed_the_suspect_stage(tmp_path):
    """THE ACCEPT CASE, and the shape the contract is: name it AND fix it. The repair edited
    `mine.py`, so re-running `mine` will produce different bytes; the engine restarts the pipeline
    from there, discarding the 1.2% file and everything built on it."""
    eng = _rollback_engine(tmp_path)
    start, refusal = eng._rollback_start(_STAGES, "train", "mine", {"mine.py"}, str(tmp_path))
    assert (start, refusal) == ("mine", None)


def test_rollback_is_accepted_when_the_repair_changed_something_the_suspect_stage_imports(tmp_path):
    """Reachability, not just the entry script: a fix to a module the stage imports changes what the
    stage does. The SAME closure `_safe_reuse_start` uses, read in the opposite direction — there an
    intersection FORBIDS reuse, here an empty one forbids the re-run."""
    eng = _rollback_engine(tmp_path)
    start, refusal = eng._rollback_start(_STAGES, "train", "mine", {"helpers.py"}, str(tmp_path))
    assert (start, refusal) == ("mine", None)


def test_rollback_to_an_unknown_stage_is_refused_and_the_pipeline_is_named(tmp_path):
    """A typo must NOT fall through to `_run_stages`' unmatched-name behaviour (a full re-run from
    stage 0). That fallback is correct for REUSE, where running more is the fail-safe direction; for
    a rollback the fail-safe direction is the opposite, so this refuses and says what exists."""
    eng = _rollback_engine(tmp_path)
    start, refusal = eng._rollback_start(_STAGES, "train", "myne", {"mine.py"}, str(tmp_path))
    assert start is None
    assert "no such stage" in refusal and "mine, train" in refusal


@pytest.mark.parametrize("suspect", ["train", "score"])
def test_rollback_to_the_failed_stage_or_a_later_one_is_refused(tmp_path, suspect):
    """Rolling back to the stage that just failed is the ordinary retry, and to a later one is
    nonsense. Neither is worth the cost of discarding completed work."""
    eng = _rollback_engine(tmp_path)
    stages = _STAGES + [{"name": "score", "command": ["python", "score.py"]}]
    start, refusal = eng._rollback_start(stages, "train", suspect, {"mine.py"}, str(tmp_path))
    assert start is None
    assert "not EARLIER" in refusal


def test_rollback_to_a_stage_already_rolled_back_to_is_refused(tmp_path):
    """THE ANTI-THRASH RUNG. One re-run per suspect per node bounds the whole feature by the
    pipeline's LENGTH rather than by a budget: whatever the Developer believes, a node can spend at
    most one re-run per stage over its entire life."""
    eng = _rollback_engine(tmp_path)
    start, refusal = eng._rollback_start(_STAGES, "train", "mine", {"mine.py"}, str(tmp_path),
                                         already_rolled_back={"mine"})
    assert start is None
    assert "already re-run it once" in refusal


def test_rollback_is_refused_when_the_repair_changed_nothing_the_suspect_stage_reaches(tmp_path):
    """Re-running a stage on byte-identical inputs and code costs its full runtime for nothing. This
    is what makes the request a CONTRACT ("name it and fix it") rather than a hope."""
    eng = _rollback_engine(tmp_path)
    start, refusal = eng._rollback_start(_STAGES, "train", "mine", {"train.py"}, str(tmp_path))
    assert start is None
    assert "changed nothing that stage reads or runs" in refusal


def test_rollback_to_a_protected_stage_names_the_operators_file_instead_of_asking_for_a_fix(tmp_path):
    """THE PROTECTED-SCORER MODE. The repo-task workflow has two: the scorer is usually authored by
    the Developer, but an operator can PROTECT it, and then the write gate refuses the agent every
    edit to it. In that mode the "change it first" rung can never be satisfied, so the refusal has to
    name the operator's file rather than read as "try harder" — otherwise the Developer spends every
    remaining attempt trying to edit a file it is structurally forbidden to touch.

    This composes with the `_unrunnable_protected_scripts` preflight rather than fighting it: that
    one refuses BEFORE the pipeline runs when a protected script is ABSENT, this one refuses a re-run
    of a protected script that is present but unfixable. Both end in the same place — the operator is
    the only participant who can act."""
    eng = _rollback_engine(tmp_path, protected=("mine.py",))
    start, refusal = eng._rollback_start(_STAGES, "train", "mine", {"train.py"}, str(tmp_path))
    assert start is None
    assert "PROTECTED" in refusal and "mine.py" in refusal
    assert "operator has to change" in refusal
    assert "NOT something you can fix" in refusal


def test_an_opaque_or_remapped_stage_allows_the_rollback_because_nothing_can_be_proven(tmp_path):
    """Where the closure cannot bound the stage (`python -m pkg`, a shell wrapper) or the eval runs
    under a non-default `cwd` (changed-file keys and script paths resolve against different bases),
    the no-change rung cannot PROVE nothing changed. It allows — fail-open here, bounded by the
    once-per-stage rung and by the retrain cap the caller charges.

    That is the mirror image of `_safe_reuse_start`'s treatment of the same blind spot, and
    deliberately so: a wrong reuse silently scores a stale model, a wrong re-run only spends time."""
    eng = _rollback_engine(tmp_path)
    opaque = [{"name": "mine", "command": ["python", "-m", "pkg.mine"]},
              {"name": "train", "command": ["python", "train.py"]}]
    assert eng._rollback_start(opaque, "train", "mine", {"train.py"}, str(tmp_path))[0] == "mine"
    assert eng._rollback_start(_STAGES, "train", "mine", {"train.py"}, str(tmp_path),
                               cwd="sub")[0] == "mine"
    # A changed NON-.py file is invisible to the import closure too, so an empty intersection does
    # not prove the stage's inputs are unchanged.
    assert eng._rollback_start(_STAGES, "train", "mine", {"config.yaml"}, str(tmp_path))[0] == "mine"


def test_no_rollback_asked_answers_nothing_rather_than_refusing(tmp_path):
    """The overwhelmingly common case has to be free and silent: an empty ask is not a refusal, so it
    neither spends an allowance nor puts text in front of the next repair."""
    eng = _rollback_engine(tmp_path)
    assert eng._rollback_start(_STAGES, "train", "", {"train.py"}, str(tmp_path)) == (None, None)
    assert eng._rollback_start([], "train", "mine", {"train.py"}, str(tmp_path)) == (None, None)


def test_a_renamed_failed_stage_refuses_rather_than_re_running_everything(tmp_path):
    """A repair that renames the failing stage loses its position, and with the failure's index
    unknown we cannot show the suspect precedes it. This is the rung whose false ACCEPT costs a full
    re-run of everything from the suspect onward, so it fails closed."""
    eng = _rollback_engine(tmp_path)
    start, refusal = eng._rollback_start(_STAGES, "fit", "mine", {"mine.py"}, str(tmp_path))
    assert start is None and "not EARLIER" in refusal


# --------------------------------------------------------------------------------------------
# ROLLBACK: the accounting and the durable bound
# --------------------------------------------------------------------------------------------

def test_a_rollback_is_charged_to_the_same_ledger_a_forced_full_retrain_is():
    """`_repair_forces_full_retrain` gained a fourth condition instead of rollback getting an
    accounting of its own. The existing three cannot see it: an accepted rollback leaves `next_start`
    SET (to the suspect), so `next_start is None` is False and the historical rule reads it as free —
    which is measurably wrong, since a rollback discards at least one completed stage plus everything
    after it. A second counter would also let a Developer alternate rollback / full-retrain and pay
    neither cap."""
    from looplab.engine.evaluate import _repair_forces_full_retrain

    class _Res:
        failed_stage = "train"
        stages = [{"name": "mine"}, {"name": "train"}]

    res = _Res()
    # the historical truth table, unchanged
    assert _repair_forces_full_retrain(res, None) is True
    assert _repair_forces_full_retrain(res, "train") is False
    # and the rollback, which the historical rule would have let through for free
    assert _repair_forces_full_retrain(res, "mine", rolled_back=True) is True


def test_the_once_per_stage_bound_is_read_back_off_the_event_log():
    """A bound a resume refunds is not a bound — and this one guards hours of GPU time, so a restarted
    node must not re-offer every stage it has already re-run. Only ACCEPTED rows count: a refusal cost
    nothing, and holding it against the node would spend the allowance on the engine's own decision
    and make a typo'd stage name permanently un-nameable."""
    from looplab.engine.evaluate import _durable_rollbacks
    from looplab.events.types import EV_STAGE_ROLLBACK

    class _E:
        def __init__(self, type, data):
            self.type, self.data = type, data

    events = [
        _E(EV_STAGE_ROLLBACK, {"node_id": 1, "generation": 0, "stage": "mine", "accepted": True}),
        _E(EV_STAGE_ROLLBACK, {"node_id": 1, "generation": 0, "stage": "prep", "accepted": False}),
        _E(EV_STAGE_ROLLBACK, {"node_id": 2, "generation": 0, "stage": "other", "accepted": True}),
        _E(EV_STAGE_ROLLBACK, {"node_id": 1, "generation": 7, "stage": "reset", "accepted": True}),
        _E("node_repaired", {"node_id": 1, "generation": 0, "stage": "noise", "accepted": True}),
    ]
    assert _durable_rollbacks(events, 1, 0) == {"mine"}
    # a node_reset starts a fresh allowance, keyed exactly as the fold keys a generation
    assert _durable_rollbacks(events, 1, 7) == {"reset"}
    assert _durable_rollbacks([], 1, 0) == set()


def test_the_attempt_loop_actually_consults_the_rollback_rule_and_records_its_answer():
    """The rule above is inert unless the LOOP calls it and the ONCE-PER-STAGE bound is inert unless
    the loop WRITES the row it reads back.

    AST, not substrings (`called_names` resolves real `ast.Call` nodes, so a commented-out call does
    not satisfy it) — and honest about what tier 3 proves: that the calls are present in
    `_evaluate`'s text, not that they executed. Reaching this behaviourally needs a real sandboxed
    multi-stage evaluation that fails at a LATER stage, a Developer that answers with a
    `rollback_stage`, and a second Engine over the same run dir; the rule and both ledger readers are
    pure and fully driven above, so what is left uncovered is the wiring, which is what this pins."""
    from _source_scan import called_names
    from looplab.engine.orchestrator import Engine

    names = called_names(Engine._evaluate)
    # `called_names` resolves the DOTTED spelling, so the mixin method is `self._rollback_start` —
    # asserting the bare name would silently pass on nothing.
    assert "self._rollback_start" in names, "the attempt loop never asks the rollback rule"
    assert "_durable_rollbacks" in names, "the once-per-stage bound is not seeded from the log"
    # and the row the bound reads back must be written
    import inspect
    src = inspect.getsource(Engine._evaluate)
    assert "EV_STAGE_ROLLBACK" in src


def test_the_rollback_event_is_diagnostic_so_no_reader_keys_on_its_position():
    """Invariant #1's real question for a non-folded event is not "does the fold read it?" but "does
    any reader key on its position?". `_proposal_authority_seq` excludes DIAGNOSTIC_EVENTS wholesale,
    which is what makes this append splice-neutral — a property of the READERS, not of the event, so
    membership is the thing to assert."""
    from looplab.events.replay import _HANDLERS
    from looplab.events.types import DIAGNOSTIC_EVENTS, EV_STAGE_ROLLBACK

    assert EV_STAGE_ROLLBACK in DIAGNOSTIC_EVENTS
    assert EV_STAGE_ROLLBACK not in _HANDLERS


def test_the_repair_emit_is_the_only_one_that_offers_a_rollback():
    """A fresh build has no failed stage to blame, and a schema property that is inert wherever it
    appears is one a model will eventually fill in anyway. So the field rides a SEPARATE spec and the
    build path's `done` stays byte-identical."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper

    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    build = dev._emit_spec()["function"]["parameters"]["properties"]
    repair = dev._repair_emit_spec()["function"]["parameters"]["properties"]
    assert set(build) == {"summary"}
    assert set(repair) == {"summary", "rollback_stage"}
    desc = repair["rollback_stage"]["description"]
    # The two things a model must know at the moment of answering, in the text it actually reads.
    assert "must have EDITED that stage's script" in desc
    assert "ONE rollback per stage per node" in desc
