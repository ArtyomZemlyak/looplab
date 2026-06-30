"""CLI surface (Typer) smoke tests — catch broken option wiring that unit tests miss
(e.g. an option assigned to a non-existent Settings field)."""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from looplab.cli import app

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def test_run_mlebench_offline_with_agent_flags(tmp_path):
    # Default backend=toy -> mlebench's templated k-NN runs offline. The agent flags must
    # be accepted and assigned to real Settings fields (regression: --agent-cmd used to
    # crash by assigning to a non-existent `aider_cmd`).
    result = runner.invoke(app, [
        "run", str(ROOT / "examples" / "mlebench_task.json"),
        "--out", str(tmp_path / "run"), "--max-nodes", "2",
        "--agent-cmd", "dummy", "--agent-surface", "*.py,*.txt",
        "--no-validate-agent", "--no-agent-patch-gate",
    ])
    assert result.exit_code == 0, result.output
    assert "BEST" in result.output


def test_run_toy_task_offline(tmp_path):
    result = runner.invoke(app, [
        "run", str(ROOT / "examples" / "toy_task.json"),
        "--out", str(tmp_path / "run"), "--max-nodes", "3",
    ])
    assert result.exit_code == 0, result.output
    assert "finished=True" in result.output


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert "LoopLab" in result.output


def test_run_bad_backend_errors_with_choices(tmp_path):
    # A typo'd backend must fail loudly (not silently degrade to the offline `toy` backend) and
    # name the valid choices.
    result = runner.invoke(app, [
        "run", str(ROOT / "examples" / "toy_task.json"),
        "--out", str(tmp_path / "run"), "--backend", "ll",
    ])
    assert result.exit_code != 0
    assert "toy, llm" in result.output


def test_run_missing_task_file_is_friendly(tmp_path):
    # A missing task file becomes a one-line error, not a raw Python traceback.
    result = runner.invoke(app, ["run", str(tmp_path / "nope.json")])
    assert result.exit_code != 0
    assert "not found" in result.output
    assert "Traceback" not in result.output


def test_inspect_missing_run_dir_errors(tmp_path):
    # A path with no events.jsonl must error clearly instead of printing a blank, exit-0 empty run.
    result = runner.invoke(app, ["inspect", str(tmp_path / "no_such_run")])
    assert result.exit_code != 0
    assert "no run found" in result.output


def test_ui_help_keeps_extra_name():
    # Regression: rich markup mode used to strip `[ui]`, turning the install hint into
    # `pip install 'looplab'` (wrong). Markdown mode keeps the extra name intact.
    result = runner.invoke(app, ["ui", "--help"])
    assert result.exit_code == 0, result.output
    assert "looplab[ui]" in result.output


# --- YAML / unified config + no-file runs --------------------------------------------------------

def test_init_writes_parseable_documented_template(tmp_path):
    import yaml
    dest = tmp_path / "looplab.yaml"
    result = runner.invoke(app, ["init", "--out", str(dest)])
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load(dest.read_text())
    assert set(("task", "settings", "out")) <= set(doc)
    # The whole template must be valid YAML, incl. the comment alignment (a `#` glued to a value
    # would corrupt e.g. llm_base_url).
    assert doc["settings"]["llm_base_url"].endswith("/v1")


def test_run_unified_yaml_applies_task_and_settings(tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text(
        "out: %s\n"
        "task:\n  kind: quadratic\n  goal: min\n  direction: min\n"
        "settings:\n  max_nodes: 2\n" % (tmp_path / "r"))
    result = runner.invoke(app, ["run", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "finished=True" in result.output
    assert "nodes=2" in result.output                       # settings: block was honored
    assert (tmp_path / "r" / "events.jsonl").exists()       # out: was honored


def test_run_no_file_from_flags(tmp_path):
    # --no-genesis builds the task purely from flags, offline (no model needed).
    result = runner.invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "min x^2", "--direction", "min",
        "--set", "max_nodes=2", "--out", str(tmp_path / "r"),
    ])
    assert result.exit_code == 0, result.output
    assert "finished=True" in result.output
    # Self-describing: a no-file run still writes a task snapshot it can resume from.
    assert (tmp_path / "r" / "task.snapshot.json").exists()


def test_run_set_unknown_key_errors(tmp_path):
    result = runner.invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "g", "--out", str(tmp_path / "r"),
        "--set", "max_node=9",
    ])
    assert result.exit_code != 0
    assert "unknown setting" in result.output


