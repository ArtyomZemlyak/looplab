"""Run workspace file discovery for the UI server (run files + declared task paths). Extracted from
`serve/server.py` (BACKLOG §4) — the route handlers live in `serve/routers/runs.py`."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Callable, Optional

from looplab.core._pathsafe import looks_secret

# ----------------------------------------------------------------- artifacts (run files + repo paths)
# Surface files currently visible to a run. Two kinds of root: the run directory itself
# (events/snapshots, per-node eval workdirs, operator subdirs) AND — for a RepoTask — the live host repo /
# reference / data paths the task declared. The latter can contain inputs, later edits, and run outputs;
# without a start-time manifest this browser must not claim which file the run produced. Both are
# read-only, walked with heavy/noise dirs pruned, and served with a traversal guard + size cap.
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


_NODE_WORKDIR_RE = re.compile(r"^node_(\d+)$")
_TRACE_INTERNAL_BASES = (
    ".spans-append.jsonl", "spans.jsonl", "spans.index.jsonl", "trace.json", "tree.html")
ArtifactExposure = Callable[[Path, Optional[str], Optional[os.stat_result]], bool]
ArtifactListingTransform = Callable[[Path, dict], Optional[dict]]


class ArtifactPolicyUnavailable(OSError):
    """The server cannot prove that generic artifact access excludes trace internals."""


def _artifact_file_identity(stt: os.stat_result) -> Optional[tuple[int, int]]:
    """Return a usable same-file identity; zero inode means this filesystem cannot prove it.

    A deliberate SUBSET of `core/atomicio.file_identity`: the check here is "the path I validated is
    the file I am about to stream", which must hold across a file that legitimately grows mid-read;
    the canonical tuple would report a swap on every appended byte. None means unprovable, so the
    caller fails closed rather than trusting a zero inode as a match.
    """
    ino = int(getattr(stt, "st_ino", 0) or 0)
    if not ino:
        return None
    return (int(getattr(stt, "st_dev", 0) or 0), ino)


def _artifact_node_id(run_dir: Path, candidate: Path) -> Optional[int]:
    """Return the node id owning a canonical run-workspace file, if any.

    Resolve the target instead of trusting the caller's root id or lexical path: a declared task root
    or a file symlink can alias ``runs/<run>/nodes/node_<id>`` and must not bypass the attempt fence.
    ``None`` means the target is not inside an engine-owned node workdir (or disappeared mid-read).
    """
    try:
        run = Path(run_dir).resolve(strict=True)
        target = Path(candidate).resolve(strict=True)
        relative = target.relative_to(run)
    except (OSError, RuntimeError, ValueError):
        return None
    if (len(relative.parts) < 3
            or os.path.normcase(relative.parts[0]) != os.path.normcase("nodes")):
        return None
    node_dir = os.path.normcase(relative.parts[1])
    match = _NODE_WORKDIR_RE.fullmatch(node_dir)
    if match is None:
        return None
    try:
        node_id = int(match.group(1))
    except ValueError:
        return None
    # Engine paths are formatted from an integer, so reject alternate spellings such as node_01.
    return node_id if node_dir == os.path.normcase(f"node_{node_id}") else None


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
    # generic artifact routes must never become an alternate raw-trace API. Bind this to
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
        # Artifact inventory is owner-facing, but it is still a generic file browser over paths
        # copied from a task snapshot. Apply the same secret-name/secret-directory boundary as the
        # model-facing filesystem tools before either listing or reading a file.
        if request_path is not None and looks_secret(
                Path(request_path.replace("\\", "/"))):
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
        # a protected trace-family directory reserves its whole subtree, not only the
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
    """Allowed visible-file roots for a run: the run dir, plus any host repo / reference / data paths the
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
    try:
        canonical_run = rd.resolve()
        canonical_home = Path.home().resolve()
    except (OSError, RuntimeError, ValueError):
        canonical_run = rd
        canonical_home = None
    for r in roots:
        try:
            b = Path(r["base"]).resolve()
        except (OSError, ValueError):              # illegal-char path (esp. Windows) → skip
            continue
        if r["id"] in seen or b in seen or not b.is_dir():   # de-dup by id AND by resolved path
            continue
        if r["id"] != "run":
            # A task snapshot is run input, not an authorization manifest. Never let a declared path
            # promote a filesystem root, the server user's entire home, or an ancestor containing
            # this/all run directories into a browsable artifact root.
            anchor = Path(b.anchor) if b.anchor else None
            if ((anchor is not None and b == anchor)
                    or (canonical_home is not None and b == canonical_home)
                    or b == canonical_run or b in canonical_run.parents):
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
    transform: Optional[ArtifactListingTransform] = None,
    start: Optional[Path] = None,
    offset: int = 0,
    limit: Optional[int] = None,
) -> tuple[list[dict], bool]:
    """Walk `base`, pruning heavy/noise dirs, capped at _ART_MAX_FILES. Returns (files, truncated). The
    walk is sorted (dirs + files) so a truncated listing is deterministic across calls/platforms rather
    than whatever arbitrary subset os.scandir happened to yield first. An optional transform may
    decorate or reject an already-authorized file; rejection happens before it consumes the cap.

    PAGES, not one cut (UI review 2026-10-14 section 7.6): `offset` skips that many LISTED files of the
    same deterministic walk and `limit` bounds the page, so a root larger than one page is read page by
    page instead of ending at file 1,500 — the MiniOneRec run root ended inside its repository copy.
    `start` walks a SUBTREE of `base` (one node's workdir) while paths stay relative to `base`, so a
    file keeps the address the content route already accepts. The defaults are the old behaviour."""
    out: list[dict] = []
    skipped = 0
    cap = _ART_MAX_FILES if limit is None else limit     # read at CALL time (a patched cap holds)
    for dirpath, dirnames, filenames in os.walk(start if start is not None else base):
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
            item = {"path": relative, "size": stt.st_size,
                    "mtime": stt.st_mtime, "is_text": _artifact_is_text(fp)}
            if transform is not None:
                item = transform(fp, item)
                if item is None:
                    continue
            if skipped < offset:
                skipped += 1
                continue
            if len(out) >= cap:
                out.sort(key=lambda f: f["path"])
                return out, True
            out.append(item)
    out.sort(key=lambda f: f["path"])
    return out, False


