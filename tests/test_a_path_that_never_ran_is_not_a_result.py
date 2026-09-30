"""A node whose declared new path never ran did not measure its idea: `inert_path`, metric withheld.

Measured 2026-09-23 on a MiniOneRec inference run. A node added a prefix-cached prefill behind a
self-check; the check died on an attribute transformers 5 removed, a fallback switched the path off,
and the node scored 1.004 with every list byte-identical to its parent -- recorded as an idea that
does not help, when the idea never executed. Both outcomes finish cleanly and print a number, so the
engine had no way to tell them apart until the node could SAY what running looks like.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import FAILURE_REASONS, Idea
from looplab.engine import activation
from looplab.engine.metric_salvage import NEVER_SALVAGED_REASONS
from looplab.engine.orchestrator import Engine
from looplab.engine.triage import _failure_reason
from looplab.events.eventstore import EventStore
from looplab.runtime.sandbox import RunResult, SubprocessSandbox
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


# ------------------------------------------------------------ the declaration

def test_markers_are_cleaned_and_bounded():
    assert activation.normalize_markers(["  prefix cache: ON ", "", 7, "prefix cache: ON"]) == [
        "prefix cache: ON"]
    assert activation.normalize_markers("one") == ["one"]
    assert activation.normalize_markers({"not": "a list"}) == []
    many = [f"m{i}" for i in range(20)]
    assert len(activation.normalize_markers(many)) == activation.MAX_MARKERS
    assert len(activation.normalize_markers(["x" * 999])[0]) == activation.MAX_MARKER_CHARS


def test_a_malformed_declaration_is_no_declaration(tmp_path):
    (tmp_path / activation.ACTIVATION_MANIFEST_NAME).write_text("{not json")
    assert activation.read_markers(tmp_path) == []
    (tmp_path / activation.ACTIVATION_MANIFEST_NAME).write_text(json.dumps({"markers": ["on"]}))
    assert activation.read_markers(tmp_path) == ["on"]
    assert activation.read_markers(tmp_path / "missing") == []


# ------------------------------------------------------------ the check

def test_a_marker_in_the_captured_streams_or_a_fresh_log_counts(tmp_path):
    assert activation.missing_markers(["ON"], texts=("warmup... ON\n",)) == []
    (tmp_path / "score.log").write_text("prefix cache: ON\n")
    assert activation.missing_markers(["prefix cache: ON"], texts=("",), workdir=tmp_path,
                                      since=time.time() - 60) == []
    assert activation.missing_markers(["never printed"], texts=("x",), workdir=tmp_path) == [
        "never printed"]


def test_a_log_left_by_an_earlier_attempt_does_not_vouch_for_this_one(tmp_path):
    stale = tmp_path / "score.log"
    stale.write_text("prefix cache: ON\n")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    assert activation.missing_markers(["prefix cache: ON"], texts=(), workdir=tmp_path,
                                      since=time.time()) == ["prefix cache: ON"]


def test_one_unreadable_log_entry_does_not_drop_every_log(tmp_path, monkeypatch):
    """crit_v51 F7: the sort key stat-ed every entry inside the listing's one `try`, so a dangling
    `latest.log` link emptied the listing and a marker `score.log` printed read as missing — a false
    `inert_path`. (A failing `stat` stands in for the link: creating one needs privileges on
    Windows.) MUTATION: the listing-wide key -> the marker is missing."""
    (tmp_path / "score.log").write_text("MARK fast path on\n")
    (tmp_path / "latest.log").write_text("")
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self.name == "latest.log":
            raise FileNotFoundError(str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    assert activation.missing_markers(["MARK fast path on"], texts=(), workdir=tmp_path,
                                      since=time.time() - 60) == []


@pytest.mark.posix_only("symlink")
def test_a_self_looping_log_link_does_not_drop_every_log(tmp_path):
    """crit_v52 F7-3: a real `loop.log -> loop.log` link raises ELOOP from `stat`, which is an
    `OSError` and not a `FileNotFoundError`. MUTATION: `_mtime`'s `except OSError` ->
    `except FileNotFoundError` -> the listing is empty and the marker reads as missing."""
    (tmp_path / "score.log").write_text("MARK fast path on\n")
    os.symlink("loop.log", tmp_path / "loop.log")
    assert activation.missing_markers(["MARK fast path on"], texts=(), workdir=tmp_path,
                                      since=time.time() - 60) == []


def test_an_appended_log_s_earlier_bytes_do_not_vouch_for_this_attempt(tmp_path):
    """crit_v52 F2 (crit_v51 F1b): a stage that RE-RAN appends to its log, so the file is fresh by
    mtime and still holds the earlier attempt's marker line. Only the bytes past the attempt-start
    cursor (`snapshot_training_logs`) are this attempt's. MUTATION: read from byte 0 whatever the
    cursor says -> the earlier line is credited."""
    from looplab.engine.eval_log_plan import snapshot_training_logs

    log, marker, since = tmp_path / "train.log", "MARK fast path on", time.time() - 60
    log.write_text(marker + "\n")                               # attempt 0 printed it
    snap = snapshot_training_logs(tmp_path)                      # attempt 1 starts
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("fast path self-check failed; falling back\n")  # attempt 1 re-ran: no marker
    assert activation.missing_markers([marker], workdir=tmp_path, since=since,
                                      snapshot=snap) == [marker]
    # …which the mtime floor alone credits: the file is fresh.
    assert activation.missing_markers([marker], workdir=tmp_path, since=since) == []
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(marker + "\n")                                  # this attempt printed it too
    assert activation.missing_markers([marker], workdir=tmp_path, since=since, snapshot=snap) == []


def test_a_log_this_attempt_created_or_rewrote_is_read_whole(tmp_path):
    """The cursor only ever SKIPS a prefix whose boundary still matches the attempt-start probe: a new
    log, a truncated one and a rewritten one (its bytes at the old boundary changed) are this
    attempt's from byte 0."""
    from looplab.engine.eval_log_plan import snapshot_training_logs

    marker, since = "MARK fast path on", time.time() - 60
    (tmp_path / "train.log").write_text("x" * 500 + "\n")
    (tmp_path / "infer.log").write_text("y" * 500 + "\n")
    snap = snapshot_training_logs(tmp_path)
    (tmp_path / "score.log").write_text(marker + "\n")          # created by this attempt
    assert activation.missing_markers([marker], workdir=tmp_path, since=since, snapshot=snap) == []
    (tmp_path / "score.log").unlink()
    (tmp_path / "train.log").write_text(marker + "\n")          # truncated and rewritten
    assert activation.missing_markers([marker], workdir=tmp_path, since=since, snapshot=snap) == []
    (tmp_path / "train.log").write_text("x" * 500 + "\n")
    snap = snapshot_training_logs(tmp_path)
    (tmp_path / "train.log").write_text(marker + "\n" + "z" * 600 + "\n")   # rewritten, longer
    assert activation.missing_markers([marker], workdir=tmp_path, since=since, snapshot=snap) == []


