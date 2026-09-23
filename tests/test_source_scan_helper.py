"""One walk of `looplab/` for the guard tests that scan source (doc 25 XP-10).

The registries themselves stay separate — the per-seam extraction heuristics ARE the value, and a
uniform runtime registry would add abstraction without any. What was duplicated was the walk, and the
copies had already diverged on the one detail that decides whether a scan runs at all: decoding.
"""
from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

from _source_scan import PKG, code_text, iter_sources, iter_trees, scan

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


def test_iter_trees_sees_an_edit_between_two_calls(tmp_path):
    """The parse is memoized per file (review 2026-09-22, TST-04), so the one way it can go wrong is
    to answer with a tree the file no longer has. The memo revalidates on `(st_mtime_ns, st_size)`
    at every call: an edit is seen, and an untouched file is served the SAME tree object."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "b.py").write_text("y = 2\n", encoding="utf-8")
    first = dict(iter_trees(pkg))
    assert dict(iter_trees(pkg))[pkg / "b.py"] is first[pkg / "b.py"], "an unchanged file re-parsed"

    (pkg / "a.py").write_text("def renamed():\n    return 1\n", encoding="utf-8")
    second = dict(iter_trees(pkg))
    names = [n.name for n in ast.walk(second[pkg / "a.py"]) if isinstance(n, ast.FunctionDef)]
    assert names == ["renamed"], "the memo served a tree the file no longer has"
    assert second[pkg / "b.py"] is first[pkg / "b.py"]


# --- TST-05: `code_text`, the text a positive pin may be satisfied by ----------------------------

_MODULE = textwrap.dedent('''\
    """Module docstring: MODULE_PROSE."""
    import os

    PROMPT = "say CODE_PROMPT to the model"   # TRAILING_PROSE after code


    class Box:
        """CLASS_PROSE in the class docstring."""

        # LINE_PROSE on a comment-only line
        def run(self):
            """METHOD_PROSE."""
            "BARE_PROSE: a string used as a block comment"
            ("PAREN_PROSE"
             "continued")
            f"FSTRING_CODE {os.sep}"
            call("# HASH_IN_CODE is inside a string, not a comment")
            pass  # self._record_eval_start_boundary(chosen)
            return (
                "ARGUMENT_CODE"
            )
''')


@pytest.mark.parametrize("literal, is_code", [
    ("MODULE_PROSE", False),                      # module docstring
    ("CLASS_PROSE", False),                       # class docstring
    ("METHOD_PROSE", False),                      # function docstring
    ("BARE_PROSE", False),                        # a bare string statement mid-body
    ("PAREN_PROSE", False),                       # implicit concatenation in parentheses
    ("continued", False),
    ("TRAILING_PROSE", False),                    # a comment after code
    ("LINE_PROSE", False),                        # a comment-only line
    ("self._record_eval_start_boundary(chosen)", False),   # CLAUDE.md's model mutation
    ("CODE_PROMPT", True),                        # a string literal in code IS code
    ('PROMPT = "say', True),
    ("# HASH_IN_CODE", True),                     # a `#` inside a string is not a comment
    ("FSTRING_CODE", True),                       # an f-string statement is not a docstring
    ("ARGUMENT_CODE", True),                      # a string on its own line INSIDE a call
    ("def run(self):", True),
    ("pass", True),
])
def test_code_text_keeps_code_and_drops_prose(literal, is_code):
    """The truth table the pin budget stands on. Each row is a literal a test could pin; the budget
    calls a pin prose-held exactly when the literal is in the source but not in `code_text` of it."""
    assert literal in _MODULE
    assert (literal in code_text(_MODULE)) is is_code


def test_code_text_keeps_the_line_structure_so_a_slice_is_the_code_of_that_slice():
    """`tests/test_pin_budget.py` tokenizes a FILE once and slices a function's lines out of it; that
    is only sound if every line of the result is the same line of the source minus its prose."""
    code = code_text(_MODULE)
    assert code.count("\n") == _MODULE.count("\n")
    for source_line, code_line in zip(_MODULE.split("\n"), code.split("\n")):
        assert source_line.startswith(code_line.rstrip()) or not code_line.strip(), (
            source_line, code_line)


def test_code_text_does_not_dedent_a_method_as_inspect_returns_it():
    """A pin that spells indentation (`"x = 1\\n        return x"`) must see the same indentation in
    the code reading as in the full one, or it reads as prose-held when it is not."""
    method = '    def f(self):\n        """Doc."""\n        x = 1  # why\n        return x\n'
    assert code_text(method) == "    def f(self):\n        \n        x = 1  \n        return x\n"


def test_a_form_feed_in_a_docstring_does_not_shift_what_is_removed():
    """Rows are counted on `\\n` alone, as `tokenize` counts them; `str.splitlines` would also break
    on the `\\x0c` and every span after it would land one line off."""
    source = 'def f():\n    """page\x0cbreak"""\n    return 1  # PROSE\n'
    assert code_text(source) == "def f():\n    \n    return 1  \n"


def test_code_text_refuses_what_does_not_tokenize():
    with pytest.raises(ValueError, match="cannot separate code from prose"):
        code_text('x = """never closed\n')


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


# --- ES-04: pass-through knobs resolve at the assignment, not via a local ------------------------

def test_no_knob_is_resolved_into_a_local_only_to_be_assigned_unchanged():
    """`Engine.__init__` resolved every option into a local and then assigned it to `self`, so a new
    pass-through knob cost three edits (doc 25 ES-04). The ones with real normalization keep their
    local — that local IS the normalization. What must not come back is the BARE relay:
    `x = _opt("x")` read exactly once, only to become `self.x = x`."""
    import ast
    import inspect

    from looplab.engine.orchestrator import Engine

    init = ast.parse(inspect.getsource(Engine.__init__).lstrip()).body[0]
    opt = {}
    for st in init.body:
        if (isinstance(st, ast.Assign) and len(st.targets) == 1
                and isinstance(st.targets[0], ast.Name) and isinstance(st.value, ast.Call)
                and getattr(st.value.func, "id", None) == "_opt" and len(st.value.args) == 1
                and isinstance(st.value.args[0], ast.Constant)):
            opt[st.targets[0].id] = st.value.args[0].value
    uses = {}
    for n in ast.walk(init):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in opt:
            uses[n.id] = uses.get(n.id, 0) + 1
    relays = []
    for st in ast.walk(init):
        if (isinstance(st, ast.Assign) and len(st.targets) == 1
                and isinstance(st.targets[0], ast.Attribute)
                and getattr(st.targets[0].value, "id", None) == "self"
                and isinstance(st.value, ast.Name) and st.value.id in opt):
            name = st.value.id
            if st.targets[0].attr == name == opt[name] and uses.get(name, 0) == 1:
                relays.append(name)
    assert not relays, (
        f"these knobs are resolved into a local and assigned unchanged: {relays}. Write "
        '`self.x = _opt("x")` at the assignment instead — the local buys nothing and costs an edit.')
