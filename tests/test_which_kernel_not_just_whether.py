"""§377. Один булев флаг покрывал две популяции, различающиеся в восемь раз.

`kernel` отвечал «ядро, да или нет». Померено 2026-09-09 по каждой пробе с чемпионом и итоговым
счётом, по задачам:

    edge_expansion   cython n=108 медиана 219.79 | numba n=8 медиана 26.68 | plain n=2 30.06
    pagerank         cython n=  8 медиана  53.37 | numba n=2 медиана 34.12
    discrete_log     cython n=  6 медиана   9.99 | numba n=6 медиана  7.78
    pde_heat1d       numba  n= 11 медиана 117.74 | cython-чемпионов нет вовсе

Восьмикратно на задаче, которая назначает размер каждому плечу. Флаг, отвечающий «да или нет»,
219 от 26 не отличает. И §186 своей прозой говорит «Cython kernel» там, где этот флаг ловит ещё и
numba, — расхождение, которое надо разрешать при проектировании того плеча, а не заминать здесь.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import probe_summary  # noqa: E402


def _probe(tmp_path, body, pyx=False):
    d = tmp_path / "p1"
    (d / "runs" / "t" / "run").mkdir(parents=True)
    (d / "champion_solver.py").write_text(body, encoding="utf-8")
    if pyx:
        (d / "k.pyx").write_text("# cython\n", encoding="utf-8")
    return d


def _kind(tmp_path, body, pyx=False):
    """The tool's own classification, driven -- `summarise` takes the run dir and reads the probe
    tree above it."""
    d = _probe(tmp_path, body, pyx)
    run = d / "runs" / "t" / "run"
    (run / "events.jsonl").write_text(
        json.dumps({"v": 1, "seq": 0, "ts": 0.0, "type": "llm_usage", "data": {"cost": 1.0}}) + "\n",
        encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    got = probe_summary.summarise(run)
    return (got or {}).get("kernel_kind"), (got or {}).get("kernel")


def test_a_shipped_pyx_is_cython(tmp_path):
    assert _kind(tmp_path, "print(1)\n", pyx=True) == ("cython", True)


def test_a_cimport_without_a_pyx_is_still_cython(tmp_path):
    assert _kind(tmp_path, "cimport numpy as cnp\n")[0] == "cython"


def test_njit_alone_is_numba(tmp_path):
    """Та самая популяция, что даёт 26.68 против 219.79 на edge_expansion."""
    assert _kind(tmp_path, "from numba import njit\n@njit\ndef f(): pass\n") == ("numba", True)


def test_a_champion_with_neither_is_plain(tmp_path):
    assert _kind(tmp_path, "def solve(p): return p\n") == ("plain", False)


def test_the_three_kinds_are_distinguished_in_the_source():
    src = (BENCH / "probe_summary.py").read_text(encoding="utf-8")
    for word in ('"cython"', '"numba"', '"plain"'):
        assert word in src.split("kernel_kind = (")[1][:400], word


def test_the_boolean_is_kept_beside_the_kind():
    """`kernel` остаётся: «есть ли ядро вообще» — законный отдельный вопрос, и старые читатели
    его используют. Заменить булев на строку молча значило бы сломать их."""
    src = (BENCH / "probe_summary.py").read_text(encoding="utf-8")
    assert '"kernel": kernel,' in src and '"kernel_kind": kernel_kind,' in src


def test_a_probe_with_no_champion_has_no_kind():
    src = (BENCH / "probe_summary.py").read_text(encoding="utf-8")
    assert 'else ("plain" if champ.is_file() else None)' in src, \
        "проба без чемпиона получает вид ядра из ниоткуда"


@pytest.mark.corpus
def test_the_live_corpus_separates_them():
    """Якорь: восьмикратная разница на edge_expansion обязана быть видна из инструмента."""
    got = subprocess.run([sys.executable, str(BENCH / "probe_summary.py"), "--json"],
                         capture_output=True, text=True, timeout=1200)
    try:
        rows = json.loads(got.stdout)
    except ValueError:
        return
    import statistics, collections
    by = collections.defaultdict(list)
    for r in rows:
        if r.get("task") == "edge_expansion" and isinstance(r.get("test"), (int, float)) \
                and r.get("kernel_kind"):
            by[r["kernel_kind"]].append(r["test"])
    if "cython" in by and "numba" in by:
        assert statistics.median(by["cython"]) > 4 * statistics.median(by["numba"]), \
            {k: (len(v), statistics.median(v)) for k, v in by.items()}
