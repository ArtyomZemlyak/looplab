"""A deliberate refusal must read as a refusal; a genuine bug must keep its traceback.

THE DEFECT. Typer renders anything that reaches the interpreter as a Rich traceback with the
message on the LAST line. Every well-worded refusal LoopLab raises on purpose therefore arrived as
a stack dump: measured before the fix, `run … -s policy=mcts -s speculation_depth=1` printed 42
lines to say `Card speculation requires policy='greedy', got 'mcts'`, a refused width re-entry 33,
and the LLM credential preflight 43. That presentation tells the operator their command CRASHED the
program and buries the one line that says what to change.

THE SPLIT IS BY TYPE, AND THAT IS THE WHOLE RISK. Turning the pretty traceback off would have been
wrong in the other direction, and catching a bare `ValueError` at the CLI boundary would be worse
still — a bug printed as a tidy one-line message is far more damaging than a refusal printed as a
traceback, because it looks handled. So `core/errors.py::OperatorRefusal` marks the family, and
BOTH halves are pinned here: the refusals, AND the control — an unexpected exception from the same
command, on the same code path, which must still print every frame.

Driving the real CLI (`typer.testing.CliRunner` over the real `app`) rather than unit-testing the
handler: the boundary lives in a Typer group class, and the property is what the operator's
terminal shows, which only an end-to-end invocation can demonstrate.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from looplab.cli import (REFUSAL_EXIT_CODE, TRACEBACK_ENV, app, deliberate_refusals,
                         refusal_report)
from looplab.core.errors import ConfigRefusal, EnvironmentRefusal, LLMError, OperatorRefusal
from looplab.engine.orchestrator import (RunStartPinError, SettledWidthPinError,
                                         SpeculationAuthorizationError)

runner = CliRunner()

# The two shapes a traceback takes on this CLI: Rich's box-drawn frame panel (pretty_exceptions is
# on by default) and the plain Python one. Neither may appear in a refusal's output.
_TRACEBACK_MARKERS = ("Traceback (most recent call last)", '  File "')


def _toy_task(tmp_path: Path) -> str:
    task = tmp_path / "task.json"
    task.write_text(json.dumps({
        "id": "toy_quadratic", "kind": "quadratic", "goal": "minimize (x-3)^2 + (y+1)^2",
        "direction": "min", "bounds": {"x": [-10, 10], "y": [-10, 10]},
    }), encoding="utf-8")
    return str(task)


def _assert_reads_as_a_refusal(result, *, expected: str):
    """The four properties the operator-facing contract is made of."""
    assert result.exit_code == REFUSAL_EXIT_CODE, result.output      # non-zero, and NOT "crashed"
    assert expected in result.output, result.output                  # the message survived intact
    assert result.output.lstrip().startswith("Refused:"), result.output   # …and leads the output
    for marker in _TRACEBACK_MARKERS:
        assert marker not in result.output, result.output


# --- the family ---------------------------------------------------------------------------------

def test_the_marker_covers_every_member_of_the_refusal_family():
    """The registry check. Each of these is raised on purpose at an operator-facing boundary, and a
    member that loses the marker silently goes back to printing 40 frames — with every behavioural
    test below still green on the OTHER members."""
    for exc_type in (ConfigRefusal, EnvironmentRefusal, LLMError,
                     RunStartPinError, SpeculationAuthorizationError, SettledWidthPinError):
        assert issubclass(exc_type, OperatorRefusal), exc_type

    # …and the historical base of each site is preserved, so no existing `except` clause or
    # `pytest.raises` in the tree changed meaning when the marker was added.
    assert issubclass(ConfigRefusal, ValueError)
    assert issubclass(EnvironmentRefusal, RuntimeError)
    assert issubclass(LLMError, RuntimeError)
    assert issubclass(RunStartPinError, RuntimeError)


def test_a_config_refusal_prints_its_message_not_a_traceback(tmp_path):
    """The reported case, verbatim."""
    result = runner.invoke(app, [
        "run", _toy_task(tmp_path), "--backend", "toy", "--out", str(tmp_path / "run"),
        "-s", "policy=mcts", "-s", "speculation_depth=1",
    ])
    _assert_reads_as_a_refusal(result, expected="Card speculation requires policy='greedy'")
    assert "-s policy=greedy" in result.output          # …and says what to change
    # The measurement that made this a defect: 42 lines of frames for a one-sentence refusal.
    assert len(result.output.strip().splitlines()) == 1, result.output


def test_an_unknown_policy_and_trust_mode_refuse_with_the_accepted_values(tmp_path):
    """Two more members found by the same sweep — `Settings.policy` / `Settings.trust_mode` are
    free-form strings, so a typo lands in the factory, not in pydantic."""
    task = _toy_task(tmp_path)
    policy = runner.invoke(app, [
        "run", task, "--backend", "toy", "--out", str(tmp_path / "p"), "-s", "policy=nope"])
    _assert_reads_as_a_refusal(policy, expected="unknown policy: 'nope'")
    assert "greedy" in policy.output                    # the accepted values, not just the problem

    trust = runner.invoke(app, [
        "run", task, "--backend", "toy", "--out", str(tmp_path / "t"), "-s", "trust_mode=nope"])
    _assert_reads_as_a_refusal(trust, expected="unknown trust_mode: 'nope'")
    assert "trusted_local" in trust.output


def test_a_refused_width_re_entry_reads_as_a_refusal_and_stays_byte_clean(tmp_path):
    """The sibling case from the same battery, and the property that refusal already owned: the log
    it declined to trust is untouched. Presentation changed; the fail-closed behaviour did not."""
    out = tmp_path / "pinned"
    task = _toy_task(tmp_path)
    first = runner.invoke(app, [
        "run", task, "--backend", "toy", "--out", str(out),
        "-s", "eval_parallel=2", "-s", "max_nodes=2", "-s", "n_seeds=2"])
    assert first.exit_code == 0, first.output
    before = ((out / "events.jsonl").read_bytes(), (out / "config.snapshot.json").read_bytes())

    result = runner.invoke(app, [
        "run", task, "--backend", "toy", "--out", str(out),
        "-s", "eval_parallel=3", "-s", "max_nodes=2", "-s", "n_seeds=2"])

    _assert_reads_as_a_refusal(result, expected="run_started pinned 2")
    assert ((out / "events.jsonl").read_bytes(),
            (out / "config.snapshot.json").read_bytes()) == before


# --- the control: an unexpected exception is NOT a refusal --------------------------------------

def test_a_genuine_bug_keeps_its_full_traceback_and_exit_1(tmp_path, monkeypatch):
    """THE control. Same command, same boundary, an ordinary defect (an attribute slip) instead of
    a refusal: the exception must reach the interpreter with its frames intact, and the exit code
    must stay 1 — which is what "this program crashed" has always meant, and is now distinguishable
    from a refusal's 2."""
    import looplab.cli as cli

    def _boom(*_a, **_kw):
        broken = {"widths": None}
        return broken["widths"].eval_parallel           # AttributeError, deep in construction

    monkeypatch.setattr(cli, "_engine", _boom)
    result = runner.invoke(app, [
        "run", _toy_task(tmp_path), "--backend", "toy", "--out", str(tmp_path / "run")])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, AttributeError), result.exception
    assert result.exc_info[2] is not None                # the frames, which the renderer walks
    assert not result.output.lstrip().startswith("Refused:"), result.output


