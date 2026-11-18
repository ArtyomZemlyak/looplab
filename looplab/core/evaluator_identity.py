"""Canonical evaluator identity and cross-node canary equivalence digests.

Why this lives in `core`:
- Both the admission layer (`serve`/`harness`) and the evaluation engine (`engine`) must derive
  byte-identical evaluator digests over the task's resolved evaluation contract, protected base
  files, declared data/reference mounts, protocol `data_content` receipts and task assets, with
  no upward imports.
- The same canonical `evaluator_digest` powers two invariants at once:
  1. Attempt-bound candidate evaluator fencing (`expected_evaluator_digest`, doc 84 §6.1): a
     candidate pinned to an evaluator contract fails closed at admission AND before evaluation
     dispatch / GPU lease acquisition when the task command, environment, inputs or protected
     scorer files move underneath it.
  2. Safe cross-node canary reuse (`equivalence_digest`, doc 69 69.31): two nodes share a passed
     canary if and only if their code/files, substrate state, evaluator digest, canary spec,
     `node_kind` and lifecycle-pinned `uses` inputs are identical.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat as _stat
from pathlib import Path
from typing import Any, Optional

import orjson

from looplab.core.pathsafe import contained_member

EVALUATOR_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_PROTECTED_HASH_BYTES = 16 * 1024 * 1024
_MAX_PROTECTED_MEMBERS = 4096
_CMD_FILE_SUFFIXES = (".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".env", ".cfg", ".ini")
_CONFIG_FLAGS = frozenset({"--config", "--config-file", "--cfg"})



def normalize_expected_evaluator_digest(value: Any) -> Optional[str]:
    """Validate an optional 64-hex SHA-256 `expected_evaluator_digest`."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected_evaluator_digest must be a 64-character hex SHA-256 string")
    cleaned = value.strip().lower()
    if EVALUATOR_DIGEST_RE.fullmatch(cleaned) is None:
        raise ValueError("expected_evaluator_digest must be a 64-character hex SHA-256 string")
    return cleaned


