"""One `--memory-dir` resolver for the three governed read commands (doc 25 CT-05).

`cross-run-concepts`, `cross-run-digest` and `claims` each accepted an argument that may name the
memory DIRECTORY or the store FILE itself, and each wrote out the same `stat` / `S_ISREG` / `S_ISDIR`
/ canonical-name dance.

It is not cosmetic. The file-vs-directory split decides which governed SOURCE the read declares to
`project_governed_sources`, and a source declared by NAME gets that layer's health and quarantine
bookkeeping applied to a store it recognises, while one declared by PATH gets the same locking with
no such claim. Getting that backwards does not fail — it silently changes what "complete" means in
the receipt the operator reads.
"""
from __future__ import annotations

import os

import pytest

from looplab.cli.governance_cmds import quarantined_claim_counts, resolve_memory_source


# ------------------------------------------------------------------ the resolution

def test_a_directory_resolves_to_its_canonical_store_declared_by_NAME(tmp_path):
    path, base, names, paths = resolve_memory_source(tmp_path, "lessons.jsonl", missing_is_dir=False)
    assert path == tmp_path / "lessons.jsonl" and base == tmp_path
    assert names == ["lessons.jsonl"] and paths == []


def test_the_canonical_file_named_DIRECTLY_still_declares_it_by_name(tmp_path):
    """Pointing at `<dir>/lessons.jsonl` is the same read as pointing at `<dir>`; declaring it by
    path instead would drop the governance layer's health bookkeeping for a store it knows."""
    store = tmp_path / "lessons.jsonl"
    store.write_text("", encoding="utf-8")
    path, base, names, paths = resolve_memory_source(store, "lessons.jsonl", missing_is_dir=False)
    assert path == store and base == tmp_path
    assert names == ["lessons.jsonl"] and paths == []


def test_a_NON_canonical_file_is_declared_by_PATH_not_by_name(tmp_path):
    """The operator pointed at some other file. It is still read under the same governance lock, but
    nothing may claim it is the canonical store."""
    other = tmp_path / "exported-lessons.jsonl"
    other.write_text("", encoding="utf-8")
    path, base, names, paths = resolve_memory_source(other, "lessons.jsonl", missing_is_dir=False)
    assert path == other and base == tmp_path
    assert names == [] and paths == [other.absolute()]


def test_a_relative_path_is_absolutised_before_it_is_declared(tmp_path, monkeypatch):
    """The declared source crosses into the governance layer, which has no idea what the CLI's cwd
    was. A relative entry there would name a different file on the other side."""
    other = tmp_path / "exported.jsonl"
    other.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _path, _base, _names, paths = resolve_memory_source(
        "exported.jsonl", "lessons.jsonl", missing_is_dir=False)
    assert paths and all(entry.is_absolute() for entry in paths)


@pytest.mark.parametrize("missing_is_dir,expected_none", [(False, True), (True, False)])
def test_a_missing_argument_follows_the_CALLERS_rule(tmp_path, missing_is_dir, expected_none):
    """The one behaviour that legitimately differs. `cross-run-concepts` treats a non-existent path
    as a directory so its refusal can name the capsule file the operator expected; `claims` refuses
    outright, because it has no useful answer about a directory that is not there."""
    result = resolve_memory_source(tmp_path / "gone", "lessons.jsonl",
                                   missing_is_dir=missing_is_dir)
    assert (result is None) is expected_none
    if result is not None:
        path, base, names, _paths = result
        assert path == tmp_path / "gone" / "lessons.jsonl" and base == tmp_path / "gone"
        assert names == ["lessons.jsonl"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX special files")
def test_something_that_is_neither_a_file_nor_a_directory_is_refused(tmp_path):
    """A FIFO/device/socket resolves to neither branch. Falling through to "treat it as a directory"
    would build a governed read against a path that can block forever on open."""
    fifo = tmp_path / "a-fifo"
    os.mkfifo(fifo)
    for missing_is_dir in (True, False):
        assert resolve_memory_source(fifo, "lessons.jsonl",
                                     missing_is_dir=missing_is_dir) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlink_to_the_canonical_store_is_followed_by_stat(tmp_path):
    """`stat` follows links, so a symlinked store reads as the regular file it points at — the
    resolution must not accidentally classify it as "neither"."""
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    link = tmp_path / "lessons.jsonl"
    link.symlink_to(real)
    path, base, names, _paths = resolve_memory_source(link, "lessons.jsonl", missing_is_dir=False)
    assert path == link and base == tmp_path and names == ["lessons.jsonl"]


def test_the_canonical_name_is_per_command_not_global(tmp_path):
    """`cross-run-concepts` resolves capsules, `claims` resolves lessons — against the same dir."""
    for name in ("concept_capsules.jsonl", "lessons.jsonl"):
        path, _base, names, _paths = resolve_memory_source(tmp_path, name, missing_is_dir=False)
        assert path.name == name and names == [name]


# ------------------------------------------------------------------ the receipt extraction

@pytest.mark.parametrize("receipt,expected", [
    ({"lessons": {"rows_quarantined": 2}, "research": {"rows_quarantined": 3}}, (2, 3)),
    ({"lessons": {"rows_quarantined": 2}}, (2, 0)),
    ({}, (0, 0)),
    ({"lessons": None, "research": None}, (0, 0)),
    ({"lessons": {}, "research": {}}, (0, 0)),
    ({"lessons": {"rows_quarantined": None}}, (0, 0)),
    (None, (0, 0)),
    ("not a receipt", (0, 0)),
])
def test_the_quarantined_counts_survive_every_receipt_shape(receipt, expected):
    """A receipt read with the wrong nesting reports 0 quarantined rows — which turns "these counts
    are lower bounds" into a confident exact answer, in three commands at once."""
    assert quarantined_claim_counts(receipt) == expected


def test_a_nonzero_count_is_never_reported_as_clean():
    """The direction that matters: under-reporting hides a partial source; the warning exists to say
    an empty match is not proof of absence."""
    lessons, research = quarantined_claim_counts(
        {"lessons": {"rows_quarantined": 1}, "research": {"rows_quarantined": 0}})
    assert (lessons, research) == (1, 0)


# ------------------------------------------------------------------ all three commands use them

def test_no_read_command_re_derives_the_file_or_directory_dance():
    """A grep guard: `S_ISREG`/`S_ISDIR` in a command body is the fourth copy, and a fourth copy is
    where a canonical-vs-path declaration goes wrong without failing."""
    import inspect

    from looplab.cli import governance_cmds

    source = inspect.getsource(governance_cmds)
    resolver = inspect.getsource(governance_cmds.resolve_memory_source)
    assert source.count("S_ISREG") == resolver.count("S_ISREG") == 1
    assert source.count("S_ISDIR") == resolver.count("S_ISDIR") == 1


def test_no_command_re_extracts_the_quarantined_rows_by_hand():
    import inspect

    from looplab.cli import governance_cmds

    source = inspect.getsource(governance_cmds)
    helper = inspect.getsource(governance_cmds.quarantined_claim_counts)
    assert source.count('"rows_quarantined"') == helper.count('"rows_quarantined"') == 2