def test_run_set_overrides_file(tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text("task:\n  kind: quadratic\n  goal: g\n  direction: min\n"
                   "settings:\n  max_nodes: 5\n")
    result = runner.invoke(app, ["run", str(cfg), "--out", str(tmp_path / "r"), "-s", "max_nodes=1"])
    assert result.exit_code == 0, result.output
    assert "nodes=1" in result.output                       # --set wins over the file


def test_run_no_task_errors():
    result = runner.invoke(app, ["run"])
    assert result.exit_code != 0
    assert "no task" in result.output


# --- Genesis: --goal with no --kind lets the LLM infer the task kind ------------------------------

def _patch_genesis(monkeypatch, task):
    """Stub the LLM client construction and the authoring call so the genesis path runs offline."""
    import looplab.cli as cli
    import looplab.genesis as genesis
    monkeypatch.setattr(cli, "make_llm_client", lambda settings, **k: object())
    monkeypatch.setattr(genesis, "author_task",
                        lambda goal, **k: genesis.GenesisResult(task=task, rationale="inferred"))


def test_run_goal_only_infers_kind_and_runs(tmp_path, monkeypatch):
    _patch_genesis(monkeypatch, {"kind": "quadratic", "goal": "g", "direction": "min",
                                 "bounds": {"x": [-10.0, 10.0], "y": [-10.0, 10.0]}})
    result = runner.invoke(app, [
        "run", "--goal", "minimize (x-3)^2", "-s", "max_nodes=2", "--out", str(tmp_path / "g"),
    ])
    assert result.exit_code == 0, result.output
    assert "Genesis -> kind=quadratic" in result.output       # it inferred + announced the kind
    assert "finished=True" in result.output                   # …and actually ran it


def test_run_no_genesis_no_kind_requires_kind(tmp_path):
    # --no-genesis with a goal but no kind must NOT silently become a quadratic toy run — it errors.
    result = runner.invoke(app, [
        "run", "--no-genesis", "--goal", "predict churn from data.csv",
        "-s", "max_nodes=1", "--out", str(tmp_path / "n"),
    ])
    assert result.exit_code != 0
    assert "no task kind" in result.output


def _stub_author(monkeypatch, **task):
    import looplab.cli as cli
    import looplab.genesis as genesis
    monkeypatch.setattr(cli, "make_llm_client", lambda settings, **k: object())
    monkeypatch.setattr(genesis, "author_task",
                        lambda goal, **k: genesis.GenesisResult(task=dict(task), rationale="r"))


def _capture_backend(monkeypatch):
    """Replace the engine factory so a (would-be LLM) run aborts right after we read its backend."""
    import looplab.cli as cli
    seen = {}

    def _cap(out, task, settings, crash_after):
        seen["backend"] = settings.backend
        raise RuntimeError("stop-before-run")
    monkeypatch.setattr(cli, "_engine", _cap)
    return seen


def test_run_generative_kind_bumps_backend_to_llm(tmp_path, monkeypatch):
    # A generative inferred kind with no chosen backend must flip the run to backend=llm (cli.py:399).
    # code_regression is generative AND validates offline (synthetic data), so the engine is reached.
    _stub_author(monkeypatch, kind="code_regression", goal="write code", direction="min")
    seen = _capture_backend(monkeypatch)
    runner.invoke(app, ["run", "--goal", "fit a model in code", "--out", str(tmp_path / "g")])
    assert seen.get("backend") == "llm"


def test_run_generative_kind_respects_explicit_backend(tmp_path, monkeypatch):
    # An explicit --backend toy must NOT be overridden by the generative-kind bump.
    _stub_author(monkeypatch, kind="code_regression", goal="write code", direction="min")
    seen = _capture_backend(monkeypatch)
    runner.invoke(app, ["run", "--goal", "fit a model in code", "--backend", "toy",
                        "--out", str(tmp_path / "g")])
    assert seen.get("backend") == "toy"


def test_run_genesis_endpoint_error_is_attributed_to_the_model(tmp_path, monkeypatch):
    import looplab.cli as cli
    import looplab.genesis as genesis
    monkeypatch.setattr(cli, "make_llm_client", lambda settings, **k: object())
    monkeypatch.setattr(genesis, "author_task",
                        lambda goal, **k: genesis.GenesisResult(error="connection refused"))
    result = runner.invoke(app, ["run", "--goal", "predict x", "--out", str(tmp_path / "e")])
    assert result.exit_code != 0
    assert "couldn't reach the model" in result.output and "connection refused" in result.output


def test_run_genesis_vague_goal_asks_for_detail(tmp_path, monkeypatch):
    import looplab.cli as cli
    import looplab.genesis as genesis
    monkeypatch.setattr(cli, "make_llm_client", lambda settings, **k: object())
    monkeypatch.setattr(genesis, "author_task",
                        lambda goal, **k: genesis.GenesisResult(task={}, reply="What data do you have?"))
    result = runner.invoke(app, ["run", "--goal", "make it good", "--out", str(tmp_path / "v")])
    assert result.exit_code != 0
    assert "couldn't author a task" in result.output and "What data do you have?" in result.output


def test_run_genesis_bad_path_is_refused_before_a_run_is_created(tmp_path, monkeypatch):
    # Genesis authored a task pointing at a path that doesn't exist -> the CLI refuses up front and
    # creates NO run dir (so a later re-run can't trap itself in a finished/errored run).
    import looplab.cli as cli
    import looplab.genesis as genesis
    monkeypatch.setattr(cli, "make_llm_client", lambda settings, **k: object())
    monkeypatch.setattr(genesis, "author_task",
                        lambda goal, **k: genesis.GenesisResult(
                            path_error="path(s) not found on this machine: /no/such/data.csv",
                            reply="I couldn't find /no/such/data.csv on this machine."))
    out = tmp_path / "p"
    result = runner.invoke(app, ["run", "--goal", "predict from /no/such/data.csv", "--out", str(out)])
    assert result.exit_code != 0
    assert "Genesis stopped" in result.output and "/no/such/data.csv" in result.output
    assert not (out / "events.jsonl").exists()       # no doomed run was spawned


def test_run_refuses_to_reenter_a_finished_run(tmp_path):
    # First run finishes; a second `run` into the SAME dir must NOT silently re-fold + exit 0 printing
    # the old result — it errors with how to start fresh / inspect, fixing the "lands in an existing
    # run and finishes immediately" trap.
    out = tmp_path / "g"
    args = ["run", "--no-genesis", "--kind", "quadratic", "--goal", "min x^2", "--direction", "min",
            "--set", "max_nodes=2", "--out", str(out)]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "finished=True" in first.output
    second = runner.invoke(app, args)
    assert second.exit_code != 0
    # Typer renders the error inside a wrapping panel; drop the box-drawing border chars and collapse
    # whitespace so the matched phrases aren't split by a wrap boundary on a long tmp path.
    flat = " ".join(second.output.translate({ord(c): " " for c in "│╭╮╰╯─"}).split())
    assert "already holds a finished run" in flat
    assert "resume" in flat and "inspect" in flat


def test_run_finished_guard_fires_before_genesis(tmp_path, monkeypatch):
    # The finished-run guard runs BEFORE Genesis, so a re-run into an occupied dir fails immediately
    # instead of spending a whole Genesis agent loop. If Genesis were reached, make_llm_client (stubbed
    # to raise) would change the error — assert we get the occupied-dir message instead.
    import looplab.cli as cli
    out = tmp_path / "g"
    first = runner.invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "min x^2", "--direction", "min",
        "--set", "max_nodes=2", "--out", str(out)])
    assert first.exit_code == 0, first.output

    def _boom(*a, **k):
        raise AssertionError("Genesis (make_llm_client) must not be reached before the finished guard")
    monkeypatch.setattr(cli, "make_llm_client", _boom)
    second = runner.invoke(app, ["run", "--goal", "predict something", "--out", str(out)])
    assert second.exit_code != 0
    flat = " ".join(second.output.translate({ord(c): " " for c in "│╭╮╰╯─"}).split())
    assert "already holds a finished run" in flat


