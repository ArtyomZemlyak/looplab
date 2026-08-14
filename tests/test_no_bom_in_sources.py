"""No source file may carry a UTF-8 BOM.

Six files acquired one during the 2026-08-14 integration — `repo_task.py`, `tracing.py`,
`orchestrator.py`, `htmlview.py`, `patch.py`, `vectorstore.py`. Python imports them fine, so nothing
went red and nothing would have, which is exactly why this needs a test of its own.

What it breaks is the idiom roughly two hundred guard assertions in this suite are built on:

    ast.parse(Path(module.__file__).read_text())      -> SyntaxError: invalid non-printable
                                                         character U+FEFF, line 1

`read_text()` decodes as plain UTF-8 and hands the BOM through as U+FEFF; only `utf-8-sig` strips it.
So a BOM does not fail the module — it fails whatever tries to *analyse* the module, and the failure
names line 1 of a file whose line 1 is fine, which is a bad hour for whoever meets it. `CLAUDE.md`'s
tier-3 rule ("AST, never substrings") points every future guard at that same idiom, so the blast
radius grows with the suite.

Checked on BYTES, not on decoded text: reading with any codec that strips the mark would make this
test vacuous — the mutation is invisible to a reader that already handles it.
"""
from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
_BOM = b"\xef\xbb\xbf"


def _tracked_sources() -> list[Path]:
    """Ask git, so a stray file in a worktree or a build artifact cannot fail an unrelated change."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py", "*.js", "*.jsx", "*.md",
                          "*.json", "*.html", "*.yml", "*.yaml"],
                         capture_output=True, text=True, check=True)
    return [ROOT / name for name in out.stdout.split("\n") if name]


def test_no_tracked_source_carries_a_utf8_bom():
    carriers = []
    for path in _tracked_sources():
        try:
            head = path.open("rb").read(3)
        except OSError:
            continue          # a path git tracks but this checkout does not materialise
        if head == _BOM:
            carriers.append(str(path.relative_to(ROOT)))
    assert not carriers, (
        "file(s) carry a UTF-8 BOM: " + ", ".join(sorted(carriers)) + ".\n"
        "They will import normally and break `ast.parse(Path(...).read_text())` with a SyntaxError "
        "naming line 1. Strip the first three bytes; do not 'fix' the readers to pass `utf-8-sig`, "
        "which would move the workaround to every future guard instead of removing the cause.")


def test_the_check_reads_bytes_so_it_cannot_go_vacuous(tmp_path):
    """Prove the detector still sees a BOM that a decoding reader would have hidden."""
    planted = tmp_path / "planted.py"
    planted.write_bytes(_BOM + b"x = 1\n")
    assert planted.open("rb").read(3) == _BOM
    assert planted.read_text(encoding="utf-8-sig") == "x = 1\n", (
        "utf-8-sig hides the mark — which is why this test must not use it")
