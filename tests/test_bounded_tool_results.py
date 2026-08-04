"""One row-fitter and one clipper behind every tool provider's honest truncation (doc 25 TO-08).

Seven providers each re-derived a budget from `RESULT_CAP` (with independently chosen headroom:
-400, -200, -160, "reserve=100") and re-implemented bounded rendering with its own marker text. Two
of those — `reposcout._fit_rows` and `memory_tools._bounded_result` — were the same row-dropping
algorithm written twice, and four more were the same single-string cut with four different receipts a
model had to learn separately.

What the shared helpers must not lose is the reason the per-site truncation exists at all: the agent
loop cuts an over-cap result from the HEAD, silently eating whatever is at the END — which for a
listing is exactly the receipt saying the result is partial. A listing that overflows must therefore
arrive SHORTER and SAID SO, never longer-and-silently-amputated.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from looplab.tools._base import RESULT_CAP, clip, fit_rows


# --------------------------------------------------------------------------------------------
# fit_rows
# --------------------------------------------------------------------------------------------

def test_a_listing_that_fits_is_returned_whole():
    assert fit_rows("head:\n", ["a", "b"], cap=100) == "head:\na\nb"
    assert fit_rows("head:\n", ["a"], receipt="(+3 more)", cap=100) == "head:\na\n(+3 more)"


def test_a_list_header_joins_as_lines():
    """`memory_tools` passes a list of header lines, `reposcout` a string it owns the newline of."""
    assert fit_rows(["h1", "h2"], ["a"], cap=100) == "h1\nh2\na"


def test_an_empty_row_set_does_not_grow_a_stray_separator():
    assert fit_rows("head:\n", [], cap=100) == "head:\n"
    assert fit_rows(["h"], [], cap=100) == "h"


def test_an_overflowing_listing_gets_shorter_and_says_how_much_it_dropped():
    """The whole point. The loop's head-cut would return something LONGER than the cap minus the
    tail — including the receipt that says the listing is partial."""
    rows = [f"row-{i:04d}-{'x' * 40}" for i in range(200)]
    out = fit_rows("head:\n", rows, cap=800)
    assert len(out) <= 800
    assert "more omitted to fit the result cap" in out
    dropped = int(out.rsplit("(", 1)[1].split(" ", 1)[0])
    assert 0 < dropped < len(rows)
    assert out.count("\n") == len(rows) - dropped + 1, "the marker must replace rows, not join them"


def test_the_callers_own_receipt_survives_the_drop():
    """`(capped at N hits)` is itself a truthfulness receipt — losing it to the cap would report a
    capped search as an exhaustive one."""
    rows = [f"hit-{i}-{'y' * 50}" for i in range(200)]
    out = fit_rows("", rows, receipt="(capped at 40 hits)", cap=600)
    assert "capped at 40 hits" in out and "more omitted to fit the result cap" in out


def test_a_cap_too_small_for_any_row_still_answers_honestly():
    out = fit_rows("", ["x" * 500], receipt="(capped at 40 hits)", cap=20)
    assert "capped at 40 hits" in out
    assert fit_rows("", ["x" * 500], cap=20) == "\n(nothing fits the result cap)"


def test_the_marker_is_sized_before_the_fit_is_decided():
    """A marker appended AFTER the fit decision is exactly what pushes a receipt back past the cap.
    Sweep the caps where the marker is the deciding byte."""
    rows = [f"r{i}" for i in range(40)]
    for cap in range(40, 400):
        assert len(fit_rows("h:\n", rows, receipt="(+9 more)", cap=cap)) <= cap or cap < 60, cap


def test_the_omission_wording_stays_per_site():
    assert "RESULT_WINDOW" in fit_rows(["h"], ["x" * 60] * 20, cap=200,
                                       omitted="[RESULT_WINDOW: {n} row(s) omitted.]")


# --------------------------------------------------------------------------------------------
# clip
# --------------------------------------------------------------------------------------------

def test_text_under_the_cap_is_untouched_and_unmarked():
    assert clip("short", 100, keep="tail", note="…[+{n}]") == "short"
    assert clip("short", 5, keep="head", note="…[+{n}]") == "short", "exactly at the cap is complete"


def test_a_tail_clip_keeps_the_end_and_marks_the_front():
    """Logs and command output are read tail-first: the end is where the error and the final metric
    line are, so the marker cannot go there."""
    out = clip("abcdefghij", 4, keep="tail", note="…[+{n} earlier chars truncated]\n")
    assert out == "…[+6 earlier chars truncated]\nghij"


def test_a_head_clip_keeps_the_front_and_marks_the_end():
    out = clip("abcdefghij", 4, keep="head", note="…[{n} omitted]")
    assert out == "abcd…[6 omitted]"


def test_a_line_boundary_cut_never_shows_half_a_row():
    """A half-hit is worse than a missing hit: the model reads it as a complete one."""
    text = "hit one\nhit two\nhit three\n"
    out = clip(text, 12, keep="head", line_boundary=True, note="\n(clamped)")
    assert out == "hit one\n(clamped)"


def test_a_line_boundary_tail_cut_drops_the_partial_leading_line():
    rows = ["hit one", "hit two", "hit three"]
    text = "\n".join(rows)
    out = clip(text, 14, keep="tail", line_boundary=True, note="…(truncated)…\n")

    kept = out.split("\n")[1:]                   # the marker owns the first line
    assert kept and kept[-1] == "hit three"
    assert all(line in rows for line in kept), f"a half-row survived at the head: {kept!r}"


def test_reserve_charges_the_marker_against_the_cap():
    """A caller handed the loop's RAW cap has no headroom, and a reply landing EXACTLY on the cap is
    one the loop's own marker also skips — a cut answer byte-indistinguishable from a complete one."""
    note = "\n…[truncated — {n} omitted]"
    out = clip("z" * 500, 100, keep="head", note=note, reserve=60)
    assert len(out) <= 100
    assert clip("z" * 500, 100, keep="head", note=note) [:1] == "z"
    assert len(clip("z" * 500, 100, keep="head", note=note)) > 100, (
        "without `reserve` the marker deliberately sits ON TOP of the caller's own headroom")