def test_file_settings_override_env(tmp_path, monkeypatch):
    # The documented precedence: a file's settings: block wins over a LOOPLAB_* env var.
    monkeypatch.setenv("LOOPLAB_MAX_NODES", "99")
    cfg = tmp_path / "r.yaml"
    cfg.write_text("task:\n  kind: quadratic\n  goal: g\n  direction: min\nsettings:\n  max_nodes: 2\n")
    result = runner.invoke(app, ["run", str(cfg), "--out", str(tmp_path / "r")])
    assert result.exit_code == 0, result.output
    assert "nodes=2" in result.output          # file beat env (99)


def test_out_flag_overrides_file_out(tmp_path):
    cfg = tmp_path / "r.yaml"
    cfg.write_text(f"out: {tmp_path / 'fromfile'}\n"
                   "task:\n  kind: quadratic\n  goal: g\n  direction: min\nsettings:\n  max_nodes: 1\n")
    result = runner.invoke(app, ["run", str(cfg), "--out", str(tmp_path / "fromflag")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "fromflag" / "events.jsonl").exists()     # --out won
    assert not (tmp_path / "fromfile").exists()


def test_data_flag_rejected_for_incompatible_kind(tmp_path):
    result = runner.invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "g", "--data", "x.csv",
        "--out", str(tmp_path / "d"),
    ])
    assert result.exit_code != 0
    assert "only meaningful for a dataset or repo" in result.output


