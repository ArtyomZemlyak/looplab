"""The decisions `_evaluate`'s attempt loop used to make inline (doc 25 ES-03) — five from the
original split, plus the two the 2026-08-06 durability fix added (`_durable_repair_ledger`,
`_effective_repair_cap`; their truth tables live in `test_repair_stop_decision.py`, beside the
resume that drives them).

Each of these was a block of straight-line code in the middle of a 600-line `while`, and that is the
whole point of the file: to reach any branch of any of them you had to drive a real sandboxed
evaluation that failed in exactly the right way — a dead provider, a repair that answers with prose
three times, a later stage failing on a multi-GPU reservation. None of the suite's repair tests does
that, so the branches each of these rules was retrofitted to fix were, individually, unexercised.

Now they are functions with names and arguments, so the truth table IS the test. The end-to-end
repair tests (`test_inline_repair.py`, `test_repair_stop_decision.py`, `test_staged_eval.py`) still
prove the loop wires them together; this file proves each one answers correctly, and the last test
proves `_evaluate` still reaches all five (on the AST — a comment naming a rule is not a call).
"""
from __future__ import annotations

import ast
from types import SimpleNamespace

from _source_scan import called_names, function_tree
from looplab.core.models import DEVELOPER_ERROR_PREFIX
from looplab.engine.evaluate import (EvaluateMixin, _repair_change_set,
                                     _repair_forces_full_retrain, _repair_provider_failure)


def _res(**kw):
    """The subset of an eval result these rules read."""
    return SimpleNamespace(**{"stderr": "", "drift": None, "exit_code": 0, "timed_out": False,
                              "stages": [], "failed_stage": None, **kw})


# ------------------------------------------------------------------ was this a repair at all?

_PROSE = "Sure! Here is the fix you asked for."


def test_the_developer_sentinel_is_a_provider_failure():
    """The shape `adapters/repo_developer.py` produces when its OWN session failed. Committing it as
    the node's code is what made 2343 "repairs" on one node possible.

    The sentinel is also not valid Python, so it advances the unparseable counter on its way past —
    measured, and harmless: it is already terminal for the node, so the counter it leaves behind is
    never read again. Asserted rather than left implicit because the ORDER (count, then classify)
    is what makes it true, and the extraction had to keep that order."""
    sentinel = f"{DEVELOPER_ERROR_PREFIX} 402 out of credits)"
    dev_err, count = _repair_provider_failure("print(1)", sentinel, {}, [], 0)
    assert dev_err is not None and "402" in dev_err
    assert dev_err.startswith(DEVELOPER_ERROR_PREFIX), (
        "the sentinel's own text is the diagnosis — not the generic not-Python message, which would "
        "hide a dead endpoint behind 'the repair returned something that is not valid Python'")
    assert count == 1


def test_prose_that_parses_is_still_not_a_repair():
    """A comment-only or docstring-only answer PARSES, so the eval exits 0 with no metric and the
    node used to terminalize as `no_metric` — telling the operator the command printed no metric
    about a provider that is dead, with no pause."""
    dev_err, _count = _repair_provider_failure("print(1)", "# just a comment\n", {}, [], 0)
    assert dev_err is not None and "no executable code" in dev_err


def test_one_truncation_is_not_a_provider_verdict_but_three_are():
    """`unparseable` keeps today's behaviour of committing the artifact and letting the next eval's
    SyntaxError inform the next repair — which is how a TRUNCATED generation recovers. The verdict
    arrives only once a repair call has answered with something that is not Python
    `_UNPARSEABLE_REPAIR_LIMIT` times ON ONE NODE, which is why the count round-trips."""
    count = 0
    verdicts = []
    for _attempt in range(3):
        dev_err, count = _repair_provider_failure("print(1)", "def f(:\n", {}, [], count)
        verdicts.append(dev_err)
    assert count == 3, "the unparseable counter must advance once per not-Python answer"
    assert verdicts[0] is None and verdicts[1] is None, (
        "a truncated generation must be allowed to recover — stopping on one is a regression")
    assert verdicts[2] is not None and "3x" in verdicts[2]


def test_the_counter_is_what_stops_it_not_the_error_text():
    """Stated separately because the deleted implementation inferred this from the SyntaxError text,
    which carries a varying provider request id and so looked new every time. Feed three DIFFERENT
    unparseable answers: the verdict must still arrive on the third."""
    count = 0
    for text in ("def f(:  # req-a\n", "def g(:  # req-b\n", "def h(:  # req-c\n"):
        dev_err, count = _repair_provider_failure("print(1)", text, {}, [], count)
    assert dev_err is not None and count == 3


def test_a_multi_file_repair_is_never_judged_on_its_whole_file_artifact():
    """A repo/multi-file repair returns "" for `code` and carries its work in `files`/`deleted`, so
    the whole-file question is not asked at all — asking it would call every repo repair a provider
    failure."""
    assert _repair_provider_failure("print(1)", "", {"a.py": "print(2)"}, [], 0) == (None, 0)
    assert _repair_provider_failure("print(1)", "", {}, ["a.py"], 0) == (None, 0)