def test_a_log_the_candidate_rewrote_in_place_is_read_whole(tmp_path):
    """crit_v55 A1: a log the CANDIDATE writes in 'w' mode, rewritten with the same bytes up to the
    old end, passes the boundary probe — the cursor read the rewrite as an append and dropped the
    marker this attempt really printed. Only the ENGINE's logs (`engine_logs`) are cut at the cursor;
    they are append-only. MUTATION: cut every log at the cursor -> the marker is missing."""
    from looplab.engine.eval_log_plan import snapshot_training_logs

    marker, since = "MARK fast path on", time.time() - 60
    body = marker + "\n" + "".join(f"epoch {i} loss {1.0 / (i + 1):.4f}\n" for i in range(5))
    (tmp_path / "own.log").write_text(body)                     # the candidate's, attempt 0
    (tmp_path / "train.log").write_text(marker + "\n")          # the engine's, attempt 0
    snap = snapshot_training_logs(tmp_path)
    with open(tmp_path / "own.log", "w", encoding="utf-8") as fh:   # attempt 1: 'w', same bytes
        fh.write(body)
    with open(tmp_path / "train.log", "a", encoding="utf-8") as fh:  # the engine appends
        fh.write("fast path self-check failed; falling back\n")
    engine = frozenset({"setup.log", "train.log", "score.log"})
    assert activation.missing_markers([marker], workdir=tmp_path, since=since, snapshot=snap,
                                      engine_logs=engine) == []
    # …and the engine's appended log alone still vouches for nothing it did not print this time.
    (tmp_path / "own.log").unlink()
    assert activation.missing_markers([marker], workdir=tmp_path, since=since, snapshot=snap,
                                      engine_logs=engine) == [marker]
    # Case-folded on the name, as `EvalLogPlan.roles` keys are (a no-op off Windows).
    assert activation.missing_markers(
        [marker], workdir=tmp_path, since=since, snapshot=snap,
        engine_logs=frozenset(os.path.normcase(n) for n in engine)) == [marker]


