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


def test_the_opt_out_reproduces_the_goal_the_campaign_ran(tmp_path):
    """The falsifier for a change that quietly rewrites the arm already measured.

    `--full-context` became the DEFAULT on 2026-08-26 -- the reference agent is shown the graded
    metric 17-61 times per task, so withholding it was a handicap applied to one arm, not a neutral
    default. But the twenty numbers taken without it must stay reproducible, and that is what
    `--no-full-context` is for. If this test goes red, those numbers can no longer be regenerated.
    """
    spec = _build(tmp_path, "--deliver", "--no-full-context")
    goal = spec["goal"]
    assert "YOU CANNOT MEASURE YOUR OWN SCORE" in goal
    assert "YOU CAN MEASURE" not in goal
    assert "n = 4408" not in goal
    assert "developer_commands" not in spec


def test_the_measurement_is_reachable_and_is_the_train_split(tmp_path):
    spec = _build(tmp_path, "--deliver", "--full-context")
    cmds = spec.get("developer_commands") or []
    # `profile` joined it on 2026-08-27 -- the arena's agent has had a line profiler all along
    # (`AlgoTuner/utils/profiler.py`, `line_profiler` 5.0.2 in the scoring venv) and ours had no way
    # to see WHERE its time went. Pinned by `test_algotune_profile_command.py`; this test is about
    # the MEASUREMENT command, so it names it rather than indexing position 0.
    assert [c["name"] for c in cmds] == ["eval_train", "profile"], cmds
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
    assert [c.name for c in task.developer_commands] == ["eval_train", "profile"]


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


def test_the_card_does_not_argue_with_itself_about_the_size(tmp_path):
    """`--role-split` names "the problem sizes" as an example of what nobody here knows. Under
    `--full-context` the goal states the measured n two paragraphs earlier, and a card that
    contradicts itself is one the model is right to disregard -- the failure `repo_developer.py`
    records for the probe clause ("two paragraphs contradicting each other is worse than either one
    alone"). What stays genuinely unknown at a known n is what the sentence must point at."""
    spec = _build(tmp_path, "--deliver", "--role-split", "--full-context")
    goal = spec["goal"]
    assert "n = 4408" in goal
    assert "nobody here knows yet (the problem sizes" not in goal, (
        "the card states the size as measured AND as unknowable in the same prompt")
    assert "nobody here knows yet" in goal, "the clause itself must survive, only its example changes"
    assert "how much memory a table would take at that n" in goal


def test_role_split_without_the_flag_is_unchanged(tmp_path):
    """The falsifier: the arm already measured must keep the wording it ran on."""
    spec = _build(tmp_path, "--deliver", "--role-split", "--no-full-context")
    assert "nobody here knows yet (the problem sizes, how much memory a table would take)" in \
        spec["goal"]


def test_every_role_gets_the_description_not_only_the_developer(tmp_path):
    """The description must ride the channel that reaches EVERYONE, not a Developer-only one.

    `RepoTask.agent_brief()` embeds `Goal:` and becomes the Developer's system prompt -- and the
    SAME `system` string is handed to `_run_step`, so a fresh multi-step session carries it too.
    `roles.py::_state_brief` opens with `Goal: {state.goal}` for the proposal roles. So the goal is
    the broad channel and `_data_brief()` (which needs a `data` mount we deliberately do not make)
    is not. This pins that: if the clause ever moves somewhere narrower, this fails.
    """
    from looplab.adapters.repo_task import RepoTask

    spec = _build(tmp_path, "--deliver", "--full-context")
    task = RepoTask.model_validate(spec)
    brief = task.agent_brief()
    for fact in ("n = 4408", "100 instances", "about 100 ms",
                 "ndarray(shape=(4408, 2), dtype=float64)", 'run_dev_command("eval_train")'):
        assert fact in brief, f"{fact!r} never reaches the developer's system prompt"
    assert f"Goal: {task.goal}" in brief, "the goal is no longer carried whole into the brief"


def test_full_context_is_what_you_get_without_asking(tmp_path):
    """The default IS the feature. Measured on this benchmark's own logs: the reference agent gets
    `Speedup: X` with `Valid Solutions: Y%` on the train split 17-61 times per task, `eval_input`
    207-429 times and `profile` 58-194 times. A default that withholds the instance size and any
    way to measure is a handicap applied to one arm, and the loop's answer to it was to invent
    sizes -- `convex_hull` is n = 267 021 and its champion was chosen from probes at n = 100,
    1 000 and 10 000."""
    spec = _build(tmp_path, "--deliver", "--one-card")
    goal = spec["goal"]
    assert "n = 4408" in goal, "the default no longer carries the measured size"
    assert "YOU CANNOT MEASURE" not in goal
    assert [c["name"] for c in spec.get("developer_commands") or []] == ["eval_train", "profile"]


