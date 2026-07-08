"""Artifact discovery for the UI server (run files + repo paths). Extracted verbatim from
`serve/server.py` (BACKLOG §4) — the route handlers live in `serve/routers/runs.py`."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

# ----------------------------------------------------------------- artifacts (run files + repo paths)
# Surface the files a run produced. Two kinds of root: the run directory itself (events/snapshots, the
# per-node eval workdirs under nodes/<id>/, operator subdirs) AND — for a RepoTask — the host repo /
# reference / data paths the task declared, since a training command may write its outputs (checkpoints,
# submissions, logs) straight into the editable repo rather than under runs/. Both are read-only, walked
# with heavy/noise dirs pruned, and served with a path-traversal guard + a size cap. Pure helpers (no
# FastAPI) so the routes own the HTTP errors.
_ART_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "env", "node_modules", ".mypy_cache",
                  ".pytest_cache", ".ipynb_checkpoints", ".idea", ".vscode", ".tox", ".cache",
                  ".DS_Store", ".eggs"}
_ART_BIN_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz", ".pdf", ".zip",
                ".gz", ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".pyc", ".pyo", ".pyd", ".so",
                ".dll", ".dylib", ".o", ".a", ".bin", ".exe", ".pkl", ".pickle", ".joblib", ".pt",
                ".pth", ".ckpt", ".safetensors", ".onnx", ".pb", ".h5", ".hdf5", ".npy", ".npz",
                ".parquet", ".feather", ".arrow", ".db", ".sqlite", ".sqlite3", ".woff", ".woff2",
                ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav", ".ogg", ".avi", ".mov", ".mkv",
                ".jar", ".class", ".wasm"}
_ART_MAX_FILES = 1500          # per root — keep listings bounded even for a big repo / data dir
_ART_MAX_BYTES = 2_000_000     # 2 MB cap for an inline text view (the tail is dropped, `truncated` set)
_LOG_TAIL_MAX = 5_000_000      # hard cap on the client-controlled `tail` byte count for node_logs


def _art_expand(p: str) -> str:
    """Resolve ~ and $ENV the way RepoTask._expand_repo_paths does (task.snapshot.json is verbatim, so a
    natural `editable_path: "~/proj"` would otherwise be a literal dir that never exists)."""
    return os.path.expanduser(os.path.expandvars(p)) if isinstance(p, str) and p else p


def _artifact_roots(rd: Path) -> list[dict]:
    """Allowed artifact roots for a run: the run dir, plus any host repo / reference / data paths the
    task snapshot declares (RepoTask). Each is {id, label, base(Path resolved)}; only EXISTING dirs are
    returned, de-duplicated. The fixed id set is what the content route validates a request against, so a
    browser can never reach a path outside these roots."""
    roots = [{"id": "run", "label": "run directory", "base": rd}]
    snap = rd / "task.snapshot.json"
    if snap.exists():
        # Whole block is best-effort: a non-JSON / foreign / malformed snapshot (a `data` that isn't a
        # dict, a path with illegal chars) must degrade to "run dir only", never 500 the listing.
        try:
            data = json.loads(snap.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # A composable snapshot stores `repo`/`dataset` (not editable_path/data); normalize so
                # the file browser still exposes the repo + data mount roots for a composable run.
                from looplab.adapters.tasks import normalize_task
                data = normalize_task(data)
                if data.get("editable_path"):
                    p = _art_expand(data["editable_path"])
                    roots.append({"id": "editable:.", "label": f"repo: {Path(p).name or p}", "base": Path(p)})
                for e in data.get("editables") or []:
                    if isinstance(e, dict) and e.get("path") and e.get("name"):
                        roots.append({"id": f"editable:{e['name']}", "label": f"repo: {e['name']}",
                                      "base": Path(_art_expand(e["path"]))})
                for ref in data.get("references") or []:
                    if isinstance(ref, dict) and ref.get("path") and ref.get("name"):
                        roots.append({"id": f"reference:{ref['name']}", "label": f"ref: {ref['name']}",
                                      "base": Path(_art_expand(ref["path"]))})
                dm = data.get("data")
                if isinstance(dm, dict):
                    for name, p in dm.items():
                        if isinstance(name, str) and isinstance(p, str) and p:
                            roots.append({"id": f"data:{name}", "label": f"data: {name}",
                                          "base": Path(_art_expand(p))})
        except Exception:  # noqa: BLE001 — best-effort discovery; any parse error → no extra roots
            pass
    out: list[dict] = []
    seen: set = set()
    for r in roots:
        try:
            b = Path(r["base"]).resolve()
        except (OSError, ValueError):              # illegal-char path (esp. Windows) → skip
            continue
        if r["id"] in seen or b in seen or not b.is_dir():   # de-dup by id AND by resolved path
            continue
        seen.add(r["id"]); seen.add(b)
        out.append({**r, "base": b})
    return out


def _artifact_is_text(p: Path) -> bool:
    """Cheap text/binary guess for the LISTING (no file read). The content route re-checks authoritatively
    by sniffing for NUL bytes."""
    return p.suffix.lower() not in _ART_BIN_EXT


def _list_artifact_files(base: Path) -> tuple[list[dict], bool]:
    """Walk `base`, pruning heavy/noise dirs, capped at _ART_MAX_FILES. Returns (files, truncated). The
    walk is sorted (dirs + files) so a truncated listing is deterministic across calls/platforms rather
    than whatever arbitrary subset os.scandir happened to yield first."""
    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _ART_SKIP_DIRS)
        for fn in sorted(filenames):
            fp = Path(dirpath) / fn
            try:
                stt = fp.stat()                  # one stat (follows symlink; broken link → OSError → skip)
            except OSError:
                continue
            if not stat.S_ISREG(stt.st_mode):    # regular files only — skip fifos/sockets/dir symlinks
                continue
            out.append({"path": fp.relative_to(base).as_posix(), "size": stt.st_size,
                        "mtime": stt.st_mtime, "is_text": _artifact_is_text(fp)})
            if len(out) >= _ART_MAX_FILES:
                out.sort(key=lambda f: f["path"])
                return out, True
    out.sort(key=lambda f: f["path"])
    return out, False
