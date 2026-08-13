"""Executable contracts for documentation surfaces that previously drifted silently.

These checks intentionally derive their inventories from the repository and the real Typer app.
They do not pin a hand-maintained command/doc list: adding a numbered document, command, relative
link or architecture-ledger status must update its human-facing authority in the same change.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_LOOPLAB_COMMAND = re.compile(r"\blooplab\s+([a-z][a-z0-9-]*)")
_FINDING = re.compile(
    r"^#### ([A-Z]{2}-\d{2})\b.* — \*\*"
    r"((?:PARTIALLY )?RESOLVED|DEFERRED|OPEN) \([^)]*\)\*\*$",
    re.M,
)


def _registered_command_names() -> set[str]:
    from looplab.cli import app

    names = set()
    for command in app.registered_commands:
        name = command.name or (command.callback.__name__.replace("_", "-")
                                if command.callback is not None else None)
        if name:
            names.add(name)
    return names


def test_index_mentions_every_numbered_document():
    index = (DOCS / "00-INDEX.md").read_text(encoding="utf-8")
    numbered = sorted(path for path in DOCS.glob("[0-9][0-9]-*.md")
                      if path.name != "00-INDEX.md")
    # Deliberately a literal, not a derived count: this is the tripwire that a document was added or
    # REMOVED without anyone touching the index, and the `missing` check below cannot see a deletion.
    # Moving it is part of adding a doc — 35 since doc 37 (the F3 worktree measurement). It went
    # 30 -> 34 in one step because docs 31/32/33 (the 2026-08-13 option tables) each landed WITHOUT
    # an index row, so the tripwire had been red on master since then and was read as pre-existing
    # noise rather than as the missing rows it was reporting. That is the failure mode this literal
    # exists to prevent, and it only works if a red one is fixed rather than inherited.
    assert len(numbered) == 35, "the derived numbered-document inventory changed"
    missing = [path.name for path in numbered if path.name not in index]
    assert not missing, f"numbered document(s) missing from docs/00-INDEX.md: {missing}"
    assert "| 09 |" in index and "No document was allocated" in index


def test_all_relative_markdown_links_resolve():
    files = [ROOT / "README.md", ROOT / "CLAUDE.md", *sorted(DOCS.rglob("*.md"))]
    broken = []
    for source in files:
        text = source.read_text(encoding="utf-8-sig")
        for raw_target in _MARKDOWN_LINK.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if (not target or target.startswith(("#", "http://", "https://", "mailto:"))):
                continue
            path_text = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not path_text:
                continue
            resolved = (source.parent / path_text).resolve()
            if not resolved.exists():
                broken.append(f"{source.relative_to(ROOT)} -> {target}")
    assert not broken, "broken relative Markdown link(s):\n  " + "\n  ".join(broken)


def test_readme_and_guide_name_only_real_cli_commands_and_cover_the_registry():
    sources = [ROOT / "README.md", *sorted((DOCS / "guide").glob("*.md"))]
    documented = set()
    for path in sources:
        documented.update(_LOOPLAB_COMMAND.findall(path.read_text(encoding="utf-8")))
    registered = _registered_command_names()
    assert len(registered) >= 40 and len(documented) >= 40, (
        "command inventories must remain non-vacuous")
    assert not documented - registered, (
        f"maintained docs name unknown command(s): {sorted(documented - registered)}")
    assert not registered - documented, (
        f"registered command(s) missing from README/user guide: {sorted(registered - documented)}")


def test_core_task_examples_load_and_optional_real_examples_are_explicit():
    from looplab.adapters.tasks import load_task

    examples = sorted((ROOT / "examples").glob("*task*.json"))
    assert len(examples) >= 15, "the example inventory unexpectedly became vacuous"
    for path in examples:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("kind") == "mlebench_real":
            assert "competition" in payload
            continue  # loading needs the documented optional mlebench package + prepared data
        task = load_task(path)
        assert task is not None, f"{path.name} did not produce a TaskAdapter"


def test_architecture_ledger_has_one_current_status_per_finding_and_exact_rollup():
    text = (DOCS / "25-architecture-modularity-review-2026-08-01.md").read_text(
        encoding="utf-8")
    all_ids = re.findall(r"^#### ([A-Z]{2}-\d{2})\b", text, re.M)
    statuses = _FINDING.findall(text)
    assert len(all_ids) == len(set(all_ids)) == 188
    assert len(statuses) == 188, "every finding heading must carry exactly one disposition"
    counts = Counter(status for _, status in statuses)
    assert counts == {
        "RESOLVED": 147,
        "PARTIALLY RESOLVED": 37,
        "DEFERRED": 2,
        "OPEN": 2,
    }
    summary = re.search(
        r"Status totals: (\d+) resolved, (\d+) partially resolved, (\d+) deferred, "
        r"(\d+) open \((\d+) total\)", text)
    assert summary and tuple(map(int, summary.groups())) == (147, 37, 2, 2, 188)


def test_current_user_docs_have_no_hidden_review_handoffs():
    current = [
        ROOT / "README.md",
        ROOT / "CLAUDE.md",
        DOCS / "00-INDEX.md",
        DOCS / "22-agent-parallelism-2026-07-19.md",
        DOCS / "23-hypothesis-card-kanban-2026-07-20.md",
        DOCS / "24-ui-phase3-validation.md",
        DOCS / "27-agent-system-mega-review-2026-08-09.md",
        DOCS / "28-deep-research-sota-roadmap-2026-08-10.md",
        DOCS / "29-operator-backlog-2026-08-11.md",
        *sorted((DOCS / "guide").glob("*.md")),
    ]
    stale = []
    for path in current:
        text = path.read_text(encoding="utf-8-sig")
        if "<!-- CODEX AGENT:" in text or "<!-- CLAUDE REVIEW" in text:
            stale.append(str(path.relative_to(ROOT)))
    assert not stale, f"hidden review handoff(s) remain in current docs: {stale}"
    maintained = "\n".join(path.read_text(encoding="utf-8-sig") for path in current)
    assert "looplab governance" not in maintained
    stale_agent_copy = [
        "fall back to the LLM developer",
        "fall back to the in-house LLM Developer",
        "one LLM identity",
        "one identity across stages",
        "from `--kind`/`--set` alone",
        "boss plans + launches",
        "boss launches a run",
    ]
    assert not [claim for claim in stale_agent_copy if claim in maintained]
    assert "unified control facade" in maintained
    assert "boss proposes → operator launches" in maintained
    assert "`--set` only changes engine settings" in maintained


def test_load_bearing_source_comments_match_current_identity_and_replay_contracts():
    """Guard exact phrases that previously outlived the behavior they described."""
    from tests._source_scan import iter_sources

    source = "\n".join(text for _path, text in iter_sources())
    stale_claims = [
        "1 card = 1 hypothesis",
        "Never mutated except by `replay.fold`",
        "the engine is the sole writer of events.jsonl",
        "Replay — the single source of truth",
        "Audit-only: the allow is recorded",
        "back to the in-house LLM Developer",
        "typically the in-house",
        "fallback is LLM anyway",
        "One identity, replay",
        "--kind/--set",
        "boss launches a run",
    ]
    assert not [claim for claim in stale_claims if claim in source]
    novelty = (ROOT / "looplab/engine/novelty.py").read_text(encoding="utf-8")
    assert "This is a behavioral admission decision" in novelty
