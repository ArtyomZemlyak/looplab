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
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterator

PKG = Path(__file__).resolve().parents[1] / "looplab"


def iter_sources(pkg: Path = PKG) -> Iterator[tuple[Path, str]]:
    """Every `.py` under *pkg*, sorted, with its decoded text.

    Sorted so a failure message lists offenders in a stable order — an unsorted `rglob` reports the
    same set in a different order per filesystem, which reads as a flapping test.
    """
    for path in sorted(pkg.rglob("*.py")):
        yield path, path.read_text(encoding="utf-8-sig", errors="replace")


def iter_trees(pkg: Path = PKG) -> Iterator[tuple[Path, ast.AST]]:
    """Every source under *pkg* parsed to an AST, with `filename` set so a SyntaxError names it."""
    for path, text in iter_sources(pkg):
        yield path, ast.parse(text, filename=str(path))


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
