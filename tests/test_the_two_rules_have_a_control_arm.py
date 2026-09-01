"""The two clauses experience never teaches had no OFF switch, so their effect was unmeasurable.

`test_card_states_the_two_rules_experience_never_teaches` proves the card STATES them. It cannot
prove they are worth stating: that needs an arm without them, and until this flag existed
`make_task.py` had no way to build one. Every probe on this box carries both clauses, so the corpus
has twelve runs of one arm and zero of the other -- which is why docs/56 §81 could report no effect
and no absence of one.

`--no-unteachable-rules` is that arm. The test that matters is not that the clauses disappear (one
grep proves that); it is that NOTHING ELSE MOVES. A control that also drops the eval-cost sentence
or the solution-space clause measures the bundle, not the two rules, and would answer a question
nobody asked while looking exactly like an answer to this one.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MAKE = REPO / "benchmarks" / "algotune" / "make_task.py"
ARENA = Path("/var/tmp/looplab-bench/AlgoTune")

pytestmark = pytest.mark.skipif(
    not ARENA.exists(), reason="AlgoTune checkout not on this box"
)

CEILING = "CEILING ON HOW SLOW YOUR SOLVER MAY BE"
KEEP_BEST = "THE BEST EVALUATED SOLVER IS WHAT GETS SUBMITTED"

# The flags every probe on this box runs under (`run_probe.sh`), so the control differs from the
# SHIPPED card and not from some configuration nothing uses.
PROBE_FLAGS = ("--full-context", "--deliver", "--one-card", "--enforce-rules")


def _goal(tmp_path, *flags):
    out = tmp_path / ("ws" + str(abs(hash(flags)) % 10000))
    r = subprocess.run(
        [sys.executable, str(MAKE), "--algotune-root", str(ARENA),
         "--task", "pde_heat1d", "--out-dir", str(out), *flags],
        capture_output=True, text=True, timeout=1800,
    )
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    spec = next(out.rglob("algotune_*.json"))
    return json.loads(spec.read_text())["goal"]


def test_the_control_arm_drops_the_ceiling(tmp_path):
    on = _goal(tmp_path, *PROBE_FLAGS)
    off = _goal(tmp_path, *PROBE_FLAGS, "--no-unteachable-rules")
    assert CEILING in on
    assert CEILING not in off, "the control arm still states the per-instance ceiling"


def test_the_control_arm_drops_keep_best(tmp_path):
    on = _goal(tmp_path, *PROBE_FLAGS)
    off = _goal(tmp_path, *PROBE_FLAGS, "--no-unteachable-rules")
    assert KEEP_BEST in on
    assert KEEP_BEST not in off, "the control arm still states that the best node is submitted"


def test_the_control_arm_drops_NOTHING_ELSE(tmp_path):
    """The whole point of the flag: what is left must be the shipped card minus two paragraphs.

    Compared BYTE for byte against a card assembled from the module's own pieces, not by splitting
    prose into sentences. The first version of this test did split on ". ", and it reported a third
    "missing" sentence that was nothing of the kind: the clauses are concatenated without
    separators, so removing one changes the SENTENCE THAT SPANS THE JUNCTION on both sides of it.
    A heuristic that manufactures a finding at a seam is worse than no test here, because the seam
    is exactly where a real over-removal would also show up.
    """
    sys.path.insert(0, str(MAKE.parent))
    try:
        import make_task
    finally:
        sys.path.pop(0)

    on = _goal(tmp_path, *PROBE_FLAGS)
    off = _goal(tmp_path, *PROBE_FLAGS, "--no-unteachable-rules")

    with_cap = make_task.timing_clause(ARENA, True)
    without_cap = make_task.timing_clause(ARENA, False)
    assert with_cap != without_cap, "this arena has no per-instance cap, so the test proves nothing"
    assert make_task.KEEP_BEST in on

    expected = on.replace(with_cap, without_cap, 1).replace(make_task.KEEP_BEST, "", 1)
    assert off == expected, (
        "the control arm is not the shipped card minus exactly those two clauses; it differs from "
        "the expected control by %d characters" % abs(len(off) - len(expected))
    )


def test_the_default_is_the_shipped_card(tmp_path):
    """Omitting the flag must be byte-identical to the arm twelve probes already ran."""
    implicit = _goal(tmp_path, *PROBE_FLAGS)
    explicit = _goal(tmp_path, *PROBE_FLAGS, "--unteachable-rules")
    assert implicit == explicit, "the flag changes the default arm, so the corpus is not comparable"


def test_the_probe_records_which_card_it_ran(tmp_path):
    """A control arm the tree cannot identify is a dollar spent on an unusable run.

    Driven through `run_probe.sh` itself under PROBE_DRY_RUN, which reaches the instrument record
    and stops before anything costs money. The record must name the variant AND must say so
    positively when there is none -- a missing line is indistinguishable from an older probe whose
    script did not write the line at all, and that ambiguity is the whole failure being prevented.
    """
    probe = REPO / "benchmarks" / "algotune" / "run_probe.sh"
    root = Path("/var/tmp/looplab-bench")
    if not (root / "AlgoTune").exists():
        pytest.skip("bench root not on this box")

    def _instrument(label, env_extra):
        out = root / "model-probes" / label
        env = {**os.environ, "PROBE_DRY_RUN": "1", **env_extra}
        r = subprocess.run(
            ["bash", str(probe), "deepseek-v4-flash", label, "44-47", "pde_heat1d",
             "http://127.0.0.1:8801", "1.00"],
            capture_output=True, text=True, timeout=1800, env=env,
        )
        rec = out / "INSTRUMENT.txt"
        assert rec.exists(), "dry run wrote no instrument record\n" + r.stdout[-2000:] + r.stderr[-2000:]
        text = rec.read_text()
        shutil.rmtree(out, ignore_errors=True)
        return text

    plain = _instrument("selftest-card-plain", {})
    assert "card_args:" in plain, "the record does not mention the card variant at all"
    assert "none" in plain.split("card_args:")[1].splitlines()[0], (
        "a probe on the shipped card does not say so positively"
    )

    control = _instrument("selftest-card-control",
                          {"PROBE_MAKE_TASK_ARGS": "--no-unteachable-rules"})
    assert "--no-unteachable-rules" in control, (
        "the control arm ran a different card and the record does not say which"
    )


def test_the_probe_carries_its_own_credential(tmp_path):
    """The script set half the credential pair and inherited the other half from the operator.

    `run_probe.sh` exported `LOOPLAB_LLM_API_KEY_BASE_URL` (re-pointed at the local meter) and
    `OPENAI_API_KEY`, but never `LOOPLAB_LLM_API_KEY`. The engine treats those two as ONE atomic
    credential and refuses when only one is present, so every probe that ever ran did so because the
    launching shell happened to carry the key from a `source .env`. Launched from a clean
    environment on 2026-09-01 the same script died in ONE SECOND with rc=2 -- after the fence check
    and after the instrument record, so it looked healthy right up to the moment it would have spent.

    Driven under PROBE_DRY_RUN with the key REMOVED from the environment, which is the condition
    that used to fail. The record must show a complete pair, and it must not show the key.
    """
    probe = REPO / "benchmarks" / "algotune" / "run_probe.sh"
    root = Path("/var/tmp/looplab-bench")
    if not (root / "AlgoTune").exists():
        pytest.skip("bench root not on this box")

    env = {k: v for k, v in os.environ.items()
           if k not in ("LOOPLAB_LLM_API_KEY", "LOOPLAB_LLM_API_KEY_BASE_URL", "OPENAI_API_KEY")}
    env["PROBE_DRY_RUN"] = "1"
    label = "selftest-credential"
    out = root / "model-probes" / label
    try:
        subprocess.run(
            ["bash", str(probe), "deepseek-v4-flash", label, "44-47", "pde_heat1d",
             "http://127.0.0.1:8801", "1.00"],
            capture_output=True, text=True, timeout=1800, env=env,
        )
        rec = (out / "INSTRUMENT.txt").read_text()
    finally:
        shutil.rmtree(out, ignore_errors=True)

    assert "llm_credential:" in rec, "the record says nothing about the credential pair"
    assert "INCOMPLETE PAIR" not in rec, (
        "the probe still depends on a key from the operator's shell:\n" + rec
    )
    assert "pair complete" in rec


def test_no_echo_in_the_record_block_mentions_the_key(tmp_path):
    """The first version of that record line printed the KEY whenever the key was set.

    `${LOOPLAB_LLM_API_KEY:+set}${LOOPLAB_LLM_API_KEY:-MISSING}` looks like a presence test and is
    not: the second expansion yields the VALUE whenever the variable is non-empty. The same defect
    shipped once already in the snapshot's ENVIRONMENT.txt.

    Asserted on the SOURCE, not on a run, and that is the point. The runtime version of this test
    planted a sentinel key in the environment and looked for it in the record -- and it stayed green
    under a deliberately leaky record line, because the script now pins the key to `meter` before
    writing anything, so the sentinel never reaches the record whatever the line says. A test that
    cannot fail is not evidence; mutation is what showed the difference. The checkable property is
    structural: no `echo` inside the record block names the key variable at all.
    """
    src = (REPO / "benchmarks" / "algotune" / "run_probe.sh").read_text()
    block = src.split('mkdir -p "$OUT"', 1)[1].split('} > "$OUT/INSTRUMENT.txt"', 1)[0]
    assert "probe:" in block, "could not isolate the instrument record block"

    offenders = [ln.strip() for ln in block.splitlines()
                 if ln.strip().startswith("echo ") and "LOOPLAB_LLM_API_KEY" in ln
                 and "LOOPLAB_LLM_API_KEY_BASE_URL" not in ln.replace("LOOPLAB_LLM_API_KEY_BASE_URL", "")]
    offenders = [ln for ln in offenders
                 if "LOOPLAB_LLM_API_KEY" in ln.replace("LOOPLAB_LLM_API_KEY_BASE_URL", "")]
    assert not offenders, (
        "an echo in the instrument record expands the API key variable, so a run can write the key "
        "into a file:\n  " + "\n  ".join(offenders)
    )