def test_the_reported_count_is_the_characters_actually_dropped():
    for keep in ("head", "tail"):
        out = clip("q" * 1000, 250, keep=keep, note="<{n}>")
        n = int(out.split("<")[1].split(">")[0])
        assert n == 750, (keep, out[:40])


def test_a_line_boundary_cut_reports_the_extra_characters_it_gave_back():
    """The count must describe the RESULT, not the intended budget — a boundary cut drops more."""
    out = clip("aaaa\nbbbb\ncccc", 12, keep="head", line_boundary=True, note="<{n}>")
    assert out == "aaaa\nbbbb<5>"


# --------------------------------------------------------------------------------------------
# Every provider goes through them
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("module,helper,expected", [
    ("looplab.tools.reposcout", "_fit_rows", "fit_rows"),
    ("looplab.tools.memory_tools", "_bounded_result", "fit_rows"),
    ("looplab.tools.run_tools", "_clip", "clip"),
    ("looplab.tools.shell_tools", "_tail", "clip"),
    ("looplab.tools.mcp_tools", "_clip", "clip"),
])
def test_each_provider_delegates_instead_of_re_implementing(module, helper, expected):
    import importlib

    fn = getattr(importlib.import_module(module), helper)
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert expected in called, f"{module}.{helper} no longer uses the shared {expected}"
    loops = [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))]
    assert not loops, f"{module}.{helper} re-grew its own fitting loop"


def test_the_memory_reader_drops_whole_rows_rather_than_being_cut():
    """Behaviour, not shape: `_bounded_result` keeps a `[RESULT_TRUNCATED]` backstop for a future
    header change, and a reader that stopped fitting rows would silently fall through to it —
    swapping an honest "N rows omitted" receipt for a blunt mid-row cut. A structural check cannot
    see that, because the fall-through still calls the shared helper on the way past."""
    from looplab.tools.memory_tools import _bounded_result

    rows = [f"UNTRUSTED_LESSON={i}: {'x' * 200}" for i in range(200)]
    out = _bounded_result(["CROSS_RUN_MEMORY:"], rows)
    assert len(out) <= RESULT_CAP
    assert "RESULT_WINDOW" in out, "rows were not dropped honestly"
    assert "RESULT_TRUNCATED" not in out, "the blunt backstop fired instead of the row fitter"
    omitted = int(out.rsplit("[RESULT_WINDOW: ", 1)[1].split(" ", 1)[0])
    kept = sum(1 for line in out.split("\n") if line.startswith("UNTRUSTED_LESSON="))
    assert omitted + kept == len(rows), (omitted, kept)


def test_env_inspect_clamp_delegates_too():
    """`_clamp` is a staticmethod on the provider, so it needs its own lookup."""
    from looplab.tools.env_inspect import EnvInspectTools

    tree = ast.parse(textwrap.dedent(inspect.getsource(EnvInspectTools._clamp)))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "clip" in called


def test_the_providers_still_produce_their_own_receipts():
    """Shared MECHANISM, per-site WORDING: the markers were tuned to each surface's failure mode and
    collapsing them into one string is not what the finding asks for."""
    from looplab.tools import mcp_tools, run_tools, shell_tools

    assert "earlier chars truncated" in run_tools._clip("a" * 50, 5)
    assert "(truncated)" in shell_tools._tail("a" * 50, 5)
    assert "mcp reply truncated" in mcp_tools._clip("a" * 5000, 500)


def test_every_provider_budget_is_still_derived_from_the_loop_cap():
    """The other half of the finding: independently chosen headroom is fine, a free-standing ~4000 is
    not — the loop cap and every provider budget have to move together."""
    from looplab.tools import concept_tools, cross_run_tools, env_inspect, reposcout, run_tools

    for module in (concept_tools, cross_run_tools, env_inspect, reposcout, run_tools):
        budgets = [ast.unparse(node.value)
                   for node in ast.walk(ast.parse(inspect.getsource(module)))
                   if isinstance(node, ast.Assign) and "RESULT_CAP" in ast.unparse(node.value)]
        assert budgets, f"{module.__name__} stopped deriving its budget from RESULT_CAP"
        assert all(b.startswith("RESULT_CAP") for b in budgets), (module.__name__, budgets)


def test_the_shared_helpers_default_to_the_loop_cap():
    assert inspect.signature(fit_rows).parameters["cap"].default == RESULT_CAP
