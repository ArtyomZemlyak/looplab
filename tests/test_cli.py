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


def test_run_refuses_a_different_task_in_an_existing_run_dir(tmp_path):
    # arch-review §3 P0-5: a `run` on a dir that already holds a DIFFERENT task must refuse rather than
    # overwrite its snapshot and reopen the old log (mixing experiments in one event log).
    import json
    out = tmp_path / "run"
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"id": "exp_a", "kind": "quadratic", "goal": "min", "direction": "min",
                             "bounds": {"x": [-5, 5]}, "seed": 1, "step": 1.0}))
    b.write_text(json.dumps({"id": "exp_b", "kind": "quadratic", "goal": "min", "direction": "min",
                             "bounds": {"x": [-5, 5]}, "seed": 1, "step": 1.0}))
    assert runner.invoke(app, ["run", str(a), "--out", str(out), "--max-nodes", "2"]).exit_code == 0
    res = runner.invoke(app, ["run", str(b), "--out", str(out), "--max-nodes", "2"])
    assert res.exit_code == 2
    assert "refusing to mix" in res.output and "exp_a" in res.output
    # re-running the SAME task in the dir is allowed (continuation)
    assert runner.invoke(app, ["run", str(a), "--out", str(out), "--max-nodes", "3"]).exit_code == 0


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


def test_atlas_and_claims_accept_d8_only_memory(tmp_path):
    import json

    from looplab.engine.claims import record_research_claims

    memory = tmp_path / "memory"
    record_research_claims(memory, run_id="run-d8", task_id="task-d8", direction="max", claims=[{
        "statement": "hard negatives improve retrieval",
        "node_ids": [3],
        "urls": ["https://example.test/evidence"],
    }])
    claims = runner.invoke(app, ["claims", str(memory), "--json"])
    assert claims.exit_code == 0, claims.output
    assert json.loads(claims.output)[0]["statement"] == "hard negatives improve retrieval"

    atlas = runner.invoke(app, ["atlas", str(memory), "--json"])
    assert atlas.exit_code == 0, atlas.output
    payload = json.loads(atlas.output)
    assert payload["n_claims"] == 1 and payload["n_runs"] == 1


def test_claims_missing_explicit_file_does_not_fall_back_to_sibling_d8(tmp_path):
    from looplab.engine.claims import record_research_claims

    record_research_claims(
        tmp_path, run_id="sibling-run", task_id="sibling-task", direction="max", claims=[{
        "statement": "this sibling claim must not satisfy an explicit missing path",
        "node_ids": [1],
    }])
    explicit_lessons = tmp_path / "lessons.jsonl"
    explicit_lessons.write_bytes(b"")
    valid = runner.invoke(app, ["claims", str(explicit_lessons), "--json"])
    assert valid.exit_code == 0
    assert "this sibling claim" in valid.output

    missing_lessons = tmp_path / "missing-lessons.jsonl"

    result = runner.invoke(app, ["claims", str(missing_lessons), "--json"])

    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "this sibling claim" not in result.output


