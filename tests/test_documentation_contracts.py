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
    # Moving it is part of adding a doc. It cannot see a doc NUMBER collision — two files may both
    # start "34-" and satisfy the count AND the membership check, which happened twice on 2026-08-13
    # (the fence audit vs the review residue, and the decision-sites survey vs the worktree
    # measurement). If you find this red, ADD THE MISSING ROW; do not just move the number.
    #   41 -> 42 (2026-08-14): a THIRD number collision, and the first one this count actually
    #   caught — which is what the paragraph above predicted it could not do on its own. Two
    #   concurrent sessions each wrote a `41-`: the external-works synergy doc (from origin/master)
    #   and the pre-chewed evidence survey (the log/metric tools branch). Both are real and neither
    #   supersedes the other, so the survey was RENUMBERED to `42-` — glob, index row, index first
    #   column and `mkdocs.yml` nav together — rather than one being dropped. Note the collision
    #   itself still slipped past the membership check exactly as the comment says; what went red
    #   was the count, because after renumbering there genuinely is one more document.
    #   42 -> 43 (2026-08-19): the operator-list audit (doc 43). No collision this time — the
    #   number was claimed by checking the glob AND the index table together, which is the
    #   procedure the three earlier collisions existed for.
    assert len(numbered) == 44, "the derived numbered-document inventory changed"
    missing = [path.name for path in numbered if path.name not in index]
    assert not missing, f"numbered document(s) missing from docs/00-INDEX.md: {missing}"
    assert "| 09 |" in index and "No document was allocated" in index


def test_all_relative_markdown_links_resolve():
    # `.ipynb_checkpoints` is EXCLUDED, and not as tidiness: this repo is edited on a JupyterHub
    # box, where saving any doc mints `docs/guide/.ipynb_checkpoints/<name>-checkpoint.md`. Its
    # relative links resolve from one directory deeper, so every sibling link in it "breaks" — a red
    # suite for a file nobody publishes and nobody reads. The rest of the codebase already skips
    # this directory by name (`tools/reposcout.py::_SKIP_DIRS`,
    # `runtime/stage_identity.py::SKIP_DIRS`); this walker simply had not.
    files = [ROOT / "README.md", ROOT / "CLAUDE.md",
             *sorted(p for p in DOCS.rglob("*.md")
                     if ".ipynb_checkpoints" not in p.parts)]
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
    sources = [ROOT / "README.md",
               *sorted(p for p in (DOCS / "guide").glob("*.md")
                       if ".ipynb_checkpoints" not in p.parts)]
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
    # The tally is DERIVED, never pinned — changed 2026-08-19. It used to be four literals
    # (`147/39/2/0`) asserted here, and CLAUDE.md's "open-item index" section names that as the
    # counter-example that decided the whole convention: closing a finding meant editing a TEST's
    # constants, so the guard made closing a finding MORE expensive than leaving it open. A status
    # guard that penalises closing is how 39 PARTIALLY RESOLVED findings sat unre-derived for
    # eleven days. What is worth guarding is that the doc's human-facing rollup AGREES with its own
    # headings, and that the inventory has not silently lost a finding — both derived, so closing a
    # finding is one edit to the heading plus one to the sentence it is a summary of.
    assert set(counts) <= {"RESOLVED", "PARTIALLY RESOLVED", "DEFERRED", "OPEN"}
    summary = re.search(
        r"Status totals: (\d+) resolved, (\d+) partially resolved, (\d+) deferred, "
        r"(\d+) open \((\d+) total\)", text)
    assert summary, "the ledger must carry a `Status totals:` rollup sentence"
    derived = (counts.get("RESOLVED", 0), counts.get("PARTIALLY RESOLVED", 0),
               counts.get("DEFERRED", 0), counts.get("OPEN", 0), len(statuses))
    assert tuple(map(int, summary.groups())) == derived, (
        "the ledger's `Status totals:` sentence disagrees with its own finding headings: "
        f"sentence says {summary.groups()}, headings derive {derived}")


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


def test_the_package_map_names_each_package_exactly_once():
    """Two rows for one package is invisible, and it silently reverted a fix for 2.7 days.

    CLAUDE.md's package map is the first thing every coding agent reads, and its rows are 3-26 KB
    long, so they are EDITED BY SPLICING text into them. A splice lands in whichever row it matches
    first. On 2026-08-13 a conflict was resolved by keeping both sides of four rows, and on
    2026-08-14 a fifth; the engine row's PRE-FIX copy became the one a reader meets first, so
    `a8d43b50`'s correction of two false statements (that a `metric_salvaged` node can NEVER become
    champion — `metric_salvage_repair` is the exception, default on; and that `widths.py` holds ONE
    rule — it holds two) was reverted for every reader while still present further down. The
    asymmetry was total: `per_experiment_gpu_budget` appeared only in the corrected copy and
    `occupancy_due` only in the stale one, so neither was simply newer. 52 commits touched this file
    in the 2.7 days it lived and none noticed, because nothing looked.

    Deliberately keyed on the PATH cell alone. It cannot see a unique row with wrong content (the
    `trust/` row credits redaction to it while `core/redact.py` owns it — a different defect,
    recorded in the backlog), and it is not meant to: this asserts the one property whose violation
    is undetectable by reading, since both copies look correct in isolation.
    """
    rows = re.findall(r"^\| (`looplab/[^`]*`) \|", (ROOT / "CLAUDE.md").read_text(), re.M)
    assert rows, "the package map has no `looplab/...` rows — the table moved or its shape changed"
    duplicated = sorted(name for name, n in Counter(rows).items() if n > 1)
    assert not duplicated, (
        f"{duplicated} appear more than once in CLAUDE.md's package map. Two rows for one package "
        "means a reader — and a splice — takes whichever comes first, and the other copy's content "
        "is invisible. Merge them into one row rather than adding a second.")


