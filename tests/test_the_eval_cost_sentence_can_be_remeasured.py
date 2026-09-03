"""The card's `eval_train` cost is a 2026-08-27 measurement of 54 calls; the corpus now holds 791.

Measured on this box on 2026-09-03 over every `run_dev_command("eval_train")` span in
`model-probes/`: n=791, median 42.1 s, fastest 3.6 s, slowest 117.3 s. The card says "40 s median,
29 s fastest, 80 s slowest" and derives "about 3 % of your session" from the median. The median is
close; the fastest is off by 8x and the slowest by 47 %, and at 117.3 s against the 1200 s session
one call is 10 %, not 3 %.

The fix is opt-in, and that is the point of this file. A card whose text moves when the corpus
grows is a ruler that moves under the experiment -- §113 stopped a running arm over a card
difference that turned out to be a fixture artefact, and §115's arm is pinned on card shas right
now. So `--eval-timings-from` recomputes the sentence and the DEFAULT stays byte-identical.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "benchmarks" / "algotune" / "make_task.py"
ALGOTUNE = Path("/var/tmp/looplab-bench/AlgoTune")

sys.path.insert(0, str(SCRIPT.parent))
import make_task  # noqa: E402


def _spans(tmp_path: Path, *durations: float) -> Path:
    """A probe tree holding `eval_train` spans of the given wall clocks, plus decoys."""
    run = tmp_path / "probeZ" / "runs" / "edge_expansion" / "run"
    run.mkdir(parents=True)
    rows = [{"kind": "tool", "duration_s": d,
             "attributes": {"tool": "run_dev_command", "input": '{"name": "eval_train"}'}}
            for d in durations]
    rows += [
        # A `check` call whose OUTPUT mentions eval_train must not be counted as one.
        {"kind": "tool", "duration_s": 999.0,
         "attributes": {"tool": "run_dev_command", "input": '{"name": "check"}',
                        "output": "run eval_train next"}},
        # A call that never completed carries no positive duration.
        {"kind": "tool", "duration_s": 0,
         "attributes": {"tool": "run_dev_command", "input": '{"name": "eval_train"}'}},
    ]
    (run / "spans.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path


def test_it_measures_only_completed_eval_train_calls(tmp_path):
    got = make_task.measured_eval_timings(_spans(tmp_path, 10.0, 20.0, 90.0))
    assert got == {"n": 3, "median": 20.0, "fastest": 10.0, "slowest": 90.0}, (
        "the 999 s `check` decoy or the zero-duration call leaked in")


def test_an_empty_corpus_keeps_the_frozen_wording(tmp_path):
    assert make_task.measured_eval_timings(tmp_path) is None
    assert make_task.eval_cost_sentence(None) == make_task.eval_cost_sentence()


def test_the_default_sentence_is_the_one_every_probe_was_given():
    frozen = make_task.eval_cost_sentence()
    assert "54 completed `eval_train` calls" in frozen
    assert "40 s median, 29 s fastest, 80 s slowest" in frozen
    assert frozen.startswith("IT COSTS ABOUT 40 SECONDS,")


def test_measured_timings_reach_every_number_in_the_sentence(tmp_path):
    said = make_task.eval_cost_sentence(
        make_task.measured_eval_timings(_spans(tmp_path, 10.0, 42.0, 117.0)))
    assert "3 completed `eval_train` calls" in said
    assert "42 s median, 10 s fastest, 117 s slowest" in said
    # The headline seconds and the derived session share both follow the measurement, or the
    # sentence contradicts itself -- which is what the frozen one does today at the true maximum.
    assert said.startswith("IT COSTS ABOUT 42 SECONDS,")
    assert "54 completed" not in said and "80 s slowest" not in said


@pytest.mark.skipif(not (ALGOTUNE / ".hf_datasets").is_dir(), reason="needs the AlgoTune checkout")
def test_the_flagless_card_is_byte_identical(tmp_path):
    """§115's arm is pinned on card shas. The flag must not move the card that is not asking."""
    def card(*flags):
        out = tmp_path / ("ws" + str(len(flags)) + str(abs(hash(flags))))
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "--algotune-root", str(ALGOTUNE), "--task",
             "edge_expansion", "--out-dir", str(out), "--deliver", "--one-card", *flags],
            capture_output=True, text=True, timeout=900)
        assert done.returncode == 0, done.stdout + done.stderr
        return json.loads((out / "algotune_edge_expansion.json").read_text(encoding="utf-8"))["goal"]

    assert card() == card(), "the card is not deterministic; nothing below means anything"
    moved = card("--eval-timings-from", "/var/tmp/looplab-bench/model-probes")
    assert moved != card(), "the flag did nothing"
    assert "54 completed `eval_train` calls" in card()
    assert "54 completed `eval_train` calls" not in moved