def test_claims_and_atlas_cli_disclose_corrupt_only_claim_store(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(b"{broken\n")

    claims = runner.invoke(app, ["claims", str(tmp_path)])
    atlas = runner.invoke(app, ["atlas", str(tmp_path)])

    assert claims.exit_code == 0, claims.output
    assert "claim evidence stores are partial" in claims.output
    assert "absence is not exact" in claims.output
    assert atlas.exit_code == 0, atlas.output
    assert "claim evidence source is PARTIAL" in atlas.output
    assert "lessons quarantined=1" in atlas.output


def test_asset_brief_llm_uses_ambient_settings_without_a_run_dir(tmp_path, monkeypatch):
    import looplab.cli.inspect_cmds as inspect_cmds
    import looplab.tools.asset_brief as asset_brief_module

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(inspect_cmds, "_make_llm_client", lambda settings: object())
    monkeypatch.setattr(asset_brief_module, "asset_brief",
                        lambda repo, client=None, task_type=None: "brief-ok")
    result = runner.invoke(app, ["asset-brief", str(repo), "--llm"])
    assert result.exit_code == 0, result.output
    assert "brief-ok" in result.output


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
    # memory_dir/knowledge_dir are ON by default (real path defaults). The scaffold must NOT emit them
    # as ACTIVE `null` lines — that would override the defaults and silently disable cross-run memory +
    # the knowledge base in every generated config. Loading the scaffolded settings must keep them set.
    from looplab.core.config import Settings
    s = Settings(**doc["settings"])
    assert s.memory_dir, "scaffolded config disabled cross-run memory (memory_dir came out falsy)"
    assert s.knowledge_dir, "scaffolded config disabled the knowledge base (knowledge_dir came out falsy)"


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


def test_speculation_gate_calibration_is_restricted_and_explicitly_threaded(
        tmp_path, monkeypatch):
    import looplab.cli as cli
    import looplab.core.hardware as hardware

    seen = {}

    def _capture(out, task, settings, crash_after, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("captured-before-run")

    monkeypatch.setattr(cli, "_engine", _capture)
    monkeypatch.setattr(hardware, "effective_gpu_inventory", lambda: [{
        "index": 0, "name": "test-gpu", "mem_total_mib": 24_576, "mem_free_mib": 20_000,
    }])
    result = runner.invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "minimize (x-3)^2 + (y+1)^2",
        "--out", str(tmp_path / "fresh"),
        "-s", "backend=toy", "-s", "max_nodes=12",
        "-s", "card_driven_selection=true", "-s", "speculation_depth=1",
        "--speculation-gate-calibration",
    ])
    assert result.exit_code == 1
    assert seen == {"speculation_gate_calibration": True}


def test_speculation_gate_calibration_rejects_non_gpu_or_ambient_receipt(tmp_path, monkeypatch):
    import looplab.core.hardware as hardware

    monkeypatch.setattr(hardware, "effective_gpu_inventory", lambda: [])
    result = runner.invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "minimize (x-3)^2 + (y+1)^2",
        "--out", str(tmp_path / "bad"),
        "-s", "backend=toy", "-s", "max_nodes=12",
        "-s", "card_driven_selection=true", "-s", "speculation_depth=1",
        "-s", f"speculation_gate_receipt={tmp_path / 'old.json'}",
        "--speculation-gate-calibration",
    ])
    assert result.exit_code == 2
    assert "restricted" in result.output
    assert "receipt must be unset" in result.output


def test_speculation_gate_calibration_rejects_reusing_a_run_dir(tmp_path, monkeypatch):
    import looplab.core.hardware as hardware
    from looplab.events.eventstore import EventStore

    out = tmp_path / "used"
    out.mkdir()
    EventStore(out / "events.jsonl").append(
        "run_started",
        {"run_id": "prior", "task_id": "toy_quadratic", "goal": "g", "direction": "min"},
    )
    monkeypatch.setattr(hardware, "effective_gpu_inventory", lambda: [{
        "index": 0, "name": "test-gpu", "mem_total_mib": 24_576, "mem_free_mib": 20_000,
    }])
    result = runner.invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "minimize (x-3)^2 + (y+1)^2",
        "--out", str(out),
        "-s", "backend=toy", "-s", "max_nodes=12",
        "-s", "card_driven_selection=true", "-s", "speculation_depth=1",
        "--speculation-gate-calibration",
    ])
    assert result.exit_code == 2
    # The EventStore lock is itself stale material, and proves the used directory was rejected before
    # calibration could append. Rich may wrap the surrounding BadParameter prose unpredictably.
    output = " ".join(result.output.split())
    assert "stale material:" in output and "events.jsonl.lock" in output


def test_speculation_gate_calibration_requires_no_genesis_before_model_call(
        tmp_path, monkeypatch):
    import looplab.cli as cli

    monkeypatch.setattr(
        cli, "make_llm_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("calibration crossed the no-Genesis guard")),
    )
    result = runner.invoke(app, [
        "run", "--kind", "quadratic", "--goal", "gate calibration",
        "--out", str(tmp_path / "genesis"),
        "--speculation-gate-calibration",
    ])
    assert result.exit_code == 2
    assert "requires --no-genesis" in result.output


