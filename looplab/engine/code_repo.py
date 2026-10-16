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
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

REPO_REL = "code/repo.git"
WORKTREES_REL = "code/worktrees"
IDENTITY = b"LoopLab <looplab@invalid>"
# The same identity for a `commit-tree` (synthetic merge inputs, the agent backup's dirty commit):
# a fixed author AND date, so a commit is a pure function of (tree, parents, message) and an
# unchanged input yields the same id pass after pass.
FIXED_COMMIT_ENV = {"GIT_AUTHOR_NAME": "LoopLab", "GIT_AUTHOR_EMAIL": "looplab@invalid",
                    "GIT_COMMITTER_NAME": "LoopLab", "GIT_COMMITTER_EMAIL": "looplab@invalid",
                    "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000"}
# The code-identity version a NEW repository is built at, stamped in its config on creation
# (`looplab.identityVersion`), so a run with no `code_snapshot` row still rebuilds at the version
# its repository was first built at (`code_origin.py::run_version`).
# `code_identity.py::SNAPSHOT_VERSION` is this value; what each version means is said there.
IDENTITY_VERSION = 2
_OBJECT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# How long a writer of `$GIT_DIR/shallow` waits for git's own `shallow.lock` (a concurrent
# `fetch --depth` holds it only for its last step).
SHALLOW_LOCK_WAIT_S = 30.0


def is_object_id(value) -> bool:
    """THE one spelling of "a full git object id": 40 (SHA-1) or 64 (SHA-256) lowercase hex. Every
    reader of a commit id off the log, a probe or a file uses it (some sites accepted 64 characters,
    others only 40, and `$GIT_DIR/shallow` silently lost a SHA-256 line)."""
    return isinstance(value, str) and _OBJECT_ID.fullmatch(value) is not None
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


def readonly_safe_dirs(path) -> list:
    """The `safe.directory` values that let a READ-ONLY git call work in `path`'s repository when
    another uid owns it. WHY: `_env` points the global config at /dev/null (hermetic), so the
    operator's own `safe.directory` is not read, and a source owned by another user failed every
    probe as "dubious ownership", reported as "not git" (review 2026-10-16). The values are the
    directory, every ancestor (git checks the work tree's TOP, which may be any of them) and each
    one's `.git` (`upload-pack` checks the git dir) — never `*`: only the repository `path` is in
    is trusted, and only for reading (no hook, fsmonitor or filter runs: `_argv`)."""
    try:
        real = Path(os.path.realpath(os.path.expanduser(str(path))))
    except (OSError, ValueError):
        return []
    out: list = []
    for anc in (real, *real.parents):
        out += [anc.as_posix(), (anc / ".git").as_posix()]
    return out


def readonly_git(path, *argv: str, **kw) -> subprocess.CompletedProcess:
    """`git -C <path> …` in a repository LoopLab only READS (the operator's source, a foreign
    branch): `~` expanded (every comparison here is of expanded paths), `--no-optional-locks` (no
    index refresh written back), and the path trusted for reading (`readonly_safe_dirs`)."""
    where = os.path.expanduser(str(path))
    safe = [arg for d in readonly_safe_dirs(where) for arg in ("-c", f"safe.directory={d}")]
    return run_git(*safe, "-C", where, "--no-optional-locks", *argv, **kw)


