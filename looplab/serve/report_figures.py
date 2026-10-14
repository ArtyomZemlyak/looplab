"""Report FIGURES: images attached to `report_generated`, stored content-addressed beside the run.

Review 2026-10-14 §6: the agent had real plots and nowhere in the report to put them. A figure is
uploaded FROM AN ARTIFACT the run already exposes — `POST /api/runs/{id}/report-figures` reads it
through the same fences as `/artifact/raw` (root, traversal, node attempt, exposure policy, the
opened descriptor's identity) and accepts a raster image only, suffix and magic number agreeing.
The bytes are copied to `<run>/report_figures/<sha256>.<ext>`, so a figure outlives a rebuilt
workdir and a later report can never show different pixels under the same id; the report carries
only `{figure_id, caption, mime, source}` (`core/advisory_payloads.py::_report_figures`).

Content addressing makes the write idempotent (the same image twice is one file) and safe to do
before the report command is admitted: an upload no report names is an orphan file, nothing more.
Served back with `nosniff` and a sandboxing CSP, never as HTML or SVG.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional

from looplab.core.atomicio import atomic_write_bytes

FIGURES_DIRNAME = "report_figures"
FIGURE_MAX_BYTES = 16 * 1024 * 1024
_FIGURE_ID = re.compile(r"[0-9a-f]{64}")
_EXT_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
_MIME_BY_EXT = {ext: mime for mime, ext in _EXT_BY_MIME.items()}


def valid_figure_id(value) -> bool:
    return isinstance(value, str) and bool(_FIGURE_ID.fullmatch(value))


def store_figure(run_dir: Path, body: bytes, mime: str) -> str:
    """Write the image under its sha256 (once) and return that id."""
    ext = _EXT_BY_MIME.get(mime)
    if ext is None or not body or len(body) > FIGURE_MAX_BYTES:
        raise ValueError("unsupported figure")
    figure_id = hashlib.sha256(body).hexdigest()
    folder = Path(run_dir) / FIGURES_DIRNAME
    folder.mkdir(exist_ok=True)
    target = folder / f"{figure_id}.{ext}"
    if not target.is_file() or target.stat().st_size != len(body):
        atomic_write_bytes(target, body)
    return figure_id


def figure_file(run_dir: Path, figure_id) -> Optional[tuple[Path, str]]:
    """`(path, mime)` of a stored figure, or None. Never follows a link out of the folder."""
    if not valid_figure_id(figure_id):
        return None
    folder = Path(run_dir) / FIGURES_DIRNAME
    for ext, mime in _MIME_BY_EXT.items():
        path = folder / f"{figure_id}.{ext}"
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not os.path.islink(path) and path.is_file() and st.st_size <= FIGURE_MAX_BYTES:
            return path, mime
    return None


def read_figure(run_dir: Path, figure_id) -> Optional[tuple[bytes, str]]:
    """The stored bytes, re-verified against their own id (a tampered file is not served)."""
    found = figure_file(run_dir, figure_id)
    if found is None:
        return None
    path, mime = found
    try:
        body = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(body).hexdigest() != figure_id:
        return None
    return body, mime
