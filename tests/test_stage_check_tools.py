"""The INTER-STAGE CHECKER may LOOK (doc 52 row 9) — the last judge in the engine off a blind tail.

What it was handed: `run.out[-4000:]` of a `run.out` that is itself a 64,000-byte tail clamp —
a few dozen records of a stage that may have run for hours. The two live-eval watchdogs
(2026-08-14) and the crash/timeout triage judge (2026-08-15) were moved off fixed slices onto
`tools/log_tools.py` because a slice was measured wrong on each of them; `docs/BACKLOG.md` §0.9
recorded this checker as the still-open residue, and it is the one whose verdict can END A NODE.

What this file drives, in the order the risk runs:
  1. THE PROPERTY — a checker that looks reaches evidence the tail structurally cannot hold (a
     silent fallback printed at the START of a stage whose tail is a healthy retrieval bar), and
     names it from the closed vocabulary;
  2. THE LINE — looking widens what the checker SEES and nothing it may SAY: prose and an
     out-of-enum kind are still `inconclusive` through the tools, exactly as without them;
  3. THE CONTRACT — `stage_check_tools=false`, no workdir, and no log yet each reproduce the
     historical prompt and the historical single completion byte for byte; the ONE difference
     with the tools on is the invitation, spliced at the end of the system message;
  4. THE MONEY — one stage check spends at most `STAGE_CHECK_LOOK_TURNS` turns and then the plain
     completion, never an unbounded loop, and a resumed pre-field run gains no look at all;
  5. the pipeline itself, through the real `run_command_eval`: a stage the checker condemns after
     looking is the failed stage, and the next stage does not run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings
from looplab.engine import train_monitor as tm
from looplab.engine.eval_stages import (STAGE_CHECK_LOOK_INVITATION, STAGE_CHECK_LOOK_TURNS,
                                        stage_check_verdict_line)
from looplab.engine.options import EngineOptions
from looplab.runtime.command_eval import STAGE_CHECK_INCONCLUSIVE, run_command_eval
from looplab.tools.log_tools import LogQueryTools

# The window `command_eval._run_stages` hands the checker, spelled here so a test that claims to be
# about the tail is actually about THAT tail.
CHECKER_WINDOW = 4000
FALLBACK_LINE = ("WARNING: loss became NaN at step 3 — falling back to the pretrained checkpoint "
                 "and continuing with evaluation only")


def _stage_log(records: int = 900) -> str:
    """A stage that fell over at its start and then evaluated a PRETRAINED model for an hour: the
    silent-fallback line is in the first 300 bytes, and everything after it is a healthy-looking
    retrieval bar ending in a plausible metric — so the last 4,000 characters say 'success'."""
    bar = "".join(f"\rRetrieving: {100 * i // records:3d}%|{'#' * (40 * i // records):<40}| "
                  f"{i}/{records} [{i // 60:02d}:{i % 60:02d}<00:00, 2.1it/s]"
                  for i in range(1, records + 1))
    return ("2026-09-06 10:00:00 | INFO | starting run rdrop-dcl\n"
            f"{FALLBACK_LINE}\n"
            "2026-09-06 10:00:02 | INFO | loaded checkpoint models/base/model.safetensors\n"
            + bar + "\nRECALL@100: 0.7261\n")


def _tail(text: str) -> str:
    return text[-CHECKER_WINDOW:]


class _Client:
    """A checker client with both halves of the contract: `complete_text` (the historical single
    completion, also the tool loop's fallback) and `chat` (the tool loop). Records everything."""

    def __init__(self, plain="OK", turns=None):
        self.plain, self.turns = plain, list(turns or [])
        self.completions, self.chats, self.results = [], [], []

    def complete_text(self, msgs):
        self.completions.append([dict(m) for m in msgs])
        return self.plain

    def chat(self, messages, tool_specs, tool_choice="auto"):
        self.chats.append(([dict(m) for m in messages], [dict(t) for t in tool_specs or []]))
        self.results.extend(m["content"] for m in messages if m.get("role") == "tool")
        if not self.turns:
            return {"content": "", "tool_calls": []}
        name, args = self.turns.pop(0)
        return {"content": "", "tool_calls": [
            {"id": f"t{len(self.chats)}", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _looker(answer: str) -> _Client:
    """A checker that searches the stage's own log once and then answers."""
    return _Client(turns=[("read_log", {"log": "train.log", "mode": "search",
                                        "pattern": "fall(ing)? back|Traceback|NaN"}),
                          ("answer", {"text": answer})])


def _engine(tmp_path, client, *, tools=True):
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    researcher, developer = task.build_roles()
    engine = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3),
                    stage_check_tools=tools)
    engine._eval_spec = {"metric": {"reader": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"}}
    engine._reflect_client = lambda: client
    return engine


def _node():
    from looplab.core.models import Idea, Node
    return Node(id=1, operator="improve",
                idea=Idea(operator="improve", params={}, rationale="r-drop on the DCL objective"))


STAGES = [{"name": "train", "check": True}, {"name": "score"}]


def _check_over(tmp_path, client, *, tools=True, log=None):
    """Build the callback BEFORE the stage writes (that is where the attempt's floor comes from),
    then write the log the stage 'produced', then check — the production order."""
    engine = _engine(tmp_path, client, tools=tools)
    check = engine._stage_check_fn(_node(), str(tmp_path), STAGES)
    if log is not None:
        (tmp_path / "train.log").write_text(log, encoding="utf-8")
    return check


# ------------------------------------------------------------------------------- 1. THE PROPERTY
def test_the_premise_the_tail_does_not_hold_the_evidence():
    log = _stage_log()
    assert FALLBACK_LINE in log and FALLBACK_LINE not in _tail(log)
    assert "RECALL@100: 0.7261" in _tail(log)


def test_the_checker_reads_the_evidence_the_tail_cannot_hold(tmp_path):
    client = _looker("I searched the stage log.\nFAIL silent_fallback: the log says "
                     "'falling back to the pretrained checkpoint' after a NaN loss at step 3")
    log = _stage_log()
    verdict = _check_over(tmp_path, client, log=log)("train", _tail(log))
    assert verdict is not None and verdict.kind == "silent_fallback"
    assert "pretrained" in verdict.concern
    # The tool call really ran, over the real log, inside the checker's own turn — and found the
    # line the window could not show.
    assert len(client.results) == 1 and "falling back to the pretrained" in client.results[0]
    assert not client.completions, "the plain completion is the fallback, not a second call"
    # What it was offered: the two log tools and the answer, nothing that reads a path.
    offered = {spec["function"]["name"] for spec in client.chats[0][1]}
    assert {"read_log", "metric_series", "answer"} <= offered
    assert not any(name in offered for name in ("read_file", "write_file", "run_command"))


# ------------------------------------------------------------------------------------ 2. THE LINE
def test_looking_widens_what_the_checker_sees_and_nothing_it_may_say(tmp_path):
    log = _stage_log()
    prose = _check_over(tmp_path / "a", _looker(
        "The retrieval bar finished but I am worried about the loss magnitude."), log=log)(
        "train", _tail(log))
    assert prose is not None and prose.kind == STAGE_CHECK_INCONCLUSIVE
    out_of_enum = _check_over(tmp_path / "b", _looker(
        "FAIL recall_below_previous_best: 0.7261 is below the champion"), log=log)("train", _tail(log))
    assert out_of_enum is not None and out_of_enum.kind == STAGE_CHECK_INCONCLUSIVE
    undeclared = _check_over(tmp_path / "c", _looker(
        "FAIL declared_condition_violated: only 3 of 15 epochs"), log=log)("train", _tail(log))
    assert undeclared is not None and undeclared.kind == STAGE_CHECK_INCONCLUSIVE
    assert _check_over(tmp_path / "d", _looker("Looked.\nOK — checkpoint saved, metric present"),
                       log=log)("train", _tail(log)) is None


def test_stage_check_verdict_line_reads_the_verdict_out_of_a_report():
    assert stage_check_verdict_line("I looked at the log.\nFAIL crash: Traceback at record 12") \
        == "FAIL crash: Traceback at record 12"
    assert stage_check_verdict_line("  OK — fine  ") == "OK — fine"
    assert stage_check_verdict_line("no verdict here\njust prose") == "no verdict here\njust prose"
    assert stage_check_verdict_line("") == "" and stage_check_verdict_line(None) == ""


# -------------------------------------------------------------------------------- 3. THE CONTRACT
def test_tools_off_reproduces_the_historical_prompt_byte_for_byte(tmp_path):
    log = _stage_log()
    off = _Client(plain="OK")
    assert _check_over(tmp_path / "off", off, tools=False, log=log)("train", _tail(log)) is None
    on = _Client(plain="OK", turns=[("answer", {"text": "OK"})])
    assert _check_over(tmp_path / "on", on, tools=True, log=log)("train", _tail(log)) is None
    assert len(off.completions) == 1 and not off.chats
    assert len(on.chats) == 1 and not on.completions
    system_off, user_off = (m["content"] for m in off.completions[0][:2])
    system_on, user_on = (m["content"] for m in on.chats[0][0][:2])
    assert user_on == user_off
    assert system_on == system_off + "\n\n" + STAGE_CHECK_LOOK_INVITATION.format(stage="train")
    assert "train.log" in system_on and "YOU CAN LOOK FURTHER" not in system_off


def test_without_a_workdir_or_a_log_the_checker_never_looks(tmp_path):
    bare = _Client(plain="OK")
    engine = _engine(tmp_path / "bare", bare)
    assert engine._stage_check_fn(_node())("train", "loss=14.8\n") is None
    assert len(bare.completions) == 1 and not bare.chats
    quiet = _Client(plain="OK")
    assert _check_over(tmp_path / "quiet", quiet, log=None)("train", "loss=14.8\n") is None
    assert len(quiet.completions) == 1 and not quiet.chats


def test_the_gate_is_its_own_switch_and_total_over_a_stub(tmp_path):
    (tmp_path / "train.log").write_text(_stage_log(), encoding="utf-8")
    plan = tm.eval_log_plan(STAGES)
    assert tm.stage_check_tools(SimpleNamespace(), tmp_path, plan) is None
    assert tm.stage_check_tools(SimpleNamespace(_repair_log_tools=True, _train_monitor_tools=True),
                                tmp_path, plan) is None
    assert isinstance(tm.stage_check_tools(SimpleNamespace(_stage_check_tools=True), tmp_path, plan),
                      LogQueryTools)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert tm.stage_check_tools(SimpleNamespace(_stage_check_tools=True), empty, plan) is None


# ----------------------------------------------------------------------------------- 4. THE MONEY
def test_one_stage_check_spends_a_bounded_look_and_then_the_plain_completion(tmp_path):
    log = _stage_log()
    greedy = _Client(plain="INCONCLUSIVE: I ran out of turns",
                     turns=[("read_log", {"log": "train.log", "mode": "tail"})] * 40)
    verdict = _check_over(tmp_path, greedy, log=log)("train", _tail(log))
    assert verdict is not None and verdict.kind == STAGE_CHECK_INCONCLUSIVE
    assert len(greedy.chats) <= STAGE_CHECK_LOOK_TURNS + 2
    assert len(greedy.completions) == 1, "the exhausted loop degrades to ONE plain completion"


def test_a_pre_field_snapshot_resumes_without_the_look():
    assert Settings().stage_check_tools is True
    assert EngineOptions().stage_check_tools is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["stage_check_tools"] is False


# ------------------------------------------------------------------- 5. the pipeline, end to end
def test_a_stage_condemned_after_looking_is_the_failed_stage(tmp_path):
    """Through `run_command_eval` with the real `run.out[-4000:]` clamp in the loop: the stage prints
    the fallback line and then an hour of healthy bar, the checker finds the line through its tools,
    and the `score` stage does not run."""
    client = _looker("FAIL silent_fallback: the log says 'falling back to the pretrained checkpoint'")
    engine = _engine(tmp_path, client)
    Path(tmp_path, "train.py").write_text(
        "import sys\n"
        f"print({FALLBACK_LINE!r})\n"
        "for i in range(1, 901):\n"
        "    sys.stdout.write('\\rRetrieving: %3d%%| %d/900 [00:00<00:00, 2.1it/s]' % (100 * i // 900, i))\n"
        "print('\\nRECALL@100: 0.7261')\n", encoding="utf-8")
    Path(tmp_path, "score.py").write_text("print('RECALL@100: 0.7261')\n", encoding="utf-8")
    stages = [{"name": "train", "command": ["python", "train.py"], "check": True},
              {"name": "score", "command": ["python", "score.py"]}]
    result = run_command_eval(
        ["true"], str(tmp_path), 120, {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"},
        stages=stages, log_dir=str(tmp_path),
        check_fn=engine._stage_check_fn(_node(), str(tmp_path), stages))
    assert os.path.exists(tmp_path / "train.log")
    assert result.failed_stage == "train"
    rows = {row["name"]: row for row in result.stages}
    assert rows["train"]["status"] == "check_failed" and "score" not in rows
    assert "silent_fallback" in rows["train"]["concern"]
    assert client.results and "falling back to the pretrained" in client.results[0]
