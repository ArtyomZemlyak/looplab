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
import os
import re
import shutil
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


# A THROWAWAY COPY OF THE TREE, never the real one — the house rule (CLAUDE.md: "re-verify by MUTATING a
# throwaway copy of the tree, never the real one"), and this audit used to break it. It rewrote the
# REAL `benchmarks/sweep_claims.py` for the length of a nested pytest, so every other process
# importing that module in that window — a sibling shard of a local `--splits 4` run in the same
# checkout — got the FORCED check: measured 2026-09-22, three of these cases and
# `test_the_snapshot_refusal_check_has_teeth` red in a four-shard run, all green alone. One copy per
# process (the cases in it run sequentially), the nested pytest pointed at the copy.
_COPIED = ("looplab", "tests", "benchmarks", "examples", "docs")
_COPIED_FILES = ("conftest.py", "pyproject.toml", "README.md", "CLAUDE.md", "mkdocs.yml")


@pytest.fixture(scope="module")
def throwaway_tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("alarm-tree")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".ipynb_checkpoints")
    for part in _COPIED:
        if (REPO / part).is_dir():
            shutil.copytree(REPO / part, root / part, ignore=ignore)
    for name in _COPIED_FILES:
        if (REPO / name).is_file():
            shutil.copy2(REPO / name, root / name)
    return root


def _pytest_in(tree: Path, tests: list, basetemp: Path):
    env = {**os.environ, "PYTHONPATH": str(tree) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run([sys.executable, "-m", "pytest", *tests, "-q", "--no-header",
                           "-p", "no:warnings", "-p", "no:cacheprovider", "-x",
                           f"--basetemp={basetemp}"],
                          capture_output=True, text=True, timeout=900, cwd=str(tree), env=env)


@pytest.mark.corpus
def test_every_named_test_passes_in_the_throwaway_tree(throwaway_tree, tmp_path):
    """THE CONTROL ARM, once for the union: a named test that cannot even run in the copy (a path it
    reads that the copy lacks) would redden under ANY forced check and make the audit vacuous."""
    named = sorted({Path(p).name for check in _checks() for p in _tests_naming(check)})
    got = _pytest_in(throwaway_tree, [str(throwaway_tree / "tests" / n) for n in named],
                     tmp_path / "bt")
    assert got.returncode == 0, got.stdout[-3000:]


@pytest.mark.corpus
@pytest.mark.parametrize("check", _checks())
def test_forcing_a_check_to_pass_reddens_the_test_that_names_it(check, throwaway_tree, tmp_path):
    named = _tests_naming(check)
    assert named, f"{check} is named by no test at all -- its alarm is wired to nothing"
    tool = throwaway_tree / "benchmarks" / TOOL.name
    src = tool.read_text(encoding="utf-8")
    lines = src.split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith(f"def {check}("))
    forced = list(lines)
    forced[at] = forced[at] + "\n    return True, 'forced'"
    try:
        tool.write_text("\n".join(forced), encoding="utf-8")
        got = _pytest_in(throwaway_tree, [str(throwaway_tree / "tests" / Path(p).name)
                                          for p in named], tmp_path / "bt")
        assert got.returncode != 0, (
            f"{check} can be forced to always pass and every test naming it stays green: "
            f"{[Path(p).name for p in named]}")
    finally:
        tool.write_text(src, encoding="utf-8")
