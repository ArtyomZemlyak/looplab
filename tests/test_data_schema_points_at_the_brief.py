"""A task with no dataset must not imply its input is undescribed.

`_NO_ASSETS` is terminal on purpose: an answer a model reads as "not that one" cost nine wasted
`read_asset` calls in one deep-research phase on 2026-08-19, so it states the CLASS of the
emptiness. That property must survive.

What it did not survive is a task that describes its input elsewhere. Measured 2026-08-26 on a
`convex_hull` run whose goal carries `n = 267021` and `ndarray(shape=(267021, 2), dtype=float64)`:
the agent called `data_schema` twice and was told the task has no data assets "and no name will
change that" — true of the tool, and read as "nothing here knows anything about the input", which
the same prompt contradicts. These tests pin both halves.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "looplab" / "tools" / "run_tools.py"


def _no_assets_text() -> str:
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"_NO_ASSETS = \((.*?)\)\n", src, re.S)
    assert m, "_NO_ASSETS is gone — this guard has nothing to check"
    return " ".join(re.findall(r'"([^"]*)"', m.group(1)))


def test_it_still_refuses_a_retry_with_another_name():
    """The 2026-08-19 property. Losing it costs calls, so it is checked first."""
    text = _no_assets_text()
    assert "no name will change that" in text
    assert "zero in total" in text
    assert "Nothing here reads source files" in text


def test_it_sends_the_reader_to_where_the_input_IS_described():
    text = _no_assets_text()
    assert "goal/brief" in text, (
        "a task with no dataset is told nothing about its input, while its goal may describe it "
        "exactly — two parts of one card arguing")
    assert "read that, not this tool" in text


def test_it_does_not_claim_the_input_is_unknowable():
    """The falsifier for a rewrite that goes back to a flat denial."""
    text = _no_assets_text()
    for banned in ("nothing is known about", "there is no description",
                   "the input is not described"):
        assert banned not in text.lower(), banned
