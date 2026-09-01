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


def _dry_run(label, *, stream="1", lane="44-47,92-95", task="discrete_log"):
    """PROBE_DRY_RUN=1: every refusal is checked, the record is written, nothing is spent."""
    out = ROOT / "model-probes" / label
    if out.exists():
        import shutil
        shutil.rmtree(out)
    env = dict(os.environ)
    env["PROBE_DRY_RUN"] = "1"
    if stream is None:
        env.pop("LOOPLAB_LLM_STREAM", None)
    else:
        env["LOOPLAB_LLM_STREAM"] = stream
    r = subprocess.run(
        ["bash", str(SCRIPT), "deepseek-v4-flash", label, lane, task,
         "http://127.0.0.1:8801", "1.00"],
        capture_output=True, text=True, timeout=600, env=env,
    )
    return r, out / "INSTRUMENT.txt"


def test_the_probe_writes_which_instrument_it_is_on(tmp_path):
    r, rec = _dry_run("t_instr_a")
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
    r, rec = _dry_run("t_instr_b", stream="false")
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
    out = ROOT / "model-probes" / "t_instr_f"
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
    r, rec = _dry_run("t_instr_c", stream=None)
    assert r.returncode == 0, r.stdout + r.stderr
    body = rec.read_text()
    assert "LOOPLAB_LLM_STREAM=1" in body, (
        f"an unset caller variable produced a record that does not say what ran:\n{body}"
    )


def test_it_pins_the_code_that_produced_the_run(tmp_path):
    r, rec = _dry_run("t_instr_d")
    body = rec.read_text()
    assert re.search(r"looplab:\s+[0-9a-f]{7}", body), f"no looplab commit recorded:\n{body}"
    assert re.search(r"AlgoTune:\s+[0-9a-f]{7}", body), f"no AlgoTune commit recorded:\n{body}"
    assert "looplab_dirty:" in body, (
        "a dirty tree is a different instrument from its commit, and nothing says whether it was"
    )


def test_no_api_key_reaches_the_record(tmp_path):
    """The probe tree goes into snapshots, and snapshots go to S3."""
    env_key = os.environ.get("LOOPLAB_LLM_API_KEY")
    r, rec = _dry_run("t_instr_e")
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
    i_rec = src.index("INSTRUMENT.txt")
    i_gate = src.index('PROBE_DRY_RUN:-0')
    # The LAST occurrence: `make_task.py` is named in a comment a hundred lines above its
    # invocation, and `index()` found the comment — the assertion then compared against a line that
    # spends nothing.
    i_task = src.rindex("make_task.py")
    assert i_rec < i_gate < i_task, (
        "the dry-run gate is no longer between the instrument record and make_task.py, so either "
        "the record cannot be tested for free or the gate no longer guards the spending"
    )
