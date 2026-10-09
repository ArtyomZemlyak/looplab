"""`looplab --help` leads with what a newcomer needs, and every command has exactly one place in it.

doc 74 EB-08: before `cli/help_panels.py` the command list followed import order — 74 commands in
one panel, `run` 63rd, `ui` 72nd, 29 summaries citing internal documents. These tests drive the REAL
`--help` output and the REAL registry, so a new command that lands without a row, a renamed command
that leaves a dead row, or a summary that grows a doc citation is red rather than quietly appended to
the bottom of the list.
"""
from __future__ import annotations

import re

from typer.testing import CliRunner

from looplab.cli import app
from looplab.cli.help_panels import HELP_PANELS, command_name

_DOC_CITATION = re.compile(r"\bdoc \d+|§|\bPART [IVX]+\b")


def _rows():
    return [(panel, name, summary) for panel, rows in HELP_PANELS for name, summary in rows]


def test_every_registered_command_has_exactly_one_row_and_every_row_is_registered():
    registered = [command_name(info) for info in app.registered_commands]
    rows = [name for _, name, _ in _rows()]
    duplicates = sorted({name for name in rows if rows.count(name) > 1})
    assert not duplicates, f"commands listed twice in HELP_PANELS: {duplicates}"
    assert not set(registered) - set(rows), (
        f"registered command(s) with no HELP_PANELS row: {sorted(set(registered) - set(rows))}")
    assert not set(rows) - set(registered), (
        f"HELP_PANELS row(s) naming no registered command: {sorted(set(rows) - set(registered))}")


def test_summaries_are_one_short_user_facing_line():
    for panel, name, summary in _rows():
        assert summary and "\n" not in summary and len(summary) <= 80, (panel, name, summary)
        assert not _DOC_CITATION.search(summary), f"{name}: a summary cites an internal document"


def test_help_opens_with_the_start_here_panel_in_table_order():
    out = CliRunner().invoke(app, ["--help"], terminal_width=120).output
    first_panel, first_rows = HELP_PANELS[0]
    assert first_panel == "Start here"
    panels = re.findall(r"╭─ (.+?) ─", out)
    assert panels[1:] == [panel for panel, _ in HELP_PANELS], panels  # [0] is the Options box
    listed = re.findall(r"^│ ([a-z][a-z0-9-]*) ", out, flags=re.M)
    assert listed[:len(first_rows)] == [name for name, _ in first_rows]
    assert listed.index("run") < 3 and listed.index("ui") < 3
    assert not _DOC_CITATION.search(out), "the command list must not cite internal documents"


def test_a_command_still_prints_its_full_docstring():
    """Only the LIST summary comes from the table; `<command> --help` keeps the full contract."""
    out = CliRunner().invoke(app, ["stop", "--help"], terminal_width=120).output
    assert "WITHOUT finalizing" in out
