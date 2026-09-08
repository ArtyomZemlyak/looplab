"""Guard: the inventory row count is CONSTANT-memory and keeps the `\\n`-only record rule.

Driven, not pinned: the counter is run over a real store and its peak allocation is measured.
"""
import tracemalloc

from looplab.tools._base import jsonl_row_count


def _store(path, rows: int):
    with open(path, "wb") as handle:
        for i in range(rows):
            handle.write(b'{"lesson":"a fairly typical durable row of text","i":%d}\n' % i)
    return path


def test_a_bare_cr_is_not_a_record_boundary_and_a_final_row_still_counts(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'{"a":1}\n{"b":"x\ry"}\n\n   \n{"c":3}')
    assert jsonl_row_count(path) == 3, (
        "only b'\\n' ends a record (core/jsonlio.py), blank rows do not count, and an "
        "unterminated final row does")


def _peak(path):
    tracemalloc.start()
    try:
        count = jsonl_row_count(path)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return count, peak


def test_the_cost_of_counting_does_not_grow_with_the_store(tmp_path):
    """The property is CONSTANT memory, so it is read off two sizes rather than one threshold."""
    small, big = _store(tmp_path / "a", 100_000), _store(tmp_path / "b", 400_000)
    assert big.stat().st_size > 4 * (1 << 20), "premise: the store must exceed the read window"
    (n_small, peak_small), (n_big, peak_big) = _peak(small), _peak(big)
    assert (n_small, n_big) == (100_000, 400_000)
    assert peak_big <= peak_small * 1.25, (
        f"peak went {peak_small} -> {peak_big} for a 4x store: the counter is holding the file "
        "(and a list of its rows) in memory, on the synchronous prompt-assembly path")
    assert peak_big < big.stat().st_size // 2, "and it must not hold the whole file either"
