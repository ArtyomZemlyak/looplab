"""Guard: the inventory row count is bounded in TIME, and a store that is merely LARGE says so.

The counter runs on the synchronous prompt-assembly path 2-3x per prompt, over stores that grow
monotonically across runs, so an unbounded walk taxes every future prompt of every future run. The
close is a ceiling above which the hook answers the UNKNOWN its vocabulary already has -- and the
two callers' reason strings have to stop calling a large store an unreadable one, because the two
send an operator to completely different places.

Driven with an ACCOUNTANT over the bytes actually read: a refusal that still walked the file would
satisfy every assertion about the exception while costing exactly what the ceiling exists to save.
"""
from __future__ import annotations

import builtins
import types

import pytest

from looplab.tools import _base
from looplab.tools._base import RowCountTooLarge, jsonl_row_count


class _CountingFile:
    """A file proxy that counts the bytes the counter actually pulls off the store."""

    def __init__(self, handle):
        self._handle = handle
        self.bytes_read = 0

    def read(self, size=-1):
        chunk = self._handle.read(size)
        self.bytes_read += len(chunk)
        return chunk

    def fileno(self):
        return self._handle.fileno()

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)


def _accountant(monkeypatch, path):
    """Patch the BUILTIN open (what `jsonl_row_count` uses) and return the byte counter."""
    real_open = builtins.open
    seen: list[_CountingFile] = []

    def _open(target, *a, **k):
        handle = real_open(target, *a, **k)
        if str(target) == str(path):
            proxy = _CountingFile(handle)
            seen.append(proxy)
            return proxy
        return handle

    monkeypatch.setattr(builtins, "open", _open)
    return seen


def _store(path, rows: int):
    with open(path, "wb") as handle:
        for i in range(rows):
            handle.write(b'{"lesson":"a fairly typical durable row of text","i":%d}\n' % i)
    return path


def test_a_store_over_the_ceiling_is_UNKNOWN_and_is_never_walked(tmp_path, monkeypatch):
    """THE PROPERTY: the refusal costs the prompt path nothing at all, not one chunk."""
    path = _store(tmp_path / "big.jsonl", 20_000)
    monkeypatch.setattr(_base, "_ROW_COUNT_CEILING", 64 * 1024)
    assert path.stat().st_size > 64 * 1024, "premise: the store must exceed the patched ceiling"

    seen = _accountant(monkeypatch, path)
    with pytest.raises(RowCountTooLarge) as caught:
        jsonl_row_count(path)

    assert seen and seen[0].bytes_read == 0, (
        "the ceiling refusal walked the store anyway — it is a stat, not a count")
    assert caught.value.size == path.stat().st_size and caught.value.ceiling == 64 * 1024
    assert isinstance(caught.value, OSError), (
        "a caller that has never heard of this type must still fail closed into UNKNOWN")


def test_a_store_under_the_ceiling_is_still_counted_EXACTLY(tmp_path, monkeypatch):
    """The ceiling costs the answer, so it is spent late: below it nothing changes."""
    path = _store(tmp_path / "small.jsonl", 5_000)
    monkeypatch.setattr(_base, "_ROW_COUNT_CEILING", path.stat().st_size + 1)
    assert jsonl_row_count(path) == 5_000


def test_a_store_that_GROWS_past_the_ceiling_mid_walk_still_stops(tmp_path, monkeypatch):
    """The stat is one instant. Every store this counts is append-only and live, and a non-regular
    source reports `st_size` 0 — so the running total, not the stat, is what bounds the work.

    MUTATION: drop the in-loop check -> this walks the whole 1 MB with a lying `st_size` and the
    only bound left is the one an appending writer can defeat.
    """
    path = _store(tmp_path / "growing.jsonl", 20_000)
    monkeypatch.setattr(_base, "os", types.SimpleNamespace(
        fstat=lambda fd: types.SimpleNamespace(st_size=0)))   # the fifo / just-appended case
    monkeypatch.setattr(_base, "_ROW_COUNT_CHUNK", 4096)
    monkeypatch.setattr(_base, "_ROW_COUNT_CEILING", 16 * 1024)

    seen = _accountant(monkeypatch, path)
    with pytest.raises(RowCountTooLarge):
        jsonl_row_count(path)
    assert seen[0].bytes_read <= 16 * 1024 + 4096, (
        f"walked {seen[0].bytes_read} bytes past the ceiling: the stat was trusted, not the walk")


def test_the_cross_run_row_says_TOO_LARGE_and_not_UNREADABLE(tmp_path, monkeypatch):
    """Both are UNKNOWN; only one of them sends the operator to the right place."""
    from looplab.tools.cross_run_tools import CrossRunTools

    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "lessons.jsonl").write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    monkeypatch.setattr(_base, "_ROW_COUNT_CEILING", 4)

    rows = CrossRunTools(memory, audience="portfolio").inventory()
    assert isinstance(rows["cross_run_search"], str), "a large store must never publish as 0"
    assert "too large" in rows["cross_run_search"]
    assert "unreadable" not in rows["cross_run_search"], (
        "'unreadable store' sends an operator hunting a corrupt file that does not exist")


def test_the_knowledge_row_degrades_ONLY_the_tool_that_reads_the_case_store(tmp_path, monkeypatch):
    """`kb_search` reads the cases; the other three read notes only, and those were counted."""
    from looplab.tools.knowledge_tools import KnowledgeTools

    notes = tmp_path / "kb"
    notes.mkdir()
    (notes / "a.md").write_text("# a\nnote body\n", encoding="utf-8")
    cases = tmp_path / "cases.jsonl"
    cases.write_text('{"task_id":"t"}\n', encoding="utf-8")

    tools = KnowledgeTools(str(notes), str(cases))          # built while the ceiling is normal
    monkeypatch.setattr(_base, "_ROW_COUNT_CEILING", 4)
    rows = tools.inventory()

    assert isinstance(rows["kb_search"], str) and "too large" in rows["kb_search"]
    assert rows["list_notes"] == 1 and rows["grep"] == 1 and rows["read_note"] == 1, (
        "the notes were read successfully — publishing UNKNOWN about them withholds three tools "
        "over a source that was never in question")
