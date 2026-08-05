"""One walk of `looplab/`, for the guard tests that scan source (doc 25 XP-10).

Fifteen tests rglob the package and read every file. They exist because several seams are duck-typed
or registry-backed, so the only way to catch a rename is to look at the source — and that is worth
keeping. What was not worth keeping is fifteen copies of the walk, because the copies had already
diverged on the one detail that decides whether a scan WORKS: how a file is decoded.

Four spellings were in use — `utf-8`, `utf-8-sig`, and each with `errors="replace"`. The difference
is not cosmetic. At least one tracked file carries a BOM, and `ast.parse` on a plain-`utf-8` read of
it raises `SyntaxError: invalid non-printable character U+FEFF` — so a scanner written with the wrong
spelling does not miss a finding quietly, it dies on an unrelated file. Three tests still parsed with
plain `utf-8` and were one BOM away from that.

`utf-8-sig` with `errors="replace"` is the spelling that works for BOTH scan kinds: it strips a BOM
so `ast.parse` accepts the text, and it never raises on a stray byte so a regex scan keeps going.

Deliberately NOT here: `tests/test_signal_delivery.py` reads a handful of NAMED files rather than
walking the package, which is a different (and correct) shape — its point is that one specific
wiring line exists in one specific file.

Also here, and about the OTHER scan kind: `called_names`/`names_read`/`function_tree`, the
comment-proof replacement for a positive `"<literal>" in inspect.getsource(f)` pin. See the block
comment above them and CLAUDE.md's "Testing conventions".
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from pathlib import Path
from typing import Iterator

PKG = Path(__file__).resolve().parents[1] / "looplab"

# Directories that live INSIDE the package tree but are not the package. `.ipynb_checkpoints` is
# Jupyter's autosave sidecar: it is gitignored (`.gitignore`: `**/.ipynb_checkpoints/`) and untracked,
# so it holds a STALE copy of whatever a file looked like when someone last opened it in a notebook
# server. Scanning it makes every guard here report the PAST as a present violation — e.g.
# `serve/.ipynb_checkpoints/assistant-checkpoint.py` still carries an
# `from looplab.tools.knowledge_tools import _fn_spec` that the real `serve/assistant.py` dropped,
# which fails `test_cross_package_private_seams` on a tree whose production source is clean. That is
# not hypothetical here: LoopLab is developed on JupyterHub, where these dirs appear routinely (three
# exist under `looplab/` right now). A guard that fires on a gitignored autosave is unactionable —
# there is no source change that would make it pass.
EXCLUDED_DIRS = frozenset({".ipynb_checkpoints", "__pycache__"})


def iter_sources(pkg: Path = PKG) -> Iterator[tuple[Path, str]]:
    """Every `.py` under *pkg*, sorted, with its decoded text.

    Sorted so a failure message lists offenders in a stable order — an unsorted `rglob` reports the
    same set in a different order per filesystem, which reads as a flapping test.
    """
    for path in sorted(pkg.rglob("*.py")):
        if EXCLUDED_DIRS.intersection(path.parts):
            continue
        yield path, path.read_text(encoding="utf-8-sig", errors="replace")


def iter_trees(pkg: Path = PKG) -> Iterator[tuple[Path, ast.AST]]:
    """Every source under *pkg* parsed to an AST, with `filename` set so a SyntaxError names it."""
    for path, text in iter_sources(pkg):
        yield path, ast.parse(text, filename=str(path))


# ------------------------------------------------------------------ the comment-proof call pin
#
# A positive source pin — `assert "self._header_join()" in inspect.getsource(f)` — is one comment
# away from vacuous, because the cheapest mutation is "delete the code, leave a comment carrying the
# pinned literal":
#
#     pass  # self._header_join()
#
# The suite holds ~62 such pins. Full call EXPRESSIONS beat bare names (a bare `_header_join` also
# matches the word in prose) but are NOT comment-proof either: the repo's own model pin,
# `test_card_speculation_engine.py:1731-1733`, pins three call expressions AND their order, and
# `pass  # self._record_eval_start_boundary(chosen)` satisfies all three `source.index()` lookups in
# the right order while the boundary event is never written — which CLAUDE.md records costing 17
# builds / 5 discards -> 12 / 0.
#
# Comments are not AST nodes. `called_names`/`names_read` resolve real `ast.Call` / `ast.Name(Load)`
# nodes, so no amount of commented-out text can satisfy them. They are the FALLBACK, not the goal:
# where the property can be driven (a real fallback with a counting accountant, a real socket that
# goes silent), drive it — a call that happens is not the same claim as an effect that lands.


def function_tree(func) -> ast.AST:
    """*func*'s own source parsed to an AST (dedented, so a method body parses standalone)."""
    return ast.parse(textwrap.dedent(inspect.getsource(func)))


def _dotted(node: ast.AST) -> str:
    """`self._header_join` / `openai.APITimeoutError` / `sock.shutdown` for a call target."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, (ast.Call, ast.Subscript)):
        inner = _dotted(node.func if isinstance(node, ast.Call) else node.value)
        return f"{inner}()" if isinstance(node, ast.Call) and inner else inner
    return ""


def called_names(func) -> list[str]:
    """Dotted target of every REAL call in *func*, in source order (nested defs included).

    Source order, not `ast.walk`'s breadth-first order, because "A is called before B" is a property
    several of these pins actually assert and BFS reports it wrong for two calls at different depths.
    """
    calls = [node for node in ast.walk(function_tree(func)) if isinstance(node, ast.Call)]
    calls.sort(key=lambda node: (node.lineno, node.col_offset))
    return [_dotted(node.func) for node in calls]


def names_read(func) -> set[str]:
    """Every bare name LOADED in *func*. A name that survives only in a comment is not in here."""
    return {node.id for node in ast.walk(function_tree(func))
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def scan(pattern: re.Pattern | str, *, pkg: Path = PKG) -> dict[str, set[str]]:
    """``{captured name: {relative file, …}}`` for every match of *pattern*.

    The file set is the point, not the count: a guard test's failure message has to say WHERE an
    unregistered name is used, or the person reading it has to re-run the grep by hand.
    """
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    found: dict[str, set[str]] = {}
    for path, text in iter_sources(pkg):
        for name in compiled.findall(text):
            found.setdefault(name, set()).add(str(path.relative_to(pkg)))
    return found
