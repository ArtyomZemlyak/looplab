"""§190's arm varies a Settings field, and the launcher had no way to pass one.

`run_probe.sh` invoked `looplab.cli run` with a fixed argument list. The arm registered in §190 sets
`developer_probe_max_calls=12` on the treatment side and leaves the control at 0; without a hook it
would have launched both sides identically and measured its control against itself — the same class
of silent no-op as §191, where the setting existed and reached nothing.

Two things are pinned: the splice exists on the `looplab.cli run` line, and the value is recorded in
`INSTRUMENT.txt` beside the card args. §113 is the record of what a probe whose inputs are not
written down costs: a whole probe, stopped on a card difference that turned out to be a fixture.
"""
from __future__ import annotations

import re
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "run_probe.sh"


def _text() -> str:
    return LAUNCHER.read_text(encoding="utf-8")


def test_the_cli_line_splices_the_settings_variable():
    src = _text()
    line = next(l for l in src.splitlines() if "--backend llm --max-nodes 20" in l)
    assert "${PROBE_LOOPLAB_SETTINGS:-}" in line, line
    # unset must expand to nothing, so a probe that sets nothing runs the shipped command
    assert ":-}" in line, "the expansion is not defaulted; an unset variable would break the run"


def test_the_instrument_records_what_the_engine_was_told():
    src = _text()
    assert 'cli_settings:' in src, "INSTRUMENT.txt does not record the settings the probe ran with"
    assert "PROBE_LOOPLAB_SETTINGS:-(none" in src, (
        "the recorded value has no explicit 'none' case, so a probe with default settings would "
        "record a blank and read as unknown")


def test_the_card_hook_is_still_there_too():
    """The two hooks are siblings; losing either makes an arm unreadable in a different way."""
    src = _text()
    assert "PROBE_MAKE_TASK_ARGS" in src
    assert "card_args:" in src


def test_the_launcher_is_still_valid_shell():
    import subprocess
    done = subprocess.run(["bash", "-n", str(LAUNCHER)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
