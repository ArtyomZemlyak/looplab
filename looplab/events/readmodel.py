"""Rebuildable SQLite read-model (I1, ADR-17). Derived projection for UI/queries;
never a source of truth — always reconstructable from events.jsonl.

**The artefact says what it covers** (docs/CODE_REVIEW.md C5, BACKLOG §4). Until 2026-08-14 the
file recorded only rows: a reader holding `readmodel.sqlite` had no way to tell whether it described
the whole log or a prefix, so a control event appended after the run finished left the projection
silently behind. The rows now travel with a WATERMARK — the schema version, the last `seq` folded,
the event count, and a digest of the `(seq, type)` prefix — written inside the SAME transaction as
the rows, so it can never describe a different set of them.

Everything here FAILS CLOSED, because a watermark that can be wrong is worse than no watermark: it
converts "obviously absent" into "silently stale". `readmodel_status` answers `current` only when a
well-formed watermark MATCHES the events it is compared against; a missing file, an unreadable
database, a missing/duplicated/malformed watermark row, a version this reader does not know, and an
uncomputable coverage digest are all `unknown`, which `readmodel_is_current` reports as not current.
Read models written before the watermark existed load exactly as they always did — the `nodes` table
is untouched — and answer `unknown`, never `current`.

The reader opens the database READ-ONLY through a `file:…?mode=ro` URI on purpose: a plain
`sqlite3.connect` CREATES an empty database at a path that has none, which would turn "this run has
no read model" into "this run has an empty one" — the exact silent-wrong this module exists to
remove.

The watermark is a fact about a DERIVED sidecar, so it stays out of the fold and out of the event
log: `publish_readmodel` writes a file, appends nothing, and nothing in `looplab/` reads the
database back. That is what keeps invariant #4 intact — a read model the engine consulted would be
precisely the cached derived state the invariant forbids.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from looplab.core.jsonutil import canonical_json_digest
from looplab.core.models import Event, RunState
from looplab.events.replay import fold

# Bump when the projection's TABLES change shape. An older/newer version in a watermark is never
# `current` — the rows may be fine, but this reader cannot prove the columns mean what it expects.
READMODEL_SCHEMA_VERSION = 1
WATERMARK_TABLE = "readmodel_watermark"
DIGEST_PREFIX = "rmcov1:"

# Closed vocabulary. `unknown` is deliberately NOT folded into `stale`: "this artefact does not say
# what it covers" and "it says, and it is behind" are different facts for an operator, and only the
# second one names a refresh that will help.
STATUS_CURRENT = "current"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"
READMODEL_STATUSES = (STATUS_CURRENT, STATUS_STALE, STATUS_UNKNOWN)


@dataclass(frozen=True)
class ReadModelWatermark:
    """The event prefix one read model was folded from, plus when it was folded."""

    schema_version: int
    covered_seq: int
    event_count: int
    digest: str
    built_at: float = 0.0

    def same_coverage(self, other: Optional["ReadModelWatermark"]) -> bool:
        """Whether *other* describes the SAME prefix. `built_at` is provenance, not identity.

        An empty digest can never match anything, including another empty one: a watermark whose
        coverage could not be computed must not be able to certify a read model as current.
        """
        if other is None or not self.digest or not other.digest:
            return False
        return (self.schema_version == other.schema_version
                and self.covered_seq == other.covered_seq
                and self.event_count == other.event_count
                and self.digest == other.digest)


def coverage_watermark(events: Sequence[Event]) -> Optional[ReadModelWatermark]:
    """What a read model over *events* would carry, or None when it has no canonical coverage.

    The digest covers the ORDERED `(seq, type)` prefix rather than only the count and max seq,
    because a heal-truncate that rewrites a row can preserve both. It is deterministic over the
    authenticated log: the bytes are immutable once appended, so two readers of one file derive one
    digest.
    """
    rows = [[int(getattr(e, "seq", -1)), str(getattr(e, "type", ""))] for e in events]
    digest = canonical_json_digest(rows, prefix=DIGEST_PREFIX)
    if not digest:  # no canonical form -> no watermark; the artefact stays honestly `unknown`
        return None
    return ReadModelWatermark(
        schema_version=READMODEL_SCHEMA_VERSION,
        covered_seq=max((r[0] for r in rows), default=-1),
        event_count=len(rows),
        digest=digest,
    )


def _readonly_uri(path: Path) -> str:
    """`file:…?mode=ro` for *path*, quoted so a `?`/`#` in a run name cannot alter the query."""
    return "file:" + urllib.parse.quote(str(path)) + "?mode=ro"


def read_watermark(db_path: str | os.PathLike) -> Optional[ReadModelWatermark]:
    """The watermark *db_path* carries, or None for every shape this reader cannot trust.

    None covers: no file, not a SQLite database, no watermark table (a read model written before
    2026-08-14), a row count other than exactly one, a non-numeric/non-text column, and a schema
    version this build does not implement. Callers treat None as "not current" — never as "current
    by default".
    """
    p = Path(db_path)
    if not p.is_file():
        return None
    try:
        con = sqlite3.connect(_readonly_uri(p), uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(
            f"SELECT schema_version, covered_seq, event_count, digest, built_at "
            f"FROM {WATERMARK_TABLE}").fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if len(rows) != 1:  # a rebuild writes exactly one row; anything else is not this contract
        return None
    try:
        version, covered_seq, event_count, digest, built_at = rows[0]
        wm = ReadModelWatermark(
            schema_version=int(version), covered_seq=int(covered_seq),
            event_count=int(event_count), digest=str(digest), built_at=float(built_at))
    except (TypeError, ValueError):
        return None
    if wm.schema_version != READMODEL_SCHEMA_VERSION or not wm.digest:
        return None
    return wm


def readmodel_status(db_path: str | os.PathLike, events: Sequence[Event]) -> str:
    """`current` / `stale` / `unknown` for *db_path* against *events*. Never guesses `current`."""
    stored = read_watermark(db_path)
    if stored is None:
        return STATUS_UNKNOWN
    want = coverage_watermark(events)
    if want is None:
        return STATUS_UNKNOWN
    return STATUS_CURRENT if stored.same_coverage(want) else STATUS_STALE


def readmodel_is_current(db_path: str | os.PathLike, events: Sequence[Event]) -> bool:
    """The fail-closed predicate: only a PROVEN coverage match is current."""
    return readmodel_status(db_path, events) == STATUS_CURRENT


def _tri(v) -> int | None:
    """Tri-state -> nullable SQLite int: None (no audit) / 0 / 1."""
    return None if v is None else (1 if v else 0)


def build_readmodel(events: Iterable[Event], db_path: str | os.PathLike) -> RunState:
    rows = list(events)  # the watermark must describe EXACTLY the events that were folded
    st = fold(rows)
    watermark = coverage_watermark(rows)
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    try:
        cur = con.cursor()
        cur.execute("DROP TABLE IF EXISTS nodes")
        cur.execute(
            "CREATE TABLE nodes("
            "id INTEGER PRIMARY KEY, parent_ids TEXT, operator TEXT, "
            "metric REAL, status TEXT, is_best INTEGER, "
            "agent_ok INTEGER, agent_fell_back INTEGER)"  # external-agent audit (ADR-7)
        )
        for n in sorted(st.nodes.values(), key=lambda n: n.id):
            rep = n.agent_report or {}
            cur.execute(
                "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)",
                (
                    n.id,
                    ",".join(map(str, n.parent_ids)),
                    n.operator,
                    # robust_metric (confirmed-mean when present, else raw): the SAME value is_best is
                    # selected by and the digest ranks/shows by, so an external `ORDER BY metric` agrees
                    # with the is_best flag. Equals the raw metric for the common unconfirmed node.
                    n.robust_metric,
                    n.status.value,
                    1 if n.id == st.best_node_id else 0,
                    _tri(rep.get("ok")),
                    _tri(rep.get("fell_back")),
                ),
            )
        # Same transaction as the rows above: a watermark committed separately could survive a
        # rebuild whose rows did not, which is the one failure this artefact must not have.
        cur.execute(f"DROP TABLE IF EXISTS {WATERMARK_TABLE}")
        if watermark is not None:
            cur.execute(
                f"CREATE TABLE {WATERMARK_TABLE}("
                "schema_version INTEGER NOT NULL, covered_seq INTEGER NOT NULL, "
                "event_count INTEGER NOT NULL, digest TEXT NOT NULL, built_at REAL NOT NULL)")
            cur.execute(
                f"INSERT INTO {WATERMARK_TABLE} VALUES (?,?,?,?,?)",
                (watermark.schema_version, watermark.covered_seq, watermark.event_count,
                 watermark.digest, time.time()))
        con.commit()
    finally:
        con.close()
    return st


def publish_readmodel(events: Iterable[Event], path: str | os.PathLike) -> RunState:
    """Build the rebuildable SQLite projection off to the side, then atomically publish it.

    The ONE spelling of the atomic publish, shared by the engine's finalization
    (`engine/finalize.py::_build_readmodel_atomic`) and the on-demand `looplab readmodel` rebuild —
    a second copy would let the two disagree about which sidecar files a failed build leaves behind.
    `build_readmodel` is called through the module global so a test can substitute it here and reach
    BOTH callers.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        state = build_readmodel(events, tmp)
        os.replace(tmp, p)
        return state
    finally:
        for candidate in (tmp, Path(f"{tmp}-journal"), Path(f"{tmp}-wal"), Path(f"{tmp}-shm")):
            try:
                candidate.unlink()
            except OSError:
                pass
