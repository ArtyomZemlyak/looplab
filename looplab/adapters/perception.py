"""Bounded, deterministic PERCEPTION of on-disk task data — the helpers `dataset_task` grew for the
grounding pre-phase, shared with `repo_task` since 2026-09-06 (doc 52 row 17; doc 51 §6).

The engine profiles a task's data at setup when the adapter exposes `columns()`, appends
`data_profiled`, folds it onto `RunState.data_profile`, and `search/foresight.py::verified_report`
primes predict-before-execute with it; `tools/run_tools.py::DataTools` serves `data_schema` /
`data_profile` / `read_asset` off the same two hooks (`columns`, `data_samples`). Six adapters
implemented them and the repo task — the family every real GPU run on the operator's box uses —
implemented neither, so on those runs the perception layer was OFF: `state.data_profile` stayed
`None` and the Researcher decided what to try from scalars alone (OmniScientist measured direct
perception winning 85 % of head-to-head judgments over precomputed scalar features).

Every reader here is BOUNDED (rows, bytes, tables, columns) and reads only what the operator
DECLARED (`data:` mounts — the same sources `runtime/read_allowlist.py` already sanctions), never
executes anything, and never raises: an unreadable or non-tabular source is skipped, which the
caller records as an empty profile rather than a guess.
"""
from __future__ import annotations

import csv
import json
import os
from typing import Optional

SAMPLE_CHARS = 65536        # head-sample cap per source for the read-only DataTools preview (≤64 KiB)
DIR_LISTING_MAX = 100       # max entries shown when previewing a directory source
SAMPLE_ROWS = 200           # rows a repo-task profile reads per table (the dataset kind has its own knob)
MAX_COLUMNS = 64            # columns a repo-task profile keeps across all its mounts
MAX_TABLES = 4              # tables a repo-task profile reads across all its mounts
MAX_JSON_BYTES = 64 << 20   # a `.json` table is loaded whole; above this it is skipped, not slurped
TABULAR_SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".parquet")


def head_sample(path: str) -> str:
    """A bounded head sample of a text file: at most ``SAMPLE_CHARS`` chars, trimmed back to the
    last complete line when the file is longer — so a CSV/TSV preview never ends on a half-row that
    would mislead the schema/profile parser (and read_asset shows whole lines). Reads only one char
    past the cap to detect truncation, so a multi-GB file is never slurped."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read(SAMPLE_CHARS + 1)
    if len(text) > SAMPLE_CHARS:               # file is longer than the cap → we truncated it
        text = text[:SAMPLE_CHARS]
        nl = text.rfind("\n")
        if nl > 0:                              # drop the dangling partial last line (keep the rest)
            text = text[:nl + 1]
    return text


def file_sample(path: str) -> str:
    """`head_sample` for a source that may be BINARY (a checkpoint, a parquet shard, an archive):
    a NUL byte in the first 4 KiB answers with the size instead of 64 KiB of replacement chars."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if b"\x00" in head:
        return f"(binary file, {os.path.getsize(path)} bytes)"
    return head_sample(path)


def add_sample(out: dict[str, str], key: str, text: str) -> None:
    """Insert a preview sample under `key`, de-duplicating by suffixing `_2`/`_3`… before the
    extension so two sources that share a basename don't clobber each other (and a `.csv` suffix —
    which the schema/profile fallback keys off — is preserved)."""
    if key in out:
        base, ext = os.path.splitext(key)
        i = 2
        while f"{base}_{i}{ext}" in out:
            i += 1
        key = f"{base}_{i}{ext}"
    out[key] = text