def test_speculation_gate_calibration_rejects_stale_snapshot_without_events(
        tmp_path, monkeypatch):
    import looplab.core.hardware as hardware

    out = tmp_path / "stale"
    out.mkdir()
    (out / "task.snapshot.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(hardware, "effective_gpu_inventory", lambda: [{
        "index": 0, "name": "test-gpu", "mem_total_mib": 24_576,
    }])
    result = runner.invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "minimize (x-3)^2 + (y+1)^2",
        "--out", str(out), "-s", "max_nodes=12", "-s", "speculation_depth=0",
        "--speculation-gate-calibration",
    ])
    assert result.exit_code == 2
    # EventStore/locking may add another stale filename and Rich may wrap the rendered panel.
    output = " ".join(result.output.split())
    assert "stale material:" in output and "task.snapshot.json" in output


def test_receipt_backed_run_forces_narrow_profile_before_engine_construction(
        tmp_path, monkeypatch):
    import looplab.cli as cli
    from looplab.engine.orchestrator import SPECULATION_CALIBRATION_PROFILE_SETTINGS

    seen = {}

    def _capture(_out, _task, settings, _crash_after, **_kwargs):
        seen.update(settings.masked_snapshot())
        raise RuntimeError("captured-before-run")

    monkeypatch.setattr(cli, "_engine", _capture)
    receipt = tmp_path / "receipt.json"
    result = runner.invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic",
        "--goal", "minimize (x-3)^2 + (y+1)^2",
        "--out", str(tmp_path / "receipt-run"),
        "-s", "backend=llm", "-s", "policy=mcts", "-s", "max_nodes=5",
        "-s", "speculation_depth=1",
        "-s", f"speculation_gate_receipt={receipt}",
    ])
    assert result.exit_code == 1
    assert seen["max_nodes"] == 5
    assert seen["speculation_depth"] == 1
    assert seen["speculation_gate_receipt"] == str(receipt)
    for field, expected in SPECULATION_CALIBRATION_PROFILE_SETTINGS.items():
        assert seen[field] == expected


def test_ordinary_cli_engine_keeps_large_budget_outside_rollout_scope(
    tmp_path, monkeypatch,
):
    import looplab.cli as cli
    from looplab.adapters.toytask import ToyTask
    from looplab.core.config import Settings

    monkeypatch.setattr(
        cli,
        "speculation_runtime_scope_digest",
        lambda _snapshot: (_ for _ in ()).throw(
            AssertionError("ordinary run crossed the bounded speculation scope")),
    )
    engine = cli._engine(
        tmp_path / "ordinary-large-budget",
        ToyTask(),
        Settings(max_nodes=65, backend="toy"),
        None,
    )

    assert engine.policy.max_nodes == 65
    assert engine._speculation_runtime_scope_sha256 == ""


def _stale_speculation_prefix(run_dir):
    """One deliberately incomplete positive-depth prefix plus valid resume snapshots."""
    import json

    from looplab.adapters.toytask import ToyTask
    from looplab.core.config import Settings
    from looplab.events.eventstore import EventStore

    run_dir.mkdir()
    task = ToyTask()
    (run_dir / "task.snapshot.json").write_text(
        json.dumps(task.model_dump(mode="json")), encoding="utf-8")
    (run_dir / "config.snapshot.json").write_text(
        json.dumps(Settings(backend="toy", max_nodes=3).masked_snapshot()), encoding="utf-8")
    EventStore(run_dir / "events.jsonl").append("run_started", {
        "run_id": run_dir.name,
        "task_id": task.id,
        "goal": task.goal,
        "direction": task.direction,
        "card_driven_selection": True,
        "speculation_depth": 1,
        # Missing receipt/runtime/implementation pins: re-entry must fail closed.
    })
    return (
        (run_dir / "events.jsonl").read_bytes(),
        (run_dir / "config.snapshot.json").read_bytes(),
        (run_dir / "task.snapshot.json").read_bytes(),
    )


