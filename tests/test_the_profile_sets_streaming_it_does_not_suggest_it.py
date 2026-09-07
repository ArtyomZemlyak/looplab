"""Streaming is a property of the ruler on this box, and the profile used to merely suggest it.

The gateway sits behind an nginx whose `proxy_read_timeout` is 300 s, and that timeout measures the
gap BETWEEN BYTES. A streamed generation resets it with every token and survives any length (673 s
is the longest on record here). A non-streamed one holds the socket silent for the whole generation
and is cut at exactly 300 s.

Measured on `meter/meter.jsonl`, 1,578 calls: all 21 gateway 504s in the corpus are non-streaming
requests at 295-305 s latency, and zero streamed calls have ever hit the ceiling. On `discrete_log`
that was 28 % of the run's calls.

The profile spelled it `${LOOPLAB_LLM_STREAM:-1}`, which means an already-set value WINS. Line 77
of `/home/jovyan/data/looplab/.env` sets it to `false`. Sourcing that file for its two credential
lines -- the obvious thing to do -- silently swapped the ruler, and nothing said so. Reading the
profile afterwards does not reveal it either: the `:-1` sits right there looking like the answer.

So the profile must SET it, and any override must be deliberate and announced.
"""
import subprocess
from pathlib import Path

import pytest

PROFILE = Path(__file__).resolve().parents[1] / "benchmarks" / "box-jhub-l40s.sh"

pytestmark = pytest.mark.skipif(not PROFILE.exists(), reason="box profile not on this checkout")


def _sourced(env_line: str) -> tuple[str, str]:
    """Source the profile with `env_line` preset; return (resulting value, stderr)."""
    script = f'{env_line}\nsource "{PROFILE}" >/dev/null\necho "RESULT=$LOOPLAB_LLM_STREAM"\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=300)
    value = ""
    for line in r.stdout.splitlines():
        if line.startswith("RESULT="):
            value = line[len("RESULT="):]
    return value, r.stderr


def test_a_stray_env_cannot_silently_turn_streaming_off():
    """The exact accident: `.env` sourced for two credentials, carrying STREAM=false with it."""
    value, err = _sourced("export LOOPLAB_LLM_STREAM=false")
    assert value == "1", (
        f"a preset LOOPLAB_LLM_STREAM=false survived the profile (got {value!r}) -- the ruler can "
        "still be swapped by sourcing someone else's .env"
    )
    assert "LOOPLAB_LLM_STREAM=false" in err, (
        "the profile overrode the value but said nothing; a silent correction is the same class of "
        "problem as a silent breakage -- you cannot tell from the output what ruler you got"
    )


def test_it_still_sets_streaming_when_nothing_is_preset():
    value, err = _sourced("unset LOOPLAB_LLM_STREAM")
    assert value == "1", f"profile did not enable streaming on a clean environment (got {value!r})"
    assert "override" not in err.lower(), "announced an override where there was nothing to override"


def test_an_explicit_opt_out_is_honoured_and_loudly_labelled():
    """Turning it off must remain possible -- and must say the numbers are off the corpus's ruler."""
    value, err = _sourced("export LOOPLAB_LLM_STREAM=false; export LOOPLAB_ALLOW_UNSTREAMED=1")
    assert value == "false", (
        f"a deliberate LOOPLAB_ALLOW_UNSTREAMED=1 opt-out was overridden anyway (got {value!r})"
    )
    assert "300 s ceiling" in err and "do not compare" in err, (
        "the opt-out is honoured but not labelled; the whole point is that a run on this setting is "
        "not comparable with the corpus:\n" + err
    )


def test_the_value_the_profile_wants_is_not_left_to_a_default_expansion():
    """A `:-` default is the wrong construct for something with these stakes; forbid its return."""
    import re

    body = PROFILE.read_text()
    # `${VAR:-}` is fine -- that is "is it set", the guard this file needs under `set -u`. What is
    # forbidden is a NON-EMPTY default, `${VAR:-1}`, because that is the construct that hands the
    # decision to whatever was already in the environment while looking like it decides.
    bad = re.findall(r"\$\{LOOPLAB_LLM_STREAM:-[^}]+\}", body)
    assert not bad, (
        f"{bad} is back in the profile. That spelling means an already-set value wins, which is "
        "exactly how the 300 s ceiling got switched back on unnoticed."
    )
