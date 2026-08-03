"""One portfolio read and one canonicalization per agent turn (doc 25 TO-11).

`find_concept_slugs` and `concept_card` each re-read `concept_capsules.jsonl`, re-deduplicated it,
and then built a full `canonicalize_concepts` map over EVERY capsule. The documented workflow is to
call one and then the other on a slug it returned, so the whole portfolio was re-canonicalized twice
inside a single agent turn — and the underlying files are the same governance snapshot the call has
already taken.

The cache is keyed on the capsule file's `file_identity` and the taxonomy revision, which is the part
that needs a test: a cache that goes stale here would answer a concept question from a superseded
portfolio or a superseded alias table, and say nothing about it.
"""
from __future__ import annotations

import json

import pytest

from looplab.tools.cross_run_tools import CrossRunTools


def _memory(tmp_path, rows):
    (tmp_path / "concept_capsules.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return tmp_path


def _row(run_id, concepts):
    """A REAL capsule, minted the way the store mints them — a hand-rolled dict is dropped by
    `_dedup_valid_capsules`, so a fixture that skipped the builder would test an empty portfolio."""
    from looplab.engine.memory import build_concept_capsule

    return build_concept_capsule(
        run_id=run_id, task_id="t", fingerprint=["retrieval"], direction="min",
        concepts=list(concepts), concept_outcomes={c: 0.9 for c in concepts})


def _tools(tmp_path, rows):
    return CrossRunTools(_memory(tmp_path, rows), audience="portfolio")


# ------------------------------------------------------------------ the read is shared

def test_a_second_call_reuses_the_same_row_objects(tmp_path):
    """Object identity is the observable: the canonical map is keyed by `id(capsule)`, so a second
    read producing fresh dicts would silently miss every lookup."""
    tools = _tools(tmp_path, [_row("a", ["alpha"]), _row("b", ["beta"])])
    first = tools._all_capsules()
    second = tools._all_capsules()
    assert [id(row) for row in first] == [id(row) for row in second]


def test_a_rewritten_portfolio_is_re_read(tmp_path):
    """The one outcome that would be WRONG is a stale answer, so the invalidation gets a driven test
    rather than a comment."""
    tools = _tools(tmp_path, [_row("a", ["alpha"])])
    assert [row["run_id"] for row in tools._all_capsules()] == ["a"]
    _memory(tmp_path, [_row("a", ["alpha"]), _row("c", ["gamma"])])
    assert [row["run_id"] for row in tools._all_capsules()] == ["a", "c"]


def test_a_same_size_replacement_is_still_noticed(tmp_path):
    """`file_identity` carries dev/ino, so an `os.replace` at an identical size is visible. A
    (size, mtime) key would not see this — the portfolio store rewrites by replace."""
    import os

    tools = _tools(tmp_path, [_row("a", ["alpha"])])
    assert tools._all_capsules()[0]["concepts"] == ["alpha"]
    replacement = tmp_path / "next.jsonl"
    replacement.write_text(json.dumps(_row("a", ["omega"])) + "\n", encoding="utf-8")
    target = tmp_path / "concept_capsules.jsonl"
    os.utime(replacement, (target.stat().st_atime, target.stat().st_mtime))
    os.replace(replacement, target)
    assert tools._all_capsules()[0]["concepts"] == ["omega"]


def test_a_missing_portfolio_reads_as_empty_and_does_not_poison_the_cache(tmp_path):
    """A memory dir with no capsule file yet is ordinary. The identity is `None` there, which must
    never be treated as a hit — otherwise the first write after it would be invisible."""
    tools = CrossRunTools(tmp_path, audience="portfolio")
    assert tools._all_capsules() == []
    _memory(tmp_path, [_row("a", ["alpha"])])
    assert [row["run_id"] for row in tools._all_capsules()] == ["a"]


# ------------------------------------------------------------------ the canonicalization is shared

def test_the_canonical_map_is_built_once_per_revision(tmp_path):
    tools = _tools(tmp_path, [_row("a", ["alpha"]), _row("b", ["beta"])])
    caps, first = tools._capsule_snapshot({}, {}, "rev-1")
    caps_again, second = tools._capsule_snapshot({}, {}, "rev-1")
    assert first is second, "the same revision must not re-canonicalize the portfolio"
    assert caps is caps_again
    assert set(first[id(caps[0])]) == {"alpha"}


def test_a_taxonomy_revision_bump_rebuilds_it(tmp_path):
    """An alias or split changes what a capsule's concepts MEAN, so the revision is part of the key —
    reusing a map across revisions would answer with the superseded taxonomy."""
    tools = _tools(tmp_path, [_row("a", ["reg/old"])])
    caps, before = tools._capsule_snapshot({}, {}, "rev-1")
    assert set(before[id(caps[0])]) == {"reg/old"}
    caps, after = tools._capsule_snapshot({"reg/old": "reg/new"}, {}, "rev-2")
    assert set(after[id(caps[0])]) == {"reg/new"}


def test_the_map_does_not_accumulate_one_entry_per_revision(tmp_path):
    """One taxonomy revision is live at a time. Keeping every past one would make a long-lived
    provider grow a map per governance edit, holding the whole portfolio's concept sets each time."""
    tools = _tools(tmp_path, [_row("a", ["alpha"])])
    for revision in ("rev-1", "rev-2", "rev-3"):
        tools._capsule_snapshot({}, {}, revision)
    _signature, _rows, by_revision = tools._capsule_cache
    assert list(by_revision) == ["rev-3"]


def test_a_rewritten_portfolio_invalidates_the_canonical_map_too(tmp_path):
    tools = _tools(tmp_path, [_row("a", ["alpha"])])
    caps, _first = tools._capsule_snapshot({}, {}, "rev-1")
    assert len(caps) == 1
    _memory(tmp_path, [_row("a", ["alpha"]), _row("c", ["gamma"])])
    caps, second = tools._capsule_snapshot({}, {}, "rev-1")
    assert len(caps) == 2
    assert set(second[id(caps[1])]) == {"gamma"}, "the new row must have a canonical set too"


# ------------------------------------------------------------------ no tool re-derives it

def test_the_portfolio_is_canonicalized_in_exactly_one_place():
    """Scoped to the builder, not the module: `_capsule_snapshot` legitimately contains the one
    remaining per-capsule loop, and `mine` (this run's OWN concepts) is still canonicalized at each
    tool — that is one small set, not the portfolio."""
    import inspect

    from looplab.tools import cross_run_tools

    source = inspect.getsource(cross_run_tools)
    assert source.count('cap.get("concepts") or [], aliases=aliases, splits=splits') == 1
    assert 'cap.get("concepts") or [], aliases=aliases, splits=splits' in inspect.getsource(
        cross_run_tools.CrossRunTools._capsule_snapshot)


@pytest.mark.parametrize("tool", ["_tool_similar_runs", "_tool_find_concept_slugs",
                                 "_tool_concept_card"])
def test_each_tool_reads_the_shared_snapshot(tool):
    import inspect

    from looplab.tools.cross_run_tools import CrossRunTools

    body = inspect.getsource(getattr(CrossRunTools, tool))
    assert "_capsule_snapshot(" in body, f"{tool} does not use the shared snapshot"
    assert "canonicalize_concepts(\n                cap" not in body, (
        f"{tool} still canonicalizes capsules itself")
