"""Artifact discovery for the UI server (run files + repo paths). Extracted verbatim from
`serve/server.py` (BACKLOG §4) — the route handlers live in `serve/routers/runs.py`."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Callable, Optional

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


_TRACE_INTERNAL_BASES = ("spans.jsonl", "spans.index.jsonl", "trace.json", "tree.html")
ArtifactExposure = Callable[[Path, Optional[str], Optional[os.stat_result]], bool]


class ArtifactPolicyUnavailable(OSError):
    """The server cannot prove that generic artifact access excludes trace internals."""


def _artifact_file_identity(stt: os.stat_result) -> Optional[tuple[int, int]]:
    """Return a usable same-file identity; zero inode means this filesystem cannot prove it."""
    ino = int(getattr(stt, "st_ino", 0) or 0)
    if not ino:
        return None
    return (int(getattr(stt, "st_dev", 0) or 0), ino)


def _trace_internal_name(name: str) -> bool:
    """Match run-root trace sources, derived views, archives, and atomic-write temporaries."""
    if not isinstance(name, str):
        return False
    normalized = name.rstrip(" .").casefold()
    return any(
        normalized == base
        or normalized.startswith(f"{base}.")
        or (normalized.startswith(f".{base}.") and normalized.endswith(".tmp"))
        for base in _TRACE_INTERNAL_BASES
    )


def _unambiguous_artifact_path(relative_path: Optional[str]) -> bool:
    """Reject Windows aliases/ADS before a generic artifact path reaches content reads."""
    if relative_path is None:
        return True
    if not isinstance(relative_path, str) or "\x00" in relative_path:
        return False
    # Treat both slash forms as separators on every host. A deployment moved between POSIX and
    # Windows must not acquire a second, less strict interpretation of the same URL.
    for component in relative_path.replace("\\", "/").split("/"):
        if ":" in component or component.endswith((" ", ".")):
            return False
    return True


def _artifact_exposure_policy(run_dir: Path) -> ArtifactExposure:
    """Build a per-request fail-closed boundary for generic artifact discovery/content.

    Artifact roots may overlap the run directory, so authorization follows the canonical target and
    file identity rather than the caller's root id or requested basename. Identity comparison catches
    hardlinks and platform aliases; canonical comparison catches symlinks. A separately generated file
    with the same basename outside the run remains a normal artifact.
    """
    # Generic artifact routes must never become an alternate raw-trace API. Bind this decision to
    # canonical paths and file identities so symlink/hardlink aliases fail closed too.
    try:
        run = Path(run_dir).resolve(strict=True)
        entries = list(run.iterdir())
    except (OSError, RuntimeError, ValueError):
        raise ArtifactPolicyUnavailable("artifact exposure policy unavailable") from None

    protected_paths: set[Path] = set()
    protected_ids: set[tuple[int, int]] = set()
    for entry in entries:
        if not _trace_internal_name(entry.name):
            continue
        try:
            target = entry.resolve(strict=True)
            stt = target.stat()
        except FileNotFoundError:
            # A protected entry disappeared between enumeration and identity capture. Its aliases
            # may still exist, so the hardlink proof is incomplete for this request: fail closed.
            raise ArtifactPolicyUnavailable("artifact exposure policy changed") from None
        except (OSError, RuntimeError, ValueError):
            raise ArtifactPolicyUnavailable("artifact exposure policy unavailable") from None
        protected_paths.add(target)
        if stat.S_ISREG(stt.st_mode):
            identity = _artifact_file_identity(stt)
            if identity is None:
                raise ArtifactPolicyUnavailable(
                    "artifact filesystem identity unavailable")
            protected_ids.add(identity)

    def exposed(
        candidate: Path,
        request_path: Optional[str] = None,
        stat_result: Optional[os.stat_result] = None,
    ) -> bool:
        if not _unambiguous_artifact_path(request_path):
            return False
        try:
            candidate_path = Path(candidate)
            lexical_parent = candidate_path.parent.resolve(strict=True)
            if lexical_parent == run and _trace_internal_name(candidate_path.name):
                return False
            target = candidate_path.resolve(strict=True)
            stt = stat_result if stat_result is not None else target.stat()
        except (OSError, RuntimeError, ValueError):
            return False

        # Reserve direct run-root family names even when a new atomic temp appeared after the policy
        # snapshot. Existing canonical paths and file identities close symlink/hardlink aliases.
        if target.parent == run and _trace_internal_name(target.name):
            return False
        # A protected trace-family directory reserves its whole subtree, not only the directory
        # entry itself. Otherwise `trace.json.backup/secret.txt` becomes a raw-trace side channel.
        if any(target == protected or protected in target.parents
               for protected in protected_paths):
            return False
        identity = _artifact_file_identity(stt)
        if identity is not None and identity in protected_ids:
            return False
        return stat.S_ISREG(stt.st_mode)

    return exposed


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
                        pp = p.get("path") if isinstance(p, dict) else p   # DataSpec dict | bare path
                        if isinstance(name, str) and isinstance(pp, str) and pp:
                            roots.append({"id": f"data:{name}", "label": f"data: {name}",
                                          "base": Path(_art_expand(pp))})
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
        seen.add(r["id"])
        seen.add(b)
        out.append({**r, "base": b})
    return out


def _artifact_is_text(p: Path) -> bool:
    """Cheap text/binary guess for the LISTING (no file read). The content route re-checks authoritatively
    by sniffing for NUL bytes."""
    return p.suffix.lower() not in _ART_BIN_EXT


def _list_artifact_files(
    base: Path,
    *,
    exposed: Optional[ArtifactExposure] = None,
) -> tuple[list[dict], bool]:
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
            relative = fp.relative_to(base).as_posix()
            # Filter before the listing cap: hidden trace internals must not displace legitimate files
            # from a deterministic 1500-entry response. Direct content re-checks the same policy.
            if exposed is not None and not exposed(fp, relative, stt):
                continue
            out.append({"path": relative, "size": stt.st_size,
                        "mtime": stt.st_mtime, "is_text": _artifact_is_text(fp)})
            if len(out) >= _ART_MAX_FILES:
                out.sort(key=lambda f: f["path"])
                return out, True
    out.sort(key=lambda f: f["path"])
    return out, False
