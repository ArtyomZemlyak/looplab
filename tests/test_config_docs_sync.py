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
    # The reverse direction: a `field_name` in backticks in the settings tables should still
    # exist on Settings — a removed knob must take its doc row with it. Scan only backticked
    # snake_case tokens that LOOK like setting names to avoid false positives on prose.
    import re
    text = _DOC.read_text(encoding="utf-8")
    fields = set(Settings.model_fields)
    candidates = set(re.findall(r"`([a-z][a-z0-9_]{2,})`", text))
    # filter obvious non-settings vocabulary: env vars are UPPER, cli flags have dashes (already
    # excluded by the pattern); allow doc-level tokens that name files/commands/values.
    ghosts = sorted(c for c in candidates
                    if c not in fields
                    and ("_" in c) and not c.startswith(("looplab", "test_", "ev_"))
                    and c + "s" not in fields and c.rstrip("s") not in fields
                    and c in {f"{x}" for x in candidates}  # identity — kept for clarity
                    and any(c.startswith(p) for p in (
                        # only flag tokens shaped like our knob families to stay low-noise:
                        "llm_", "agent_", "novelty_", "confirm_", "holdout_", "lessons_",
                        "inline_repair_", "memora_", "foresight_", "proxy_", "deep_research_",
                        "surrogate_", "ablate_", "report_", "research_", "sandbox_", "eval_")))
    assert not ghosts, (
        f"configuration.md names setting-shaped token(s) {ghosts} that are not Settings fields "
        "— a removed/renamed knob left its doc row behind.")