# The number-words the three surfaces below spell out. Short on purpose: a registry that outgrew
# this map would be a registry whose enumerations should stop being written by hand at all.
_COUNT_WORDS = {8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen"}


def test_every_failure_reason_surface_names_all_of_them():
    """The three places that ENUMERATE `FAILURE_REASONS`, derived from the registry.

    This is the repo's most-repeated documentation defect and it has now recurred inside its own
    fix. The list is written out in prose three times; each time a reason is added, all three go
    stale, and each correction so far has been a hand-count of a hand-list:

      * 2026-08-13 — concepts.md said "eight" and omitted `diverged`/`stalled`/`needs_failed`;
      * 2026-08-14 — a merge left BOTH generations of that bullet in place, one naming eleven and
        one naming eight;
      * 2026-08-20 (`2933423c`) — the count was corrected to "twelve" and the LIST left at eleven,
        so the sentence miscounted its own enumeration. `not_learning` had been in the registry
        since `364cef55` the previous day, and `50ab168e` edited the settings row that omits it
        without noticing.

    `tests/test_config_docs_sync.py` cannot catch any of this: it asserts every `Settings` field is
    NAMED somewhere in configuration.md and never reads a DEFAULT — which CLAUDE.md's docs-sync rule
    explicitly requires to be correct ("every `Settings` field must have a row with the CORRECT
    default"). The settings table's own row for `inline_repair_reasons` prints that default as a
    JSON array, so here it is parsed and compared to the tuple.

    Additive-only by construction: adding a reason fails this until the three surfaces name it, and
    nothing here penalises REMOVING one.
    """
    from looplab.core.models import FAILURE_REASONS

    reasons = list(FAILURE_REASONS)
    word = _COUNT_WORDS.get(len(reasons))
    assert word, f"FAILURE_REASONS has {len(reasons)} members — extend _COUNT_WORDS"
    problems = []

    # 1. The settings table's DEFAULT cell, parsed as the JSON array it is printed as.
    config = (DOCS / "guide" / "configuration.md").read_text(encoding="utf-8")
    row = [line for line in config.splitlines() if line.startswith("| `inline_repair_reasons`")]
    assert len(row) == 1, "the inline_repair_reasons settings row moved or was duplicated"
    arrays = re.findall(r"`(\[\"crash\"[^`]*\])`", row[0])
    assert len(arrays) == 1, "the inline_repair_reasons row no longer prints its default as an array"
    documented = json.loads(arrays[0])
    if documented != reasons:
        problems.append(
            f"docs/guide/configuration.md `inline_repair_reasons` default is {documented} but "
            f"`core/models.py::FAILURE_REASONS` is {reasons}")

    # 2. The concepts bullet: a spelled count AND the members it then lists.
    concepts = (DOCS / "guide" / "concepts.md").read_text(encoding="utf-8")
    # `\s+` on the tail anchor, not a literal space: this bullet is a wrapped markdown paragraph, and
    # "mechanical three" straddled the line break the day two branches wrapped it differently. A
    # registry-derivation check that a REFLOW can redden teaches "re-wrap until green", which is the
    # opposite of what it is for — the rule is about which reasons are named, never about where the
    # line ends.
    bullet = re.search(r"\*\*any\*\* of the (\w+) `FAILURE_REASONS`(.{0,600}?)mechanical\s+three",
                       concepts, re.S)
    assert bullet, "the concepts.md inline-repair bullet moved — re-derive this check"
    if bullet.group(1) != word:
        problems.append(f"docs/guide/concepts.md says '{bullet.group(1)}' FAILURE_REASONS, not '{word}'")
    missing = [r for r in reasons if f"`{r}`" not in bullet.group(2)]
    if missing:
        problems.append(f"docs/guide/concepts.md's inline-repair list omits {missing}")

    # 3. The process diagram (CLAUDE.md: stale diagram is a bug, in the SAME change).
    diagram = (DOCS / "infographic" / "agent-architecture.html").read_text(encoding="utf-8")
    block = re.search(r"reasons = ALL (\w+) FAILURE_REASONS by default \(([^)]*)\)", diagram)
    assert block, "the diagram's inline-repair block moved — re-derive this check"
    if block.group(1).lower() != word:
        problems.append(f"the process diagram says 'ALL {block.group(1)}', not '{word.upper()}'")
    missing = [r for r in reasons if r not in block.group(2)]
    if missing:
        problems.append(f"the process diagram's inline-repair list omits {missing}")

    assert not problems, (
        "FAILURE_REASONS surfaces disagree with the registry — update them in the SAME change as "
        "the registry (CLAUDE.md docs-sync rule):\n  " + "\n  ".join(problems))
