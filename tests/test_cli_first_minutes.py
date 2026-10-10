"""The messages a terminal user meets in the minutes after the demo (doc 75 UX-02, UX-12, UX-24, UX-26).

* `looplab smoke` against a dead endpoint printed only "Connection error." and exit 1, while the run
  preflight's refusal of the same endpoint classified it and named the fix; both now read one table;
* a run under an external agent printed nothing while it waited for one;
* `looplab init`'s scaffold listed five of the seven developer backends and kept `backend: llm` active
  (outranking `LOOPLAB_BACKEND`), and said nothing about the `data.csv` and the model it needs.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.config import DEVELOPER_BACKENDS

ROOT = Path(__file__).resolve().parents[1]


def test_smoke_against_a_dead_endpoint_names_the_cause_and_the_fix(monkeypatch):
    """Driven against a REAL refused connection (port 9 on loopback), so the classifier sees the
    transport's own exception chain, as it would on a user's machine."""
    import looplab.cli.export_cmds as export_cmds

    monkeypatch.setenv("LOOPLAB_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    result = CliRunner().invoke(app, ["smoke"])
    assert result.exit_code == 1
    assert "text FAILED" in result.output and "[unreachable]" in result.output, result.output
    assert "LOOPLAB_LLM_BASE_URL" in result.output and "--backend toy" in result.output
    assert export_cmds.smoke.__doc__.startswith("Check the configured model endpoint")


def test_a_run_under_an_external_agent_says_it_is_waiting_for_one(tmp_path, monkeypatch):
    import looplab.cli.run_cmds as run_cmds

    monkeypatch.setattr(run_cmds, "_open_and_drive", lambda *a, **k: None)   # no real wait
    result = CliRunner().invoke(app, [
        "run", str(ROOT / "examples" / "toy_task.json"), "--backend", "toy",
        "-s", "external_harness=true", "--out", str(tmp_path / "ext")])
    assert result.exit_code == 0, result.output
    assert "waiting for an external agent" in result.output and "looplab harness-mcp" in result.output


def test_init_lists_every_developer_backend_and_leaves_the_default_backend_commented(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    text = (tmp_path / "looplab.yaml").read_text(encoding="utf-8")
    line = next(row for row in text.splitlines() if "developer_backend:" in row)
    assert line.split("#", 2)[-1].strip() == " | ".join(DEVELOPER_BACKENDS)
    assert "backend" not in yaml.safe_load(text)["settings"]
    assert "data.csv" in result.output and "looplab init --kind quadratic" in result.output


def test_init_of_an_offline_kind_runs_with_no_model_and_says_nothing_about_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["init", "--kind", "quadratic"])
    assert result.exit_code == 0 and "needs a model" not in result.output, result.output
    assert yaml.safe_load((tmp_path / "looplab.yaml").read_text())["settings"]["backend"] == "toy"
