"""The CLI reference names exactly the Typer app's commands (doc 52 row 25): compared, not grepped.

The command block at the top of `docs/guide/cli-reference.md` is hand-written — its one-liners are
curated, which a regenerated docstring dump would not be — so what is pinned is the SET, both
ways, derived from `looplab.cli.app` itself: a command the app registers that the block does not
name is a red test, and so is a line naming a command the app no longer has. Four commands had
landed without a line before this guard existed (`landlock-check`, `memory-orphans`,
`prior-citations`, `reap-service-files`).
"""
from __future__ import annotations

import re
from pathlib import Path

from looplab.cli import app

_DOC = Path(__file__).resolve().parents[1] / "docs" / "guide" / "cli-reference.md"


def _registered() -> set[str]:
    return {cmd.name or cmd.callback.__name__.replace("_", "-") for cmd in app.registered_commands}


def _documented_block() -> set[str]:
    text = _DOC.read_text(encoding="utf-8")
    block = text.split("```text\n", 1)[1].split("```", 1)[0]
    return set(re.findall(r"^looplab ([a-z0-9-]+)", block, re.M))


def test_every_registered_command_has_a_line_in_the_reference_block():
    missing = sorted(_registered() - _documented_block())
    assert not missing, f"command(s) the app registers with no line in cli-reference.md's block: {missing}"


def test_no_line_names_a_command_the_app_no_longer_has():
    ghosts = sorted(_documented_block() - _registered())
    assert not ghosts, f"cli-reference.md names command(s) the app does not register: {ghosts}"


def test_the_set_is_derived_from_the_app_not_a_hand_list():
    assert len(_registered()) >= 50, "the Typer app registers far fewer commands than it did — a group fell off"
