"""One tolerant-JSONL-prefix rule, four readers (doc 25 EV-05).

An append-only log's recoverable prefix ends at the first line that is torn, unparseable, or valid
JSON but not an object. Four readers implement that rule — `iter_jsonl` (streaming), the incremental
`_parse_jsonl_region` behind `EventStore.read_all`, `span_index._scan_light`, and `log_divergence`'s
walk — and their agreement used to be maintained by comments reading "matches iter_jsonl". It is not
a cosmetic agreement: the byte offset each returns is a durable watermark. If `_scan_light` accepted
one line more than `read_all`, the span index would claim coverage of bytes the event log treats as
damage; if it accepted one less, an already-indexed span would be re-indexed and duplicated.

This file is the shared equivalence test the extraction makes possible: every reader is driven over
the SAME corpus of adversarial buffers and must agree on where the prefix ends.
"""
from __future__ import annotations

import re
from pathlib import Path

import orjson
from _source_scan import iter_sources

import pytest

from looplab.events.eventstore import (
    JsonlRecordInvalid,
    _parse_jsonl_region,
    decode_jsonl_line,
    iter_jsonl,
    scan_jsonl_region,
)
from looplab.events.span_index import _scan_light


def _row(**kw) -> bytes:
    return orjson.dumps(kw)


# Each case: the raw buffer, then the records the prefix rule must accept from it.
CORPUS: list[tuple[str, bytes, list[dict]]] = [
    ("empty", b"", []),
    ("one record", _row(a=1) + b"\n", [{"a": 1}]),
    ("no trailing newline is torn", _row(a=1), []),
    ("torn final line after a good one", _row(a=1) + b"\n" + b'{"b": ', [{"a": 1}]),
    ("blank lines are skipped, not damage",
     _row(a=1) + b"\n\n   \n" + _row(b=2) + b"\n", [{"a": 1}, {"b": 2}]),
    ("corrupt line ends the prefix",
     _row(a=1) + b"\nnot json\n" + _row(b=2) + b"\n", [{"a": 1}]),
    ("valid JSON but not an object ends the prefix",
     _row(a=1) + b"\n[1, 2, 3]\n" + _row(b=2) + b"\n", [{"a": 1}]),
    ("a bare JSON string is not a record",
     b'"hello"\n' + _row(a=1) + b"\n", []),
    ("a JSON number is not a record", b"42\n" + _row(a=1) + b"\n", []),
    ("null is not a record", b"null\n" + _row(a=1) + b"\n", []),
    ("unicode payloads survive", _row(k="é中\U0001f600") + b"\n",
     [{"k": "é中\U0001f600"}]),
    ("a trailing-whitespace line still parses", _row(a=1) + b"   \n", [{"a": 1}]),
    ("empty object is a record", b"{}\n" + _row(a=1) + b"\n", [{}, {"a": 1}]),
]
IDS = [name for name, _buf, _want in CORPUS]