def test_resume_authorization_preflight_precedes_lifecycle_writes(tmp_path):
    from looplab.engine.orchestrator import SpeculationAuthorizationError

    run_dir = tmp_path / "stale-resume"
    before = _stale_speculation_prefix(run_dir)
    result = runner.invoke(app, ["resume", str(run_dir)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SpeculationAuthorizationError)
    assert (run_dir / "events.jsonl").read_bytes() == before[0]
    assert (run_dir / "config.snapshot.json").read_bytes() == before[1]
    assert (run_dir / "task.snapshot.json").read_bytes() == before[2]


def test_run_authorization_preflight_precedes_snapshot_and_reopen_writes(tmp_path):
    from looplab.engine.orchestrator import SpeculationAuthorizationError

    run_dir = tmp_path / "stale-run"
    before = _stale_speculation_prefix(run_dir)
    result = runner.invoke(app, [
        "run", str(run_dir / "task.snapshot.json"), "--no-genesis", "--out", str(run_dir),
        "-s", "backend=toy",
    ])

    assert result.exit_code == 1
    assert isinstance(result.exception, SpeculationAuthorizationError)
    assert (run_dir / "events.jsonl").read_bytes() == before[0]
    assert (run_dir / "config.snapshot.json").read_bytes() == before[1]
    assert (run_dir / "task.snapshot.json").read_bytes() == before[2]


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
    import looplab.engine.genesis as genesis
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
    import looplab.engine.genesis as genesis
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
    import looplab.engine.genesis as genesis
    monkeypatch.setattr(cli, "make_llm_client", lambda settings, **k: object())
    monkeypatch.setattr(genesis, "author_task",
                        lambda goal, **k: genesis.GenesisResult(error="connection refused"))
    result = runner.invoke(app, ["run", "--goal", "predict x", "--out", str(tmp_path / "e")])
    assert result.exit_code != 0
    assert "couldn't reach the model" in result.output and "connection refused" in result.output


def test_run_genesis_vague_goal_asks_for_detail(tmp_path, monkeypatch):
    import looplab.cli as cli
    import looplab.engine.genesis as genesis
    monkeypatch.setattr(cli, "make_llm_client", lambda settings, **k: object())
    monkeypatch.setattr(genesis, "author_task",
                        lambda goal, **k: genesis.GenesisResult(task={}, reply="What data do you have?"))
    result = runner.invoke(app, ["run", "--goal", "make it good", "--out", str(tmp_path / "v")])
    assert result.exit_code != 0
    assert "couldn't author a task" in result.output and "What data do you have?" in result.output


def test_file_settings_override_env(tmp_path, monkeypatch):
    # The documented precedence: a file's settings: block wins over a LOOPLAB_* env var.
    monkeypatch.setenv("LOOPLAB_MAX_NODES", "99")
    cfg = tmp_path / "r.yaml"
    cfg.write_text("task:\n  kind: quadratic\n  goal: g\n  direction: min\nsettings:\n  max_nodes: 2\n")
    result = runner.invoke(app, ["run", str(cfg), "--out", str(tmp_path / "r")])
    assert result.exit_code == 0, result.output
    assert "nodes=2" in result.output          # file beat env (99)


def test_parallel_aliases_preserve_cli_file_snapshot_over_env_precedence(monkeypatch):
    from looplab.core.appconfig import build_settings
    from looplab.core.config import Settings

    monkeypatch.setenv("LOOPLAB_EVAL_PARALLEL", "9")
    from_file = build_settings({"max_parallel": 3}, {}, {})
    assert from_file.eval_parallel == 3       # legacy file beats lower-priority canonical env
    old_snapshot = Settings(**{"max_parallel": 4})
    assert old_snapshot.eval_parallel == 4    # legacy snapshot beats canonical env too

    cli_legacy = build_settings({"eval_parallel": 8}, {}, {"max_parallel": 2})
    assert cli_legacy.eval_parallel == 2      # CLI legacy beats lower-priority file canonical


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
    from looplab.core.appconfig import coerce_scalar, parse_sets
    assert parse_sets(["llm_model =  qwen3:8b "]) == {"llm_model": "qwen3:8b"}   # key + value stripped
    assert coerce_scalar("NaN") == "NaN" and coerce_scalar("Infinity") == "Infinity"
    assert coerce_scalar("null") is None and coerce_scalar("3") == 3


def test_run_kind_pins_genesis(tmp_path, monkeypatch):
    # --kind does NOT skip genesis: it pins the kind, and genesis fills the rest within it.
    import looplab.cli as cli
    import looplab.engine.genesis as genesis
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


def test_approve_rejects_a_nonexistent_node_id(tmp_path):
    """final ultra-review §F: `approve --node-id <typo>` must fail loudly rather than append a grant
    the fold silently ignores (subject-bound approval only honors a real candidate) while printing
    'approved' — the confusing no-op the review flagged."""
    from looplab.events.eventstore import EventStore
    rd = tmp_path / "run"
    rd.mkdir()
    es = EventStore(rd / "events.jsonl")
    es.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    es.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                               "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    es.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    es.append("approval_requested", {"node_id": 0, "generation": 0})
    result = runner.invoke(app, ["approve", str(rd), "--node-id", "999"])
    assert result.exit_code == 2, result.output
    assert "no node #999" in result.output
    assert not any(e.type == "approval_granted" for e in EventStore(rd / "events.jsonl").read_all())
    # approving the real node works
    ok = runner.invoke(app, ["approve", str(rd), "--node-id", "0"])
    assert ok.exit_code == 0, ok.output
    assert any(e.type == "approval_granted" for e in EventStore(rd / "events.jsonl").read_all())


def test_approve_with_no_best_node_errors(tmp_path):
    """`approve` with no --node-id and no evaluated best must refuse, not append a bare `None` grant
    that folds to approved=True and finalizes a run with no champion (code-review test gap)."""
    from looplab.events.eventstore import EventStore
    rd = tmp_path / "run"
    rd.mkdir()
    es = EventStore(rd / "events.jsonl")
    es.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    es.append("approval_requested", {})
    result = runner.invoke(app, ["approve", str(rd)])               # no --node-id, no evaluated node
    assert result.exit_code == 2, result.output
    assert "no verifiable pending approval subject" in result.output
    assert not any(e.type == "approval_granted" for e in EventStore(rd / "events.jsonl").read_all())


def test_approve_defaults_to_pending_subject_even_when_id_is_zero(tmp_path):
    """`approve` with no --node-id ratifies the exact pending subject; guard the falsy-zero footgun."""
    from looplab.events.eventstore import EventStore
    rd = tmp_path / "run"
    rd.mkdir()
    es = EventStore(rd / "events.jsonl")
    es.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    es.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                               "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    es.append("node_evaluated", {"node_id": 0, "metric": 0.5})      # node 0 IS the best
    es.append("approval_requested", {"node_id": 0, "generation": 0})
    result = runner.invoke(app, ["approve", str(rd)])
    assert result.exit_code == 0, result.output
    grants = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "approval_granted"]
    assert len(grants) == 1 and grants[0].data.get("node_id") == 0


