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


# --------------------------------------------------------------------- the DEFAULT column, compared
# Doc 50 DX-03 / doc 52 row 25: this file asserted every field had a ROW and never that the row's
# default was the field's. The comparator below understands the table's own conventions — a
# backticked value, a trailing annotation (`0` (AUTO)), `—` / `_(unset)_` for None or empty, `~` for
# the home directory, JSON for a list or dict — so a real drift is the only thing it can report.
_UNSET_CELLS = {"—", "_(unset)_", "(unset)", "null", "none", ""}
# The one row whose default is rendered in prose below the table rather than in the cell, because
# it is a nested dict (`agent_control`, the governance matrix). Shrink-only.
_PROSE_DEFAULTS = {"agent_control": "*(see below)*"}


def _documented_defaults() -> dict:
    import re
    text = _DOC.read_text(encoding="utf-8")
    return {m.group(1): m.group(2).strip() for m in re.finditer(
        r"^\| `([A-Za-z_][A-Za-z0-9_]*)` \| `LOOPLAB_[A-Z0-9_]+` \| (.+?) \| ", text, re.M)}


def _normalized_cell(cell: str) -> str:
    import re
    c = re.sub(r"\s*\([^()]*\)\s*$", "", cell.strip()).strip()      # `0` (AUTO) -> `0`
    if c.startswith("`") and c.endswith("`"):
        c = c[1:-1]
    return c.strip()


def documented_default_matches(cell: str, value) -> bool:
    """Does the table cell state `value`, under the table's own rendering conventions?"""
    import json
    import os
    from pathlib import Path

    c = _normalized_cell(cell)
    if c.lower() in _UNSET_CELLS:
        return value is None or value in ("", [], {})
    if isinstance(value, bool):
        return c.lower() == ("true" if value else "false")
    if isinstance(value, (int, float)):
        try:
            return float(c) == float(value)
        except ValueError:
            return False
    if value is None:
        return False
    if isinstance(value, (list, dict, tuple)):
        try:
            return json.loads(c) == json.loads(json.dumps(value))
        except ValueError:
            return False
    text = str(value)
    bare = c.strip('"').strip("'")
    return bare == text or os.path.expanduser(bare) == text or bare == text.replace(str(Path.home()), "~")


def test_every_documented_default_is_the_fields_declared_default(monkeypatch):
    """Compared, not grepped: each row's default cell against a live `Settings()` built with the
    two directory overrides the test environment sets removed (their factories read the env)."""
    for var in ("LOOPLAB_MEMORY_DIR", "LOOPLAB_KNOWLEDGE_DIR"):
        monkeypatch.delenv(var, raising=False)
    live = Settings()
    drift = []
    for name, cell in _documented_defaults().items():
        if not hasattr(live, name):
            continue                                     # the ghost-row test owns this case
        if _PROSE_DEFAULTS.get(name) == cell:
            continue
        if not documented_default_matches(cell, getattr(live, name)):
            drift.append((name, cell, getattr(live, name)))
    assert not drift, ("configuration.md documents a DEFAULT that is not the field's — "
                       f"(field, documented, actual): {drift}")


def test_the_default_comparator_reads_the_tables_conventions():
    """The comparator's own truth table, so a convention it stops understanding is a red test here
    and not a silent pass above."""
    assert documented_default_matches("`0` (AUTO)", 0) and not documented_default_matches("`1` (AUTO)", 0)
    assert documented_default_matches("`true`", True) and not documented_default_matches("`true`", False)
    assert documented_default_matches("—", None) and documented_default_matches("_(unset)_", "")
    assert not documented_default_matches("—", 0), "an unset cell is not a documented zero"
    assert documented_default_matches('`["a", "b"]`', ["a", "b"]) and not documented_default_matches('`["a"]`', ["a", "b"])
    assert documented_default_matches("`~/.looplab/memory`", str(__import__("pathlib").Path.home() / ".looplab" / "memory"))
    assert documented_default_matches("`audit`", "audit") and not documented_default_matches("`gate`", "audit")
    assert documented_default_matches("`-1.0` (AUTO)", -1.0) and documented_default_matches("`8`", 8.0)

