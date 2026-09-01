"""A probe recorded what the run did and nothing about the gateway it did it through.

Measured 2026-09-01: `accEE`, `accPde` and `remEE` ran entirely UNSTREAMED, and `remEE` lost nine
calls to the gateway's 300 s ceiling — forty-five minutes returning nothing on a $1.00 budget. None
of that appears in a score, a card or a run log. It survives only as a `stream` field on the meter's
rows, and the meter is one file for the whole box that no snapshot copies, while every comparison in
`docs/56` is drawn from probe trees.

§73 built a "controlled pair" on `remEE` against a streamed run before anyone looked, and the
conclusion had to be withdrawn (§80). So the settings that decide what the ruler measures are now
written into the probe's own directory, at launch, before a token is spent.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "benchmarks" / "algotune" / "run_probe.sh"
ROOT = Path("/var/tmp/looplab-bench")

pytestmark = pytest.mark.skipif(
    not (ROOT / "looplab").is_dir() or not (ROOT / "AlgoTune").is_dir(),
    reason="no bench stand on this box",
)


# NOT the live corpus. Six `t_instr_*` directories from this very file were sitting among the real
# probes on 2026-09-01, holding an INSTRUMENT.txt each. Harmless as it happened -- no events.jsonl,
# so no corpus statistic counted them -- but the version of this file that leaves run data behind
# would enter every summary silently and leave no trace in git. `PROBE_OUT_ROOT` is why the script
# now separates where the STAND is from where a probe WRITES.
OUT_ROOT = Path(os.environ.get("PYTEST_PROBE_OUT_ROOT") or "")


def _dry_run(label, *, stream="1", lane="44-47,92-95", task="discrete_log", out_root=None):
    """PROBE_DRY_RUN=1: every refusal is checked, the record is written, nothing is spent."""
    base = Path(out_root) if out_root else ROOT / "model-probes"
    out = base / label
    if out.exists():
        import shutil
        shutil.rmtree(out)
    env = dict(os.environ)
    env["PROBE_DRY_RUN"] = "1"
    env["PROBE_OUT_ROOT"] = str(base)
    if stream is None:
        env.pop("LOOPLAB_LLM_STREAM", None)
    else:
        env["LOOPLAB_LLM_STREAM"] = stream
    r = subprocess.run(
        ["bash", str(SCRIPT), "deepseek-v4-flash", label, lane, task,
         "http://127.0.0.1:8801", "1.00"],
        capture_output=True, text=True, timeout=600, env=env,
    )
    # THE SCRIPT'S OWN REASON, HERE, WHERE NO CALLER CAN FORGET IT. Three tests in this file failed
    # during the full suite of 2026-09-01 with `FileNotFoundError: .../INSTRUMENT.txt` -- the record
    # was never written -- and `run_probe.sh` prints exactly why it refuses on every path that can
    # refuse: a foreign result directory left open, a busy lane, a task with no dataset, a run
    # directory already carrying `run_finished`. Two of the three callers read the record without
    # checking the status first, so the diagnosis was captured, returned, and thrown away. I could
    # not name the cause afterwards because my own harness had eaten it.
    assert r.returncode == 0, (
        "run_probe.sh refused (rc=%d) and wrote no record; its own reason:\n%s%s"
        % (r.returncode, r.stdout, r.stderr))
    return r, out / "INSTRUMENT.txt"


def test_the_probe_writes_which_instrument_it_is_on(tmp_path):
    r, rec = _dry_run("t_instr_a", out_root=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert rec.is_file(), (
        "a probe still leaves no record of the gateway it ran through:\n" + r.stdout + r.stderr
    )
    body = rec.read_text()
    assert "LOOPLAB_LLM_STREAM=1" in body, f"the setting that decides the 300 s ceiling is missing:\n{body}"
    assert "ALGOTUNE_BASELINE_CACHE_DIR=" in body, f"the ruler's cache is not recorded:\n{body}"
    assert "ALGOTUNE_EVAL_WORKERS=auto" in body, f"the eval regime is not recorded:\n{body}"


def test_a_stray_unstreamed_setting_is_overridden_and_the_record_says_what_RAN(tmp_path):
    """The record must carry the EFFECTIVE setting, not the one the caller asked for.

    The bench profile now forces streaming (§fix `${VAR:-1}` loses arguments), so launching with
    `LOOPLAB_LLM_STREAM=false` produces a STREAMED probe. The first version of this test asserted
    the record would read `false` — it was asserting the old, broken behaviour, and the record was
    right. A record of the requested value would be worse than none: it would name an instrument
    the run was not on.
    """
    r, rec = _dry_run("t_instr_b", out_root=tmp_path, stream="false")
    assert r.returncode == 0, r.stdout + r.stderr
    body = rec.read_text()
    assert "LOOPLAB_LLM_STREAM=1" in body, (
        f"the record carries the requested setting rather than the effective one:\n{body}"
    )


def test_a_deliberate_opt_out_is_recorded_as_the_other_instrument(tmp_path):
    """`LOOPLAB_ALLOW_UNSTREAMED=1` is the one way to reach the instrument that cost remEE 9 calls."""
    env = dict(os.environ)
    env["PROBE_DRY_RUN"] = "1"
    env["LOOPLAB_LLM_STREAM"] = "false"
    env["LOOPLAB_ALLOW_UNSTREAMED"] = "1"
    env["PROBE_OUT_ROOT"] = str(tmp_path)
    out = tmp_path / "t_instr_f"
    if out.exists():
        import shutil
        shutil.rmtree(out)
    r = subprocess.run(["bash", str(SCRIPT), "deepseek-v4-flash", "t_instr_f", "44-47,92-95",
                        "discrete_log", "http://127.0.0.1:8801", "1.00"],
                       capture_output=True, text=True, timeout=600, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    body = (out / "INSTRUMENT.txt").read_text()
    assert "LOOPLAB_LLM_STREAM=false" in body, (
        f"a deliberately unstreamed probe is indistinguishable from a streamed one:\n{body}"
    )


def test_an_unset_setting_records_what_the_profile_resolved_it_to(tmp_path):
    """Unset in the caller's environment is not unset by the time the run starts."""
    r, rec = _dry_run("t_instr_c", out_root=tmp_path, stream=None)
    assert r.returncode == 0, r.stdout + r.stderr
    body = rec.read_text()
    assert "LOOPLAB_LLM_STREAM=1" in body, (
        f"an unset caller variable produced a record that does not say what ran:\n{body}"
    )