def test_a_long_log_is_read_at_both_ends(tmp_path):
    """crit_v55 A3: the check read only a log's last 8 MiB, so a warmup marker followed by 9 MiB of
    training output read as missing — a false `inert_path`. Both ends are read now; only the middle
    of a log longer than twice the window goes unread, and that is the stated bound. MUTATION: read
    the tail alone -> the warmup marker is missing.

    Written as BYTES: the windows are byte counts, and a text write on Windows turns each `\n` into
    `\r\n` — 13 bytes a line where 12 were meant, which carried the "at most two windows" log past
    two windows on the Windows CI leg (master run 142)."""
    cap, since = activation._MAX_LOG_BYTES, time.time() - 60
    filler = "loss 0.1234\n" * (cap // 12 + 1)                     # a little over one window
    (tmp_path / "train.log").write_bytes(("WARMUP_ON\n" + filler + filler + "TAIL_ON\n").encode())
    assert activation.missing_markers(["WARMUP_ON", "TAIL_ON"], workdir=tmp_path, since=since) == []
    (tmp_path / "train.log").write_bytes((filler + filler + "MIDDLE_ON\n" + filler + filler).encode())
    assert activation.missing_markers(["MIDDLE_ON"], workdir=tmp_path, since=since) == ["MIDDLE_ON"]
    short = tmp_path / "short"
    short.mkdir()                                     # at most two windows: read whole, once
    under = "loss 0.1234\n" * (cap // 12 - 1)
    (short / "train.log").write_bytes((under + "MIDDLE_ON\n" + under).encode())
    assert (short / "train.log").stat().st_size <= 2 * cap
    assert activation.missing_markers(["MIDDLE_ON"], workdir=short, since=since) == []


def test_a_log_whose_boundary_is_unknown_is_not_read(tmp_path):
    """No credit is the refusing direction: a listing that failed at the attempt's start, or a log
    that could not be read then, vouches for nothing. MUTATION: treat an unknown floor as 0."""
    from looplab.engine.eval_log_plan import TrainingLogCursor, TrainingLogSnapshot, _log_path_key

    marker, since = "MARK fast path on", time.time() - 60
    (tmp_path / "train.log").write_text(marker + "\n")
    assert activation.missing_markers([marker], workdir=tmp_path, since=since,
                                      snapshot=TrainingLogSnapshot({}, complete=False)) == [marker]
    unread = TrainingLogSnapshot({_log_path_key(tmp_path / "train.log"): TrainingLogCursor(
        offset=None, identity=None, probe=None)})
    assert activation.missing_markers([marker], workdir=tmp_path, since=since,
                                      snapshot=unread) == [marker]


def test_the_match_is_exact_and_case_sensitive():
    assert activation.missing_markers(["Cache: ON"], texts=("cache: on",)) == ["Cache: ON"]


# ------------------------------------------------------------ the reason

def test_inert_path_is_a_registered_reason_that_is_never_salvaged():
    assert "inert_path" in FAILURE_REASONS
    assert "inert_path" in NEVER_SALVAGED_REASONS


def test_the_classifier_names_it_off_the_engine_s_own_flag():
    res = RunResult(exit_code=0, stdout="", stderr="", metric=None, timed_out=False,
                    inert_path={"missing": ["ON"], "metric": 1.004})
    assert _failure_reason(res) == "inert_path"
    clean = RunResult(exit_code=0, stdout="", stderr="", metric=None, timed_out=False)
    assert _failure_reason(clean) != "inert_path"


# ------------------------------------------------------------ driven through the real engine

class _Dev:
    def __init__(self):
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.errors: list = []

    def implement(self, idea):
        return "print('unused')\n"

    def repair(self, idea, code, error):
        self.errors.append(error)
        return code


class _Researcher:
    """Repairs once, then gives up -- so the test sees exactly one repair and then the terminal."""

    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    def triage_crash(self, node, error, attempt, **kw):
        if attempt <= 1:
            return {"action": "repair", "rationale": "make the declared path run"}
        return {"action": "abandon", "rationale": "stop after one look"}


def _drive(tmp_path, *, prints: str, markers):
    """Seed one node whose files declare `markers`, and run the REAL `_evaluate` over a command that
    prints `prints` and then its metric."""
    run_dir = tmp_path / "run"
    dev = _Dev()
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=_Researcher(), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True)
    code = f"print({prints!r}); print('METRIC: 0.5')"
    eng._eval_spec = {"command": ["python", "-c", code], "cwd": ".",
                      "metric": {"kind": "stdout_regex", "pattern": "METRIC: ([0-9.]+)"},
                      "timeout": 120.0}
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    files = {}
    if markers is not None:
        files[activation.ACTIVATION_MANIFEST_NAME] = activation.manifest_text(markers)
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": "print('unused')\n", "files": files})

    async def _bounded() -> bool:
        with anyio.move_on_after(300) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the eval did not terminate"
    events = [e for e in EventStore(run_dir / "events.jsonl").read_all()
              if e.type in ("node_evaluated", "node_failed") and e.data.get("node_id") == 0]
    return events, dev


def test_a_declared_path_that_never_announced_itself_is_withheld_and_sent_to_repair(tmp_path):
    events, dev = _drive(tmp_path, prints="RECO_PREFIX_CACHED_PREFILL disabled: verification raised",
                         markers=["prefix cache: ON"])
    assert [e.type for e in events] == ["node_failed"], events
    assert events[0].data.get("reason") == "inert_path"
    assert dev.errors, "the node must reach repair, not a terminal verdict on its idea"
    assert "[inert_path]" in dev.errors[0] and "'prefix cache: ON'" in dev.errors[0]
    assert "0.5" in dev.errors[0], "the withheld number is named, so the repair knows what it measured"


def test_a_path_that_announced_itself_is_scored_as_usual(tmp_path):
    events, dev = _drive(tmp_path, prints="prefix cache: ON", markers=["prefix cache: ON"])
    assert [e.type for e in events] == ["node_evaluated"], events
    assert events[0].data.get("metric") == 0.5
    assert dev.errors == []


def test_a_node_that_declares_nothing_is_judged_exactly_as_before(tmp_path):
    events, _dev = _drive(tmp_path, prints="anything at all", markers=None)
    assert [e.type for e in events] == ["node_evaluated"]
    events, _dev = _drive(tmp_path / "empty", prints="anything at all", markers=[])
    assert [e.type for e in events] == ["node_evaluated"]


_MARKED_TRAIN = "print('MARK fast path on'); open('ckpt.txt', 'w').write('v0')\n"
_FALLBACK_TRAIN = ("print('fast path self-check failed; falling back'); "
                   "open('ckpt.txt', 'w').write('v1')\n")
_FIXED_INFER = "open('preds.txt', 'w').write(open('ckpt.txt').read())\n"


def _two_stage_run(tmp_path, *, repairs, flaky: int = 1, repair_log_tools: bool = True,
                   train0: str = _MARKED_TRAIN):
    """crit_v51 F1's pipeline through the real sandbox and inline repair: attempt 0's `train` prints
    the marker and `infer` fails `flaky` times; each repair writes the next files of `repairs`. A
    repair that rewrites `train.py` RE-RUNS `train` — appending to `train.log`; one that fixes only
    `infer.py` REUSES it. Returns the node's terminal and `train.log`'s lines."""
    from looplab.adapters.repo_task import EvalSpec, RepoTask

    py = sys.executable
    src, run_dir = tmp_path / "src", tmp_path / "run"
    src.mkdir()
    counter = tmp_path / "flaky.count"
    (src / "train.py").write_text(train0)
    (src / "infer.py").write_text(
        f"import os\nc = {str(counter)!r}\nn = int(open(c).read()) if os.path.exists(c) else 0\n"
        f"if n < {flaky}:\n    open(c, 'w').write(str(n + 1)); raise RuntimeError('flaky OOM')\n"
        + _FIXED_INFER)
    (src / "looplab_eval.py").write_text("import json; print(json.dumps({'metric': 0.5}))\n")
    (src / "looplab_stages.json").write_text(json.dumps({"stages": [
        {"name": "train", "command": [py, "train.py"], "timeout": 120},
        {"name": "infer", "command": [py, "infer.py"], "timeout": 120}]}))
    (src / activation.ACTIVATION_MANIFEST_NAME).write_text(
        activation.manifest_text(["MARK fast path on"]))
    task = RepoTask(id="r", direction="max", editable_path=str(src), edit_surface=["*.py", "*.json"],
                    eval=EvalSpec(command=[py, "looplab_eval.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}, cwd="."))
    plan = list(repairs)

    class Dev:
        last_files: dict = {}
        last_deleted: list = []

        def implement(self, idea):
            return ""

        def repair(self, idea, code, error):
            self.last_files = dict(plan.pop(0)) if plan else {}
            return ""

    researcher, _ = task.build_roles()
    eng = Engine(run_dir, task=task, researcher=researcher, developer=Dev(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=4,
                 inline_repair_retrain_cap=2)
    eng._repair_log_tools = repair_log_tools   # False: no watcher takes a snapshot for the check
    anyio.run(eng.run)
    events = EventStore(run_dir / "events.jsonl").read_all()
    log = (run_dir / "nodes" / "node_0" / "train.log").read_text().splitlines()
    return [(e.type, e.data.get("reason"), e.data.get("metric")) for e in events
            if e.type in ("node_evaluated", "node_failed")], log


@pytest.mark.parametrize("repair_log_tools", [True, False])
def test_a_re_run_stage_s_earlier_marker_does_not_vouch_for_the_scored_path(tmp_path,
                                                                          repair_log_tools):
    """crit_v52 F2, driven: the repaired `train` falls back and prints no marker; its log still holds
    attempt 0's line. The number measured the fallback, so it is withheld (`inert_path`) — it was
    scored 0.5 as the declared path before the attempt-start cursor. Both ways the check gets its
    cursor: the watchers' snapshot, and its own when nothing else takes one."""
    terminal, log = _two_stage_run(tmp_path, repairs=[
        {"train.py": _FALLBACK_TRAIN, "infer.py": _FIXED_INFER}], repair_log_tools=repair_log_tools)
    assert log[0] == "MARK fast path on" and "MARK fast path on" not in log[1:]
    assert terminal == [("node_failed", "inert_path", None)], terminal


def test_a_reused_stage_s_earlier_marker_does_not_vouch_for_it_either(tmp_path):
    """crit_v51 F1 (why 01ab182d was reverted): `train` re-ran as the fallback, then a repair of
    `infer` alone REUSED it — the scored checkpoint is the fallback's, and the appended log's only
    marker is attempt 0's. Crediting a reused stage's whole log scored it 0.5; the owed fix (doc 69,
    69.10b) may credit only the bytes of the stage's last `ok` run."""
    terminal, log = _two_stage_run(tmp_path, repairs=[
        {"train.py": _FALLBACK_TRAIN}, {"infer.py": _FIXED_INFER}], flaky=2)
    assert log[0] == "MARK fast path on" and "MARK fast path on" not in log[1:]
    assert terminal == [("node_failed", "inert_path", None)], terminal


# The marker goes ONLY to the candidate's own log, rewritten in 'w' mode by every run of `train`.
_OWN_LOG = ("f = open('own.log', 'w')\nf.write('MARK fast path on\\n')\n"
            "for i in range(5): f.write(f'epoch {i} loss {1.0 / (i + 1):.4f}\\n')\n")
_OWN_LOG_TRAIN = _OWN_LOG + "f.close(); open('ckpt.txt', 'w').write('v0')\n"


@pytest.mark.parametrize("case", ["later_stage_failed", "train_crashed"])
def test_a_marker_in_a_log_the_candidate_rewrote_is_credited(tmp_path, case):
    """crit_v55 A1, driven through the real sandbox and inline repair: `train` writes its marker to
    its OWN log in 'w' mode and re-runs deterministically — after `infer` failed (the repair touched
    `train.py`), or after `train` itself crashed past the marker (the fix grows the log past the old
    end). The bytes at the old boundary match, and the attempt-start cursor dropped the marker this
    attempt really printed: a real 0.5 withheld as `inert_path`."""
    if case == "later_stage_failed":
        terminal, _log = _two_stage_run(tmp_path, train0=_OWN_LOG_TRAIN, repairs=[
            {"train.py": _OWN_LOG_TRAIN + "# reviewed\n", "infer.py": _FIXED_INFER}])
    else:
        crash = _OWN_LOG + "f.flush()\nraise RuntimeError('checkpoint save failed: disk quota')\n"
        fixed = (_OWN_LOG + "f.write('saved checkpoint\\n'); f.close(); "
                 "open('ckpt.txt', 'w').write('v1')\n")
        terminal, _log = _two_stage_run(tmp_path, train0=crash, flaky=0,
                                        repairs=[{"train.py": fixed}])
    assert terminal == [("node_evaluated", None, 0.5)], terminal


def test_a_re_run_stage_that_prints_its_marker_again_is_scored(tmp_path):
    """The control: the same pipeline whose repaired `train` still takes the declared path."""
    terminal, _log = _two_stage_run(tmp_path, repairs=[
        {"train.py": _MARKED_TRAIN.replace("v0", "v1"), "infer.py": _FIXED_INFER}])
    assert terminal == [("node_evaluated", None, 0.5)], terminal


# ------------------------------------------------------------ the Developer declares it

def _build(monkeypatch, done_args, *, plan_steps=()):
    """A REAL `LLMRepoDeveloper.implement()` whose `done` carries `done_args`; returns its files."""
    import sys
    import looplab.agents.agent as agent_mod
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": list(plan_steps)})
        tools.execute("write_file", {"path": "solution.py", "content": "print('prefix cache: ON')\n"})
        return finalize(dict(done_args))

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task, plan_decompose=bool(plan_steps), plan_min_steps=2)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    return dev.last_files


def test_both_dones_offer_the_declaration():
    from looplab.adapters.repo_task import LLMRepoDeveloper
    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    for spec in (dev._emit_spec(), dev._repair_emit_spec()):
        props = spec["function"]["parameters"]["properties"]
        assert props["activation_markers"]["type"] == "array"
        assert "withheld" in props["activation_markers"]["description"]


def test_a_declared_marker_is_written_into_the_node_s_files(monkeypatch):
    files = _build(monkeypatch, {"summary": "s", "activation_markers": ["prefix cache: ON"]})
    assert json.loads(files[activation.ACTIVATION_MANIFEST_NAME]) == {"markers": ["prefix cache: ON"]}


def test_the_last_plan_step_declares_it_too(monkeypatch):
    files = _build(monkeypatch, {"summary": "s", "activation_markers": ["prefix cache: ON"]},
                   plan_steps=[{"title": "write it", "detail": "d"}, {"title": "wire it", "detail": "d"}])
    assert json.loads(files[activation.ACTIVATION_MANIFEST_NAME]) == {"markers": ["prefix cache: ON"]}


def test_no_declaration_writes_no_file(monkeypatch):
    assert activation.ACTIVATION_MANIFEST_NAME not in _build(monkeypatch, {"summary": "s"})


# ------------------------------------------------------------ a marker the evaluated code never prints
#
# Measured 2026-09-23, MiniOneRec inf11 node 0: declared `PER_DEPTH_SCORER_TEST_OK`, which only its
# TEST file printed; the service printed `PER_DEPTH_SCORER_ACTIVE depths=...`. The path ran, quality
# held, and the node was withheld as `inert_path` over a string the eval could never print.

from looplab.engine.repair_verify import activation_markers_not_in_code  # noqa: E402

_SERVICE = {"service/engine.py": "print('PER_DEPTH_SCORER_ACTIVE depths=', depths)\n",
            "tests/test_scorer.py": "print('PER_DEPTH_SCORER_TEST_OK depths=')\n"}


def test_a_marker_only_a_test_prints_is_bounced_and_says_where_it_was():
    out = activation_markers_not_in_code(["PER_DEPTH_SCORER_TEST_OK"], _SERVICE)
    assert "'PER_DEPTH_SCORER_TEST_OK'" in out and "tests/test_scorer.py" in out


def test_a_marker_the_service_prints_passes_and_a_fragment_is_enough():
    assert activation_markers_not_in_code(["PER_DEPTH_SCORER_ACTIVE"], _SERVICE) == ""
    assert activation_markers_not_in_code(["SCORER_ACTIVE depths"], _SERVICE) == ""


def test_a_marker_nowhere_at_all_is_bounced_and_none_declared_is_fine():
    assert "appears in no file" in activation_markers_not_in_code(["NEVER"], _SERVICE)
    assert activation_markers_not_in_code([], _SERVICE) == ""
    assert activation_markers_not_in_code(None, _SERVICE) == ""


def test_the_real_build_bounces_a_marker_only_its_test_prints(monkeypatch):
    import sys
    import looplab.agents.agent as agent_mod
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask

    refusals: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": []})
        tools.execute("write_file", {"path": "solution.py",
                                     "content": "print('PER_DEPTH_SCORER_ACTIVE')\n"})
        args = {"summary": "s", "activation_markers": ["PER_DEPTH_SCORER_TEST_OK"]}
        validate = opts.get("validate")
        if validate is not None and (refusal := validate(args)):
            refusals.append(refusal)
        return finalize(args)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    LLMRepoDeveloper(object(), task, plan_decompose=False).implement(
        Idea(operator="draft", params={}, rationale="x"))
    assert refusals and "'PER_DEPTH_SCORER_TEST_OK'" in refusals[0]


def test_a_marker_declared_on_an_earlier_step_is_still_checked_at_the_last(monkeypatch):
    """Measured 2026-09-23 (inf12 node 0): markers declared on steps 3 and 4 of a 5-step build, one
    of them in no file at all, and step 5 -- the only validated emit -- declared none. The check has
    to ask the node's EFFECTIVE declaration, not only the last emit's arguments."""
    import sys
    import looplab.agents.agent as agent_mod
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask

    refusals: list = []
    step = {"n": 0}

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": [{"title": "write the path", "detail": "d"},
                                       {"title": "finish", "detail": "d"}]})
        step["n"] += 1
        if step["n"] == 1:
            tools.execute("write_file", {"path": "solution.py", "content": "print('PATH_ACTIVE')\n"})
            return finalize({"summary": "wrote it",
                             "activation_markers": ["PATH_ACTIVE", "PATH_INIT_ACTIVE"]})
        args = {"summary": "done"}
        validate = opts.get("validate")
        if validate is not None and (refusal := validate(args)):
            refusals.append(refusal)
        return finalize(args)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    LLMRepoDeveloper(object(), task, plan_decompose=True, plan_min_steps=2).implement(
        Idea(operator="draft", params={}, rationale="x"))
    assert refusals and "'PATH_INIT_ACTIVE'" in refusals[0] and "'PATH_ACTIVE'" not in refusals[0]


def test_a_marker_only_a_docstring_or_comment_names_is_bounced():
    # Measured 2026-09-24: `FP8_DECODE_MLP_FALLBACK` was a docstring's word for the fallback; the
    # code printed `FP8_DECODE_MLP_ACTIVE`. The substring search passed it, and a path that had run
    # was filed `inert_path`.
    from looplab.engine.repair_verify import activation_markers_not_in_code
    body = ('"""Falls back to bf16 and prints ``FP8_DECODE_MLP_FALLBACK``."""\n'
            "# FP8_DECODE_MLP_OFF is what the old version printed\n"
            "def enable(chunk):\n"
            "    print(f'FP8_DECODE_MLP_ACTIVE chunk={chunk}')\n")
    out = activation_markers_not_in_code(["FP8_DECODE_MLP_FALLBACK"], {"svc/fp8.py": body})
    assert "svc/fp8.py" in out and "docstring or comment" in out
    assert activation_markers_not_in_code(["FP8_DECODE_MLP_OFF"], {"svc/fp8.py": body})
    assert activation_markers_not_in_code(["FP8_DECODE_MLP_ACTIVE"], {"svc/fp8.py": body}) == ""


def test_a_file_that_does_not_parse_or_is_not_python_is_searched_whole():
    from looplab.engine.repair_verify import activation_markers_not_in_code
    assert activation_markers_not_in_code(["GO_FAST on"], {"run.sh": "echo 'GO_FAST on'\n"}) == ""
    assert activation_markers_not_in_code(["GO_FAST on"], {"a.py": "print('GO_FAST on'\n"}) == ""
