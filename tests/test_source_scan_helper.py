"""One walk of `looplab/` for the guard tests that scan source (doc 25 XP-10).

The registries themselves stay separate — the per-seam extraction heuristics ARE the value, and a
uniform runtime registry would add abstraction without any. What was duplicated was the walk, and the
copies had already diverged on the one detail that decides whether a scan runs at all: decoding.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from _source_scan import PKG, iter_sources, iter_trees, scan

TESTS = Path(__file__).resolve().parent


def test_no_guard_test_re_derives_the_walk():
    needle = "rglob(" + '"*.py")'          # split so this file is not its own offender
    offenders = [path.name for path in sorted(TESTS.glob("test_*.py"))
                 if needle in path.read_text(encoding="utf-8-sig", errors="replace")]
    assert offenders == [], (
        f"{offenders} walk the package themselves; use `_source_scan` so the decoding stays uniform")


def test_the_walk_covers_the_package_and_is_ordered():
    paths = [path for path, _text in iter_sources()]
    assert len(paths) > 100, "the walk found suspiciously few sources"
    assert paths == sorted(paths), (
        "an unsorted walk reports the same offenders in a different order per filesystem, which "
        "reads as a flapping test")
    assert PKG / "core" / "models.py" in paths
    assert all(path.suffix == ".py" for path in paths)


def test_a_bom_is_stripped_so_ast_parse_accepts_the_text(tmp_path):
    """The divergence that mattered. Four decodings were in use across the copies, and three tests
    still `ast.parse`d a plain-`utf-8` read — which raises `SyntaxError: invalid non-printable
    character U+FEFF` on a BOM'd file. That is not a missed finding, it is a scanner that dies on an
    unrelated file, and the repo already contains at least one BOM'd source."""
    (tmp_path / "bom.py").write_bytes(b"\xef\xbb\xbfimport os\n")
    with pytest.raises(SyntaxError):
        ast.parse((tmp_path / "bom.py").read_text(encoding="utf-8"))
    (path, tree), = list(iter_trees(tmp_path))
    assert path.name == "bom.py" and isinstance(tree, ast.Module)


def test_an_undecodable_byte_does_not_stop_the_scan(tmp_path):
    """The other half of the same choice: a regex scan must keep going past a stray byte rather than
    raise, or one bad file hides every finding in the rest of the package."""
    (tmp_path / "bad.py").write_bytes(b"x = '\xff\xfe'\n")
    (tmp_path / "good.py").write_text("marker = 1\n", encoding="utf-8")
    found = scan(r"(marker)", pkg=tmp_path)
    assert found == {"marker": {"good.py"}}


def test_scan_reports_where_each_name_was_found(tmp_path):
    """The file set is the point: a guard test's failure message has to say WHERE, or whoever reads
    it re-runs the grep by hand."""
    (tmp_path / "a.py").write_text('render(prompts, "alpha")\n', encoding="utf-8")
    (tmp_path / "b.py").write_text('render(prompts, "alpha")\nrender(p, "beta")\n', encoding="utf-8")
    found = scan(re.compile(r'render\(\s*[\w.]+\s*,\s*"([a-z_]+)"'), pkg=tmp_path)
    assert found == {"alpha": {"a.py", "b.py"}, "beta": {"b.py"}}


def test_iter_trees_names_the_file_in_a_syntax_error(tmp_path):
    """A scan over a hundred files must say which one failed."""
    (tmp_path / "broken.py").write_text("def (\n", encoding="utf-8")
    with pytest.raises(SyntaxError) as info:
        list(iter_trees(tmp_path))
    assert "broken.py" in str(info.value.filename)


def test_a_subtree_can_be_scanned_on_its_own():
    """Several guards are scoped to one package — `core` imports nothing above itself, `tools` names
    `serve` in exactly one place — so the walk has to take a root."""
    core = {path for path, _ in iter_sources(PKG / "core")}
    assert core and core < {path for path, _ in iter_sources()}
    assert all(path.is_relative_to(PKG / "core") for path in core)


def test_the_named_file_readers_are_deliberately_left_alone():
    """`test_signal_delivery` reads a handful of NAMED files rather than walking the package. That is
    a different and correct shape — its point is that one specific wiring line exists in one specific
    file — so folding it in would have made the test say less."""
    source = (TESTS / "test_signal_delivery.py").read_text(encoding="utf-8-sig")
    assert "route.call_sites" in source
    assert "_source_scan" not in source


# --- XP-11: one canonical Engine construction for the suite --------------------------------------

def test_the_engine_factory_passes_overrides_straight_through():
    """`make_engine` must never become a second, LAGGING spelling of Engine's keyword API — which is
    exactly what 29 private `_engine` factories already were (doc 25 XP-11). Anything it does not
    name itself goes to `Engine` untouched, so a new knob needs no change here."""
    import inspect

    from factories import make_engine

    params = inspect.signature(make_engine).parameters
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), (
        "make_engine stopped forwarding **overrides; it now hides part of Engine's API")
    named = {n for n, p in params.items() if p.kind is not inspect.Parameter.VAR_KEYWORD}
    assert named == {"run_dir", "task_file", "task", "researcher", "developer", "sandbox",
                     "policy", "n_seeds", "max_nodes"}, (
        f"make_engine grew/lost a named parameter ({named}); each one it names is a knob a caller "
        "can no longer express in Engine's own vocabulary")


def test_the_engine_factory_builds_a_real_engine(tmp_path):
    from looplab.engine.orchestrator import Engine

    from factories import make_engine

    engine = make_engine(tmp_path / "run", n_seeds=1, max_nodes=1)
    assert isinstance(engine, Engine)
    assert engine.task is not None and engine.researcher is not None