def test_a_bare_valueerror_from_the_same_call_is_not_swallowed(tmp_path, monkeypatch):
    """The precise hazard the marker exists to avoid. `Engine.__init__` raises `ConfigRefusal`
    (a `ValueError`) for its config checks — catching `ValueError` at the boundary instead would
    have caught THIS too, and reported a bug as a tidy one-line refusal."""
    import looplab.cli as cli

    def _boom(*_a, **_kw):
        raise ValueError("internal invariant violated")

    monkeypatch.setattr(cli, "_engine", _boom)
    result = runner.invoke(app, [
        "run", _toy_task(tmp_path), "--backend", "toy", "--out", str(tmp_path / "run")])

    assert result.exit_code == 1, result.output
    assert type(result.exception) is ValueError, result.exception
    assert "Refused:" not in result.output


def test_a_refusal_raised_inside_the_loop_reads_the_same_as_one_raised_before_it(tmp_path,
                                                                                 monkeypatch):
    """anyio wraps whatever escapes a task group in an `ExceptionGroup` — even a lone exception —
    so `Engine.run` delivers a MID-RUN refusal grouped while the identical refusal at the preflight
    boundary arrives bare. Measured before `deliberate_refusals` looked inside the group: the same
    `LLMError` printed 1 line at exit 2 from the preflight and 137 lines at exit 1 from inside the
    eval loop. The grouping is a structured-concurrency artifact; it must not decide presentation.
    """
    from looplab.core.errors import LLMError
    from looplab.engine.orchestrator import Engine

    def _dead_endpoint(self, *_a, **_kw):
        raise LLMError("LLM request to http://dead/v1 failed: connection refused")

    monkeypatch.setattr(Engine, "_evaluate", _dead_endpoint)
    result = runner.invoke(app, [
        "run", _toy_task(tmp_path), "--backend", "toy", "--out", str(tmp_path / "run"),
        "-s", "max_nodes=1"])

    _assert_reads_as_a_refusal(result, expected="LLM request to http://dead/v1 failed")


