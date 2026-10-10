"""`looplab --help` leads with what a newcomer needs, and every command has exactly one place in it.

doc 74 EB-08: before `cli/help_panels.py` the command list followed import order — 74 commands in
one panel, `run` 63rd, `ui` 72nd, 29 summaries citing internal documents. These tests drive the REAL
`--help` output and the REAL registry, so a new command that lands without a row, a renamed command
that leaves a dead row, or a summary that grows a doc citation is red rather than quietly appended to
the bottom of the list.
"""
from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from looplab.cli import app
from looplab.cli.help_panels import HELP_PANELS, command_name

_DOC_CITATION = re.compile(r"\bdoc \d+|§|\bPART [IVX]+\b")
# Rich FORCES colour when `GITHUB_ACTIONS` is set, so CI's `--help` carries ANSI escapes that a
# terminal-free local run does not (master run 2213: the panel regex found nothing). Read the text.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Rich swaps its box for the console it draws on: rounded (`╭─`/`│`) here, SQUARE (`┌─`) under the
# legacy Windows console, ASCII (`+-`/`|`) under a non-UTF encoding — the Windows leg found the
# rounded-only pattern matching nothing. Read the panel structure, not one box style.
_PANEL_TITLE = re.compile(r"^[╭┌+][─-] (.+?) [─-]", flags=re.M)
_ROW_NAME = re.compile(r"^[│|] ([a-z][a-z0-9-]*) ", flags=re.M)


def _help(*args):
    return _ANSI.sub("", CliRunner().invoke(app, [*args, "--help"], terminal_width=120).output)


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
    out = _help()
    first_panel, first_rows = HELP_PANELS[0]
    assert first_panel == "Start here"
    panels = _PANEL_TITLE.findall(out)
    assert panels[1:] == [panel for panel, _ in HELP_PANELS], panels  # [0] is the Options box
    listed = _ROW_NAME.findall(out)
    assert listed[:len(first_rows)] == [name for name, _ in first_rows]
    assert listed.index("run") < 3 and listed.index("ui") < 3
    assert not _DOC_CITATION.search(out), "the command list must not cite internal documents"


def test_a_command_still_prints_its_full_docstring():
    """Only the LIST summary comes from the table; `<command> --help` keeps the full contract."""
    out = _help("stop")
    assert "WITHOUT finalizing" in out


def test_run_help_reads_at_80_columns_and_defines_genesis(monkeypatch):
    """doc 75 UX-10: 23 options in one panel left about 20 characters for each description at 80
    columns, "Genesis" appeared three times undefined, and the form every example uses was called
    "legacy". Rich lays out each `rich_help_panel` as its own table, so grouping narrows the flag
    column; the three `--x/--no-x` pairs that set it widest are `Settings` fields `-s` reaches and
    are hidden (they still work). Measured on a wrapped line's indent: the help column."""
    import re

    monkeypatch.setenv("COLUMNS", "80")
    text = _ANSI.sub("", CliRunner().invoke(app, ["run", "--help"], terminal_width=80).output)
    panel, indents = None, {}
    for line in text.splitlines():   # every box style `_PANEL_TITLE` reads (the Windows leg's too)
        head = _PANEL_TITLE.match(line)
        if head:
            panel = head.group(1)
            continue
        if line[:1] in ("╰", "└", "+"):
            panel = None
            continue
        wrapped = re.match(r"[│|](\s+)(\S.*?)\s*[│|]\s*$", line)
        if panel and panel != "Arguments" and wrapped and not wrapped.group(2).startswith("-"):
            indents.setdefault(panel, set()).add(len(wrapped.group(1)) + 1)
    widths = {name: 80 - 2 - min(found) for name, found in indents.items()}
    assert widths and min(widths.values()) >= 40, widths
    assert "legacy" not in text
    assert "Genesis: a model writes the task" in " ".join(text.split())
    for hidden in ("--validate-agent", "--agent-patch-gate", "--require-approval"):
        assert hidden not in text


# doc 75 UX-11: the help a USER reads. These panels hold the commands of the first hour; their
# `--help` cites no internal document or module and fits a screen. Maintainer and research commands
# that still cite are a SHRINK-ONLY backlog (`tests/data/help_citation_backlog.txt`).
_USER_PANELS = ("Start here", "Run control", "External coding agent", "Export")
_USER_EXTRA = ("comparability",)
_CITATION = re.compile(r"doc ?\d|§|[a-z_]+\.py\b|::|ADR-")


def _help80(command: str) -> str:
    return _ANSI.sub("", CliRunner().invoke(app, [command, "--help"], terminal_width=80).output)


def _all_commands():
    return [(panel, name) for panel, rows in HELP_PANELS for name, _ in rows]


def test_the_help_a_user_reads_cites_no_internal_document_and_fits_a_screen():
    users = [name for panel, name in _all_commands() if panel in _USER_PANELS] + list(_USER_EXTRA)
    offenders = {}
    for name in users:
        text = _help80(name)
        cited = [line.strip() for line in text.splitlines() if _CITATION.search(line)]
        if cited or len(text.splitlines()) > 60:
            offenders[name] = (len(text.splitlines()), cited[:2])
    assert offenders == {}


def test_the_command_list_says_experiment_where_the_user_reads_it():
    """doc 75 UX-18: `run`, `inspect`, the Report and the glossary call a candidate an EXPERIMENT;
    "node" is the event log's word. The command list's user panels, and the two everyday read
    commands beside them, say "experiment"; literal names (`--node`, `nodes/`, tag `node-<id>`)
    keep theirs, which is why this reads the summaries and the prose, not the whole screen."""
    word = re.compile(r"\bnodes?\b|\bper-node\b")
    summaries = {name: summary for panel, rows in HELP_PANELS for name, summary in rows
                 if panel in _USER_PANELS or name in ("timings", "tensorboard")}
    assert {name: s for name, s in summaries.items() if word.search(s)} == {}
    for name in ("timings", "tensorboard", "export-git"):
        prose = " ".join(line for line in _help80(name).splitlines()
                         if not re.match(r"^\s*[│|]", line))      # the description, not option rows
        assert not word.search(prose.replace("node-<id>", "").replace("nodes/", "")), (name, prose)


def test_maintainer_help_citations_only_shrink():
    backlog = {line.strip() for line in
               (Path(__file__).parent / "data" / "help_citation_backlog.txt").read_text().splitlines()
               if line.strip() and not line.startswith("#")}
    citing = {name for _, name in _all_commands() if _CITATION.search(_help80(name))}
    assert citing - backlog == set(), "a new --help cites an internal document; say it for the user"
    assert backlog - citing == set(), "a backlog command no longer cites: delete its row"


def test_tui_names_the_role_it_calls_the_assistant():
    text = " ".join(_help80("tui").split())
    assert "the run-chat Assistant (the agent role named `boss` in settings)" in text or \
        "the run-chat Assistant (the agent role named boss in settings)" in text