def test_a_node_with_no_code_of_its_own_had_no_artifact_to_defect():
    assert _repair_provider_failure("", _PROSE, {}, [], 0) == (None, 0)
    assert _repair_provider_failure("   \n ", _PROSE, {}, [], 0) == (None, 0)


# ------------------------------------------------------------------ the repair's REAL change set

def test_only_files_whose_content_moved_are_changed():
    """`developer.last_files` is the node's whole cumulative solution for the repo developer, so a
    raw key set would always intersect the train stage and defeat checkpoint reuse."""
    changed, new_deleted = _repair_change_set(
        {"train.py": "old", "score.py": "same"}, set(),
        {"train.py": "old", "score.py": "same", "extra.py": "new"}, [])
    assert changed == {"extra.py"}
    assert new_deleted == []


def test_a_deletion_that_predates_this_repair_cannot_veto_stage_reuse():
    """The cumulative read is the defect: `repaired_deleted` is seeded from `node.deleted` at
    repair_from, so blocking on it would permanently disable stage reuse for any node whose
    implement ever deleted a file."""
    changed, new_deleted = _repair_change_set(
        {}, {"old.py"}, {}, ["old.py"])
    assert new_deleted == [] and changed == set()


def test_this_repairs_own_deletion_does_veto_it():
    changed, new_deleted = _repair_change_set(
        {}, {"old.py"}, {}, ["old.py", "just_now.py"])
    assert new_deleted == ["just_now.py"]
    assert changed == {"just_now.py"}, "a deletion is a change to the reachability closure"


# ------------------------------------------------------- which repairs count against the cap

def test_a_first_stage_failure_is_an_ordinary_retry():
    """Nothing completed, so a full re-run discards nothing — bounded by the attempt budget like any
    other retry, NOT by the retrain cap."""
    assert not _repair_forces_full_retrain(_res(stages=[{"name": "train"}], failed_stage="train"),
                                           None)


def test_a_single_command_eval_never_consumes_the_retrain_cap():
    assert not _repair_forces_full_retrain(_res(stages=[], failed_stage=None), None)


def test_a_later_stage_failure_that_refuses_reuse_forces_a_full_retrain():
    later = _res(stages=[{"name": "train"}, {"name": "score"}], failed_stage="score")
    assert _repair_forces_full_retrain(later, None)
    assert not _repair_forces_full_retrain(later, "score"), (
        "reuse was granted, so no completed earlier-stage work is discarded")


def test_a_renamed_later_stage_still_consumes_the_cap():
    """Leaving the renamed case uncounted let a stage-renaming repair burn unlimited full trains.

    Deliberately NOT a separate branch, and saying so is the point: the rule is judged from the
    PRE-repair `res.stages` alone, so a repair that renames or drops the failed stage is
    indistinguishable here from one that did not — which is exactly the property. What makes that
    true is the SIGNATURE (there is no post-repair stage list to consult), and what keeps it true is
    the argument pin in the last test of this file; this is the scenario it protects."""
    assert "stages" not in _repair_forces_full_retrain.__code__.co_varnames[:2], (
        "the rule must take the eval RESULT, not a stage list a caller could resolve post-repair")
    renamed = _res(stages=[{"name": "prep"}, {"name": "train"}, {"name": "gone"}],
                   failed_stage="gone")
    assert _repair_forces_full_retrain(renamed, None)


# ------------------------------------------------------------- the node's account of the failure

class _TextHost:
    """The two engine attributes `_eval_failure_text` reads."""

    def __init__(self, spec=None):
        self._eval_spec = spec

    def _redact(self, text: str) -> str:
        return text


def _failure_text(res, spec=None) -> str:
    return EvaluateMixin._eval_failure_text(_TextHost(spec), res)


def test_a_real_stderr_tail_is_the_diagnosis_verbatim():
    assert _failure_text(_res(stderr="Traceback...\nZeroDivisionError\n")) == (
        "Traceback...\nZeroDivisionError\n")


def test_blank_but_truthy_stderr_takes_the_fallback():
    """`"  \\n \\t "` is truthy, so it used to survive the `or` and become the node's WHOLE
    diagnosis: the repair prompt, the `node_repaired.error_in` audit row and the terminal's `error`
    field were all whitespace."""
    text = _failure_text(_res(stderr="  \n \t "))
    assert text.strip(), "a whitespace stderr must not become the node's whole diagnosis"
    assert "no_metric" in text


def test_a_drift_report_outranks_the_no_metric_hint():
    text = _failure_text(_res(stderr="", drift={"reported": 1.0, "recomputed": 0.1}))
    assert text.startswith("metric drift:")


