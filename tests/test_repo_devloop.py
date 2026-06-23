"""RepoTask e2e Developer loop: a `setup` step (dependency install) runs before each eval,
and an eval FAILURE is fed back to the Developer's `repair` (the error-feedback loop fires
for repo tasks, where node.code is empty — the fix that lets an e2e agent fix runtime
errors / missing deps)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

from autornd.command_eval import run_command_eval
from autornd.models import Idea
from autornd.orchestrator import Engine
from autornd.policy import GreedyTree
from autornd.repo_task import EvalSpec, RepoTask
from autornd.sandbox import SubprocessSandbox

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}


# ------------------------------ setup step ----------------------------------

def test_setup_runs_before_eval(tmp_path):
    # The command needs a file that ONLY the setup step creates -> setup must run first.
    (tmp_path / "main.py").write_text(
        'import json, os\nassert os.path.exists("dep.txt")\nprint(json.dumps({"metric": 1.0}))\n',
        encoding="utf-8")
    res = run_command_eval([sys.executable, "main.py"], str(tmp_path), 60, _M,
                           setup=[sys.executable, "-c", "open('dep.txt','w').write('ok')"])
    assert res.exit_code == 0 and res.metric == 1.0


def test_setup_failure_short_circuits(tmp_path):
    res = run_command_eval([sys.executable, "-c", "print(1)"], str(tmp_path), 60, _M,
                           setup=[sys.executable, "-c", "import sys; sys.exit(2)"])
    assert res.exit_code == 2 and res.metric is None and "setup failed" in res.stderr


# ------------------------- error-feedback repair loop -----------------------

class _RepairDev:
    """Fails first (writes a bad config), then on repair writes a valid one — modelling an
    e2e agent that fixes a runtime error surfaced by the eval."""
    def __init__(self):
        self.last_files: dict[str, str] = {}
        self.repaired = False

    def implement(self, idea: Idea) -> str:
        self.last_files = {"config.json": json.dumps({"wrong": 1})}   # missing needed_x -> eval fails
        return ""

    def repair(self, idea: Idea, code: str, error: str) -> str:
        self.repaired = True
        self.last_files = {"config.json": json.dumps({"needed_x": 3.0})}  # fix
        return ""


def test_repair_loop_fires_for_repo_task_on_eval_failure(tmp_path):
    dev = _RepairDev()
    t = RepoTask(id="r", goal="g", direction="max", editable_path=str(FIXTURE),
                 edit_surface=["*.json"], protect=["ttrain_strict.py"],
                 eval=EvalSpec(command=[sys.executable, "ttrain_strict.py"], metric=_M))
    researcher, _ = t.build_roles()
    engine = Engine(tmp_path / "run", task=t, researcher=researcher, developer=dev,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=4))
    state = anyio.run(engine.run)
    assert dev.repaired                                   # repair WAS invoked (code is empty)
    # the draft failed (bad config); a debug node repaired it -> a node reached metric 0.0
    assert any(n.operator == "debug" for n in state.nodes.values())
    assert any(n.metric == 0.0 for n in state.nodes.values())
