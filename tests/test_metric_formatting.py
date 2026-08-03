"""One metric formatter, three configurations, and a JS twin (doc 25 XP-09).

Three copies rendered the same best-metric value three ways — `?` vs `—`, `%.4g` vs `%.5g`, exponent
switch or not — so an operator comparing a run in the TUI against the same run in a cross-run scope
report could read two different numbers, and a formatting fix had to be found and applied three
times. The rule now lives once in `core.fitness`, and the differences that remain are keyword
arguments with a reason each.
"""
from __future__ import annotations

import inspect
import math
import re
from pathlib import Path

import pytest

from looplab.core.fitness import format_metric
from looplab.events.digest import fmt_num
from looplab.serve.scope_report import _fmt_metric
from looplab.serve.tui_format import fmt_metric

UI_FORMAT_JS = Path(__file__).resolve().parents[1] / "ui" / "src" / "format.js"


# ------------------------------------------------------------------ the shared rule

@pytest.mark.parametrize("value,expected", [
    (None, "—"), (float("nan"), "—"),
    (0, "0"), (1, "1"), (-0.0, "-0"), (0.1, "0.1"),
    (1234.5678, "1235"), (1 / 3, "0.3333"),
    (1e-7, "1.00e-07"), (1e7, "1.00e+07"),
    ("already text", "already text"),
])
def test_the_default_configuration_is_the_human_one(value, expected):
    assert format_metric(value) == expected


def test_the_exponent_switch_has_an_inclusive_lower_and_exclusive_upper_edge():
    """Off-by-one here silently changes how a whole column reads."""
    assert format_metric(1e-3) == "0.001", "1e-3 itself stays decimal"
    assert format_metric(9.99e-4) == "9.99e-04"
    assert format_metric(999_000.0) == "999000"
    assert format_metric(1e6) == "1.00e+06", "1e6 itself switches"
    assert format_metric(0) == "0", "zero is never exponential"
    # The window is checked BEFORE the significant-digit round, so a value just under the edge can
    # still come out exponential once rounding pushes it over. Pinned as behaviour, not endorsed as
    # design — every pre-extraction copy did this, and changing it would move displayed numbers.
    assert format_metric(999_999.0) == "1e+06"


def test_absent_nan_is_separable_from_absent_none():
    """`fmt_num` needs exactly this split: it formats raw PARAM values too, where a NaN must stay
    visible as a diverged number rather than becoming "unknown"."""
    assert format_metric(float("nan"), absent_nan=False) == "nan"
    assert format_metric(None, absent_nan=False) == "—", "None is absent regardless"


# ------------------------------------------------------------------ the three call sites

def test_no_call_site_re_derives_the_rule():
    for func in (fmt_num, fmt_metric, _fmt_metric):
        source = inspect.getsource(func)
        assert "format_metric(" in source, f"{func.__name__} does not use the shared rule"
        assert "1e-3" not in source and "1e6" not in source, (
            f"{func.__name__} re-derives the exponent window")
        assert ":.2e" not in source, f"{func.__name__} re-derives the exponent form"


@pytest.mark.parametrize("value,expected", [
    (None, "?"), (1234.5678, "1235"), (1 / 3, "0.3333"), (1e-7, "1e-07"), (1e7, "1e+07"),
])
def test_the_digest_output_is_unchanged(value, expected):
    """PROMPT text, and prompt strings are contracts: `?` so the model cannot read a dash as a value,
    and no exponent switch because `%.4g` is what the Researcher has been reading."""
    assert fmt_num(value) == expected


def test_the_digest_keeps_a_nan_visible():
    """`fmt_params` routes raw param VALUES through `fmt_num`. Rendering a NaN param as "?" would
    report "I don't know this value" when the fact worth reporting is that it diverged."""
    assert fmt_num(float("nan")) == "nan"


@pytest.mark.parametrize("value,expected", [
    (None, "—"), (float("nan"), "—"), (1234.5678, "1234.6"), (1 / 3, "0.33333"),
])
def test_the_scope_report_keeps_its_extra_digit_and_gains_a_nan_fix(value, expected):
    """A cross-run table is read column-by-column, so it takes one more significant digit and no
    exponent switch. Routing through the shared rule also fixed a NaN, which used to print the bare
    text `nan` in a report that already spells absence `—`."""
    assert _fmt_metric(value) == expected


def test_the_tui_is_the_shared_default():
    """It was the most complete of the three copies, so it is the shape the default keeps."""
    for value in (None, float("nan"), 0, 1e-7, 1e7, 1234.5678, "text"):
        assert fmt_metric(value) == format_metric(value)
    assert fmt_metric(1 / 3, 2) == format_metric(1 / 3, precision=2) == "0.33"


# ------------------------------------------------------------------ the JS twin

def test_the_js_twin_still_implements_the_same_rule():
    """`ui/src/format.js::fmt` cannot import Python. It is the fourth copy the finding counted, and it
    stays a copy — so pin the decisions that must not drift, and let a change to either side be a
    visible edit here rather than two surfaces quietly disagreeing.

    The parity is at the level of the RULE, not the byte: `%g` and `Number.prototype.toString`
    disagree on how to render the result (`1e+06` vs `1000000`, `1.00e-07` vs `1.00e-7`). That
    predates the extraction on both sides and is display-only, so it is recorded rather than
    "fixed" — changing either would move numbers an operator is already used to reading.
    """
    source = UI_FORMAT_JS.read_text(encoding="utf-8")
    body = source[source.index("export function fmt(") : source.index("export function fmtInt")]
    assert re.search(r"Number\.isNaN\(v\)\) return '—'", body), "same absent sentinel for NaN/None"
    assert "typeof v !== 'number'" in body, "same non-numeric passthrough"
    assert re.search(r"a < 1e-3 \|\| a >= 1e6", body), "same exponent window"
    assert "v.toExponential(2)" in body, "same two-digit exponent form"
    assert re.search(r"Number\(v\.toPrecision\(p\)\)\.toString\(\)", body), (
        "same significant-digit rendering with trailing zeros dropped")
    assert "p = 4" in body, "same default precision as `format_metric`"


def test_the_python_side_names_the_twin():
    """So the next person changing one goes looking for the other."""
    assert "format.js" in (format_metric.__doc__ or "")