def test_the_no_metric_hint_names_the_key_the_eval_actually_reads():
    """The terse "no_metric" gave the repair agent nothing to fix, so the debug node just re-ran and
    failed again. The hint has to carry the CONFIGURED key, not a generic one."""
    text = _failure_text(_res(stderr=""), spec={"metric": {"key": "auc", "kind": "stdout_json"}})
    assert "'auc'" in text and "print(json.dumps(" in text


def test_a_non_stdout_metric_reader_gets_the_other_hint():
    text = _failure_text(_res(stderr=""), spec={"metric": {"key": "auc", "kind": "file"}})
    assert "check the eval's metric reader" in text and "print(json.dumps(" not in text


# ---------------------------------------------- a repair may refine, never grow onto a sibling GPU

class _FootprintHost:
    def __init__(self, gpu_mem=None):
        self._gpu_mem = gpu_mem or {}

    def _clamp_resource_footprint(self, footprint):
        return dict(footprint)          # the pool envelope is not what this rule decides


def _footprint(proposed, reservation, gpu_mem=None, code="", files=None):
    node = SimpleNamespace(idea=SimpleNamespace(footprint=proposed))
    return EvaluateMixin._repaired_footprint(
        _FootprintHost(gpu_mem), node, code, files or {}, reservation)


def test_an_undeclared_idea_stays_undeclared():
    assert _footprint(None, {"pin": True, "count": 2}) is None


def test_a_cpu_only_reservation_zeroes_the_repairs_gpu_demand():
    assert _footprint({"gpus": 4}, {"cpu_only": True})["gpus"] == 0


def test_a_pinned_reservation_clamps_the_repair_to_the_devices_already_held():
    assert _footprint({"gpus": 8}, {"pin": True, "count": 2})["gpus"] == 2
    # …and a repair that shrinks its demand keeps the smaller number: this clamps, never raises.
    assert _footprint({"gpus": 1}, {"pin": True, "count": 2})["gpus"] == 1


def test_an_unpinned_reservation_leaves_the_declaration_alone():
    assert _footprint({"gpus": 3}, {})["gpus"] == 3
    assert _footprint({"gpus": 3}, None)["gpus"] == 3


def test_memory_demand_is_clamped_to_the_smallest_device_actually_held():
    out = _footprint({"gpus": 2, "gpu_mem_mib": 80_000},
                     {"pin": True, "count": 2, "gpu_ids": [0, 1]},
                     gpu_mem={0: 40_000, 1: 24_000})
    assert out["gpu_mem_mib"] == 24_000


def test_an_unknown_devices_memory_is_not_invented():
    """`_gpu_mem` misses a device (discovery failed), so there is no floor to clamp to and the
    declaration is retained rather than silently shrunk to a fabricated ceiling."""
    out = _footprint({"gpus": 1, "gpu_mem_mib": 80_000},
                     {"pin": True, "count": 1, "gpu_ids": [7]}, gpu_mem={})
    assert out["gpu_mem_mib"] == 80_000


# ------------------------------------------------------------------------ the loop still asks

def test_the_attempt_loop_reaches_every_rule_it_no_longer_inlines():
    """A rule nobody calls is a comment. Pinned on real `ast.Call` nodes (`called_names`), so a
    re-derivation of any of these inside `_evaluate` — the drift this split exists to prevent — has
    to delete the call, and deleting the call fails here."""
    calls = called_names(EvaluateMixin._evaluate)
    for rule in ("self._eval_failure_text", "self._repaired_footprint",
                 "_repair_provider_failure", "_repair_change_set",
                 "_repair_forces_full_retrain",
                 # The 2026-08-06 durability pair. `_durable_repair_ledger` is the one whose absence
                 # is INVISIBLE — drop the call and every counter silently reverts to a process-local
                 # 0/[], which no outcome test of a single-process run can see (that is exactly how
                 # the defect shipped). `_effective_repair_cap` is what makes `0` bounded.
                 "_durable_repair_ledger", "_effective_repair_cap",
                 "self._trust_scan_surface", "self._trust_scan_signals"):
        assert calls.count(rule) == 1, (
            f"`_evaluate` must reach {rule} exactly once (found {calls.count(rule)})")

    # …and the retrain rule gets the PRE-repair eval result, not the POST-repair stage resolution.
    # That distinction has no branch inside the rule (see `test_a_renamed_later_stage_...`): it is
    # held entirely by what the caller hands over, so it is pinned HERE. Passing `_stages` — the
    # resolution taken after the repair rewrote the pipeline — is the defect the comment records,
    # and it loses the failed stage's identity for first- and later-stage failures alike.
    retrain = [node for node in ast.walk(function_tree(EvaluateMixin._evaluate))
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "_repair_forces_full_retrain"]
    assert [[ast.unparse(arg) for arg in call.args] for call in retrain] == [["res", "next_start"]], (
        "the retrain rule must judge the eval RESULT this attempt produced, never a stage list "
        "resolved after the repair")
