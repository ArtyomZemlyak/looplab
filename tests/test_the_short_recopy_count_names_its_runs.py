"""§343. «3 re-copied SHORT of its source» — счёт без имён, по которому нельзя действовать.

Строка стояла в КАЖДОМ цикле снимка часами. Чтобы узнать, что это три append-only лога ОДНОЙ живой
пробы — `events.jsonl`, `spans.jsonl` и `.spans-append.jsonl`, растущие между `cp -ru` и обходом,
ровно как и должны, — понадобился `find -newermt` против последнего снимка. Предыдущий цикл,
оставивший позади частично скопированный прогон, читается ОТТУДА ЖЕ и точно так же, а вот он уже
тревога. Имя прогона — первый элемент пути под архивируемым деревом — отличает их за одну строку.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[1] / "benchmarks" / "snapshot.sh"


def _archive_tree(src: Path, arch: Path) -> subprocess.CompletedProcess:
    """The real `archive_tree` out of snapshot.sh, echoing the counters it sets."""
    fn = subprocess.run(["sed", "-n", "/^archive_tree() {/,/^}/p", str(SNAPSHOT)],
                        capture_output=True, text=True, timeout=120).stdout
    assert "cp -ru" in fn, "could not extract archive_tree from snapshot.sh"
    return subprocess.run(
        ["bash", "-c", f"set -u\n{fn}\narchive_tree {src} {arch}\n"
                       'echo "REPAIRED=$ARCH_REPAIRED IN=[$ARCH_REPAIRED_IN]"'],
        capture_output=True, text=True, timeout=600)


def _probe(root: Path, probe: str, lines: int) -> Path:
    """A file where the bench really keeps one: `<probes>/<probe>/runs/<task>/run/events.jsonl`."""
    p = root / probe / "runs" / "discrete_log" / "run" / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join('{"i": %d}\n' % i for i in range(lines)), encoding="utf-8")
    return p


def test_a_short_archive_copy_names_the_run_it_was_in(tmp_path):
    """`cp -ru` пропускает файл, чья копия НОВЕЕ источника, даже если она короче — та самая ловушка
    `-u`, ради которой счётчик и существует. Здесь она построена руками, и имя прогона должно
    попасть в `ARCH_REPAIRED_IN`."""
    src, arch = tmp_path / "model-probes", tmp_path / "arch"
    source = _probe(src, "remDL13", 400)
    dest = arch / "model-probes" / "remDL13" / "runs" / "discrete_log" / "run" / "events.jsonl"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"i": 0}\n', encoding="utf-8")          # короче
    os.utime(source, (1_700_000_000, 1_700_000_000))          # и НОВЕЕ источника
    os.utime(dest, (1_800_000_000, 1_800_000_000))

    r = _archive_tree(src, arch)
    assert "REPAIRED=1" in r.stdout, r.stdout + r.stderr
    assert "IN=[remDL13]" in r.stdout, f"прогон не назван: {r.stdout}"
    assert dest.read_text().count("\n") == 400, "починка не догнала источник"


def test_two_short_files_in_one_run_name_it_once(tmp_path):
    """Три лога одной живой пробы — это ОДНА проба, а не три тревоги."""
    src, arch = tmp_path / "model-probes", tmp_path / "arch"
    run = src / "remDL13" / "runs" / "discrete_log" / "run"
    run.mkdir(parents=True)
    for name in ("events.jsonl", "spans.jsonl"):
        (run / name).write_text("x\n" * 100, encoding="utf-8")
        d = arch / "model-probes" / "remDL13" / "runs" / "discrete_log" / "run" / name
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text("x\n", encoding="utf-8")
        os.utime(run / name, (1_700_000_000, 1_700_000_000))
        os.utime(d, (1_800_000_000, 1_800_000_000))
    r = _archive_tree(src, arch)
    assert "REPAIRED=2" in r.stdout, r.stdout
    assert "IN=[remDL13]" in r.stdout, f"имя повторено или потеряно: {r.stdout}"


def test_two_different_runs_are_both_named(tmp_path):
    src, arch = tmp_path / "model-probes", tmp_path / "arch"
    for probe in ("remDL13", "accEE"):
        s = _probe(src, probe, 100)
        d = arch / "model-probes" / probe / "runs" / "discrete_log" / "run" / "events.jsonl"
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text("x\n", encoding="utf-8")
        os.utime(s, (1_700_000_000, 1_700_000_000))
        os.utime(d, (1_800_000_000, 1_800_000_000))
    r = _archive_tree(src, arch)
    assert "REPAIRED=2" in r.stdout, r.stdout
    assert "remDL13" in r.stdout and "accEE" in r.stdout, r.stdout


def test_a_clean_archive_names_nothing(tmp_path):
    src, arch = tmp_path / "model-probes", tmp_path / "arch"
    _probe(src, "remDL13", 100)
    r = _archive_tree(src, arch)
    assert "REPAIRED=0 IN=[]" in r.stdout, r.stdout


def test_the_line_the_operator_reads_carries_the_names_and_the_benign_case():
    """Счётчик бесполезен, если имена не доходят до строки. И у строки должен быть безобидный
    случай, названный прямо: живой прогон растёт между копией и обходом."""
    src = SNAPSHOT.read_text(encoding="utf-8")
    marker = 'R=", $ARCH_REPAIRED re-copied SHORT of its source'
    at = src.find(marker)
    assert at >= 0, "строка отчёта не найдена"
    after = src[at:at + 260]
    assert "ARCH_REPAIRED_IN" in after, f"имена не доходят до строки: {after}"
    assert "LIVE grows between the copy and the walk" in after, after
