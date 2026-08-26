"""`--full-context` gives this arm what the ARENA gives its own agent, and nothing more.

Measured on 2026-08-26, on the campaign's own logs: AlgoTuner shows its agent `Speedup: X` with
`Valid Solutions: Y%` on the TRAIN split 17-61 times per task, plus `eval_input` 207-429 times and
`profile` 58-194 times. Our task text said, in capitals, "YOU CANNOT MEASURE YOUR OWN SCORE, AND
YOU ARE NOT MEANT TO" -- so the loop timed probes at sizes it invented: `convex_hull`'s real n is
267 021 and the probes that chose its champion ran at n = 100, 1 000 and 10 000.

These tests pin the three halves of the repair: the size is READ from the dataset, the measurement
is REACHABLE, and the test split stays out of reach.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "make_task.py"

REFERENCE = '''
class Task:
    def solve(self, problem):
        return []

    def is_solution(self, problem, solution):
        return True
'''


def _root(tmp: Path, *, n: int = 4408, with_dataset: bool = True, task: str = "demo") -> Path:
    root = tmp / "AlgoTune"
    src = root / "AlgoTuneTasks" / task
    src.mkdir(parents=True)
    (src / "description.txt").write_text("a demo task\n", encoding="utf-8")
    (src / f"{task}.py").write_text(REFERENCE, encoding="utf-8")
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    if with_dataset:
        data = root / ".hf_datasets" / "oripress__AlgoTune" / "data" / task
        data.mkdir(parents=True)
        # A ragged adjacency list plus an external array reference: both real shapes from the
        # campaign's own dataset, and both were described WRONGLY by the first implementation.
        import numpy

        npy = data / "_npy_data"
        npy.mkdir()
        arr = numpy.zeros((n, 2), dtype=numpy.float64)
        numpy.save(npy / "a.npy", arr)
        record = {"k": n, "seed": 42, "problem": {
            "adjacency_list": [[1, 2, 3], [4], [], [5, 6]],
            "points": {"__type__": "ndarray_ref", "npy_path": "_npy_data/a.npy"},
        }}
        for subset in ("train", "test"):
            (data / f"{task}_T100ms_n{n}_size100_{subset}.jsonl").write_text(
                "\n".join(json.dumps(record) for _ in range(100)) + "\n", encoding="utf-8")
    return root


def _build(tmp: Path, *flags: str, root: Path | None = None, task: str = "demo") -> dict | str:
    root = root or _root(tmp, task=task)
    out = tmp / "out"
    out.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--algotune-root", str(root),
         "--task", task, "--out-dir", str(out), *flags],
        capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        return proc.stderr
    return json.loads((out / f"algotune_{task}.json").read_text(encoding="utf-8"))


def test_the_size_reaches_the_goal_and_comes_from_the_dataset(tmp_path):
    spec = _build(tmp_path, "--deliver", "--full-context")
    assert isinstance(spec, dict), spec
    goal = spec["goal"]
    assert "n = 4408" in goal, f"the measured size never reached the goal:\n{goal[-1500:]}"
    assert "100 instances" in goal
    assert "about 100 ms" in goal, "the per-instance time target is what makes the size actionable"


def test_the_false_clause_is_gone_and_the_true_half_stays(tmp_path):
    spec = _build(tmp_path, "--deliver", "--full-context")
    goal = spec["goal"]
    assert "YOU CANNOT MEASURE" not in goal, "the arm is still told the metric is unknowable"
    assert "YOU CAN MEASURE YOUR OWN SCORE" in goal
    assert "YOUR OUTPUT IS THE FILE" in goal, (
        "--deliver's true half -- write the solver early -- was thrown out with its false half")


def test_without_the_flag_the_goal_is_what_the_campaign_ran(tmp_path):
    """The falsifier for a change that quietly rewrites the arm already measured."""
    spec = _build(tmp_path, "--deliver")
    goal = spec["goal"]
    assert "YOU CANNOT MEASURE YOUR OWN SCORE" in goal
    assert "YOU CAN MEASURE" not in goal
    assert "n = 4408" not in goal
    assert "developer_commands" not in spec


def test_the_measurement_is_reachable_and_is_the_train_split(tmp_path):
    spec = _build(tmp_path, "--deliver", "--full-context")
    cmds = spec.get("developer_commands") or []
    assert [c["name"] for c in cmds] == ["eval_train"], cmds
    argv = cmds[0]["command"]
    assert "--subset" in argv and argv[argv.index("--subset") + 1] == "train", argv
    assert "looplab_eval.py" in " ".join(argv), "it must run the SAME bridge the scorer runs"
    assert cmds[0]["timeout"] <= 600, "DeveloperCommandSpec refuses more than 600 s"
    assert 'run_dev_command("eval_train")' in spec["goal"], (
        "a capability the goal never names is a capability the model will not use")


def test_the_spec_validates_as_a_repo_task(tmp_path):
    """A task the engine refuses to load is not a feature."""
    from looplab.adapters.repo_task import RepoTask

    spec = _build(tmp_path, "--deliver", "--full-context")
    task = RepoTask.model_validate(spec)
    assert [c.name for c in task.developer_commands] == ["eval_train"]


def test_an_external_array_is_described_as_an_array(tmp_path):
    """The first implementation printed `points: {__type__: str, npy_path: str}` for `convex_hull`
    -- telling the reader a (267021, 2) float64 array was a two-key dict of strings. Misinforming
    about the shape is the exact failure this clause exists to end."""
    spec = _build(tmp_path, "--deliver", "--full-context")
    goal = spec["goal"]
    assert "ndarray(shape=(4408, 2), dtype=float64)" in goal, goal[-900:]
    assert "npy_path" not in goal, "the pointer leaked into the description instead of its target"


def test_a_ragged_list_says_its_lengths_vary(tmp_path):
    """`edge_expansion`'s adjacency list has 4408 entries of length 0 to 19. Reporting the first
    one's length as the shape would tell the reader the graph is regular, and an algorithm chosen
    for a regular graph is the wrong algorithm."""
    spec = _build(tmp_path, "--deliver", "--full-context")
    assert "lengths vary: 0..3" in spec["goal"], spec["goal"][-900:]


def test_the_test_split_stays_out_of_reach(tmp_path):
    """Both splits live in one directory and share one `_npy_data`. Nothing may hand over either."""
    spec = _build(tmp_path, "--deliver", "--full-context")
    assert not spec.get("data"), f"a data mount appeared: {spec.get('data')}"
    blob = json.dumps(spec)
    assert "_test.jsonl" not in blob and "_npy_data" not in blob, blob[:400]
    for cmd in spec.get("developer_commands") or []:
        argv = cmd["command"]
        # NOT a bare `"test" not in argv`: pytest's own tmp dir is named after this test, so that
        # spelling failed on the harness rather than on the code. The claim is about the SPLIT.
        assert "--subset" in argv and argv[argv.index("--subset") + 1] == "train", argv
        assert not any(str(a).endswith("_test.jsonl") for a in argv), argv


def test_a_task_with_no_dataset_is_refused_not_silently_degraded(tmp_path):
    """A flag that falls back to the old behaviour without saying so mislabels the arm."""
    root = _root(tmp_path, with_dataset=False)
    err = _build(tmp_path, "--deliver", "--full-context", root=root)
    assert isinstance(err, str), "it built a --full-context task with no context"
    assert "no train split found" in err, err