def test_a_missing_split_warns_by_default_and_refuses_when_demanded(tmp_path):
    """Two different failures, and conflating them costs either way. Now that the default is ON, a
    hard failure would break every task whose split is not on this machine; but `--full-context`
    typed on the command line still means "I require this"."""
    import subprocess
    import sys as _sys

    root = _root(tmp_path, with_dataset=False)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    base = [_sys.executable, str(SCRIPT), "--algotune-root", str(root),
            "--task", "demo", "--out-dir", str(out), "--deliver"]

    implicit = subprocess.run(base, capture_output=True, text=True, timeout=180)
    assert implicit.returncode == 0, implicit.stderr
    assert "no train split found" in implicit.stderr, "it degraded in silence"
    assert "building WITHOUT it" in implicit.stderr

    demanded = subprocess.run(base + ["--full-context"], capture_output=True, text=True, timeout=180)
    assert demanded.returncode != 0, "an explicit --full-context accepted a task with no context"
    assert "refusing" in demanded.stderr


def test_the_eval_train_command_carries_the_same_ruler_as_the_scorer(tmp_path, monkeypatch):
    """Caught on the first real invocation, 2026-08-26 07:53, and it is the whole point of the tool.

    The dev command runs in a sandbox that does NOT carry the campaign's environment. Without
    `ALGOTUNE_EVAL_WORKERS` the bridge takes the SERIAL path and keys its baseline
    `<task>__train__lane22r3`, while the campaign's own warm entry is `<task>__train__w22x1r3`.
    So the command missed the cache and re-timed the reference IN THE SAME PASS -- 44 cache files
    became 45 -- which the bridge answers with `speedup: null` + `baseline_measured_in_pass`. A
    measurement tool that measures on a different ruler than the scorer, and then cannot return a
    number, is worse than no tool.
    """
    monkeypatch.setenv("ALGOTUNE_EVAL_WORKERS", "1")
    monkeypatch.setenv("ALGOTUNE_BASELINE_CACHE_DIR", "/tmp/some-cache")
    monkeypatch.delenv("ALGOTUNE_MIN_TIMEOUT_S", raising=False)

    spec = _build(tmp_path, "--deliver", "--full-context")
    env = (spec["developer_commands"][0]).get("env") or {}
    assert env.get("ALGOTUNE_EVAL_WORKERS") == "1", (
        f"the command runs on a different ruler than the scorer: {env}")
    assert env.get("ALGOTUNE_BASELINE_CACHE_DIR") == "/tmp/some-cache"
    assert "ALGOTUNE_MIN_TIMEOUT_S" not in env, (
        "an absent key must not be fabricated — a task built outside a campaign has no ruler to "
        "inherit")


def test_no_ruler_in_the_environment_pins_nothing(tmp_path, monkeypatch):
    """The falsifier for a fix that invents defaults: a task built outside a campaign must not be
    handed a ruler nobody chose."""
    for key in ("ALGOTUNE_EVAL_WORKERS", "ALGOTUNE_BASELINE_CACHE_DIR", "ALGOTUNE_MIN_TIMEOUT_S"):
        monkeypatch.delenv(key, raising=False)
    spec = _build(tmp_path, "--deliver", "--full-context")
    assert not ((spec["developer_commands"][0]).get("env") or {}), spec["developer_commands"][0]


def test_one_eval_train_call_cannot_cost_half_the_developer_session(tmp_path):
    """Measured on both probe attempts, 2026-08-26, and it is why neither wrote a file.

    The Developer's session is bounded at 1200 s of wall clock it cannot see. `eval_train` was
    pinned at the model's 600 s cap, so a single hung call cost HALF of it — and that is exactly
    what happened twice: one `run_dev_command` of 600 s returning `exit=-9` and `(no output)`,
    795 s and 730 s of the 1200 s gone into tools, ZERO nodes in ninety minutes against 29 minutes
    to first node for the same task without this command.

    450 s clears the slowest real evaluation observed on this split (374.6 s) by 20 % and caps a
    hung one at 37 % of the session. It does not make the tool safe — two bad calls still end a
    session — so the clause must also SAY what it costs.
    """
    spec = _build(tmp_path, "--deliver", "--full-context")
    cmd = (spec["developer_commands"] or [{}])[0]
    session_budget = 1200.0
    assert cmd["timeout"] <= 0.4 * session_budget, (
        f"one call can take {cmd['timeout']}s of a {session_budget}s session")
    assert cmd["timeout"] >= 374.6, (
        "it no longer clears the slowest evaluation actually observed on this split")

    goal = spec["goal"]
    assert "CHARGED TO A CLOCK YOU CANNOT SEE" in goal, (
        "the model is offered an expensive tool without being told what it is charged against")
    assert "write the solver FIRST" in goal
    assert "ends with no file written has produced nothing" in goal
