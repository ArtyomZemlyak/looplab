"""The two official MLE-bench extras as post-run instruments (doc 52 row 22).

`mle-bench/extras` runs an LLM rule-violation detector over the agent's code and logs and a Dolos
plagiarism check against public kernels; neither had a counterpart in the tree. Both are records —
`<run_dir>/mlebench_extras.json` — and move nothing. Driven here through a real toy run, a real
fold, a fake `dolos` on PATH that writes the CSVs the real one writes, and an injected judge.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import anyio
import pytest

from looplab.adapters.mlebench_extras import (
    EXTRAS_SIDECAR, MLEBENCH_RULES, RULE_IDS, RuleFinding, RuleViolationVerdict, champion_record,
    extras_report, plagiarism_check, rule_violation_judge, rule_violation_messages)
from looplab.core.evidence import EVIDENCE_LABEL
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import make_engine


def _finished_run(tmp_path):
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2)
    anyio.run(eng.run)
    return tmp_path / "run"


# ------------------------------------------------------------------ the rule-violation judge

def test_the_prompt_carries_the_rules_the_description_and_the_evidence_under_the_label():
    msgs = rule_violation_messages(rules=MLEBENCH_RULES, description="predict spooky authors",
                                   code="import pandas\n", transcript="[build] rationale: tf-idf")
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert all(rid in system for rid in RULE_IDS), "every rule id is in the system message"
    assert "predict spooky authors" in user and "import pandas" in user and "tf-idf" in user
    assert EVIDENCE_LABEL in user and user.index(EVIDENCE_LABEL) < user.index("import pandas"), (
        "the code and transcript are evidence, labelled before they start")
    assert "quoted" in system.lower(), "a finding without a quoted line is asked not to be made"


def test_the_judge_returns_a_plain_record_and_marks_an_unknown_rule():
    seen = {}

    def fake(msgs):
        seen["msgs"] = msgs
        return RuleViolationVerdict(verdict="violation", rationale="read the answer file", findings=[
            RuleFinding(rule="test_label_access", evidence="pd.read_csv('private/test.csv')", confidence=0.9),
            RuleFinding(rule="made_up_rule", evidence="x", confidence=0.2)])

    out = rule_violation_judge(None, description="d", code="c", transcript="t", judge=fake)
    assert out["status"] == "ok" and out["verdict"] == "violation"
    assert [f["rule"] for f in out["findings"]] == ["test_label_access", "made_up_rule"]
    assert [f["known_rule"] for f in out["findings"]] == [True, False]
    assert seen["msgs"][1]["content"].count(EVIDENCE_LABEL) == 1
    assert rule_violation_judge(None, description="d", code="c", transcript="t",
                                judge=lambda m: None)["status"] == "unanswered"


# ------------------------------------------------------------------ the transcript, off the log

def test_the_champion_record_rebuilds_a_transcript_from_the_durable_rows(tmp_path):
    rd = tmp_path / "r"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "try a tree"},
                                  "code": "print('solution')\n", "files": {"helper.py": "X = 1\n"}})
    store.append("node_repaired", {"node_id": 0, "attempt": 1, "code": "print('fixed')\n",
                                   "error_in": "NameError: y", "rationale": "define y", "changed": ["solution.py"]})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.5,
                                    "stdout_tail": '{"metric": 0.5}'})
    events = store.read_all()
    rec = champion_record(events, fold(events))
    assert rec["node_id"] == 0 and rec["metric"] == 0.5
    assert "[build] operator=draft rationale: try a tree" in rec["transcript"]
    assert "[repair 1] error: NameError: y" in rec["transcript"] and "define y" in rec["transcript"]
    assert '[node_evaluated] metric=0.5 tail: {"metric": 0.5}' in rec["transcript"]
    assert rec["code"].startswith("print('fixed')") and "# --- helper.py ---\nX = 1" in rec["code"], (
        "the code is the repaired champion plus its committed files, the scan surface's own shape")


# ------------------------------------------------------------------ plagiarism, through Dolos

def _fake_dolos(bin_dir: Path, *, similarity: str = "0.73", fail: bool = False):
    """A `dolos` that writes the two CSVs the real CLI writes for `run -f csv -o <dir> files…`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "dolos"
    body = "#!/bin/sh\nexit 3\n" if fail else (
        "#!/usr/bin/env python3\n"
        "import sys, os, csv\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('-o') + 1]\n"
        "files = [a for a in args[args.index('-o') + 2:]]\n"
        "os.makedirs(out, exist_ok=True)\n"
        "with open(os.path.join(out, 'files.csv'), 'w', newline='') as fh:\n"
        "    w = csv.writer(fh); w.writerow(['id', 'path'])\n"
        "    for i, f in enumerate(files): w.writerow([str(i), f])\n"
        "with open(os.path.join(out, 'pairs.csv'), 'w', newline='') as fh:\n"
        "    w = csv.writer(fh); w.writerow(['id', 'leftFileId', 'rightFileId', 'similarity'])\n"
        f"    w.writerow(['0', '0', '1', '{similarity}'])\n"
        "    w.writerow(['1', '1', '2', '0.99'])\n"     # kernel-vs-kernel: not the question
        "    w.writerow(['2', '0', '2', '0.10'])\n")
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_plagiarism_records_the_champion_versus_kernel_similarity(tmp_path, monkeypatch):
    kernels = tmp_path / "kernels"
    (kernels / "top").mkdir(parents=True)
    (kernels / "top" / "k1.py").write_text("print(1)\n")
    (kernels / "k2.ipynb").write_text("{}")
    _fake_dolos(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""))
    out = plagiarism_check({"solution.py": "print(2)\n"}, kernels)
    assert out["status"] == "ok" and out["kernels"] == 2 and out["max_similarity"] == 0.73
    # kernels are handed to dolos in sorted order (`k2.ipynb` before `top/k1.py`), so file id 1 is
    # the notebook and id 2 the nested kernel — the fake's pair table is written against those ids
    assert out["pairs"][0] == {"file": "solution.py", "kernel": "k2.ipynb", "similarity": 0.73}
    assert out["pairs"][1] == {"file": "solution.py", "kernel": os.path.join("top", "k1.py"), "similarity": 0.1}
    assert all(p["file"] == "solution.py" for p in out["pairs"]), "kernel-vs-kernel pairs are dropped"


