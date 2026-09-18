"""The campaign driver refuses an empty API key instead of exporting one.

WHAT THIS IS ABOUT, measured on 2026-09-10. The final campaign refused all twenty task-arms of arm B
inside one minute:

    Refused: LLM credential preflight failed: LOOPLAB_LLM_API_KEY_BASE_URL was set without
    LOOPLAB_LLM_API_KEY ... This one cause is why all 7 of these fail

and arm A died the same minute on its own half ("CRITICAL - API key not found"). Twenty task-arms,
two arms, one missing variable, and every one of them discovered it separately.

The driver was a participant, not a bystander. It exports `LOOPLAB_LLM_API_KEY_BASE_URL`
unconditionally and then `LOOPLAB_LLM_API_KEY="${LOOPLAB_LLM_API_KEY:-${OPENROUTER_API_KEY:-}}"`,
which falls back to the EMPTY STRING -- a half pair. The engine treats the two as ONE credential
re-selected from a single source, so a half pair in the process environment does not merge with a
complete pair in `.env`, it replaces it: the driver could destroy a working credential rather than
merely fail to supply one.

These tests drive the SHIPPED function out of `campaign.sh` (the same sed-extraction the serial-ruler
tests use) rather than a copy of its logic, because a copy is what stops matching the day the real
one changes.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign.sh"


def _run(env_overrides: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    """Extract the real `require_llm_credentials` and call it under a chosen environment."""
    fn = tmp_path / "fn.sh"
    extract = subprocess.run(
        ["sed", "-n", "/^require_llm_credentials() {/,/^}/p", str(SCRIPT)],
        capture_output=True, text=True, check=True)
    assert "LOOPLAB_LLM_API_KEY" in extract.stdout, "the function did not extract"
    fn.write_text(extract.stdout, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("LOOPLAB_LLM_API_KEY", "OPENROUTER_API_KEY")}
    env.update({"LOOPLAB_LLM_BASE_URL": "https://gateway.example/v1", "AT": str(tmp_path)})
    env.update(env_overrides)
    return subprocess.run(["bash", "-c", f". {fn}; require_llm_credentials"],
                          capture_output=True, text=True, env=env, timeout=60)


def test_an_unset_key_is_refused_before_the_first_token(tmp_path):
    got = _run({}, tmp_path)
    assert got.returncode == 2, got.stdout + got.stderr
    assert "REFUSED" in got.stderr and "LOOPLAB_LLM_API_KEY is empty" in got.stderr


def test_an_EMPTY_key_is_refused_too(tmp_path):
    """`${VAR:-default}` treats unset and empty alike, and so must the guard: the campaign's own
    fallback produces the empty string, which is the case that actually happened."""
    got = _run({"LOOPLAB_LLM_API_KEY": ""}, tmp_path)
    assert got.returncode == 2, got.stdout + got.stderr


def test_a_complete_pair_passes_silently(tmp_path):
    """A guard that fires on a healthy stand is a guard the operator learns to ignore."""
    got = _run({"LOOPLAB_LLM_API_KEY": "sk-or-v1-fixture"}, tmp_path)
    assert got.returncode == 0, got.stdout + got.stderr
    assert got.stderr.strip() == "", got.stderr


def test_the_openrouter_spelling_still_satisfies_it(tmp_path):
    """The driver's own fallback accepts `OPENROUTER_API_KEY`, so the guard must agree with the
    line it protects -- a guard stricter than the code it guards refuses working stands."""
    got = _run({"OPENROUTER_API_KEY": "sk-or-v1-fixture"}, tmp_path)
    assert got.returncode == 0, got.stdout + got.stderr


def test_the_refusal_says_where_it_looked_and_what_the_pair_is(tmp_path):
    """The 2026-09-10 operator had to read the ENGINE's refusal to learn that the two variables are
    one credential. The driver refuses first now, so it is the driver that has to say it."""
    err = _run({}, tmp_path).stderr
    for phrase in ("ONE credential", "REPLACES a complete pair in .env",
                   "Looked in:", "OPENROUTER_API_KEY", "2026-09-10"):
        assert phrase in err, (phrase, err)


def test_the_guard_runs_before_the_first_task_and_after_the_free_refusals(tmp_path):
    """Source-pinned, because the ORDER is the property -- and it is a NARROWER order than "first".

    The guard sat above the export block for one test run, which put it ahead of the configuration
    refusals (an arm-A model entry that would bypass the meter, an unmetered campaign, the lane
    plan). Those cost nothing and need no key, and requiring a live credential to reach them makes a
    config check unrunnable offline: three existing tests went red inside a minute. A guard belongs
    before the first thing that SPENDS, not before everything.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    call = src.index("require_llm_credentials || exit $?")
    assert call < src.index("\nfor T in $TASKS; do"), "it must precede the task loop"
    assert call > src.index("would bypass the meter"), \
        "the free configuration refusals must still fire without a credential"
