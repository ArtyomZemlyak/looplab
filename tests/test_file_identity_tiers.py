"""Rewrite detection has named strength tiers, and a weaker one is a stated choice (doc 25 SC-11).

Six independent mechanisms answered "was the log replaced under me": blake2b probe signatures,
mtime/ctime pairs, 5-tuple stat signatures with a retry loop, an `(ino, ctime, size, mtime, …)` cache
key, and full descriptor watching. The tuples differed subtly — some omitted `st_dev`, some the
Windows reparse field — each with its own rationale, so a weakness found in one fence had to be
re-derived for every sibling.

The strengths genuinely differ, so this is NOT full unification. What is unified is the vocabulary:

  * `same_file_entry`  — replacement only. Growth keeps the answer; a new inode changes it.
  * `file_identity`    — same file AND unchanged. Every way the bytes could have been swapped.

A site needing something between the two says so AGAINST these definitions. This file pins that no
consumer silently spells a third tuple, and pins the two real bugs the unification fixed.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from looplab.core.atomicio import file_identity, same_file_entry

_PKG = Path(__file__).resolve().parents[1] / "looplab"

# Modules that legitimately keep a NARROWER signature, each with the reason it must state.
DOCUMENTED_VARIANTS = {
    "serve/log_pages.py": "timestamps only; dev/ino and size are checked separately at the read site",
    "serve/scope_sources.py": "lstat/fstat-shared identity; Windows' ctime diverges between them",
    "serve/command_observation.py": "Windows reports a transient ctime difference through fstat",
}


# ------------------------------------------------------------------ the two tiers

def test_the_replacement_tier_answers_replacement_only(tmp_path):
    """Growth must NOT change it — a reader holding a cursor into an append-only log has to keep
    that cursor across normal appends, or every append restarts the read."""
    log = tmp_path / "events.jsonl"
    log.write_bytes(b"one\n")
    before = same_file_entry(log.stat())

    log.write_bytes(b"one\ntwo\n")                      # grew in place
    assert same_file_entry(log.stat()) == before

    replacement = tmp_path / "new"
    replacement.write_bytes(b"one\n")
    os.replace(replacement, log)                       # same name, new inode
    assert same_file_entry(log.stat()) != before


def test_the_full_tier_notices_a_same_size_rewrite_the_weak_tier_cannot(tmp_path):
    """The reason both tiers exist. An A/B/A edit or a restore keeps dev/ino and size; only the
    timestamp fields catch it."""
    log = tmp_path / "events.jsonl"
    log.write_bytes(b"AAAA")
    entry_before, full_before = same_file_entry(log.stat()), file_identity(log.stat())

    os.utime(log, ns=(0, 0))
    log.write_bytes(b"BBBB")                            # same size, in place
    os.utime(log, ns=(10**9, 10**9))

    assert same_file_entry(log.stat()) == entry_before, "the weak tier is deliberately blind here"
    assert file_identity(log.stat()) != full_before, "the full tier must catch a same-size rewrite"


def test_the_full_tier_is_strictly_stronger():
    st = os.stat(__file__)
    assert set(same_file_entry(st)).issubset(set(file_identity(st)))
    assert len(file_identity(st)) > len(same_file_entry(st))


# ------------------------------------------------------------------ the bugs unification fixed

def test_the_state_cache_key_no_longer_omits_st_dev():
    """It keyed on `(str(rd), st_ino, …)`. Inode numbers are only unique per DEVICE, so two runs on
    different filesystems could collide — and the payload served is a whole projected RunState."""
    from looplab.serve import appstate

    import inspect
    source = inspect.getsource(appstate)
    assert "stt.st_ino, stt.st_ctime_ns" not in source, "the hand-rolled cache key is back"
    assert "*file_identity(stt)" in source


def test_the_attention_feed_signature_covers_the_windows_reparse_field():
    """Three hand-spelled 5-tuples were `file_identity` minus `st_file_attributes`, so a log that
    gained a reparse point compared EQUAL and the cached projection was served for a different file."""
    from looplab.serve.routers import attention

    import inspect
    source = inspect.getsource(attention)
    assert "st_ctime_ns," not in source, "a hand-rolled stat signature is back in the attention feed"
    assert source.count("file_identity(") >= 3


def _stat_on_another_device(base: os.stat_result) -> os.stat_result:
    """`base`, moved to a different device and IDENTICAL in every other field.

    Built as a real `os.stat_result` rather than a duck-typed stand-in: the production code reads
    `st_mtime`/`st_ctime` as well as the `_ns` pair, and a shim that happened to omit one would make
    the test pass for the wrong reason.
    """
    swapped = os.stat_result(
        (base.st_mode, base.st_ino, base.st_dev + 1, base.st_nlink, base.st_uid, base.st_gid,
         base.st_size, base.st_atime, base.st_mtime, base.st_ctime),
        {"st_atime_ns": base.st_atime_ns, "st_mtime_ns": base.st_mtime_ns,
         "st_ctime_ns": base.st_ctime_ns})
    assert file_identity(swapped)[1:] == file_identity(base)[1:], "only st_dev may differ"
    assert file_identity(swapped) != file_identity(base)
    return swapped


def test_the_run_summary_cache_notices_a_log_that_differs_only_in_st_dev(tmp_path, monkeypatch):
    """`run_projections` keyed on `(st_ino, st_ctime_ns, st_size, st_mtime_ns)` — `file_identity`
    minus BOTH `st_dev` and the Windows reparse field, with no stated reason.

    The reachable wrong outcome is STALE SAME-ID data, not a collision between two runs: the cache is
    keyed by `rd.name`, so run A can never be served run B's summary. What it CAN do is keep serving
    generation A's summary for a run whose events.jsonl was replaced by a file on a different device
    that matches on the four fields it did keep — and inode numbers are synthesized from the path on
    geesefs/s3fs, so a restored or rsynced run dir on a FUSE/S3 mount collides by construction.

    Driven, not pinned: the log below really is re-read and re-folded, and the dashboard row really
    does carry the new goal. A source pin for `file_identity(` would hold with the write site still
    flattening the tuple and the read site still slicing four fields off it.
    """
    from looplab.events.eventstore import EventStore
    from looplab.serve import run_projections
    from looplab.serve.server import make_app

    rd = tmp_path / "demo"
    rd.mkdir()
    log = rd / "events.jsonl"
    EventStore(log).append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g",
                                           "direction": "min"})
    srv = make_app(tmp_path).state.looplab

    assert [s["goal"] for s in run_projections.run_summaries(srv)] == ["g"]
    generation_a = log.stat()
    assert srv.summary_cache, "precondition: the first projection cached a summary"

    # Generation B: the SAME run id, genuinely different bytes, on a different device — with stat
    # numbers that collide with generation A's on every field the hand-rolled signature kept.
    text = log.read_text(encoding="utf-8")
    assert '"goal":"g"' in text
    log.write_text(text.replace('"goal":"g"', '"goal":"b"', 1), encoding="utf-8")
    swapped = _stat_on_another_device(generation_a)
    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, *a, **kw:
                        swapped if self == log else real_stat(self, *a, **kw))

    assert [s["goal"] for s in run_projections.run_summaries(srv)] == ["b"], (
        "the run-summary cache served generation A for a log it can only tell apart by st_dev")


# ------------------------------------------------------------------ no third tuple, silently

def _hand_rolled_signature_lines() -> list[str]:
    """Sites that CONSTRUCT a stat signature, outside the module that owns the definitions.

    AST, not grep: the question is whether a TUPLE is being built out of stat fields, which is what a
    signature is. A line that merely reads two fields (`st_size` next to an `st_mtime` sort key) is
    not a competing definition, and a regex over field names cannot tell the two apart — it flagged
    thirty incidental reads.
    """
    import ast as _ast

    from _source_scan import PKG, iter_sources

    def _is_stat_field(node) -> bool:
        return isinstance(node, _ast.Attribute) and node.attr.startswith("st_")

    offenders = []
    # Through `_source_scan`, not a fresh rglob: at least one tracked file carries a UTF-8 BOM, and
    # a guard that walks the package itself decodes it differently from every other guard.
    for path, source in iter_sources():
        rel = path.relative_to(PKG).as_posix()
        if rel == "core/atomicio.py":                   # the owner
            continue
        try:
            tree = _ast.parse(source)
        except SyntaxError:                             # not our business here
            continue
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Tuple):
                continue
            if sum(1 for element in node.elts if _is_stat_field(element)
                   or (isinstance(element, _ast.Call) and element.args
                       and _is_stat_field(element.args[0]))) >= 2:
                offenders.append(f"{rel}:{node.lineno}")
    return offenders


# The sites SC-11 named are converted. An AST sweep then found the pattern is far more widespread
# than the finding's "six different ways" — measured below — so the rest is a LEDGER rather than a
# silent backlog: the number cannot grow without this test going red, and shrinking it is the work.
UNCONVERTED_SIGNATURE_SITES = 22


def test_the_backlog_of_hand_rolled_signatures_does_not_grow():
    """SC-11 said six mechanisms; an AST sweep for tuples built out of `st_*` reads finds far more.

    The cap is the MEASURED count, not a round number with slack: a `<=` with headroom lets a new
    hand-rolled signature appear as long as an unrelated one is converted in the same tree, which is
    precisely the drift this ledger exists to stop. Re-measure and re-pin whenever a site converts.

    Converting them all needs per-site judgement — several are deliberately the weak
    replacement-only tier and correct as they stand — so this pins the COUNT instead of pretending
    at coverage. A new hand-rolled signature makes this red; converting one and lowering the number
    is the intended direction of travel.

    At least one of the unconverted sites carries the same defect that was just fixed in the
    attention feed: `tools/_runcache.py` spells `file_identity`'s fields reordered and WITHOUT
    `st_file_attributes`, so it cannot see a file that gained a reparse point.
    """
    offenders = sorted(set(_hand_rolled_signature_lines()))
    undeclared = [o for o in offenders if o.split(":")[0] not in DOCUMENTED_VARIANTS]
    assert len(undeclared) <= UNCONVERTED_SIGNATURE_SITES, (
        f"a new hand-rolled stat signature appeared ({len(undeclared)} > "
        f"{UNCONVERTED_SIGNATURE_SITES}): {undeclared}")


def test_the_sites_this_change_converted_stay_converted():
    """The six SC-11 named. These must not reappear in the hand-rolled sweep."""
    converted = {"serve/routers/attention.py", "serve/appstate.py"}
    offenders = {o.split(":")[0] for o in _hand_rolled_signature_lines()}
    assert not (converted & offenders), (
        f"a converted site went back to a hand-rolled signature: {sorted(converted & offenders)}")


@pytest.mark.parametrize("rel", sorted(DOCUMENTED_VARIANTS))
def test_each_declared_variant_names_the_canonical_it_departs_from(rel):
    """A narrower tuple is fine; a narrower tuple whose reader cannot find what it narrowed is not.
    This is the difference between a decision and an omission."""
    from _source_scan import PKG

    source = (PKG / rel).read_text(encoding="utf-8-sig")
    assert "atomicio.file_identity" in source or "atomicio import" in source, (
        f"{rel} narrows the canonical signature without naming it")