def test_it_pins_the_code_that_produced_the_run(tmp_path):
    r, rec = _dry_run("t_instr_d", out_root=tmp_path)
    body = rec.read_text()
    assert re.search(r"looplab:\s+[0-9a-f]{7}", body), f"no looplab commit recorded:\n{body}"
    assert re.search(r"AlgoTune:\s+[0-9a-f]{7}", body), f"no AlgoTune commit recorded:\n{body}"
    assert "looplab_dirty:" in body, (
        "a dirty tree is a different instrument from its commit, and nothing says whether it was"
    )


def test_no_api_key_reaches_the_record(tmp_path):
    """The probe tree goes into snapshots, and snapshots go to S3."""
    env_key = os.environ.get("LOOPLAB_LLM_API_KEY")
    r, rec = _dry_run("t_instr_e", out_root=tmp_path)
    body = rec.read_text()
    assert "API_KEY" not in body, f"a key name reached the record:\n{body}"
    if env_key:
        assert env_key not in body, "the API key itself was written into the probe tree"


def test_the_record_is_written_before_anything_is_spent(tmp_path):
    """The dry-run gate must sit BELOW the record, or the record cannot be checked without $1.

    It used to sit above: the first version of this fix was unreachable without spending a dollar,
    which is a fix whose falsifier cannot run.
    """
    src = SCRIPT.read_text()

    def where(marker):
        """Position of a marker that must appear EXACTLY ONCE, and in code rather than prose.

        Both of this test's earlier anchors -- "INSTRUMENT.txt" and "make_task.py" -- also occur in
        comments, tens of lines from the statements they were meant to locate. The original version
        hit that trap once and left a note about it; the 2026-09-01 rewrite hit it again, in the
        very comment it had just added. Uniqueness is the property that makes an anchor mean
        something: a new comment mentioning the name now fails loudly here instead of silently
        re-pointing the assertion at prose.
        """
        n = src.count(marker)
        assert n == 1, f"anchor {marker!r} occurs {n} times; it cannot locate anything"
        return src.index(marker)

    i_task = where('/make_task.py" --algotune-root')
    i_rec = where('} > "$OUT/INSTRUMENT.txt"')
    i_gate = where('PROBE_DRY_RUN:-0')
    # AGAINST THE THING THAT ACTUALLY SPENDS, which is the model call. This assertion used to name
    # make_task and was right by accident: make_task happened to sit below the gate, so it stood in
    # for "the expensive part". On 2026-09-01 make_task moved ABOVE the record -- so the record
    # could hash the card it produces -- and the test went red while both properties it exists to
    # protect still held. A proxy that fails when the proxy moves is testing the layout, not the
    # promise.
    i_spend = where('python -m looplab.cli run "')

    assert i_task < i_rec, (
        "the card is built after the instrument record, so card_sha256 cannot describe the card "
        "this probe was actually given"
    )
    assert i_rec < i_gate, (
        "the instrument record is no longer above the dry-run gate, so it cannot be checked "
        "without spending a dollar"
    )
    assert i_gate < i_spend, (
        "the dry-run gate no longer sits above the model call, so it no longer guards the spending"
    )


