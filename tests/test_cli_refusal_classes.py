"""Every error a CLI command raises is either a CALLING mistake or a REFUSAL, by type (doc 75 UX-01).

Click's split, which git (`usage:` / `fatal:`) and cargo (`error:` + `help:`) share: a mistake in
how the command was called prints the usage block and "Try --help"; anything else is a plain error.
LoopLab had one grammar for both — 34 `raise typer.BadParameter(` sites in `looplab/cli`, so a
missing task file, an invalid task, an unreachable model and a run in the wrong state all printed
"Usage … Try 'looplab run --help' … Invalid value:", as if a flag had been mistyped, while the run
preflight's refusal of the very same unreachable model printed one `Refused:` block with its fixes.

A refusal is now `cli/__init__.py::CliRefusal` (an `OperatorRefusal` that stays a `BadParameter`,
so callers that catch one keep working) and reaches the CLI boundary as `Refused:`. What remains a
bare `BadParameter` is a reviewed calling mistake, named below with why. A count would not hold
this — the doc 75 plan's "<= 20" was set against a grep that counted comments — so the rule is a
table: a new bare `BadParameter` is red until it is classified here or raised as a `CliRefusal`.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from typer.testing import CliRunner

from looplab.cli import app

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "looplab" / "cli"

# (module, innermost function) -> number of bare `raise typer.BadParameter(` there, each a mistake
# in HOW the command was called — a flag value or a flag combination — for which the usage block and
# "Try --help" are the right answer.
SYNTAX = {
    ("__init__", "_choice"): 1,                           # a value outside a flag's choices
    ("export_cmds", "export_git"): 1,                     # OUT given twice, or not at all
    ("harness_cmds", "harness_mcp"): 1,                   # a ValueError that is not a refusal
    ("run_cmds", "_calibration_envelope_task_dict"): 1,   # --speculation-gate-calibration's flags
    ("run_cmds", "_pin_offline_speculation_profile"): 3,  # calibration/receipt flag combinations
    ("run_cmds", "init"): 1,                              # init --kind outside the kinds
    ("run_cmds", "resume"): 1,                            # --max-nodes < 1
    # no task at all; no --kind for a goal; a `-s KEY=VALUE` that does not parse; a setting value
    # `Settings` refuses — the last two pinned by `test_cli_refusals.py` to keep the usage line
    ("run_cmds", "run"): 4,
    ("run_cmds", "stop"): 2,                              # --timeout value; --timeout without --wait
}


def _raise_sites(name: str) -> Counter:
    """`(module, innermost function) -> count` of `raise <name>(...)` in `looplab/cli`."""
    sites: Counter = Counter()
    for path in sorted(CLI.glob("*.py")):

        class _Visitor(ast.NodeVisitor):
            stack: list[str] = []

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Raise(self, node):
                if (isinstance(node.exc, ast.Call) and ast.unparse(node.exc.func) == name
                        and self.stack):
                    sites[(path.stem, self.stack[-1])] += 1

        _Visitor().visit(ast.parse(path.read_text(encoding="utf-8")))
    return sites


def test_every_bare_bad_parameter_is_a_reviewed_calling_mistake():
    assert dict(_raise_sites("typer.BadParameter")) == SYNTAX, (
        "a new `raise typer.BadParameter(` in looplab/cli: if it refuses the operator's INPUT (a "
        "file, a task, a run's state, a model) raise `CliRefusal`; if it is a calling mistake, add "
        "it to SYNTAX with the reason")


def test_the_refusals_are_raised_as_refusals():
    assert sum(_raise_sites("CliRefusal").values()) >= 20


def _refused(result) -> str:
    output = result.output
    assert result.exit_code == 2, output
    assert "Refused:" in output, output
    assert "Usage:" not in output and "Try '" not in output, output
    return output


def test_a_missing_config_file_is_refused_not_a_usage_error(tmp_path):
    output = _refused(CliRunner().invoke(app, ["run", str(tmp_path / "nope.yaml")]))
    assert "config file not found" in output


def test_harness_mcp_without_its_token_is_refused_not_a_usage_error(monkeypatch):
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    output = _refused(CliRunner().invoke(app, ["harness-mcp"]))
    assert "LOOPLAB_HARNESS_TOKEN" in output


def test_genesis_without_a_model_takes_the_run_preflights_refusal(tmp_path, monkeypatch):
    """Driven through the REAL refusal text: only the network probe is replaced, by the classified
    failure a dead endpoint produces. The remedies are the preflight's own — `--backend toy` and
    `looplab smoke` — and Genesis itself is never reached."""
    import looplab.agents.preflight as preflight
    import looplab.engine.genesis as genesis

    monkeypatch.setattr(preflight, "_probe_role_endpoints", lambda *a, **k: [
        preflight._ProbeFailure("unreachable", "strategist: Connection refused")])
    monkeypatch.setattr(genesis, "author_task",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Genesis ran")))
    output = _refused(CliRunner().invoke(app, [
        "run", "--goal", "minimize (x-3)^2", "--out", str(tmp_path / "g")]))
    assert "[unreachable]" in output and "--backend toy" in output and "looplab smoke" in output


def test_backend_toy_keeps_a_goal_offline(tmp_path):
    """doc 75 UX-25: `--backend toy` means offline, so with no explicit `--genesis` the task is built
    from the flags and no model is asked to write it."""
    result = CliRunner().invoke(app, [
        "run", "--goal", "minimize (x-3)^2", "--kind", "quadratic", "--direction", "min",
        "--backend", "toy", "--max-nodes", "2", "--out", str(tmp_path / "toy")])
    assert result.exit_code == 0, result.output
    assert "Genesis" not in result.output


def test_an_invalid_task_is_one_line_per_field_not_a_pydantic_dump(tmp_path, monkeypatch):
    """doc 75 UX-02: the scaffold `looplab init` writes, run before its `data.csv` exists."""
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    output = _refused(CliRunner().invoke(app, ["run", "looplab.yaml", "--backend", "toy"]))
    for dump in ("pydantic.dev", "input_value", "type=value_error"):
        assert dump not in output, output
    first = output.splitlines()[0]
    assert first.startswith("Refused: invalid task: data path(s) not found: data_path="), output
    assert "data.csv" in first and "use an absolute path" not in output
