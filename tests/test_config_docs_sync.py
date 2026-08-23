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
from looplab.serve.settings_ui_schema import (
    SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT,
    SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT,
)

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


def test_the_catalogue_count_is_stated_ONCE_and_no_stale_copy_survives_beside_it():
    """`assert <the true sentence> in text` is satisfied by ONE true copy and says nothing about
    the others.

    docs/guide/configuration.md carries this claim in a paragraph that a merge duplicated: four
    copies of "server-owned curated catalogue with **N of the M direct `Settings` fields**", of
    which a later change re-pointed exactly one. The count guard below passed — the live sentence
    was there — while the three lines under it told the reader 183 of 216 and the true answer was
    186 of 219. A pin that only looks for the truth cannot see a lie printed next to it.

    So the rule is about the WHOLE FILE: this pair of numbers appears here, and every place it
    appears says the same thing. Derived from the catalogue and the live model, never restated.
    """
    import re
    text = _DOC.read_text(encoding="utf-8")
    live = (SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT, SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT)
    stated = [(int(a), int(b)) for a, b in
              re.findall(r"\*\*(\d+) of the (\d+) direct `Settings` fields", text)]
    assert stated, ("docs/guide/configuration.md no longer states the catalogue size at all — "
                    "it is the sentence tests/test_settings_ui_schema.py's row count is FOR")
    wrong = sorted({pair for pair in stated if pair != live})
    assert not wrong, (
        f"docs/guide/configuration.md states the catalogue size as {wrong} somewhere as well as "
        f"the live {live}. Every copy has to move together, or the file contradicts itself in "
        "the same paragraph — re-point them all, or delete the duplicates.")
    # ...and the searchable-key count is the same catalogue, so it is the same number.
    searchable = [int(n) for n in re.findall(r"search spans all (\d+) catalogued keys", text)]
    bad = sorted({n for n in searchable if n != SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT})
    assert not bad, (
        f"docs/guide/configuration.md says search spans {bad} catalogued keys; the catalogue holds "
        f"{SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT}")


def test_settings_catalogue_counts_and_profile_semantics_are_current():
    text = _DOC.read_text(encoding="utf-8")
    assert (f"{SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT} of the "
            f"{SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT} direct `Settings` fields") in text
    profile = text.split("## Profile (one-word preset)", 1)[1].split("## Search budget", 1)[0]
    # CODEX AGENT: product Settings deliberately ship Part IV/V on; `thorough` is the extra
    # trust/quality bundle and must not be documented as the switch for every intelligence feature.
    assert "every intelligence feature" not in profile
    assert "Part IV/V" in profile and "normally-disabled quality/trust bundle" in profile


def test_cli_docs_expose_recovery_and_raw_snapshot_boundaries():
    root = _DOC.parents[2]
    cli = (root / "docs" / "guide" / "cli-reference.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "looplab finalize RUN_DIR [--task-file TASK.json]" in cli
    assert "inclusive range `1..64`" in cli
    assert "raw launch snapshot + current folded best result" in cli
    assert "raw launch snapshot + current folded best result" in readme
