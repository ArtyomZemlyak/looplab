"""Rewrite detection has named strength tiers, and a weaker one is a stated choice (doc 25 SC-11).

Six independent mechanisms answered "was the log replaced under me": blake2b probe signatures,
mtime/ctime pairs, 5-tuple stat signatures with a retry loop, an `(ino, ctime, size, mtime, …)` cache
key, and full descriptor watching. The tuples differed subtly — some omitted `st_dev`, some the
Windows reparse field — each with its own rationale, so a weakness found in one fence had to be
re-derived for every sibling.

The strengths genuinely differ, so this is NOT full unification. What is unified is the vocabulary:

  * `same_file_entry`  — replacement only. Growth keeps the answer; a new inode changes it.
  * `same_file_kind`   — replacement OR a change of what the entry IS (type/mode/reparse point).
  * `file_identity`    — same file AND unchanged. Every way the bytes could have been swapped.

A site needing something between the two says so AGAINST these definitions. This file pins that no
consumer silently spells a third tuple, and pins the three real bugs the unification fixed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from looplab.core.atomicio import file_identity, same_file_entry, same_file_kind

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


def test_the_kind_tier_survives_growth_but_not_a_mode_change(tmp_path):
    """The middle tier's whole reason to exist: it is a TOCTOU re-validation, so it must hold across
    the normal path (a lock file gains bytes, a run directory gains children) and break the moment the
    entry stops being the entry that was validated."""
    lock = tmp_path / "engine.lock"
    lock.write_bytes(b"1234\n")
    before = same_file_kind(lock.stat())

    lock.write_bytes(b"1234\n5678\n")                  # grew in place — still the same lock
    assert same_file_kind(lock.stat()) == before

    os.chmod(lock, 0o600)                              # authority changed under the probe
    assert same_file_kind(lock.stat()) != before
    # ...and the weakest tier cannot see that at all, which is why these fences could not use it.
    assert same_file_entry(lock.stat()) == same_file_entry(lock.stat())


def test_the_kind_tier_refuses_an_entry_that_became_a_different_kind(tmp_path):
    """A regular file swapped for a directory (or a symlink, on the platforms that have them) under
    the same name is the exact substitution `_engine_liveness` and the events-stream fence exist to
    refuse — and `st_dev`/`st_ino` alone can answer it only by luck of inode reuse."""
    entry = tmp_path / "run"
    entry.mkdir()
    before = same_file_kind(entry.lstat())

    entry.rmdir()
    entry.write_bytes(b"not a directory\n")
    assert same_file_kind(entry.lstat()) != before


def test_the_lessons_store_stamp_notices_a_compaction_that_preserved_size_and_mtime(tmp_path):
    """A DRIVEN regression for one of the 2026-09-08 conversions.

    `lessons_store_stamp` gated the cross-run refresh on `(size, mtime_ns)`, but `compact_lessons` /
    `consolidate_lessons_file` REPLACE the store through an atomic rename. A compaction that lands on
    the same byte count with the mtime restored is a different file the old stamp called unchanged —
    so the run kept serving priors from the pre-compaction window until some later write moved the
    size. `file_identity` carries `st_dev`/`st_ino`, so the replacement is visible.
    """
    from looplab.engine.lessons import LessonMemory

    class _Engine:
        memory_dir = str(tmp_path)

    memory = LessonMemory.__new__(LessonMemory)        # the stamp reads nothing else off the engine
    memory._e = _Engine()

    store = tmp_path / "lessons.jsonl"
    store.write_bytes(b'{"statement": "aaa"}\n')
    before = memory.lessons_store_stamp()
    stat_before = store.stat()

    replacement = tmp_path / "lessons.jsonl.tmp"       # same LENGTH, different bytes, then renamed
    replacement.write_bytes(b'{"statement": "bbb"}\n')
    os.utime(replacement, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    os.replace(replacement, store)

    after = memory.lessons_store_stamp()
    assert store.stat().st_size == stat_before.st_size and store.stat().st_mtime_ns == stat_before.st_mtime_ns, (
        "the fixture failed to reproduce a size- and mtime-preserving replacement")
    assert after != before, (
        "the lessons refresh gate served the pre-compaction window: a replaced store read as unchanged")


class _WithAttributes:
    """One real `os.stat_result`, reported with a Windows file-attribute word set.

    A proxy rather than a rebuilt `os.stat_result`: `st_file_attributes` is not one of the ten
    positional fields, so it cannot be constructed on this platform at all, and every other field
    must stay EXACTLY what the filesystem said or the fixture proves the wrong thing.
    """

    def __init__(self, base: os.stat_result, attributes: int) -> None:
        self._base = base
        self.st_file_attributes = attributes

    def __getattr__(self, name):
        return getattr(self._base, name)


def test_the_event_log_trusted_growth_fence_re_verifies_a_changed_attribute_word(tmp_path, monkeypatch):
    """A DRIVEN regression for the 2026-09-08 `EventStore._trusted_growth_stat` conversion.

    That fence answers "is this growth MY OWN append, so the cached prefix needs no proof?" — which
    is the full tier — but spelled `(dev, ino, size, mtime_ns, ctime_ns)` by hand: `file_identity`
    minus `st_file_attributes`, the same omission that let a reparse-point acquisition compare EQUAL
    in three other caches. The fence's answer decides whether `read_all` SKIPS re-validating its
    cached prefix, so a false "mine" is a skipped proof.

    Driven end to end through `append()`, not pinned: the store below really writes a second record,
    and the observable is whether the prefix-verification arm ran (`_full_verified_bytes` advances
    only there). With the old tuple this fixture is equal by construction — asserted, not claimed.
    """
    from looplab.events.eventstore import EventStore

    log = tmp_path / "events.jsonl"
    store = EventStore(log)
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    assert store._cache_bytes > 0 and store._full_verified_bytes == 0, (
        "precondition: the first append seeds the cache and proves no prefix (there was none)")

    # The teeth: the five fields the OLD signature carried are IDENTICAL here, so the hand-rolled
    # tuple could not have told these two observations apart. Only the attribute word differs.
    generation = os.stat(log)
    assert file_identity(_WithAttributes(generation, 0x800))[:-1] == file_identity(generation)[:-1]
    assert file_identity(_WithAttributes(generation, 0x800)) != file_identity(generation)

    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, *a, **kw: (
        _WithAttributes(real_stat(self, *a, **kw), 0x800) if self == log
        else real_stat(self, *a, **kw)))

    store.append("node_created", {"node_id": 1})
    assert store._full_verified_bytes > 0, (
        "the trusted-growth fence accepted a log whose attribute word moved under it and skipped "
        "the prefix proof")
    assert [e.type for e in store.read_all()] == ["run_started", "node_created"]


def test_the_scope_probe_binds_the_run_directorys_kind_not_its_timestamps(tmp_path):
    """The middle tier, driven where `scope_generate` hand-spelled it.

    `ScopeSourceProbes.probe_key` is the cheap staleness key behind every scope-report GET, and its
    directory component was a hand-written `(dev, ino, mode, file_attributes)` — field-for-field and
    order-for-order `same_file_kind`, which is why naming the tier left the PERSISTED probe digest
    byte-identical. The two halves of the tier are what this drives: a child artifact moves the
    container's mtime and must NOT invalidate a report, while the container changing what it is must.
    """
    from looplab.events.eventstore import EventStore
    from looplab.serve.scope_generate import ScopeSourceProbes
    from looplab.serve.scope_sources import probe_scope_log_sig

    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append("run_started", {
        "run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})

    probes = ScopeSourceProbes(type("_Srv", (), {"root": tmp_path})())
    log_sig = probe_scope_log_sig(tmp_path, "demo")
    before = probes.probe_key("demo", log_sig)

    mtime_before = rd.stat().st_mtime_ns
    (rd / "node-1").mkdir()                             # a child artifact, not report evidence
    os.utime(rd, ns=(mtime_before + 10**9, mtime_before + 10**9))
    assert rd.stat().st_mtime_ns != mtime_before, (
        "the fixture failed to move the container's mtime, so the next assertion proves nothing")
    assert probes.probe_key("demo", log_sig) == before, (
        "a child artifact invalidated every scope report on that run")

    os.chmod(rd, 0o700)                                 # the container's authority changed
    assert probes.probe_key("demo", log_sig) != before, (
        "the probe key could not see the run directory stop being the directory it validated")


def test_the_knowledge_index_revision_notices_a_replaced_note(tmp_path):
    """The second driven conversion: `KnowledgeTools._source_revision` keyed the in-memory index on
    `(path, size, mtime_ns)`, so an edited-and-renamed note of the same length with a restored mtime
    kept the OLD embeddings serving `kb_search`."""
    from looplab.tools.knowledge_tools import KnowledgeTools

    note = tmp_path / "one.md"
    note.write_text("alpha beta\n", encoding="utf-8")
    tools = KnowledgeTools(knowledge_dir=str(tmp_path))
    before = tools._source_revision()
    stat_before = note.stat()

    replacement = tmp_path / "one.md.tmp"
    replacement.write_text("gamma delt\n", encoding="utf-8")   # same LENGTH, different bytes
    os.utime(replacement, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    os.replace(replacement, note)

    # The teeth, in the assertion rather than in a claim: the two fields the OLD tuple carried are
    # provably identical here, so the old revision was equal by construction.
    assert (note.stat().st_size, note.stat().st_mtime_ns) == (
        stat_before.st_size, stat_before.st_mtime_ns)
    assert tools._source_revision() != before, (
        "the knowledge index kept a replaced note's stale embedding")


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


def test_the_cross_run_cache_notices_a_log_that_gains_a_reparse_point(tmp_path, monkeypatch):
    """`RunStateCache.sig` repeated the full tuple minus `st_file_attributes`, so a Windows
    path that gained a reparse point could keep serving the prior path's folded state.

    Drive the reachable outcome: generation B contains a different goal but reports generation A's
    metadata in every old signature field. The reparse attribute is the only observable change.
    """
    from looplab.events.eventstore import EventStore
    from looplab.tools._runcache import RunStateCache

    rd = tmp_path / "demo"
    rd.mkdir()
    log = rd / "events.jsonl"
    EventStore(log).append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g",
                                           "direction": "min"})
    cache = RunStateCache(tmp_path)

    assert cache.state("demo").goal == "g"
    generation_a = log.stat()

    text = log.read_text(encoding="utf-8")
    assert '"goal":"g"' in text
    log.write_text(text.replace('"goal":"g"', '"goal":"b"', 1), encoding="utf-8")

    class _ReparseGeneration:
        st_file_attributes = 0x400

        def __getattr__(self, name):
            return getattr(generation_a, name)

    generation_b = _ReparseGeneration()
    assert file_identity(generation_b)[:-1] == file_identity(generation_a)[:-1]
    assert file_identity(generation_b) != file_identity(generation_a)
    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, *a, **kw:
                        generation_b if self == log else real_stat(self, *a, **kw))

    assert cache.state("demo").goal == "b", (
        "the cross-run cache served generation A for a path that gained a reparse point")


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

    def _parallel_assignment_values(tree) -> set[int]:
        """Tuple nodes that are the right-hand side of `a, b = st.x, st.y`.

        Python spells multiple assignment with tuple SYNTAX, and the sweep's question is whether a
        signature — a tuple that outlives its statement and is later compared as a unit — is being
        built. `size, mtime_ns, ctime_ns = stt.st_size, stt.st_mtime_ns, stt.st_ctime_ns` builds
        none: it names three locals, and the compiler does not even materialize a tuple. The 2026-09-08
        re-derivation of the ledger found exactly one such site (`events/span_index.py`, whose three
        locals are threaded separately into a per-field persisted header) sitting in the count as a
        false positive — which is worse than noise, because a REAL signature added to that file
        later would have been pre-paid for. The narrowing is deliberately minimal: only a Tuple that
        is an `Assign.value` whose target is itself a Tuple/List, never `sig = (st.st_dev, st.st_ino)`.
        """
        skip: set[int] = set()
        for node in _ast.walk(tree):
            if (isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Tuple)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], (_ast.Tuple, _ast.List))):
                skip.add(id(node.value))
        return skip

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
        unpacked = _parallel_assignment_values(tree)
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Tuple) or id(node) in unpacked:
                continue
            if sum(1 for element in node.elts if _is_stat_field(element)
                   or (isinstance(element, _ast.Call) and element.args
                       and _is_stat_field(element.args[0]))) >= 2:
                offenders.append(f"{rel}:{node.lineno}")
    return offenders


# The sites SC-11 named are converted. An AST sweep then found the pattern is far more widespread
# than the finding's "six different ways" — measured below — so the rest is a LEDGER rather than a
# silent backlog: the number cannot grow without this test going red, and shrinking it is the work.
UNCONVERTED_SIGNATURE_SITES = 2


def test_the_backlog_of_hand_rolled_signatures_does_not_grow():
    """SC-11 said six mechanisms; an AST sweep for tuples built out of `st_*` reads finds far more.

    The cap is the MEASURED count, not a round number with slack: a `<=` with headroom lets a new
    hand-rolled signature appear as long as an unrelated one is converted in the same tree, which is
    precisely the drift this ledger exists to stop. Re-measure and re-pin whenever a site converts.

    Converting them all needs per-site judgement — several are deliberately the weak
    replacement-only tier and correct as they stand — so this pins the COUNT instead of pretending
    at coverage. A new hand-rolled signature makes this red; converting one and lowering the number
    is the intended direction of travel.

    The cross-run state cache was the first follow-up conversion: its hand-rolled tuple omitted
    `st_file_attributes`, so it could not see a file that gained a reparse point. The 2026-09-08
    pass converted the four sites that spelled `(st_dev, st_ino)` by hand — exactly
    `same_file_entry`, the replacement tier — and took the ledger from 21 to 17. The second
    2026-09-08 pass named the tier those conversions kept ALMOST asking for — `same_file_kind`,
    `(dev, ino, mode, file_attributes)`, the TOCTOU re-validation four sites spelled by hand and two
    spelled without the Windows reparse field — and converted the five weak (size, mtime_ns) change
    detectors that could not see a REPLACEMENT: 17 to 5.

    The third 2026-09-08 pass RE-DERIVED each of those five rather than inheriting its verdict, and
    5 became 2:

      * `events/eventstore.py`'s trusted-growth tuple CONVERTED to `file_identity`. The inherited
        reason ("converting risks spuriously aborting appends on Windows") does not survive reading
        the one site that consumes the value: a mismatch costs `read_all` a SHORTCUT and sends it to
        the prefix-verification arm. Nothing can abort, and the failure direction is toward proof.
      * `serve/scope_generate.py`'s directory component CONVERTED to `same_file_kind`, which it
        already was field-for-field and order-for-order — so the persisted probe digest is unchanged
        and the "persisted width" objection never applied to that half of the file.
      * `events/span_index.py` was never a signature and is no longer counted as one: the SWEEP was
        wrong, not the site. `_parallel_assignment_values` above is the fix, and the three locals
        now say at the site why they stay three.
      * `events/traceview.py` and `serve/scope_report_store.py` are CONFIRMED refusals, and each now
        states its reason at the site rather than only in doc 25. traceview's `change_token`
        strictly subsumes the two fields `file_identity` would add; `_stat_identity` is both an
        lstat/fstat cross-source comparison (the declared-variant class) and a persisted digest
        whose only use is equality against a freshly derived one, so a two-width migration buys
        nothing.
    """
    offenders = sorted(set(_hand_rolled_signature_lines()))
    undeclared = [o for o in offenders if o.split(":")[0] not in DOCUMENTED_VARIANTS]
    assert len(undeclared) <= UNCONVERTED_SIGNATURE_SITES, (
        f"a new hand-rolled stat signature appeared ({len(undeclared)} > "
        f"{UNCONVERTED_SIGNATURE_SITES}): {undeclared}")


def test_the_sites_this_change_converted_stay_converted():
    """The SC-11 conversions and follow-up. These must not reappear in the sweep."""
    # File-granular on purpose: a file belongs here once NOTHING in it spells a signature by hand.
    # 2026-09-08 converted the four hand-spelled `(st_dev, st_ino)` pairs — each asked the
    # replacement question and nothing else, which IS `same_file_entry`, the tier whose whole point
    # is that growth keeps the answer. Two of the four files still carry other signatures
    # (`eventstore`'s trusted-growth tuple, `engine_proc`'s dev/ino/MODE triples), so only the two
    # that came out clean can be pinned here; the other two stay in the ledger count above.
    #
    # The second 2026-09-08 pass took `engine_proc` off that list by naming its triples
    # `same_file_kind`, and added `routers/runs.py`, `engine/lessons.py` and `tools/knowledge_tools.py`
    # — every hand-rolled signature in those four files is now one of the three tiers.
    #
    # The third pass added `events/eventstore.py` (trusted growth is `file_identity`) and
    # `serve/scope_generate.py` (the probe's directory component is `same_file_kind`). NOT
    # `events/span_index.py`: nothing there was ever converted — the sweep simply stopped calling a
    # parallel assignment a signature — so claiming it here would be a conversion that never happened.
    converted = {"serve/routers/attention.py", "serve/appstate.py", "tools/_runcache.py",
                 "engine/resources.py", "serve/run_commands.py", "serve/engine_proc.py",
                 "serve/routers/runs.py", "engine/lessons.py", "tools/knowledge_tools.py",
                 "events/eventstore.py", "serve/scope_generate.py"}
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