def test_approve_never_defaults_to_a_different_best_or_preapproves(tmp_path):
    from looplab.events.eventstore import EventStore
    rd = tmp_path / "run"
    rd.mkdir()
    es = EventStore(rd / "events.jsonl")
    es.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    for node_id, metric in ((0, 10.0), (7, 1.0)):
        es.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                   "idea": {"operator": "draft", "params": {}, "rationale": ""}})
        es.append("node_evaluated", {"node_id": node_id, "generation": 0, "metric": metric})

    early = runner.invoke(app, ["approve", str(rd)])
    assert early.exit_code == 2 and "not currently awaiting" in early.output
    es.append("approval_requested", {"node_id": 0, "generation": 0})
    approved = runner.invoke(app, ["approve", str(rd)])
    assert approved.exit_code == 0, approved.output
    grant = [event for event in es.read_all() if event.type == "approval_granted"][-1]
    assert grant.data == {"node_id": 0, "generation": 0}  # best is #7, pending subject is #0


def test_asset_brief_llm_mode_does_not_crash(tmp_path):
    # `asset-brief <repo> --llm` referenced an undefined `run_dir`, so the agentic mode raised NameError
    # and was 100% broken. It must at least reach the offline degrade path (no reachable endpoint here).
    r = runner.invoke(app, ["asset-brief", str(tmp_path), "--llm"])
    assert not isinstance(r.exception, NameError), r.exception
    assert r.exit_code == 0, r.output


def test_inspect_model_override_targets_llm_model_field():
    # concept-steward/task-facets/claim-steward used model_copy(update={"model":...}); the Settings field
    # is `llm_model`, so the override was silently dropped. Guard the coercion the CLI relies on.
    from looplab.core.config import Settings
    s = Settings()
    s.llm_model = "my-override-model"
    assert s.llm_model == "my-override-model"
    # the old buggy form wrote a phantom attr and left llm_model at the default
    assert Settings().model_copy(update={"model": "x"}).llm_model == Settings().llm_model
