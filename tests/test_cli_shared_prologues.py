"""Three CLI prologues that were written once per command (doc 25 CT-11, CT-13, CT-14).

The `looplab/cli/` package is where the same six lines get pasted, because each command reads as a
self-contained script. These three had already drifted:

* CT-13 — four commands re-derived "does this dir hold a run", two of them with a SHORTER message
  than the shared helper's, and each then re-opened its own `EventStore` on the path it had just
  checked. Three of the four also hand-paired the mid-file corruption check; pairing by hand is
  exactly how a fifth command would ship without it.
* CT-14 — five diagnostics wrote the same degrade-without-an-endpoint block, differing only in the
  fallback wording.
* CT-11 — `_persist_node_concepts` took a `state` it never read.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from looplab.cli import _require_healthy_log, _require_run_dir, app
from looplab.cli import concept_cmds, run_cmds
from looplab.events.eventstore import EventStore

runner = CliRunner()


# ------------------------------------------------------------------ CT-13: one run-dir prologue

@pytest.mark.parametrize("command", ["resume", "stop", "finalize", "repair-log"])
def test_every_mutating_command_reports_a_missing_run_the_same_way(tmp_path, command):
    """`stop` and `finalize` used to print a bare `no run found at <dir>` — no "(no events.jsonl)",
    no guidance — so the same mistake got a different, less actionable answer depending on which verb
    the operator typed."""
    result = runner.invoke(app, [command, str(tmp_path / "no_such_run")])
    assert result.exit_code == 2
    assert "no run found at" in result.output
    assert "(no events.jsonl)" in result.output


def test_each_command_keeps_its_own_second_sentence(tmp_path):
    """The `hint:` parameter exists so sharing the check does not flatten the advice: `resume` can
    say "use `run` to start one" where the generic answer would send the operator to `--out`."""
    resumed = runner.invoke(app, ["resume", str(tmp_path / "nope")]).output
    stopped = runner.invoke(app, ["stop", str(tmp_path / "nope")]).output
    repaired = runner.invoke(app, ["repair-log", str(tmp_path / "nope")]).output
    assert "use `run` to start one" in resumed
    assert "Pass the run directory created by" in stopped
    assert "repairs a run's event log" in repaired


def test_no_command_re_derives_the_existence_check():
    source = inspect.getsource(run_cmds)
    assert 'events.jsonl").exists()' not in source, (
        "a command re-derived the run-dir check instead of calling _require_run_dir")


def test_the_health_check_is_reached_through_the_shared_prologue():
    """`_require_healthy_log` is still importable and still standalone — `run` calls it on a dir it
    just CREATED, where there is no run to require yet — but no command may hand-pair it with its own
    existence check again."""
    source = inspect.getsource(run_cmds)
    assert source.count("_require_healthy_log(") == 1, (
        "only `run` (which creates the dir) may call the health check directly")
    assert "_require_healthy_log(store, out)" in source


def test_healthy_true_actually_refuses_a_mid_file_corruption(tmp_path):
    """Driven, not asserted from source: the flag has to reach the divergence check."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log = run_dir / "events.jsonl"
    log.write_text(
        json.dumps({"seq": 0, "type": "run_started", "data": {}, "ts": 0.0}) + "\n"
        + "{not json\n"
        + json.dumps({"seq": 1, "type": "node_created", "data": {}, "ts": 0.0}) + "\n",
        encoding="utf-8")
    with pytest.raises(typer.Exit) as info:
        _require_run_dir(run_dir, healthy=True)
    assert info.value.exit_code == 2
    # ... and the DEFAULT stays permissive, so the read commands still fold the recoverable prefix.
    assert isinstance(_require_run_dir(run_dir), EventStore)


def test_repair_log_must_not_fail_closed_on_the_log_it_repairs(tmp_path):
    """The one caller that deliberately opts out. If `repair-log` ever passed `healthy=True` it would
    refuse every log it exists to fix, and the only documented recovery path would be unreachable."""
    source = inspect.getsource(run_cmds.repair_log_cmd)
    assert "_require_run_dir(run_dir, hint=" in source
    # The CALL, not the word — the line above it explains in prose why the flag is absent.
    assert "healthy=True)" not in source and "healthy=True," not in source

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        json.dumps({"seq": 0, "type": "run_started", "data": {}, "ts": 0.0}) + "\n"
        + "{not json\n"
        + json.dumps({"seq": 1, "type": "node_created", "data": {}, "ts": 0.0}) + "\n",
        encoding="utf-8")
    result = runner.invoke(app, ["repair-log", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "repaired" in result.output


def test_the_shared_prologue_returns_the_store_it_validated(tmp_path):
    """Re-opening `EventStore(run_dir / "events.jsonl")` on the line below the check is the shape that
    let the two drift apart; the store comes back from the check that proved the path."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    store = _require_run_dir(run_dir)
    assert store.path == run_dir / "events.jsonl"
    for command in (run_cmds.stop, run_cmds.finalize):
        source = inspect.getsource(command)
        assert 'EventStore(run_dir / "events.jsonl")' not in source, (
            f"{command.__name__} re-opens the store the prologue already returned")


# ------------------------------------------------------------------ CT-14: one degrade block

def test_every_degrading_diagnostic_uses_the_shared_optional_client():
    source = inspect.getsource(concept_cmds)
    # The literal lives in `_optional_client`'s default argument and nowhere else. A command that
    # re-derived the block would spell it again.
    assert source.count('"no LLM endpoint') == 1
    assert source.count("_optional_client(") >= 6, (
        "the five degrading diagnostics plus the definition")


def test_the_optional_client_degrades_with_the_callers_wording(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def _boom(_settings):
        raise RuntimeError("no base_url configured")

    monkeypatch.setattr(concept_cmds, "_make_llm_client", _boom)
    echoed: list[str] = []
    monkeypatch.setattr(concept_cmds.typer, "echo", lambda msg, **kw: echoed.append(str(msg)))

    settings, client = concept_cmds._optional_client(run_dir, None, "showing heuristic tags only")
    assert client is None, "an unreachable endpoint degrades rather than raising"
    assert settings is not None, "the run's pinned settings still come back for the offline path"
    assert echoed == ["(no LLM endpoint: no base_url configured; showing heuristic tags only)"]


def test_lesson_guard_deliberately_does_not_degrade():
    """It is LLM-ONLY and exits 1. Folding it in would turn "I could not check this" into "checked,
    nothing found" — a different contract, not a different message."""
    source = inspect.getsource(concept_cmds.lesson_guard_cmd)
    assert "_optional_client(" not in source
    assert "_make_llm_client(" in source
    assert "raise typer.Exit(1)" in source


# ------------------------------------------------------------------ CT-11: no dead parameter

def test_the_persist_helper_takes_no_state():
    """It reads the log itself (it needs the CAS tail seq anyway), so a `state` parameter invited a
    reader to believe the caller's fold was consulted here — and to keep a stale one alive to pass."""
    params = inspect.signature(concept_cmds._persist_node_concepts).parameters
    assert "state" not in params
    assert list(params)[:4] == ["store", "raw_tags", "mode", "vocab_size"]


def test_no_caller_still_passes_one():
    source = inspect.getsource(concept_cmds)
    assert "_persist_node_concepts(store, state," not in source
    assert "_persist_node_concepts(store," in source
