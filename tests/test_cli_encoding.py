"""The command entry point must be total on legacy/non-UTF output streams."""
from __future__ import annotations

import os
import subprocess
import sys


def test_help_degrades_unencodable_glyphs_instead_of_crashing():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252:strict"
    result = subprocess.run(
        [sys.executable, "-m", "looplab.cli", "ui", "--help"],
        cwd=os.getcwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="cp1252",
        errors="strict",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    assert "UnicodeEncodeError" not in result.stdout
    assert "--run-root" in result.stdout and "--no-build" in result.stdout
