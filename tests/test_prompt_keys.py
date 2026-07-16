"""PROMPT_KEYS registry enforcement (docs/15 §P4.7).

A prompt override lands as `<prompt_dir>/<key>.md`, so a typo'd key at a `render()` call site —
or a renamed key with a stale override file — silently falls back to the built-in default: the
operator's tuned prompt just stops applying, with no error anywhere. Same registry+source-scan
discipline as event types / hints / signals / task hooks.
"""
from __future__ import annotations

import re
from pathlib import Path

from looplab.core.prompts import PROMPT_KEYS

_PKG = Path(__file__).resolve().parents[1] / "looplab"
# \s* after the paren crosses newlines: best_of_n spells `render(\n    prompts, "key", …)` —
# the original same-line-only pattern was blind to it (the P4 review's own HIGH finding).
_CALL = re.compile(r'render\(\s*[\w.]+\s*,\s*"([a-z_]+)"')


def _call_keys() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for f in _PKG.rglob("*.py"):
        for key in _CALL.findall(f.read_text(encoding="utf-8", errors="replace")):
            found.setdefault(key, set()).add(str(f.relative_to(_PKG)))
    return found


def test_every_render_key_is_registered():
    unknown = {k: fs for k, fs in _call_keys().items() if k not in PROMPT_KEYS}
    assert not unknown, (
        f"render() call site(s) use unregistered prompt key(s) {unknown} — register in "
        "core/prompts.py::PROMPT_KEYS (and document the override file name) or fix the typo.")


def test_every_registered_key_has_a_call_site():
    calls = set(_call_keys())
    orphaned = [k for k in PROMPT_KEYS if k not in calls]
    assert not orphaned, (
        f"registered prompt key(s) {orphaned} have no render() call site — a rename left the "
        "registry (and any operator override files named after the old key) behind.")


def test_registered_keys_are_valid_override_filenames():
    for k in PROMPT_KEYS:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", k), f"{k!r}: keys become <key>.md filenames"


def test_prompt_store_override_roundtrip(tmp_path):
    # End-to-end: an override file named after a registered key actually replaces the default.
    from looplab.core.prompts import PromptStore, render
    (tmp_path / "developer_system.md").write_text("OVERRIDDEN $x", encoding="utf-8")
    store = PromptStore(str(tmp_path))
    assert render(store, "developer_system", "default", x="1") == "OVERRIDDEN 1"
    assert render(store, "researcher_system", "default") == "default"   # no file -> default
    assert render(None, "developer_system", "default") == "default"     # no store -> default


def test_prompt_vars_named_name_or_default_do_not_collide(tmp_path):
    # A template may use $name/$default as substitution vars; get()/render() take name/default as
    # positional-only params, so those vars pass cleanly through **vars instead of raising TypeError.
    from looplab.core.prompts import PromptStore, render
    (tmp_path / "researcher_system.md").write_text("Hi $name ($default)", encoding="utf-8")
    store = PromptStore(str(tmp_path))
    assert render(store, "researcher_system", "unused", name="Ada", default="D") == "Hi Ada (D)"
    assert store.get("researcher_system", "unused", name="Ada", default="D") == "Hi Ada (D)"
    assert render(None, "k", "Hi $name", name="Bob") == "Hi Bob"