def _times(root: Path, task: str, subset: str, regime: str, ms: float) -> Path:
    d = root / ".baseline_times"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{task}__{subset}__{regime}.json"
    path.write_text(json.dumps({str(i): ms for i in range(100)}), encoding="utf-8")
    return d


def test_the_reference_time_is_this_boxs_measurement_when_one_exists(tmp_path, monkeypatch):
    """The filename's `T100ms` is the DATASET BUILDER's machine, not this one.

    Live numbers, 2026-08-26: `convex_hull_T100ms_n267021_size100_train.jsonl` names 100 ms, and
    the per-instance reference timings the scorer divides by are 40.07 ms median
    (`convex_hull__train__w22x1r3.json`) and 29.45 ms (`convex_hull__train__lane22r3.json`). The
    goal card asserted the 100 ms as "MEASURED FROM THE DATASET ON THIS MACHINE -- not a guess, and
    not something to re-derive", so the Researcher sized its proposal against a reference 2.5-3.4x
    more expensive than the real one and predicted "~8-15x" for a solver its own probe measured at
    29.9 ms/instance -- about 1.0x on the ruler its nodes are scored on.
    """
    root = _root(tmp_path, task="demo")
    _times(tmp_path / "cache", "demo", "train", "w22x1r3", 40.07)
    monkeypatch.setenv("ALGOTUNE_BASELINE_CACHE_DIR", str(tmp_path / "cache" / ".baseline_times"))
    spec = _build(tmp_path, "--deliver", "--full-context", root=root)
    assert isinstance(spec, dict), spec
    goal = spec["goal"]
    assert "40 ms" in goal, f"the measured reference time never reached the goal:\n{goal[:2600]}"
    assert "100 ms of budget per instance" not in goal, (
        "the per-instance budget is still quoted from the dataset's file name")
    assert "MEASURED FROM THE DATASET ON THIS MACHINE -- not a guess" in goal, (
        "the SIZE is still read from the dataset and that half of the claim is true")


def test_an_unmeasured_box_attributes_the_number_instead_of_claiming_it(tmp_path, monkeypatch):
    """With nothing measured the target may still be quoted -- but as the builder's number."""
    monkeypatch.setenv("ALGOTUNE_BASELINE_CACHE_DIR", str(tmp_path / "empty"))
    spec = _build(tmp_path, "--deliver", "--full-context")
    goal = spec["goal"]
    assert "about 100 ms" in goal, "the only number there is must still be offered"
    assert "ON THE MACHINE THAT BUILT IT" in goal, (
        "an unmeasured target is being passed off as this machine's reference time")
    assert "order of magnitude" in goal


def test_regimes_that_disagree_are_shown_as_a_range(tmp_path, monkeypatch):
    """This process is not `taskset`-ed the way the run is, so it cannot know which regime scores
    the nodes. Picking one median and calling it THE number would restate the original defect with
    a smaller error; the spread is the honest answer."""
    _times(tmp_path / "cache", "demo", "train", "w22x1r3", 40.07)
    _times(tmp_path / "cache", "demo", "train", "lane22r3", 29.45)
    monkeypatch.setenv("ALGOTUNE_BASELINE_CACHE_DIR", str(tmp_path / "cache" / ".baseline_times"))
    goal = _build(tmp_path, "--deliver", "--full-context")["goal"]
    assert "29-40 ms" in goal, goal[:2600]
    assert "the slower end is not yours" in goal


def test_the_file_names_number_is_attributed_to_whoever_measured_it(tmp_path, monkeypatch):
    """Naming both numbers without saying whose is worse than naming one.

    The card quotes the measured per-instance cost AND the dataset's `T<N>ms`. If the second is
    printed bare, the reader has two candidate denominators and no rule; the run that produced this
    finding sized its whole proposal against the wrong one 360 times. The sentence that says the
    file name is the BUILDING machine's target is what makes the pair readable, so it is pinned.
    """
    monkeypatch.setenv("ALGOTUNE_BASELINE_CACHE_DIR", str(_times(tmp_path / "c", "demo", "train",
                                                                 "w22x1r3", 40.07)))
    spec = _build(tmp_path, "--deliver", "--full-context")
    goal = spec["goal"]
    assert "40 ms" in goal
    assert "the target the machine that BUILT the dataset hit" in goal, (
        "the file name's number is printed without saying whose machine it describes")