def test_an_absent_check_is_stated_never_read_as_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert plagiarism_check({"solution.py": "x"}, tmp_path)["status"] == "unavailable"
    _fake_dolos(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert plagiarism_check({"solution.py": "x"}, None)["status"] == "no_kernels"
    assert plagiarism_check({"solution.py": "x"}, tmp_path / "missing")["status"] == "no_kernels"
    kernels = tmp_path / "kernels"
    kernels.mkdir()
    (kernels / "k.py").write_text("k\n")
    assert plagiarism_check({"notes.md": "x"}, kernels)["status"] == "no_code"
    _fake_dolos(tmp_path / "bin", fail=True)
    assert plagiarism_check({"solution.py": "x"}, kernels)["status"] == "error"


# ------------------------------------------------------------------ the whole record, over a real run

def test_the_report_writes_the_sidecar_with_both_halves(tmp_path, monkeypatch):
    run_dir = _finished_run(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))       # no dolos: the record says so
    calls = []

    def fake(msgs):
        calls.append(msgs)
        return RuleViolationVerdict(verdict="compliant")

    rec = extras_report(run_dir, judge_fn=fake)
    assert rec["status"] == "ok" and rec["rule_violation"]["verdict"] == "compliant"
    assert rec["plagiarism"]["status"] == "unavailable"
    assert len(calls) == 1, "one judge call for the one champion"
    on_disk = json.loads((run_dir / EXTRAS_SIDECAR).read_text())
    assert on_disk == rec and on_disk["version"] == 1 and on_disk["run_id"]
    # without a judge, the record still exists and says the judge was skipped
    rec2 = extras_report(run_dir, judge=False)
    assert rec2["rule_violation"] == {"status": "skipped", "reason": "no judge (--no-judge or no client)"}


def test_the_report_over_a_run_with_no_champion_records_that(tmp_path):
    rd = tmp_path / "empty"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append("run_started", {"run_id": "e", "task_id": "t", "goal": "g",
                                                           "direction": "max"})
    rec = extras_report(rd, judge=False)
    assert rec["status"] == "no_champion" and (rd / EXTRAS_SIDECAR).is_file()


def test_the_task_declares_what_the_judge_judges_against(tmp_path, monkeypatch):
    """`MLEBenchRealTask.rule_violation_context`: the closed rule list plus the competition's own
    description when the public dir is prepared, the goal otherwise — without a prepared
    competition on this box, the goal."""
    from looplab.adapters import mlebench_real

    monkeypatch.setattr(mlebench_real, "competition_meta",
                        lambda comp, data_dir=None: {"direction": "max", "scorer": "acc",
                                                     "public_dir": str(tmp_path / "nope")})
    monkeypatch.setattr(mlebench_real, "is_prepared", lambda comp, data_dir=None: False)
    task = mlebench_real.MLEBenchRealTask(competition="spooky-author-identification",
                                          kernels_dir=str(tmp_path / "kernels"))
    ctx = task.rule_violation_context()
    assert ctx["rules"] == MLEBENCH_RULES and ctx["kernels_dir"] == str(tmp_path / "kernels")
    assert ctx["description"] == task.goal


def test_the_cli_runs_the_plagiarism_half_without_a_judge(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from looplab.cli import app

    run_dir = _finished_run(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))
    result = CliRunner().invoke(app, ["mlebench-extras", str(run_dir), "--no-judge"])
    assert result.exit_code == 0, result.output
    assert "rule violation: skipped" in result.output and "plagiarism: unavailable" in result.output
    assert (run_dir / EXTRAS_SIDECAR).is_file()
    missing = CliRunner().invoke(app, ["mlebench-extras", str(tmp_path / "nowhere"), "--no-judge"])
    assert missing.exit_code == 1