def test_init_scaffolds_the_requested_kind(tmp_path):
    import yaml
    dest = tmp_path / "r.yaml"
    result = runner.invoke(app, ["init", "--out", str(dest), "--kind", "repo"])
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load(dest.read_text())
    assert doc["task"]["kind"] == "repo"        # active block matches --kind (not always dataset)


def test_set_value_stripped_and_nonfinite_kept_as_string():
    from looplab.appconfig import coerce_scalar, parse_sets
    assert parse_sets(["llm_model =  qwen3:8b "]) == {"llm_model": "qwen3:8b"}   # key + value stripped
    assert coerce_scalar("NaN") == "NaN" and coerce_scalar("Infinity") == "Infinity"
    assert coerce_scalar("null") is None and coerce_scalar("3") == 3


def test_run_kind_pins_genesis(tmp_path, monkeypatch):
    # --kind does NOT skip genesis: it pins the kind, and genesis fills the rest within it.
    import looplab.cli as cli
    import looplab.genesis as genesis
    seen = {}
    monkeypatch.setattr(cli, "make_llm_client", lambda settings, **k: object())

    def _author(goal, **k):
        seen["kind"] = k.get("kind")                          # capture the pinned kind passed through
        return genesis.GenesisResult(
            task={"kind": k.get("kind"), "goal": goal, "direction": "min",
                  "bounds": {"x": [-10.0, 10.0], "y": [-10.0, 10.0]}}, rationale="pinned")
    monkeypatch.setattr(genesis, "author_task", _author)
    result = runner.invoke(app, [
        "run", "--kind", "quadratic", "--goal", "minimize x^2", "-s", "max_nodes=1",
        "--out", str(tmp_path / "k"),
    ])
    assert result.exit_code == 0, result.output
    assert seen["kind"] == "quadratic"                        # the pin reached genesis
    assert "Genesis -> kind=quadratic" in result.output and "finished=True" in result.output
