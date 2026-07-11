"""CLAUDE.md's docs-sync rule, machine-checked (docs/15 §P4.5).

The repo declares stale docs a bug: every `Settings` field must have a row in
docs/guide/configuration.md. That held by discipline alone — the next forgotten row would rot
silently. This test converts the rule into a red test (the same registry+test pattern as
event types / hints / signals / layout).

Combined-row convention: the doc may cover sibling fields with one row (e.g. a single
`researcher_x / developer_x / strategist_x` row) — a field passes if its NAME appears anywhere
in the file, so combined rows and prose mentions both count. What CANNOT pass is a field the
doc never names at all.
"""
from __future__ import annotations

from pathlib import Path

from looplab.core.config import Settings

_DOC = Path(__file__).resolve().parents[1] / "docs" / "guide" / "configuration.md"


def test_every_settings_field_is_documented():
    text = _DOC.read_text(encoding="utf-8")
    missing = [f for f in Settings.model_fields if f not in text]
    assert not missing, (
        f"Settings field(s) {missing} have no mention in docs/guide/configuration.md — "
        "CLAUDE.md requires the settings table row in the SAME change that adds the field.")


def test_no_ghost_rows_for_removed_fields():
    # The reverse direction: a settings-TABLE row's leading backticked name must still exist on
    # Settings — a removed knob must take its doc row with it. Scanning only `| \`name\`` table
    # rows (not prose backticks) keeps this exact: every field family is covered with zero
    # false positives on hook names / file names mentioned in prose.
    import re
    text = _DOC.read_text(encoding="utf-8")
    fields = set(Settings.model_fields)
    rows = re.findall(r"^\|\s*`([a-z][a-z0-9_]+)`", text, re.M)
    # combined rows spell `researcher_x / developer_x` in one cell — split on the slash form:
    names = set()
    for r in rows:
        names.add(r)
    ghosts = sorted(n for n in names if n not in fields)
    assert not ghosts, (
        f"configuration.md settings-table row(s) {ghosts} name no existing Settings field "
        "— a removed/renamed knob left its doc row behind.")