def _json_safe(obj: Any) -> Any:
    """Deterministic JSON-serializable projection for spec dictionaries."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return {"__bytes_sha256__": hashlib.sha256(obj).hexdigest(), "len": len(obj)}
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((_json_safe(v) for v in obj), key=lambda v: orjson.dumps(v).decode("utf-8"))
    if callable(obj):
        mod = getattr(obj, "__module__", "") or ""
        qual = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None) or type(obj).__name__
        return {"__callable__": f"{mod}:{qual}"}
    return str(obj)


def _hash_regular_file(path: Path) -> str:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
        return f"non_regular:{_stat.S_IFMT(st.st_mode)}"
    if st.st_size > _MAX_PROTECTED_HASH_BYTES:
        return "unavailable:oversized"
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return "unreadable"
    return h.hexdigest()


def protected_files_digest_map(repo_spec: Optional[dict]) -> dict[str, str]:
    """Content digests of protected source members, respecting workspace namespaces and globs."""
    if not isinstance(repo_spec, dict) or not repo_spec:
        return {}
    out: dict[str, str] = {}
    editables = [ed for ed in repo_spec.get("editables") or () if isinstance(ed, dict) and ed.get("path")]
    for ed in editables:
        ed_name = str(ed.get("name") or ".")
        prefix = "" if ed_name in (".", "") else ed_name.rstrip("/") + "/"
        root = Path(os.path.expanduser(os.path.expandvars(str(ed["path"]))))
        # protected_names are WORKSPACE names; editable protect entries are SOURCE relative.
        patterns = set(str(p).replace("\\", "/") for p in ed.get("protect") or ())
        for raw in repo_spec.get("protected_names") or ():
            name = str(raw).replace("\\", "/")
            owners = [other for other in editables if str(other.get("name") or ".") not in (".", "")
                      and name.startswith(str(other["name"]).rstrip("/") + "/")]
            if owners:
                owner = max(owners, key=lambda other: len(str(other["name"])))
                if owner is not ed:
                    continue
            elif prefix:
                continue
            patterns.add(name[len(prefix):] if prefix else name)
        for rel in sorted(patterns):
            if not root.is_dir():
                out[f"{ed_name}:{rel}"] = "root_missing"
                continue
            try:
                matches = sorted(root.glob(rel)) if any(c in rel for c in "*?[") else [root / rel]
                if not matches:
                    out[f"{ed_name}:{rel}"] = "missing"
                for match in matches:
                    target = contained_member(root, match.relative_to(root).as_posix())
                    if target is None:
                        out[f"{ed_name}:{rel}"] = "outside_root"
                        continue
                    members = target.rglob("*") if target.is_dir() else [target]
                    for index, member in enumerate(members):
                        if index >= _MAX_PROTECTED_MEMBERS:
                            out[f"{ed_name}:{rel}"] = "unavailable:member_limit"
                            break
                        if member.is_dir() and not member.is_symlink():
                            continue
                        key = f"{ed_name}:{member.relative_to(root).as_posix()}"
                        out[key] = _hash_regular_file(member)
            except (OSError, ValueError, RuntimeError):
                out[f"{ed_name}:{rel}"] = "unreadable"
    return out


def protocol_data_content(run_dir: Optional[Path], run_uid: Optional[str] = None) -> dict[str, Any]:
    """Latest recorded `data_content` fingerprints from `protocol_amendments.jsonl`, if any."""
    if run_dir is None:
        return {}
    journal = Path(run_dir) / "protocol_amendments.jsonl"
    if not journal.is_file():
        return {}
    out: dict[str, Any] = {}
    try:
        raw_lines = journal.read_bytes().splitlines()
    except OSError:
        return {"__journal_error__": "unreadable"}
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = orjson.loads(line)
        except ValueError:
            return {"__journal_error__": "corrupt"}
        if not isinstance(row, dict):
            continue
        if run_uid and row.get("run_uid") and row.get("run_uid") != run_uid:
            continue
        if row.get("kind") != "task":
            continue
        payload = row.get("payload")
        content = payload.get("data_content") if isinstance(payload, dict) else None
        if isinstance(content, dict):
            for name, pair in content.items():
                if isinstance(pair, dict):
                    out[str(name)] = pair.get("after")
    return out


def command_referenced_files(
    eval_spec: Optional[dict], repo_spec: Optional[dict] = None
) -> dict[str, str]:
    """Content digests of script and `--config` files explicitly named in `eval_spec` commands."""
    if not isinstance(eval_spec, dict) or not eval_spec:
        return {}
    argvs: list[list[str]] = []
    cmd = eval_spec.get("command")
    if isinstance(cmd, (list, tuple)):
        argvs.append([str(x) for x in cmd if isinstance(x, str) and x])
    for st in eval_spec.get("stages") or ():
        if isinstance(st, dict) and isinstance(st.get("command"), (list, tuple)):
            argvs.append([str(x) for x in st["command"] if isinstance(x, str) and x])
    if not argvs:
        return {}
    candidates: list[str] = []
    for argv in argvs:
        idx = 0
        while idx < len(argv):
            tok = argv[idx]
            if tok in _CONFIG_FLAGS and idx + 1 < len(argv):
                candidates.append(argv[idx + 1])
                idx += 2
                continue
            if "=" in tok:
                flag, _, rhs = tok.partition("=")
                if flag in _CONFIG_FLAGS and rhs:
                    candidates.append(rhs)
                    idx += 1
                    continue
            if not tok.startswith("-") and tok.lower().endswith(_CMD_FILE_SUFFIXES):
                candidates.append(tok)
            idx += 1
    if not candidates:
        return {}
    default_root: Optional[Path] = None
    if isinstance(repo_spec, dict):
        editables = [ed for ed in (repo_spec.get("editables") or ()) if isinstance(ed, dict) and ed.get("path")]
        chosen = next((ed for ed in editables if str(ed.get("name") or ".") in (".", "")), editables[0] if editables else None)
        if chosen is not None:
            default_root = Path(os.path.expanduser(os.path.expandvars(str(chosen["path"]))))
    out: dict[str, str] = {}
    for raw in sorted(set(candidates)):
        expanded = os.path.expanduser(os.path.expandvars(raw))
        if os.path.isabs(expanded):
            out[f"abs:{expanded}"] = _hash_regular_file(Path(expanded))
        elif default_root is not None:
            rel = raw.replace("\\", "/")
            while rel.startswith("./"):
                rel = rel[2:]
            if not rel:
                continue
            if not default_root.is_dir():
                out[f"rel:{rel}"] = "root_missing"
                continue
            target = contained_member(default_root, rel)
            out[f"rel:{rel}"] = "outside_root" if target is None else _hash_regular_file(target)
    return out


def evaluator_digest_from_specs(
    *,
    eval_spec: Optional[dict],
    repo_spec: Optional[dict] = None,
    assets: Optional[dict] = None,
    data_content: Optional[dict] = None,
) -> str:
    """Canonical 64-hex SHA-256 over the resolved evaluator contract and protected base inputs."""
    repo_summary: dict[str, Any] = {}
    if isinstance(repo_spec, dict) and repo_spec:
        editables = [
            {"name": str(ed.get("name") or "."), "path": str(ed.get("path") or "")}
            for ed in (repo_spec.get("editables") or ())
            if isinstance(ed, dict)
        ]
        refs = [
            {k: ref.get(k) for k in ("name", "path", "mount") if k in ref}
            for ref in (repo_spec.get("references") or ())
            if isinstance(ref, dict)
        ]
        repo_summary = {
            "editables": editables,
            "data": _json_safe(repo_spec.get("data") or {}),
            "references": _json_safe(refs),
            "protected_names": sorted(str(p).replace("\\", "/") for p in (repo_spec.get("protected_names") or ())),
            "task_python": repo_spec.get("task_python"),
            "protected_files_sha256": protected_files_digest_map(repo_spec),
        }
    cmd_files = command_referenced_files(eval_spec, repo_spec)
    asset_hashes: dict[str, str] = {}
    for name, content in sorted((assets or {}).items(), key=lambda kv: str(kv[0])):
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        asset_hashes[str(name)] = hashlib.sha256(raw).hexdigest()
    # A failed content observation is never a stable evaluator identity.
    for digest_map in (repo_summary.get("protected_files_sha256", {}), cmd_files):
        if any(value in ("unreadable", "outside_root") or value.startswith(("non_regular:", "unavailable:"))
               for value in digest_map.values()):
            return ""
    if "__journal_error__" in (data_content or {}):
        return ""
    material = {
        "v": 1,
        "eval_spec": _json_safe(eval_spec or {}),
        "repo_spec": repo_summary,
        "command_files_sha256": cmd_files,
        "assets_sha256": asset_hashes,
        "data_content": _json_safe(data_content or {}),
    }
    return hashlib.sha256(orjson.dumps(material, option=orjson.OPT_SORT_KEYS)).hexdigest()


def evaluator_mismatch_detail(expected: Optional[str], current: Optional[str]) -> Optional[dict]:
    """Structured fail-closed refusal when `expected_evaluator_digest` does not match `current`."""
    if expected is None:
        return None
    if not current or expected != current:
        shown = current if current else "unavailable"
        return {
            "code": "stale_evaluator_digest",
            "message": (
                f"expected_evaluator_digest {expected[:12]}... does not match the run's current "
                f"evaluator digest ({shown[:12] if current else shown})"
            ),
            "expected_evaluator_digest": expected,
            "current_evaluator_digest": current,
            "remediation": (
                "reread harness-progress.current_evaluator_digest and task.snapshot.json before "
                "resubmitting under a new candidate key"
            ),
            "retryable": False,
        }
    return None


def canary_equivalence_digest(
    *,
    code: Optional[str],
    files: Optional[dict],
    deleted: Optional[list],
    activation_manifest_name: str,
    evaluator_digest: Optional[str],
    canary_spec: Optional[dict],
    node_kind: Optional[str],
    uses: Optional[list] = None,
    uses_pins: Optional[dict] = None,
    substrate_fp: Optional[dict] = None,
    has_repo_spec: bool = False,
) -> str:
    """Full cross-node equivalence digest for safe `eval_canary` reuse, or `""` when unsafe."""
    if not evaluator_digest or not isinstance(canary_spec, dict) or not canary_spec:
        return ""
    use_list = [int(u) for u in (uses or ()) if type(u) is int]
    pinned_uses: dict[str, Any] = {}
    if use_list:
        if not isinstance(uses_pins, dict):
            return ""
        for uid in sorted(set(use_list)):
            key = str(uid)
            pin = uses_pins.get(key, uses_pins.get(uid))
            if isinstance(pin, dict):
                gen, code_id = pin.get("generation"), pin.get("code")
                if type(gen) is not int or gen < 0 or not isinstance(code_id, str) or not code_id:
                    return ""
                pinned_uses[key] = {"generation": gen, "code": code_id}
            elif type(pin) is int and pin >= 0:
                pinned_uses[key] = pin
            else:
                return ""
    if has_repo_spec:
        if not isinstance(substrate_fp, dict) or not substrate_fp or substrate_fp.get("dirty") == "unknown":
            return ""
    clean_files = {
        str(k): v
        for k, v in (files or {}).items()
        if k != activation_manifest_name
    }
    clean_deleted = sorted(str(d) for d in (deleted or ()) if d != activation_manifest_name)
    material = {
        "v": 1,
        "code": code or "",
        "files": _json_safe(clean_files),
        "deleted": clean_deleted,
        "evaluator_digest": evaluator_digest,
        "canary_spec": _json_safe(canary_spec),
        "node_kind": "artifact" if node_kind == "artifact" else "experiment",
        "uses_pins": pinned_uses,
        "substrate_fp": _json_safe(substrate_fp or {}),
    }
    return hashlib.sha256(orjson.dumps(material, option=orjson.OPT_SORT_KEYS)).hexdigest()