def test_a_dry_run_writes_nothing_into_the_live_corpus(tmp_path):
    """The tests in this file used to leave directories among the real probes.

    Six of them -- `t_instr_a` .. `t_instr_f` -- were sitting in
    /var/tmp/looplab-bench/model-probes on 2026-09-01, each holding an INSTRUMENT.txt. Nothing was
    corrupted, because none carried an events.jsonl and every corpus statistic keys off that. The
    defect is not the damage, it is that the damage was one fixture field away and would have
    entered `probe_summary`, `bench_runs_report` and docs/56 without a trace in git.

    Asserted by taking a census of the corpus before and after, so it fails for a test that writes
    there under ANY label, not only the ones this file happens to use today.
    """
    corpus = ROOT / "model-probes"
    before = {p.name for p in corpus.iterdir()} if corpus.is_dir() else set()
    _dry_run("t_instr_hermetic", out_root=tmp_path)
    after = {p.name for p in corpus.iterdir()} if corpus.is_dir() else set()
    assert after == before, (
        "a dry run added %s to the live probe corpus" % sorted(after - before)
    )
    assert (tmp_path / "t_instr_hermetic" / "INSTRUMENT.txt").exists(), (
        "the run wrote nowhere at all, so the census above proves nothing"
    )


def test_a_refusal_is_reported_as_a_refusal_and_not_as_a_missing_file(tmp_path):
    """The failure mode that cost an hour: three tests reported `FileNotFoundError: INSTRUMENT.txt`.

    That is the symptom of every refusal `run_probe.sh` has -- a foreign result directory left open,
    a busy lane, a task with no dataset, a run directory already carrying `run_finished` -- and it
    names none of them. The script prints its reason on each of those paths; the harness captured it
    and dropped it. Driven here with a refusal the guard is guaranteed to take (a task with no
    dataset), asserting the message reaches the failure rather than the exception from reading a
    file that was never written.
    """
    with pytest.raises(AssertionError) as e:
        _dry_run("t_instr_refusal", out_root=tmp_path, task="definitely_not_a_task_zzz")
    text = str(e.value)
    assert "run_probe.sh refused" in text, text
    assert "FileNotFoundError" not in text, text
    # …and the script's own words, whichever guard fired, are carried through.
    assert "ОТКАЗ" in text or "refus" in text.lower(), text


# --- the lane guard must see PROBES, not every process that says looplab.cli --------------------

def test_the_lane_guard_ignores_a_looplab_cli_that_is_not_a_bench_probe():
    """Caught by sampling the guard through a full suite on 2026-09-01.

    `python -m looplab.cli ui --help` and `python -m looplab.cli resume /tmp/pytest-of-…/run` both
    ran as pytest children, inheriting its affinity on the SERVICE lanes, and the guard matched them
    on the module name alone. Either was enough to make a concurrent dry run refuse with "на полосе
    уже 1 процесс(ов)" -- which is how three tests in this file failed on and off for two days.
    Neither occupies a bench lane.
    """
    src = SCRIPT.read_text()
    i = src.index('BUSY=$(python3 - "$LANE" "$ROOT"')
    guard = src[i:i + 1600]
    assert 'root in line' in guard, "the guard no longer requires the bench root"
    assert '"run", "resume"' in guard, "the guard no longer distinguishes the occupying verbs"
    # The two real offenders, and a real probe, checked against those two conditions directly.
    root = "/var/tmp/looplab-bench"
    def occupies(line):
        return ("looplab.cli" in line
                and any(f" {v} " in f" {line} " for v in ("run", "resume"))
                and root in line)
    assert not occupies("/opt/conda/bin/python -m looplab.cli ui --help")
    assert not occupies("/opt/conda/bin/python -m looplab.cli resume "
                        "/tmp/pytest-of-jovyan/pytest-805/test_crash0/run --task-id x")
    assert occupies("python -m looplab.cli run "
                    "/var/tmp/looplab-bench/model-probes/remEEref6/ws/algotune_edge_expansion.json")
    assert occupies("python -m looplab.cli resume /var/tmp/looplab-bench/model-probes/x/runs/t/run")


def test_the_guard_still_refuses_when_a_real_probe_holds_the_lane(tmp_path):
    """Narrowing it must not disarm it: the guard exists because two probes on one lane make both
    measurements worthless, and that is the failure it was written for."""
    src = SCRIPT.read_text()
    i = src.index('BUSY=$(python3 - "$LANE" "$ROOT"')
    guard = src[i:i + 1600]
    assert '"AlgoTuner"' in guard and '"algotune.sh"' in guard, (
        "arm A's entry points lost their match; they are only ever started by this bench")
    assert 'os.sched_getaffinity' in guard, "the guard no longer looks at affinity at all"
    assert 'BUSY" != "0"' in src, "the refusal on a busy lane is gone"
