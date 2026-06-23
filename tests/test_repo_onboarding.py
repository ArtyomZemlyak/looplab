"""RepoTask Phase 3 — onboarding: the agent proposes a trusted eval + metric adapter, a
human ratifies it (ratify_freeze) or it auto-confirms (autonomous), then the loop runs the
command-eval through the frozen, protected adapter."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

from looplab.command_eval import read_metric
from looplab.eventstore import EventStore
from looplab.models import Idea
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.replay import fold
from looplab.repo_task import RepoTask
from looplab.sandbox import SubprocessSandbox

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"

# An agent-written adapter that reads the metric the framework wrote to metrics.json.
_ADAPTER = ('import json, os\n'
            'def read_metric(workdir):\n'
            '    with open(os.path.join(workdir, "metrics.json")) as f:\n'
            '        return json.load(f)["metric"]\n')


def _onboarder():
    return {
        "eval_spec": {"command": [sys.executable, "ttrain.py"],
                      "metric": {"kind": "adapter", "path": "LOOPLAB_adapter.py"},
                      "params_style": "none", "timeout": 60},
        "adapter_files": {"LOOPLAB_adapter.py": _ADAPTER},
        "goal": "read the metric from the tracker",
    }


def _task(**kw):
    return RepoTask(id="onb", goal="maximize", direction="max", onboard=True, eval=None,
                    editable_path=str(FIXTURE), edit_surface=["*.json"], protect=["ttrain.py"], **kw)


def _engine(rd, developer, trust="ratify_freeze"):
    t = _task()
    researcher, dev = t.build_roles()
    return Engine(rd, task=t, researcher=researcher, developer=developer or dev,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                  onboarder=_onboarder, eval_trust_mode=trust)


# ------------------------------- unit ---------------------------------------

def test_adapter_metric_reader(tmp_path):
    (tmp_path / "LOOPLAB_adapter.py").write_text(_ADAPTER, encoding="utf-8")
    (tmp_path / "metrics.json").write_text('{"metric": 0.7}', encoding="utf-8")
    assert read_metric("", str(tmp_path), {"kind": "adapter", "path": "LOOPLAB_adapter.py"}) == 0.7
    # a broken adapter -> None (node_failed), never crashes the caller
    (tmp_path / "bad.py").write_text("def read_metric(w): return 1/0\n", encoding="utf-8")
    assert read_metric("", str(tmp_path), {"kind": "adapter", "path": "bad.py"}) is None


def test_fold_onboarding_events():
    ev = [type("E", (), {"type": "spec_proposed", "data": {"eval_spec": {"x": 1}}})(),
          type("E", (), {"type": "spec_approved", "data": {}})()]
    st = fold(ev)
    assert st.proposed_spec == {"eval_spec": {"x": 1}} and st.spec_confirmed


# ---------------------------- ratify_freeze ---------------------------------

def test_ratify_freeze_pauses_then_runs_after_approval(tmp_path):
    rd = tmp_path / "run"
    state = anyio.run(_engine(rd, developer=None).run)
    # Paused: proposed + approval requested, NOT confirmed, NOT finished.
    assert not state.finished and state.proposed_spec is not None
    assert state.spec_approval_requested and not state.spec_confirmed

    EventStore(rd / "events.jsonl").append("spec_approved", {})   # human ratifies
    state2 = anyio.run(_engine(rd, developer=None).run)           # resume
    assert state2.finished and state2.best().metric == -9.0       # eval ran via the adapter
    assert (rd / "nodes" / "node_0" / "LOOPLAB_adapter.py").exists()  # frozen adapter written


def test_cli_approve_ratifies_pending_spec(tmp_path):
    rd = tmp_path / "run"
    anyio.run(_engine(rd, developer=None).run)                    # pauses awaiting spec
    from typer.testing import CliRunner
    from looplab.cli import app
    res = CliRunner().invoke(app, ["approve", str(rd)])
    assert res.exit_code == 0 and "eval spec" in res.output
    assert fold(EventStore(rd / "events.jsonl").read_all()).spec_confirmed


# ----------------------------- autonomous -----------------------------------

class _CheatAdapterDev:
    """Tries to overwrite the frozen adapter with one that always returns 1.0."""
    def __init__(self):
        self.last_files: dict[str, str] = {}

    def implement(self, idea: Idea) -> str:
        self.last_files = {"LOOPLAB_adapter.py": "def read_metric(w):\n    return 1.0\n"}
        return ""


def test_autonomous_auto_confirms_and_freezes_adapter(tmp_path):
    rd = tmp_path / "run"
    # autonomous: no human gate; the agent ships a cheat adapter that must be IGNORED
    # (the ratified one is frozen + protected) -> true baseline metric -9, not 1.0.
    state = anyio.run(_engine(rd, developer=_CheatAdapterDev(), trust="autonomous").run)
    assert state.finished and state.spec_confirmed
    assert state.best().metric == -9.0                           # cheat adapter rejected
    assert "return 1.0" not in (rd / "nodes" / "node_0" / "LOOPLAB_adapter.py").read_text()
