"""Every declared `array` property must carry `items`, or Google models cannot be used at all.

Google's function-declaration validator refuses an array property with no `items` — and it refuses
the WHOLE request, not just that tool:

    GenerateContentRequest.tools[0].function_declarations[3].parameters.properties[stages].items:
    missing field.   INVALID_ARGUMENT

So one such property anywhere in a toolset makes every Google model undeclarable for the phase that
composes it. Measured 2026-08-21: a `google/gemini-3.7-flash` run died 110 s in, at its FIRST
implement session, with `developer_crash` and an auto-pause — the offender was `declare_stages`,
whose `stages` array described its shape in PROSE and declared no `items`. OpenAI- and
DeepSeek-family endpoints accept that, which is why it survived this long and why a test is what
holds the line rather than the next run.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _array_dicts_without_items(tree: ast.AST):
    """Every dict literal that says `"type": "array"` and has no `"items"` key."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        if "type" not in keys:
            continue
        value = node.values[keys.index("type")]
        if isinstance(value, ast.Constant) and value.value == "array" and "items" not in keys:
            out.append(node.lineno)
    return out


def test_no_declared_array_omits_items():
    """AST over the real source (CLAUDE.md tier 3): a schema in a comment cannot satisfy or break
    this, and a substring scan for '"type": "array"' could not tell whether `items` was present."""
    # `_source_scan`'s walk, not a private `rglob` (doc 25 XP-10): the copies of that walk had
    # already diverged on DECODING, and a hard-coded `encoding="utf-8"` here would turn this guard
    # into a `UnicodeDecodeError` at collection the moment one source arrived BOM'd.
    from _source_scan import iter_trees

    offenders = []
    for path, tree in iter_trees():
        for lineno in _array_dicts_without_items(tree):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert offenders == [], (
        "array schema(s) with no `items` — Google refuses the whole toolset over these: "
        + ", ".join(offenders))


def test_the_scan_would_actually_catch_one():
    """A guard that cannot fail is not a guard. Drive the predicate over a schema that HAS the
    defect, so a refactor that quietly stops walking dict literals goes red here rather than going
    quiet everywhere."""
    bad = ast.parse('spec = {"stages": {"type": "array", "description": "ordered stages"}}')
    assert _array_dicts_without_items(bad), "the scan no longer detects a missing `items`"
    good = ast.parse('spec = {"stages": {"type": "array", "items": {"type": "object"}}}')
    assert not _array_dicts_without_items(good), "the scan flags a schema that is fine"


def test_the_write_tools_declare_stages_carries_items():
    """The specific tool that broke it, driven through the real provider rather than its source."""
    from looplab.adapters.repo_write_tools import RepoWriteTools

    specs = RepoWriteTools(None, [], []).specs()
    arrays = [(fn.get("name"), prop, schema)
              for spec in specs
              for fn in [spec.get("function", spec)]
              for prop, schema in (((fn.get("parameters") or {}).get("properties") or {})).items()
              if schema.get("type") == "array"]
    assert arrays, "declare_stages no longer declares an array — re-point this test"
    for name, prop, schema in arrays:
        assert schema.get("items"), f"{name}.{prop} is an array with no `items`"