def is_dubious_ownership(result: subprocess.CompletedProcess) -> bool:
    """A refusal git made for OWNERSHIP, said apart from "not a repository"."""
    return result.returncode != 0 and b"dubious ownership" in (result.stderr or b"")


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
            run_git("--git-dir", str(staging), "config", "looplab.identityVersion",
                    str(IDENTITY_VERSION))
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

    def identity_version(self) -> Optional[int]:
        """The version stamped at creation (`IDENTITY_VERSION`); None when absent (a repository
        built before the stamp existed) or unreadable."""
        if not self.exists():
            return None
        out = self.git("config", "--get", "looplab.identityVersion", check=False)
        text = out.stdout.decode("ascii", "replace").strip()
        return int(text) if out.returncode == 0 and text.isdigit() else None

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
    def _commit_header(ref: str, when: int, message: str, parent: Optional[str],
                       merges: Iterable = ()) -> bytes:
        msg = message.encode("utf-8", "replace")
        head = (b"commit %s\n" % ref.encode("utf-8")
                + b"author %s %d +0000\n" % (IDENTITY, int(when))
                + b"committer %s %d +0000\n" % (IDENTITY, int(when))
                + b"data %d\n" % len(msg) + msg + b"\n")
        merges = list(merges or ())
        if merges and not parent:
            # Silently dropped before: a root commit that claimed none of the merges it was given.
            # A merge as the FIRST parent instead would be what every first-parent walk lands on.
            raise CodeRepoError("git_failed", "merge parents need a first parent")
        if parent:
            head += b"from %s\n" % parent.encode("ascii")
            # Further git parents AFTER the first (doc 79, 2026-10-16): only ever node commits a
            # REAL merge in an agent worktree brought in (`code_identity.py::merged_node_commits`),
            # so the first parent stays the base every first-parent walk lands on.
            for sha in merges:
                head += b"merge %s\n" % sha.encode("ascii")
        return head

    def write_tree_commit(self, ref: str, files: Iterable, *, message: str, when: int,
                          parent: Optional[str] = None) -> str:
        """A commit whose tree is EXACTLY `files` ((path, bytes, executable) rows), parent optional.

        A `parent` this repository does not hold is still NAMED, because the commit id is what the
        log decided (`code_origin.py`). fast-import cannot start from an absent commit, so the tree
        is imported alone and the commit object written with `hash-object`, byte for byte what
        fast-import writes (`tests/test_code_identity.py` compares the two ids). The caller makes
        the commit a shallow cut (`add_shallow`): measured 2026-10-16 (git 2.43), a cut at the
        absent PARENT leaves `log`/`rev-list`/`bundle` failing, a cut at the commit itself does not."""
        if parent and not self.has_object(parent):
            scratch = f"refs/looplab/tmp/tree-{uuid.uuid4().hex[:12]}"
            try:
                tree = self.tree(self.write_tree_commit(scratch, files, message="tree\n", when=0))
            finally:
                self.git("update-ref", "-d", scratch, check=False)
            msg = message.encode("utf-8", "replace")
            raw = (b"tree %s\nparent %s\n" % (str(tree).encode("ascii"), parent.encode("ascii"))
                   + b"author %s %d +0000\n" % (IDENTITY, int(when))
                   + b"committer %s %d +0000\n\n" % (IDENTITY, int(when)) + msg)
            sha = self.git("hash-object", "-t", "commit", "-w", "--stdin",
                           input=raw).stdout.decode("ascii").strip()
            self.update_ref(ref, sha)
            return sha
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
                             when: int, merges: Iterable = ()) -> str:
        """A commit on top of `parent`: `changes[path]` is (bytes, executable) or None (delete).
        `merges` are further git parents; the tree is still `parent`'s plus `changes`."""
        body = [self._commit_header(ref, when, message, parent, merges)]
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

    def has_object(self, sha: str) -> bool:
        return self.exists() and self.git("cat-file", "-e", sha, check=False).returncode == 0

    def commit_tree(self, tree: str, parents: Iterable = (), message: str = "") -> str:
        """A commit of `tree` under the FIXED identity and date (`FIXED_COMMIT_ENV`): the same
        inputs always give the same id. Objects only, no ref."""
        argv = ["commit-tree", tree, "-m", message]
        for parent in parents or ():
            argv += ["-p", parent]
        return self.git(*argv, env_extra=FIXED_COMMIT_ENV).stdout.decode().strip()

    def fetch_readonly(self, source, refspecs: Iterable, *, depth: Optional[int] = None,
                       deepen: Optional[int] = None, timeout: float = 120,
                       check: bool = True) -> subprocess.CompletedProcess:
        """Fetch FROM a repository LoopLab only reads (the operator's source, a foreign branch):
        the one spelling of that fetch, so every caller has the same safety flags.

        Read-only on `source`: its `upload-pack` reads and writes nothing, and no tag, FETCH_HEAD,
        submodule or maintenance follows. Protocol v2, so a commit that is no ref tip any more is
        still served by id (v0 refuses an unadvertised object; measured, git 2.43). `upload-pack`
        runs with the source trusted for reading (`readonly_safe_dirs`) on ITS command line: a
        local transport clears `-c` config (`GIT_CONFIG_PARAMETERS`) for it (measured). An
        unbounded fetch from a shallow source needs `--update-shallow` (git rejects the refs
        otherwise, measured); a bounded one (`depth` / `deepen`) takes the cut as its own.

        NOT under `lock()`: git writes refs and `$GIT_DIR/shallow` under its own lockfiles, and
        `add_shallow` takes the same `shallow.lock`, so a long fetch never stalls the engine's
        attempt records (review 2026-10-16). Never fetch a commit already present with `depth`:
        that rewrites it as a shallow cut and truncates the history behind it (measured)."""
        src = os.path.expanduser(str(source))
        upload = " ".join(["git", *(shlex.quote(a) for d in readonly_safe_dirs(src)
                                    for a in ("-c", f"safe.directory={d}")), "upload-pack"])
        argv = ["-c", "protocol.version=2", "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                "--no-recurse-submodules", "--no-auto-maintenance", f"--upload-pack={upload}"]
        if depth is not None:
            argv.append(f"--depth={int(depth)}")
        elif deepen is not None:
            argv.append(f"--deepen={int(deepen)}")
        else:
            argv.append("--update-shallow")
        return self.git(*argv, src, *refspecs, timeout=timeout, check=check)

    def commit_parents(self, commit: str) -> list:
        """The parent ids a commit OBJECT names, read off its header — never a walk, so a parent
        that is absent (or cut by `shallow`) is still named."""
        out = self.git("cat-file", "commit", commit, check=False)
        if out.returncode != 0:
            return []
        parents = []
        for line in out.stdout.split(b"\n\n", 1)[0].splitlines():
            if line.startswith(b"parent "):
                parents.append(line[7:].decode("ascii", "replace").strip())
        return parents

    def shallow(self) -> list:
        """The commits `$GIT_DIR/shallow` cuts history at (sorted); [] when there is no file."""
        try:
            text = (self.path / "shallow").read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            return []
        return sorted({line.strip() for line in text.splitlines() if is_object_id(line.strip())})

    def add_shallow(self, commits: Iterable) -> None:
        """Merge `commits` into `$GIT_DIR/shallow`. Only for a commit that IS present and whose
        parents are NOT: the line is what lets git stop at a commit whose history was never fetched
        (`code_origin.py`).

        Under GIT'S OWN lock protocol (`shallow.lock` created exclusively, written, renamed over
        `shallow`), not `lock()` alone: a read-only `fetch --depth` (`fetch_readonly`) runs outside
        the run's lock and rewrites the file under that lockfile, so a plain temp+rename here could
        drop the cut the fetch just wrote, or the fetch this one."""
        want = {c for c in commits if is_object_id(c)}
        if not want or want <= set(self.shallow()):
            return
        lock = self.path / "shallow.lock"
        deadline = time.monotonic() + SHALLOW_LOCK_WAIT_S
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise CodeRepoError("git_failed", "shallow.lock is held: another git process "
                                                      "is writing the shallow file") from None
                time.sleep(0.05)
        renamed = False
        try:
            have = set(self.shallow())               # re-read under the lock
            with os.fdopen(fd, "wb") as fh:
                fh.write("".join(f"{c}\n" for c in sorted(have | want)).encode("ascii"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(lock, self.path / "shallow")
            renamed = True
        finally:
            if not renamed:
                try:
                    os.unlink(lock)                  # never leave git's lock behind
                except OSError:
                    pass                             # nothing to remove

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