@pytest.mark.parametrize("buf,want", [(b, w) for _n, b, w in CORPUS], ids=IDS)
def test_streaming_reader_accepts_exactly_the_prefix(buf, want, tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(buf)
    assert list(iter_jsonl(path)) == want


@pytest.mark.parametrize("buf,want", [(b, w) for _n, b, w in CORPUS], ids=IDS)
def test_every_reader_agrees_on_the_same_prefix(buf, want, tmp_path):
    """The equivalence the four copies used to maintain by comment."""
    path = tmp_path / "log.jsonl"
    path.write_bytes(buf)

    streamed = list(iter_jsonl(path))
    region, region_consumed = _parse_jsonl_region(buf)
    scanned, scan_consumed = scan_jsonl_region(buf)
    # `_scan_light` projects spans, so drive it with buffers it can normalize and compare COVERAGE —
    # the number it persists as its watermark — rather than payloads.
    _light, light_covered = _scan_light(buf, 0)

    assert streamed == want
    assert [obj for obj, _off in region] == want
    assert [obj for obj, _start, _end in scanned] == want
    assert region_consumed == scan_consumed == light_covered, (
        "the readers disagree on where the recoverable prefix ends; the difference is a durable "
        "watermark, so one of them indexes bytes another treats as damage")
    # The watermark is always a newline boundary, and it never covers a rejected line.
    assert region_consumed == 0 or buf[region_consumed - 1:region_consumed] == b"\n"


@pytest.mark.parametrize("buf,want", [(b, w) for _n, b, w in CORPUS], ids=IDS)
def test_region_offsets_point_at_the_bytes_that_produced_each_record(buf, want):
    """`_scan_light` seeks by these offsets to recover a span line verbatim; `read_all` rewinds to
    them to reject a record. An offset that is off by one silently returns a neighbouring line."""
    scanned, _consumed = scan_jsonl_region(buf)
    for obj, start, end in scanned:
        assert orjson.loads(buf[start:end]) == obj
        assert buf[end:end + 1] == b"\n"
        assert start == 0 or buf[start - 1:start] == b"\n"


def test_the_three_line_outcomes_are_distinct():
    """Blank is SKIP, not stop — a stop there would truncate a log at a stray newline."""
    assert decode_jsonl_line(b'{"a": 1}') == {"a": 1}
    assert decode_jsonl_line(b"") is None
    assert decode_jsonl_line(b"   \t ") is None
    for damaged in (b"not json", b"[1]", b'"s"', b"7", b"null", b"true", b"{"):
        with pytest.raises(JsonlRecordInvalid):
            decode_jsonl_line(damaged)


def test_a_mid_file_corruption_is_reported_rather_than_silently_dropped(tmp_path):
    """`log_divergence` shares the same line rule but the OPPOSITE stop policy: it must walk PAST
    the bad line to count what a reader would drop. Pinned here so a future unification does not
    quietly give it the reader's break."""
    from looplab.core.models import Event
    from looplab.events.eventstore import log_divergence

    path = tmp_path / "events.jsonl"
    rows = [orjson.dumps(Event(seq=i, ts=1.0, type="t", data={}).model_dump(mode="json"))
            for i in range(3)]
    path.write_bytes(rows[0] + b"\n" + b"[1,2]\n" + rows[1] + b"\n" + rows[2] + b"\n")
    div = log_divergence(path)
    assert div == {"good_records": 1, "corrupt_line": 2, "dropped_lines": 2}
    # ...while the readers stop at it, which is exactly why the divergence has to be reported.
    assert len(list(iter_jsonl(path))) == 1


# The signature of an inlined APPEND-ONLY stop: a JSON decode failure whose next statement is
# `break`. Deliberately narrow. A `continue` there is a different (and legitimate) policy — mutable
# stores and the span index's random-access row read both step over damage on purpose — and a
# tuple-`except` that merely LISTS the error type among others is not this rule at all.
_INLINED_STOP = re.compile(
    r"except\s+orjson\.JSONDecodeError\s*:\s*\n"     # the decode failure, alone in its except
    r"(?:\s*#[^\n]*\n)*"                             # any why-comment lines
    r"\s*break\b")                                   # ...and the append-only stop


def test_no_reader_reimplements_the_line_rule():
    """Source guard, in the spirit of the seam registries in CLAUDE.md: the rule is spelled once.

    Two deliberate exemptions, both because their POLICY differs rather than their code:
    `read_jsonl_lenient*` steps over the same damage on purpose (a mutable store must not let one
    bad line hide every later one), and `serve/log_pages.py` runs a bounded streaming scanner with
    its own byte cap and seq fence whose decode failure is re-raised as a `ValueError` rather than
    breaking here. Both are matched by neither the pattern below nor this list by accident — the
    pattern is narrow on purpose, and a future copy that DOES spell the stop inline will trip it."""
    pkg = Path(__file__).resolve().parents[1] / "looplab"
    definition_site = pkg / "events" / "eventstore.py"
    offenders = []
    for path, source in iter_sources(pkg):
        if path == definition_site:
            continue
        if _INLINED_STOP.search(source):
            offenders.append(str(path.relative_to(pkg.parent)))
    assert not offenders, (
        "these modules re-derive the tolerant-JSONL stop rule instead of calling "
        f"eventstore.decode_jsonl_line / scan_jsonl_region: {offenders}")


def test_the_source_guard_can_actually_see_an_inlined_stop():
    """A source scan that matches nothing is indistinguishable from one whose pattern is broken."""
    assert _INLINED_STOP.search(
        "        try:\n"
        "            obj = orjson.loads(line)\n"
        "        except orjson.JSONDecodeError:\n"
        "            break  # corrupt tail\n")
    assert _INLINED_STOP.search(
        "        except orjson.JSONDecodeError:\n"
        "            # a why-comment may sit in between\n"
        "            break\n")
    # ...and does NOT fire on the two exempt policies, so the exemptions are earned, not asserted.
    assert not _INLINED_STOP.search(
        "        except orjson.JSONDecodeError:\n            continue  # skip this row\n")
    assert not _INLINED_STOP.search(
        "        except (OSError, ValueError, orjson.JSONDecodeError) as exc:\n"
        "            raise Boom() from exc\n")


# --- EV-12: generic JSONL store I/O lives in core, not in the event package ----------------------

def test_the_generic_jsonl_helpers_live_in_core():
    """`events/eventstore.py` hosted the mutable-store readers/writers because it was the module that
    needed them first. Nothing about them is event-related, and subsystems that only want to read a
    JSONL file (lessons, memory, the knowledge tools, trust/harden, the spans reader) should not have
    to import `events` to do it (doc 25 EV-12)."""
    from looplab.core import jsonlio

    for name in ("JsonlRecordInvalid", "decode_jsonl_line", "scan_jsonl_region", "iter_jsonl",
                 "read_jsonl_lenient", "read_jsonl_lenient_with_health", "write_jsonl_atomic",
                 "replace_jsonl_rows_atomic_preserving_quarantine"):
        assert hasattr(jsonlio, name), f"core.jsonlio lost {name}"


def test_both_spellings_are_the_same_objects():
    """The re-export contract: every existing `from looplab.events.eventstore import ...` and every
    monkeypatch seam through it must keep working, which requires object IDENTITY, not just a name
    that resolves. A star-import copy would satisfy the name and silently break the patch."""
    from looplab.core import jsonlio
    from looplab.events import eventstore

    for name in ("JsonlRecordInvalid", "decode_jsonl_line", "scan_jsonl_region", "iter_jsonl",
                 "read_jsonl_lenient", "read_jsonl_lenient_with_health", "write_jsonl_atomic",
                 "replace_jsonl_rows_atomic_preserving_quarantine"):
        assert getattr(eventstore, name) is getattr(jsonlio, name), f"{name} is a COPY, not a re-export"


def test_the_core_home_does_not_import_the_event_package():
    """`core` imports nothing above itself (CLAUDE.md layering). If the moved helpers had dragged an
    `events` import along, the move would have bought nothing."""
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "looplab" / "core" / "jsonlio.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    upward = []
    for node in ast.walk(tree):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        names = ([module] if module else
                 [a.name for a in node.names] if isinstance(node, ast.Import) else [])
        for name in names:
            if name and name.startswith("looplab.") and not name.startswith("looplab.core"):
                upward.append(f"line {node.lineno}: {name}")
    assert not upward, f"core/jsonlio reaches outside core: {upward}"


def test_the_stop_versus_skip_distinction_survived_the_move():
    """The load-bearing part. `iter_jsonl` STOPS at the first bad line (append-only: a bad line is a
    torn tail); `read_jsonl_lenient` SKIPS and continues (a rewritten store must not let one damaged
    line hide everything after it). Picking the wrong one is a durable correctness bug."""
    import tempfile
    from pathlib import Path as _P

    from looplab.core.jsonlio import iter_jsonl, read_jsonl_lenient

    with tempfile.TemporaryDirectory() as d:
        path = _P(d) / "s.jsonl"
        path.write_bytes(b'{"a":1}\nnot json\n{"a":2}\n')
        assert [r["a"] for r in iter_jsonl(path)] == [1], "iter_jsonl must STOP at the damage"
        assert [r["a"] for r in read_jsonl_lenient(path)] == [1, 2], "lenient must SKIP and continue"