def coerce_cell(v):
    """Coerce a CSV cell string to int/float when it is fully numeric, else leave it as-is. CSV cells
    are always strings, so without this the grounding profiler would label every numeric column as
    categorical (it keys numeric-ness off real int/float values)."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return v
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return v


def dir_listing(entries: list[str]) -> str:
    shown = entries[:DIR_LISTING_MAX]
    more = ("" if len(entries) <= DIR_LISTING_MAX
            else f"\n… (+{len(entries) - DIR_LISTING_MAX} more)")
    plural = "entry" if len(entries) == 1 else "entries"
    return f"directory: {len(entries)} {plural}\n" + "\n".join(shown) + more


def primary_table(dirpath: str, entries: list[str],
                  suffixes: tuple[str, ...] = (".csv", ".tsv")) -> Optional[str]:
    """The most representative table among a directory's already-listed ``entries`` — prefer
    ``train*``, else the first with one of ``suffixes`` — as a FILENAME, or None."""
    files = [f for f in entries if f.lower().endswith(suffixes)]
    if not files:
        return None
    pick = next((f for f in files if f.lower().startswith("train")), None) or files[0]
    return pick if os.path.isfile(os.path.join(dirpath, pick)) else None


def mount_table(path: str) -> Optional[str]:
    """The table a declared mount offers for profiling: the file itself when it is tabular, else
    the primary table at the top level of a directory mount, else None."""
    try:
        if os.path.isfile(path):
            return path if path.lower().endswith(TABULAR_SUFFIXES) else None
        if os.path.isdir(path):
            pick = primary_table(path, sorted(os.listdir(path)), TABULAR_SUFFIXES)
            return os.path.join(path, pick) if pick else None
    except OSError:
        return None
    return None


def _dedupe_header(header: list[str]) -> list[str]:
    # De-duplicate header names (suffix _2, _3, …) so two physical columns sharing a name don't
    # collapse into one key with interleaved doubled samples that misprofile both.
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, h in enumerate(header):
        name = h or f"col{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def tabular_columns(path: str, n: int, *, max_json_bytes: Optional[int] = None) -> dict[str, list]:
    """The first ``n`` rows of a tabular file as ``{column: values}`` — CSV/TSV (cells coerced),
    JSON (a column-oriented dict of lists, or a list of row dicts), JSONL (row dicts) and, when
    `pyarrow` is importable, one Parquet batch. ``{}`` for anything else, anything unreadable, or a
    ``.json`` above ``max_json_bytes`` (that reader loads the whole document)."""
    p = str(path)
    low = p.lower()
    n = max(1, int(n))
    try:
        if low.endswith((".csv", ".tsv")):
            with open(p, newline="", encoding="utf-8-sig", errors="replace") as f:
                reader = csv.reader(f, delimiter="\t" if low.endswith(".tsv") else ",")
                rows = []
                for i, row in enumerate(reader):
                    rows.append(row)
                    if i >= n:                  # header (row 0) + n data rows
                        break
            if len(rows) < 2:
                return {}
            header = _dedupe_header(rows[0])
            cols: dict[str, list] = {h: [] for h in header}
            for row in rows[1:]:
                for h, val in zip(header, row):
                    cols[h].append(coerce_cell(val))
            return cols
        if low.endswith(".json"):
            if max_json_bytes is not None and os.path.getsize(p) > max_json_bytes:
                return {}
            with open(p, encoding="utf-8-sig", errors="replace") as f:
                obj = json.load(f)
            if isinstance(obj, dict) and obj and all(isinstance(v, list) for v in obj.values()):
                return {k: list(v)[:n] for k, v in obj.items()}
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                keys = list(obj[0].keys())   # guard non-dict rows: a mixed list must not crash
                return {k: [r.get(k) if isinstance(r, dict) else None for r in obj[:n]]
                        for k in keys}
            return {}
        if low.endswith(".jsonl"):
            rows_d: list[dict] = []
            with open(p, encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    if len(rows_d) >= n:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        rows_d.append(obj)
            if not rows_d:
                return {}
            keys = list(rows_d[0].keys())
            return {k: [r.get(k) for r in rows_d] for k in keys}
        if low.endswith(".parquet"):
            try:
                import pyarrow.parquet as pq
            except Exception:  # noqa: BLE001 — an optional reader; without it the table is skipped
                return {}
            batch = next(pq.ParquetFile(p).iter_batches(batch_size=n), None)
            if batch is None:
                return {}
            return {name: batch.column(i).to_pylist() for i, name in enumerate(batch.schema.names)}
    except (OSError, ValueError, TypeError, AttributeError, csv.Error):
        return {}
    return {}
