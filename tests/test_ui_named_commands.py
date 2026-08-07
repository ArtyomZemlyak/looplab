"""Every CLI command the UI *names* to an operator must exist.

A screen that prints a command an operator cannot run is the same defect class as a count nobody
measured: it reads as authoritative and it is wrong, and nothing goes red. `PortfolioConcepts.jsx`
carried `looplab governance concept-merge` in `<code>` for a week — the Typer app is FLAT, has never
had a `governance` group, and answers "No such command 'governance'." at
`cli/__init__.py::REFUSAL_EXIT_CODE`.

This drives the REAL registry (`looplab.cli.app.registered_commands`) rather than pinning a literal,
so renaming or dropping a command turns every UI mention of it red in the same change. The scan is
deliberately narrowed to CODE-FORMATTED spans (`<code>…</code>` in JSX, or a backtick span in a
comment): that is exactly the text a screen presents as "type this", and it keeps ordinary prose
containing the word "looplab" out of the check.
"""
from __future__ import annotations

import re
from pathlib import Path

# A `<code>…</code>` element (JSX children may span lines) or an inline backtick span. Both are the
# "this is literal text you can type" convention this repo's UI already uses.
_CODE_SPAN = re.compile(r"<code[^>]*>(.*?)</code>|`([^`\n]{1,200})`", re.S)
# The FIRST token after `looplab` is the command: the app has no sub-groups, so a two-word spelling
# is by construction a command that does not exist.
_LOOPLAB_COMMAND = re.compile(r"\blooplab\s+([A-Za-z][\w-]*)")


def _ui_src() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "src"


def _registered_command_names() -> set[str]:
    from looplab.cli import app
    names = set()
    for command in app.registered_commands:
        # Typer derives an unnamed command's CLI spelling from the callback, underscores to dashes.
        name = command.name or (command.callback.__name__.replace("_", "-")
                                if command.callback is not None else None)
        if name:
            names.add(name)
    return names


def _named_commands() -> list[tuple[Path, int, str, str]]:
    """(file, line, command, span) for every code-formatted `looplab <cmd>` under ui/src."""
    found = []
    root = _ui_src()
    for path in sorted(root.rglob("*.js")) + sorted(root.rglob("*.jsx")):
        text = path.read_text(encoding="utf-8")
        for match in _CODE_SPAN.finditer(text):
            span = match.group(1) or match.group(2) or ""
            for command in _LOOPLAB_COMMAND.findall(span):
                line = text.count("\n", 0, match.start()) + 1
                found.append((path, line, command, " ".join(span.split())[:80]))
    return found


def test_every_ui_named_cli_command_is_registered():
    registered = _registered_command_names()
    assert "ui" in registered and "concept-merge" in registered, (
        "the registry read is what makes this test non-vacuous; it returned nothing usable")
    unknown = [
        f"{path.name}:{line} names `looplab {command}` ({span!r}) — not a registered command"
        for path, line, command, span in _named_commands() if command not in registered
    ]
    assert not unknown, (
        "the UI presents a CLI command that does not exist:\n  " + "\n  ".join(unknown)
        + f"\nregistered: {sorted(registered)}")


def test_the_scan_actually_reaches_ui_source():
    """A silently-empty scan would make the test above pass forever.

    Pin the shape, not a count: at least one code-formatted `looplab <cmd>` must be found, and the
    known-good `looplab ui` in `apiClient.js` must be among them.
    """
    found = _named_commands()
    assert found, f"no code-formatted `looplab <cmd>` found under {_ui_src()} — the scan is broken"
    assert any(path.name == "apiClient.js" and command == "ui" for path, _, command, _ in found), (
        "apiClient.js's `looplab ui` should be visible to this scan; the span regex has drifted")
