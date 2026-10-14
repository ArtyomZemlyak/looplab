"""The run-owned code repository: git plumbing for per-attempt code identity (doc 79).

ONE PRIVATE BARE REPOSITORY PER RUN, at `RUN_DIR/code/repo.git`. It is never the operator's
repository and never shares its object database, which is what doc 37 §5c measured as the
disqualifying blast radius of a `git worktree` of the SOURCE repo: a node that runs `git add -A`
there writes the operator's untracked checkpoints into the operator's `.git`. Here the worst a
`git add -A` in an agent worktree can do is grow this run's own repository.

Every commit is written with `git fast-import` (one process per snapshot, no index, no checkout),
under a fixed identity and the event's own timestamp, so the same inputs give the same commit id.
Git runs hermetically: no user/system config, no hooks, no fsmonitor, no attribute filters
(`info/attributes` pins `-text -filter -ident -working-tree-encoding` so bytes are preserved).

This module is plumbing only; what a base or an attempt IS lives in `engine/code_identity.py`.
"""
from __future__ import annotations

import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Iterable, Optional

REPO_REL = "code/repo.git"
WORKTREES_REL = "code/worktrees"
IDENTITY = b"LoopLab <looplab@invalid>"
ATTRIBUTES = b"* -text -filter -ident -working-tree-encoding\n"
# Untracked output an agent worktree must not ingest by `git add -A` (doc 37 §6: checkpoints are
# 99 % of a node workspace). The run repository is private, but it is still this run's disk.
EXCLUDES = (b"checkpoint-*/\n*.safetensors\n*.bin\n*.pt\n*.pth\n*.ckpt\n*.arrow\n*.npy\n"
            b"wandb/\noutputs/\n__pycache__/\n.ipynb_checkpoints/\n")
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