def test_a_mixed_group_is_a_bug_report_not_a_refusal():
    """FAIL CLOSED. A batch that hit a dead endpoint AND tripped a real defect is a BUG: rendering
    the half that reads nicely would hide the half that matters, which is exactly the swallow this
    whole split exists to prevent."""
    refusal = LLMError("endpoint gone")
    bug = AttributeError("'NoneType' object has no attribute 'metric'")

    assert [type(e) for e in deliberate_refusals(ExceptionGroup("evals", [refusal]))] == [LLMError]
    assert deliberate_refusals(ExceptionGroup("evals", [refusal, bug])) == []
    assert deliberate_refusals(ExceptionGroup("evals", [bug])) == []
    # …and nesting does not launder either verdict.
    nested_ok = ExceptionGroup("outer", [ExceptionGroup("inner", [refusal])])
    nested_bad = ExceptionGroup("outer", [ExceptionGroup("inner", [refusal]), bug])
    assert len(deliberate_refusals(nested_ok)) == 1
    assert deliberate_refusals(nested_bad) == []


# --- the escape hatch and the rendering rule ----------------------------------------------------

def test_the_traceback_env_var_puts_the_frames_back(tmp_path, monkeypatch):
    """Nothing is swallowed: a maintainer debugging a refusal that fires when it should not can
    still get every frame, without editing code."""
    monkeypatch.setenv(TRACEBACK_ENV, "1")
    result = runner.invoke(app, [
        "run", _toy_task(tmp_path), "--backend", "toy", "--out", str(tmp_path / "run"),
        "-s", "policy=mcts", "-s", "speculation_depth=1",
    ])
    assert result.exit_code == 1
    assert isinstance(result.exception, ConfigRefusal), result.exception


def test_refusal_report_keeps_a_chained_cause_and_never_repeats_it():
    """`raise LLMError("LLM base URL is malformed") from exc` puts the only concrete detail in
    `__cause__`; printing just the head would drop it. But most sites already interpolate their
    cause, and repeating it would rebuild the wall this fix removed."""
    try:
        try:
            raise ValueError("Invalid IPv6 URL")
        except ValueError as exc:
            raise LLMError("LLM base URL is malformed") from exc
    except LLMError as refusal:
        report = refusal_report(refusal)
    assert report.splitlines()[0] == "Refused: LLM base URL is malformed"
    assert "caused by ValueError: Invalid IPv6 URL" in report

    try:
        try:
            raise ValueError("Invalid IPv6 URL")
        except ValueError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
    except LLMError as refusal:
        quoted = refusal_report(refusal)
    assert quoted == "Refused: LLM request failed: Invalid IPv6 URL"   # not repeated below itself


def test_the_boundary_is_on_the_path_the_console_script_and_the_tests_share():
    """A boundary in `Typer.__call__` would be invisible to `CliRunner`, i.e. to every test above,
    while still "working" when a human ran the command — the failure mode this pin exists for."""
    import typer

    from looplab.cli import _RefusalBoundaryGroup

    command = typer.main.get_command(app)
    assert isinstance(command, _RefusalBoundaryGroup)
    assert "invoke" in vars(_RefusalBoundaryGroup)


# --- the control in the other direction ---------------------------------------------------------

@pytest.mark.parametrize("bad_input,expected", [
    (["-s", "not_a_real_key=7"], "unknown setting"),
    (["-s", "max_nodes=-1"], "greater than or equal to 1"),
])
def test_input_errors_click_already_owned_are_unchanged(tmp_path, bad_input, expected):
    """`-s` parsing and `Settings` validation were ALREADY one-line `BadParameter`s at exit 2, and
    this change must not have re-routed them through the refusal boundary (which would drop the
    usage line Click prints with them)."""
    result = runner.invoke(app, [
        "run", _toy_task(tmp_path), "--backend", "toy", "--out", str(tmp_path / "run"),
    ] + bad_input)
    assert result.exit_code == 2
    flat = " ".join(result.output.split())
    assert expected in flat and "Usage:" in flat
    assert "Refused:" not in result.output


def test_a_malformed_task_file_still_names_the_file_and_the_parse_error(tmp_path):
    """The third case from the same battery. Already a `BadParameter` before this change — pinned
    so the refusal boundary is never "fixed" by widening it to swallow this path too."""
    broken = tmp_path / "broken.json"
    broken.write_text('{"kind": "quadratic", "id": ', encoding="utf-8")
    result = runner.invoke(app, [
        "run", str(broken), "--backend", "toy", "--out", str(tmp_path / "run")])
    assert result.exit_code == 2
    flat = " ".join(result.output.split())
    assert "could not read" in flat and "broken.json" in flat
    for marker in _TRACEBACK_MARKERS:
        assert marker not in result.output, result.output