# ---------------------------------------------------------------- artifact previews (doc 77 §3)
# Two read views beside the inline text one, and both stop at what a FILE says about itself:
#
#   * `/artifact/raw` serves the BYTES of a raster image only — PNG, JPEG, GIF, WebP, checked by
#     magic number as well as by suffix — so the browser's own decoder draws a chart the candidate
#     wrote. SVG is refused on purpose: it is a document that can carry script, and this origin is
#     the control plane. The response is `nosniff` + `Content-Security-Policy: sandbox;
#     default-src 'none'`, so even a mislabelled body cannot run as a page here.
#   * `/artifact/meta` describes ANY regular file without interpreting it as code: size, sha256
#     (bounded), a magic-number kind, and three structural headers that are pure data — the
#     safetensors JSON header, a zip central directory (the container PyTorch's `torch.save`
#     writes) and the `.npy` header dict. Nothing is unpickled, imported or executed; a `.pt`'s
#     pickle stays opaque bytes, which is the point.
ART_RAW_MAX_BYTES = 16 * 1024 * 1024          # one image inline; a bigger one is metadata only
ART_HASH_MAX_BYTES = 512 * 1024 * 1024        # sha256 is skipped (and SAID) over this size
SAFETENSORS_HEADER_MAX = 16 * 1024 * 1024     # the format's own readers refuse ~100 MB; 16 is ample
_META_LIST_CAP = 200

ART_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".gif": "image/gif", ".webp": "image/webp"}