class CodeRepoError(RuntimeError):
    """A git operation on the run code repository failed. `code` is stable for callers."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _env() -> dict:
    try:
        from looplab.runtime.sandbox import git_subprocess_env
        env = {k: v for k, v in git_subprocess_env().items() if not k.startswith("GIT_CONFIG_")}
    except Exception:  # noqa: BLE001 - a minimal environment is still hermetic
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return env


def _argv(*argv: str) -> list:
    return ["git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=" + os.devnull, "-c", "core.attributesFile=" + os.devnull,
            "-c", "commit.gpgsign=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
            "-c", "core.longpaths=true", "-c", "core.quotePath=false", *argv]


def run_git(*argv: str, cwd=None, input: Optional[bytes] = None, timeout: float = 120,
            check: bool = True, env_extra: Optional[dict] = None) -> subprocess.CompletedProcess:
    """One hermetic git call. Raises `CodeRepoError` on a missing binary, a timeout or (with
    `check`) a nonzero exit; stderr is summarized, never the submitted bytes."""
    try:
        env = _env()
        if env_extra:
            env.update(env_extra)
        result = subprocess.run(_argv(*argv), cwd=None if cwd is None else str(cwd), input=input,
                                env=env, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise CodeRepoError("git_missing", "git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise CodeRepoError("git_timeout", f"git {argv[0] if argv else ''} exceeded {timeout:g}s") from exc
    if check and result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise CodeRepoError("git_failed", f"git {argv[0] if argv else ''} failed: {tail[0][:300]}")
    return result


def quoted(path: str) -> bytes:
    """C-quoted path for fast-import (always quoted), the spelling `events/git_export.py` uses."""
    out = ['"']
    for ch in path:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\%03o" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out).encode("utf-8")


def safe_path(path) -> Optional[str]:
    """A repository-relative POSIX path, or None for one that cannot be a tree entry: empty,
    absolute, `.`/`..`, a `.git` component (any case), NUL or a trailing slash."""
    if not isinstance(path, str) or not path or "\x00" in path:
        return None
    norm = path.replace("\\", "/")
    if norm.startswith("/") or norm.endswith("/"):
        return None
    parts = norm.split("/")
    if any(p in ("", ".", "..") or p.rstrip(". ").casefold() == ".git" for p in parts):
        return None
    return norm


def _lock_for(path: Path):
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
    return lock


class _RepoLock:
    """Thread lock plus an advisory interprocess lock (POSIX flock) around repository writes."""

    def __init__(self, repo: "CodeRepo"):
        self.repo, self.fh = repo, None

    def __enter__(self):
        self.thread_lock = _lock_for(self.repo.path)
        self.thread_lock.acquire()
        try:
            import fcntl
            self.fh = open(self.repo.path.parent / ".repo.lock", "a+b")
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            self.fh = None
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            self.fh.close()
        self.thread_lock.release()


class CodeRepo:
    """The private bare repository of one run."""

    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / REPO_REL

    def exists(self) -> bool:
        return (self.path / "HEAD").is_file() and (self.path / "objects").is_dir()

    def git(self, *argv: str, **kw) -> subprocess.CompletedProcess:
        return run_git("--git-dir", str(self.path), *argv, **kw)

    def lock(self) -> _RepoLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return _RepoLock(self)

    def ensure(self) -> "CodeRepo":
        """Create the repository once, whole: init in a sibling directory, then rename."""
        if self.exists():
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.parent / f".repo-init-{uuid.uuid4().hex[:12]}"
        try:
            run_git("init", "--bare", "--quiet", "--template=", str(staging))
            (staging / "info").mkdir(exist_ok=True)
            (staging / "info" / "attributes").write_bytes(ATTRIBUTES)
            (staging / "info" / "exclude").write_bytes(EXCLUDES)
            run_git("--git-dir", str(staging), "config", "user.name", "LoopLab")
            run_git("--git-dir", str(staging), "config", "user.email", "looplab@invalid")
            run_git("--git-dir", str(staging), "config", "core.hooksPath", os.devnull)
            run_git("--git-dir", str(staging), "symbolic-ref", "HEAD", "refs/heads/base")
            try:
                os.rename(staging, self.path)
            except OSError:
                if not self.exists():
                    raise
        finally:
            if staging.exists():
                from looplab.core.atomicio import rmtree_readonly_aware
                rmtree_readonly_aware(staging)
        return self

    def rev(self, ref: str) -> Optional[str]:
        if not self.exists():
            return None
        out = self.git("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
        sha = out.stdout.decode().strip()
        return sha if out.returncode == 0 and sha else None

    def refs(self, prefix: str = "refs/") -> dict:
        if not self.exists():
            return {}
        out = self.git("for-each-ref", "--format=%(refname) %(objectname)", prefix, check=False)
        rows = {}
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            name, _, sha = line.rpartition(" ")
            if name:
                rows[name] = sha
        return rows

    def update_ref(self, ref: str, sha: str) -> None:
        self.git("update-ref", ref, sha)

    def fast_import(self, stream: bytes) -> None:
        self.git("fast-import", "--quiet", "--date-format=raw", "--force", input=stream, timeout=300)

    @staticmethod
    def _commit_header(ref: str, when: int, message: str, parent: Optional[str]) -> bytes:
        msg = message.encode("utf-8", "replace")
        head = (b"commit %s\n" % ref.encode("utf-8")
                + b"author %s %d +0000\n" % (IDENTITY, int(when))
                + b"committer %s %d +0000\n" % (IDENTITY, int(when))
                + b"data %d\n" % len(msg) + msg + b"\n")
        if parent:
            head += b"from %s\n" % parent.encode("ascii")
        return head

    def write_tree_commit(self, ref: str, files: Iterable, *, message: str, when: int,
                          parent: Optional[str] = None) -> str:
        """A commit whose tree is EXACTLY `files` ((path, bytes, executable) rows), parent optional."""
        body = [self._commit_header(ref, when, message, parent), b"deleteall\n"]
        for path, data, executable in files:
            safe = safe_path(path)
            if safe is None:
                continue
            body.append(b"M %s inline %s\ndata %d\n" % (b"100755" if executable else b"100644",
                                                         quoted(safe), len(data)) + data + b"\n")
        body.append(b"\ndone\n")
        self.fast_import(b"".join(body))
        sha = self.rev(ref)
        if sha is None:
            raise CodeRepoError("git_failed", f"fast-import did not create {ref}")
        return sha

    def write_overlay_commit(self, ref: str, parent: str, changes: dict, *, message: str,
                             when: int) -> str:
        """A commit on top of `parent`: `changes[path]` is (bytes, executable) or None (delete)."""
        body = [self._commit_header(ref, when, message, parent)]
        for path in sorted(changes):
            safe = safe_path(path)
            if safe is None:
                continue
            row = changes[path]
            if row is None:
                body.append(b"D %s\n" % quoted(safe))
            else:
                data, executable = row
                body.append(b"M %s inline %s\ndata %d\n" % (b"100755" if executable else b"100644",
                                                             quoted(safe), len(data)) + data + b"\n")
        body.append(b"\ndone\n")
        self.fast_import(b"".join(body))
        sha = self.rev(ref)
        if sha is None:
            raise CodeRepoError("git_failed", f"fast-import did not create {ref}")
        return sha

    def tree(self, commit: str) -> Optional[str]:
        out = self.git("rev-parse", "--verify", "--quiet", commit + "^{tree}", check=False)
        sha = out.stdout.decode().strip()
        return sha if out.returncode == 0 and sha else None

    def changed(self, old: str, new: str) -> list:
        """[{path, status}] between two commits (A/M/D/T), renames off, sorted by path."""
        out = self.git("diff-tree", "-r", "-z", "--no-renames", "--name-status", old, new)
        parts = out.stdout.split(b"\x00")
        rows = []
        for i in range(0, len(parts) - 1, 2):
            status, path = parts[i].decode("ascii", "replace"), parts[i + 1].decode("utf-8", "replace")
            if status:
                rows.append({"path": path, "status": status[:1]})
        return sorted(rows, key=lambda r: r["path"])

    def numstat(self, old: str, new: str) -> dict:
        out = self.git("diff-tree", "-r", "--no-renames", "--numstat", old, new)
        added = removed = files = 0
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            a, _, rest = line.partition("\t")
            r, _, _ = rest.partition("\t")
            files += 1
            added += int(a) if a.isdigit() else 0
            removed += int(r) if r.isdigit() else 0
        return {"files": files, "insertions": added, "deletions": removed}

    def diff(self, old: str, new: str, *, paths=(), max_bytes: int = 256 * 1024) -> tuple:
        """Unified diff text (binary files summarized), cut at `max_bytes`; returns (text, truncated)."""
        argv = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", old, new]
        if paths:
            argv += ["--", *paths]
        out = self.git(*argv)
        raw = out.stdout
        truncated = len(raw) > max_bytes
        return raw[:max_bytes].decode("utf-8", "replace"), truncated

    def patch(self, old: str, new: str) -> bytes:
        """A full binary-safe patch (`git apply` input) between two commits."""
        return self.git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
                        "--binary", "--full-index", old, new).stdout

    def blob(self, commit: str, path: str) -> Optional[bytes]:
        out = self.git("cat-file", "blob", f"{commit}:{path}", check=False)
        return out.stdout if out.returncode == 0 else None

    def entries(self, commit: str) -> dict:
        """{path: (mode, blob_sha)} of a commit's whole tree."""
        out = self.git("ls-tree", "-r", "-z", "--full-tree", commit)
        rows = {}
        for item in out.stdout.split(b"\x00"):
            if not item:
                continue
            meta, _, path = item.partition(b"\t")
            mode, kind, sha = meta.decode().split(" ")
            if kind == "blob":
                rows[path.decode("utf-8", "replace")] = (mode, sha)
        return rows
