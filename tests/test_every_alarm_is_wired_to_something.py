"""§397. Проверка, чью тревогу никто не дёргает, — это не проверка, а мнение.

Двадцать одна проверка списка живёт в `sweep_claims.py`, и у каждой есть ветка «не держится». Что
эту ветку кто-то ДЕЙСТВИТЕЛЬНО проверяет, никто не мерил. Способ прямой: заставить проверку
безусловно возвращать «держится» и посмотреть, покраснеет ли хоть один тест, который её называет.

Померено 2026-09-09: **все 21 краснеют**. Ни одной немой сигнализации. Это результат, а не
предположение, и он должен оставаться результатом по мере добавления проверок — поэтому аудит
приколочен здесь, а не записан в докс.

Не грепом: тест, УПОМИНАЮЩИЙ имя проверки, ничего не доказывает — этот промах в тетради встречается
четыре раза (§358, §391, §393, §395). Здесь проверка подменяется на месте и запускается настоящий
pytest по тем файлам, которые её называют.

Помечен `corpus`: он запускает pytest двадцать один раз и в обычный прогон соседей не входит.
"""
from __future__ import annotations

import glob
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "benchmarks" / "sweep_claims.py"
_DEF = re.compile(r"^def (check_\w+)\(", re.M)


def _checks() -> list:
    return _DEF.findall(TOOL.read_text(encoding="utf-8"))


def _tests_naming(check: str) -> list:
    out = []
    for path in sorted(glob.glob(str(REPO / "tests" / "*.py"))):
        if path.endswith(Path(__file__).name):
            continue
        if check in Path(path).read_text(encoding="utf-8", errors="replace"):
            out.append(path)
    return out


def test_there_are_checks_to_audit():
    """Не-вакуумность самого аудита: если разбор имён сломается, он «пройдёт» на пустом списке."""
    assert len(_checks()) >= 20, _checks()


@pytest.mark.corpus
@pytest.mark.parametrize("check", _checks())
def test_forcing_a_check_to_pass_reddens_the_test_that_names_it(check, tmp_path):
    named = _tests_naming(check)
    assert named, f"{check} is named by no test at all -- its alarm is wired to nothing"
    src = TOOL.read_text(encoding="utf-8")
    lines = src.split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith(f"def {check}("))
    forced = list(lines)
    forced[at] = forced[at] + "\n    return True, 'forced'"
    backup = tmp_path / "sweep_claims.py.orig"
    backup.write_text(src, encoding="utf-8")
    try:
        TOOL.write_text("\n".join(forced), encoding="utf-8")
        got = subprocess.run([sys.executable, "-m", "pytest", *named, "-q", "--no-header",
                              "-p", "no:warnings", "-x"],
                             capture_output=True, text=True, timeout=900, cwd=str(REPO))
        assert got.returncode != 0, (
            f"{check} can be forced to always pass and every test naming it stays green: "
            f"{[Path(p).name for p in named]}")
    finally:
        TOOL.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