def sniff_kind(head: bytes) -> Optional[str]:
    """A file's kind from its first bytes, or None. Magic numbers only — never the suffix."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head.startswith(b"\x93NUMPY"):
        return "npy"
    if head.startswith(b"\x89HDF\r\n\x1a\n"):
        return "hdf5"
    if head[:2] in (b"\x1f\x8b",):
        return "gzip"
    if head[:1] == b"\x80" and len(head) > 1 and head[1:2] in (b"\x02", b"\x03", b"\x04", b"\x05"):
        return "pickle"
    return None


_IMAGE_KIND_MIME = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif",
                    "webp": "image/webp"}


def raster_mime(path: str, head: bytes) -> Optional[str]:
    """The MIME type to serve a raster image as, or None when suffix and magic do not AGREE."""
    declared = ART_IMAGE_MIME.get(Path(str(path)).suffix.lower())
    sniffed = _IMAGE_KIND_MIME.get(sniff_kind(head) or "")
    return declared if declared is not None and declared == sniffed else None


def preview_mime(path: str, head: bytes) -> Optional[str]:
    """`raster_mime`, plus PDF (doc 77 §3): a `.pdf` whose bytes begin `%PDF-`. The browser's own PDF
    viewer draws it from an object URL, under the same `nosniff` + sandboxing CSP as an image — a PDF
    is a document for that viewer, never HTML or script in this origin. SVG and HTML stay refused."""
    mime = raster_mime(path, head)
    if mime is not None:
        return mime
    if Path(str(path)).suffix.lower() == ".pdf" and head[:5] == b"%PDF-":
        return "application/pdf"
    return None


def safetensors_summary(f, size: int) -> Optional[dict]:
    """The safetensors header as a bounded summary, read from an open binary file at offset 0.

    Layout: an 8-byte little-endian header length, then that many bytes of JSON mapping tensor
    name -> {dtype, shape, data_offsets}, plus an optional `__metadata__` string map. None when the
    bytes are not that shape."""
    import json as _json
    import math as _math
    f.seek(0)
    raw_len = f.read(8)
    if len(raw_len) != 8:
        return None
    n = int.from_bytes(raw_len, "little")
    if n <= 1 or n > SAFETENSORS_HEADER_MAX or n + 8 > size:
        return None
    body = f.read(n)
    if len(body) != n or body[:1] != b"{":
        return None
    try:
        from looplab.core.jsonutil import bounded_json_loads
        doc = bounded_json_loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, _json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    meta = doc.get("__metadata__")
    tensors, dtypes, params, sample = 0, {}, 0, []
    for name, spec in doc.items():
        if name == "__metadata__" or not isinstance(spec, dict):
            continue
        shape = spec.get("shape")
        dtype = str(spec.get("dtype") or "?")[:16]
        if not isinstance(shape, list) or not all(isinstance(d, int) and d >= 0 for d in shape):
            return None
        tensors += 1
        dtypes[dtype] = dtypes.get(dtype, 0) + 1
        params += _math.prod(shape) if shape else 1
        if len(sample) < 50:
            sample.append({"name": str(name)[:200], "dtype": dtype, "shape": shape[:8]})
    return {
        "header_bytes": n, "tensor_count": tensors, "parameter_count": params, "dtypes": dtypes,
        "metadata": ({str(k)[:100]: str(v)[:300] for k, v in list(meta.items())[:32]}
                     if isinstance(meta, dict) else None),
        "tensors_sample": sample, "tensors_truncated": tensors > len(sample),
    }


def zip_summary(f) -> Optional[dict]:
    """Member names/sizes from a zip central directory (a `torch.save` file is one). No member is
    decompressed and nothing is unpickled."""
    import zipfile
    try:
        f.seek(0)
        with zipfile.ZipFile(f) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        return None
    names = [{"name": i.filename[:300], "size": i.file_size} for i in infos[:_META_LIST_CAP]]
    pickled = any(i.filename.endswith(".pkl") or i.filename.endswith("/data.pkl") for i in infos)
    return {"members": len(infos), "entries": names, "entries_truncated": len(infos) > len(names),
            "pickle_inside": pickled}


def npy_summary(f) -> Optional[dict]:
    """dtype / shape / order from a `.npy` header — a Python literal dict, read with
    `ast.literal_eval`, which evaluates literals and nothing else."""
    import ast
    f.seek(0)
    head = f.read(10)
    if len(head) < 10 or not head.startswith(b"\x93NUMPY"):
        return None
    major = head[6]
    if major == 1:
        hlen = int.from_bytes(head[8:10], "little")
    else:
        more = f.read(2)
        if len(more) != 2:
            return None
        hlen = int.from_bytes(head[8:10] + more, "little")
    if hlen > 65536:
        return None
    try:
        header = ast.literal_eval(f.read(hlen).decode("latin-1").strip())
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    if not isinstance(header, dict):
        return None
    return {"dtype": str(header.get("descr"))[:64], "shape": list(header.get("shape") or ())[:16],
            "fortran_order": bool(header.get("fortran_order"))}


def artifact_meta(f, size: int, path: str) -> dict:
    """Everything `/artifact/meta` says about one opened file. Reads only; total over bad input."""
    import hashlib
    f.seek(0)
    head = f.read(64)
    kind = sniff_kind(head)
    out: dict = {"size": size, "kind": kind, "sha256": None}
    if size <= ART_HASH_MAX_BYTES:
        f.seek(0)
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
        out["sha256"] = h.hexdigest()
    else:
        out["sha256_skipped"] = "too_large"
    suffix = Path(str(path)).suffix.lower()
    if suffix == ".safetensors" or (kind is None and suffix in (".bin", ".st")):
        summary = safetensors_summary(f, size)
        if summary is not None:
            out["kind"] = "safetensors"
            out["safetensors"] = summary
    if kind == "zip":
        out["zip"] = zip_summary(f)
    if kind == "npy":
        out["npy"] = npy_summary(f)
    image_mime = raster_mime(path, head)
    out["inline_image"] = bool(image_mime) and size <= ART_RAW_MAX_BYTES
    if image_mime:
        out["mime"] = image_mime
    return out
