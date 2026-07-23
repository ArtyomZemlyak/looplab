"""Workspace seeding / materialization for the engine (extracted from orchestrator.py): task
assets written into each eval workdir, the ADR-7 multi-file node edits applied on top of them,
the RepoTask editable-tree seeding (tracked-files copy vs full copytree, reference/data symlink
mounts), the item-#4 workspace fingerprint that detects source drift across a resume, and the
eval-`cwd` remap that keeps a command eval inside the sandboxed copy.

`WorkspaceSeeder` wraps the engine instance (`self._e`) rather than owning copies of its state:
the method bodies are verbatim moves from the Engine, reading the engine's assets/repo-spec/
tracer/store through `self._e` and calling sibling cluster methods through the Engine's thin
delegators (so a test monkeypatching e.g. `engine._write_assets` still intercepts every
internal call). `materialize` is the one NEW method: it wraps the seed → node-files → assets
call triple that `_evaluate` and both confirm paths repeated verbatim (the ablation probes are
NOT a caller — they deliberately seed only assets; see the comment in `_ablate`).

Layering: this module must not import the orchestrator (TYPE_CHECKING only) and never imports
serve — it touches only engine.triage, events and stdlib."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from looplab.engine.triage import _dir_fingerprint, _shallow_fingerprint
from looplab.events.types import EV_WORKSPACE_SEEDED

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


class WorkspaceSeeder:
    """The engine's workspace seeding / materialization cluster. See the module docstring for
    the `self._e` (engine handle) convention."""

    def __init__(self, engine: "Engine") -> None:
        self._e = engine

    def write_assets(self, workdir) -> None:
        if not self._e._assets:
            return
        wd = Path(workdir)
        wd.mkdir(parents=True, exist_ok=True)
        for name, content in self._e._assets.items():
            (wd / name).write_text(content, encoding="utf-8")

    def write_node_files(self, node, workdir) -> None:
        """Materialize a multi-file solution's helper files (ADR-7 patch-gated agent)
        into the eval workdir. Skipped: `solution.py` (the sandbox writes it from
        `node.code`) and any **task-asset name** — an agent must never be able to
        overwrite a task-owned file (e.g. the private `grader.py` answer key) via an
        in-surface `*.py` edit. Paths are surface-gated (no escapes) by the developer; we
        re-check defensively. Call BEFORE `_write_assets` so task assets always win."""
        files = getattr(node, "files", None) or {}
        deleted = getattr(node, "deleted", None) or []
        if not files and not deleted:
            return
        # Case-insensitive protected match (defense-in-depth): the surface gate uses fnmatch and
        # NTFS is case-insensitive, so a case-variant name (Ttrain.PY) would otherwise dodge the
        # freeze and overwrite the real metric/grader/eval file on Windows.
        import os as _os
        _prot_names = ("solution.py", *self._e._assets, *self._e._repo_spec.get("protected_names", []))
        protected = {_os.path.normcase(n) for n in _prot_names}
        # A `dir/**` protect entry guards the whole TREE under `dir` (a read-only mounted data source);
        # honor that prefix here too so this defense-in-depth layer matches SurfacePolicy (exact mode).
        _prot_prefixes = tuple(_os.path.normcase(n[:-2]) for n in _prot_names if n.endswith("/**"))
        def _is_prot(rel: str) -> bool:
            r = _os.path.normcase(rel)
            return r in protected or r.startswith(_prot_prefixes) if _prot_prefixes else r in protected
        wd = Path(workdir).resolve()
        wd.mkdir(parents=True, exist_ok=True)
        def _protected_after_resolve(target) -> bool:
            # Check the RESOLVED relative path against the protected set, not the raw name: a name like
            # "sub/../grader.py" passes a raw-string compare yet resolves to wd/grader.py and would
            # overwrite the protected grader otherwise.
            try:
                rel = target.relative_to(wd).as_posix()
            except ValueError:
                return False
            return _is_prot(rel)
        for name, content in files.items():
            if _is_prot(str(name).replace("\\", "/")):
                continue
            target = (wd / name).resolve()
            if wd not in target.parents:        # defense-in-depth: never escape workdir
                continue
            if _protected_after_resolve(target):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        # Apply accepted deletions (the agent removed an in-surface file). Skip protected names
        # and never escape the workdir; missing is fine (idempotent).
        for name in deleted:
            if _os.path.normcase(str(name).replace("\\", "/")) in protected:
                continue
            target = (wd / name).resolve()
            if wd not in target.parents:
                continue
            if _protected_after_resolve(target):
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

    def workspace_fingerprint(self) -> dict:
        """A per-source fingerprint of the editable repos + mounted data (item #4): the git
        HEAD SHA when the source is a git repo, else a cheap content signature over
        (relpath, size, mtime). Used to detect that the operator's source changed between a
        run's start and a resume. {} for non-repo tasks."""
        if not self._e._repo_spec:
            return {}
        srcs: dict[str, str] = {}
        # Editable repos are the drift-detection TARGET (the agent edits them, the search
        # continues over them) and are small code trees -> deep content fingerprint. Data and
        # reference mounts are typically large + immutable inputs -> cheap shallow signature, so
        # the fingerprint never walks a multi-GB dataset on every (re)start.
        for ed in self._e._repo_spec.get("editables", []):
            srcs[f"editable:{ed['name']}"] = _dir_fingerprint(ed["path"])
        for name, spec in self._e._repo_spec.get("data", {}).items():
            src = spec["path"] if isinstance(spec, dict) else spec   # DataSpec dict | bare path
            srcs[f"data:{name}"] = _shallow_fingerprint(src)
        for ref in self._e._repo_spec.get("references", []):
            if ref.get("mount"):
                srcs[f"ref:{ref['name']}"] = _shallow_fingerprint(ref["path"])
        return srcs

    def seed_workspace(self, workdir) -> None:
        """RepoTask (ADR-7): materialize the editable repo tree(s) into the eval workdir, plus
        any runtime-mounted reference repos and data files. Phase 4: each editable repo is
        mounted at its own subdir (name=".") -> workspace root). The agent's `Node.files` edits
        are applied on top by `_write_node_files`; task assets win last. No-op for non-repo
        tasks."""
        if not self._e._repo_spec:
            return
        import shutil
        wd = Path(workdir)
        wd.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "node_modules")
        sp = (self._e.tracer.span("seed_workspace") if self._e.tracer is not None
              else __import__("contextlib").nullcontext(None))
        with sp as _h:
            seeded: list[str] = []
            # Fail loud on a data/reference mount name that collides with a top-level entry of the ROOT
            # editable (name "."/"" — seeded at the workspace root). The root repo is materialized FIRST,
            # so the mount's dst (`wd/<name>`) is already occupied and link_input/copy_input silently skip
            # it — their `dst.exists()` idempotency guard can't tell a repo file from a resumed mount — so
            # the eval reads the repo's placeholder instead of the declared source AND the WORKSPACE_SEEDED
            # record falsely claims the mount succeeded, silently invalidating the whole run's metrics.
            # (Non-root editables mount at `wd/<name>`, already guarded against name collisions at task
            # build.) Read the SOURCE tree, not the persisted workdir, so this stays correct across resume;
            # drop the entries the seed deliberately ignores so an absurd `.git`-named mount can't misfire.
            import os as _os
            _root_ed = next((ed for ed in self._e._repo_spec.get("editables", [])
                             if ed.get("name") in (".", "")), None)
            if _root_ed:
                try:
                    _shadow = {e for e in _os.listdir(_root_ed["path"])
                               if e not in {".git", "__pycache__", ".venv", "node_modules"}
                               and not e.endswith(".pyc")}
                except OSError:
                    _shadow = set()
                _mounts = ([r["name"] for r in self._e._repo_spec.get("references", [])]
                           + list(self._e._repo_spec.get("data", {})))
                _clash = next((m for m in _mounts if m in _shadow), None)
                if _clash is not None:
                    raise RuntimeError(
                        f"mount name {_clash!r} collides with a top-level entry of the root repo "
                        f"({_root_ed['path']}): the repo is seeded at the workspace root first, so the "
                        f"mount would be silently shadowed and the eval would read the repo's copy "
                        f"instead of the declared source. Rename the mount or the repo entry.")
            for ed in self._e._repo_spec.get("editables", []):
                dst = wd if ed["name"] in (".", "") else wd / ed["name"]
                mode = (ed.get("seed_mode") or self._e._seed_mode or "auto")
                n = self._e._seed_repo_tree(ed["path"], dst, ignore, mode)
                seeded.append(f"{ed['name']}[{mode}]:{'copytree' if n < 0 else str(n)+' tracked'}")
            for ref in self._e._repo_spec.get("references", []):
                if ref.get("mount"):             # runtime dependency -> symlink read-only input
                    self._e._link_input(ref["path"], wd / ref["name"])
                    seeded.append(f"ref:{ref['name']}->link")
            for name, spec in self._e._repo_spec.get("data", {}).items():
                # A DataSpec {path, mount, edit, …}; a bare string path is back-compat (all defaults).
                src = spec["path"] if isinstance(spec, dict) else spec
                mount = spec.get("mount", True) if isinstance(spec, dict) else True
                dst = wd / name
                if mount:
                    self._e._link_input(src, dst)          # default: read-only symlink mount at ./<name>
                    seeded.append(f"data:{name}->link")
                else:                                       # copy INTO the workdir (editable if edit=true)
                    self.copy_input(src, dst, ignore)
                    seeded.append(f"data:{name}->copy")
            if _h is not None:
                _h.set_many(materialized=", ".join(seeded))
            # Observability: surface WHAT got materialized into this node's workdir (the "data setup"
            # step) in the activity feed — which editable trees were seeded (tracked vs full copy) and
            # which data/reference inputs were mounted. node_id parsed from the workdir name.
            try:
                nid = int(str(wd.name).split("_")[-1])
            except (ValueError, IndexError):
                nid = None
            self._e.store.append(EV_WORKSPACE_SEEDED, {"node_id": nid, "materialized": seeded})

    def seed_repo_tree(self, src, dst, ignore, mode: str = "auto") -> int:
        """Materialize an editable repo's *source* into the node workdir under a seeding `mode`:
        - "auto" (default) / "tracked": copy the git-TRACKED files (the real code surface — fast,
          deterministic) so a working tree bloated with untracked artifacts (model checkpoints,
          datasets — often many GB) is NOT deep-copied into every node. "auto" silently falls back
          to a full copy when `src` is not a git repo; "tracked" also falls back (there's nothing
          else to copy) but is the explicit "code only" intent.
        - "all": force a full recursive copytree (legacy behavior) — use for small repos or when
          untracked files are needed at eval time.
        Returns the number of tracked files copied, or -1 when a full copytree was used."""
        import shutil
        import subprocess
        src = Path(src); dst = Path(dst)
        tracked = None
        if mode != "all":
            # Ask git directly (no `.git`-at-root check): the editable repo is often a SUBDIR of a
            # larger git repo whose `.git` lives in a parent, so `(src/'.git').exists()` is False even
            # though `git -C src ls-files` correctly lists the files tracked under src. Use it whenever
            # git returns a non-empty tracked set; otherwise (non-git / nothing tracked) fall back.
            try:
                out = subprocess.run(["git", "-C", str(src), "ls-files", "-z"],
                                     capture_output=True, text=True, timeout=120)
                if out.returncode == 0:
                    files = [p for p in out.stdout.split("\0") if p]
                    if files:
                        tracked = files
            except Exception:
                tracked = None                   # git missing / not a repo -> copytree fallback
        if tracked is None:
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)
            return -1
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for rel in tracked:
            s = src / rel
            if s.is_dir() or not s.exists():     # submodule dir / deleted-but-tracked path
                continue
            d = dst / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            n += 1
        return n

    def link_input(self, src, dst) -> None:
        """Mount a large, read-only task input (dataset / reference repo) into the node workdir as a
        SYMLINK rather than a deep copy: these are immutable inputs the eval reads, not the agent's
        edit target, so per-node copies just burn wall-clock + disk (acute on an S3-backed FUSE
        mount). Idempotent (resume / re-seed); falls back to a copy (`copy_input`) if the symlink
        can't be made."""
        import os as _os
        src = Path(src); dst = Path(dst)
        if dst.is_symlink() or dst.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            _os.symlink(src, dst, target_is_directory=src.is_dir())
            return
        except OSError:
            pass
        self.copy_input(src, dst)

    def copy_input(self, src, dst, ignore=None) -> None:
        """The ONE copy-in path for data/reference sources (the `mount:false` branch of
        `seed_workspace` and `link_input`'s no-symlink fallback used to duplicate it with subtle
        divergence). Idempotent. For a directory, try a CoW clone first (`cp --reflink=always`):
        on a reflink-capable fs (btrfs/XFS) a per-node "copy" of a multi-GB dataset costs ~zero
        bytes and milliseconds — and edits stay node-local (copy-on-write), so the editable-copy
        semantics are preserved. Without CoW an N-node run pays N full byte copies (mega-review:
        20 GB × 50 nodes ≈ 1 TB), which remains the documented cost of `mount:false` on ext4.
        `--reflink=always` fails FAST on a non-CoW fs / cross-device copy → full copytree fallback
        with the usual ignore patterns (the verbatim clone skips them — data trees don't carry
        .git/.venv; the pruning is a code-tree concern)."""
        import shutil
        import subprocess
        import sys as _sys
        src = Path(src); dst = Path(dst)
        if dst.is_symlink() or dst.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if _sys.platform != "win32" and shutil.which("cp"):
                r = subprocess.run(["cp", "-R", "--reflink=always", "--", str(src), str(dst)],
                                   capture_output=True)
                if r.returncode == 0:
                    return
                shutil.rmtree(dst, ignore_errors=True)   # a partial clone must not survive the fallback
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)
        elif src.is_file():
            shutil.copy2(src, dst)

    def sandbox_cwd(self, workdir, cwd_spec) -> str:
        """Resolve the eval `cwd` against the node's sandbox workdir. A relative cwd joins the
        workdir (the conventional case). An ABSOLUTE cwd that points inside an editable repo's
        *source* is remapped onto the node workdir, so the eval runs in the sandboxed copy (with
        the agent's edits + the seeded tree) instead of the shared original repo — `Path(wd)/'/abs'`
        would otherwise collapse to '/abs', silently bypassing the sandbox. An absolute cwd that is
        not under any editable source is trusted as given (e.g. an external tool dir)."""
        wd = Path(workdir).resolve()
        p = Path(cwd_spec)
        if not p.is_absolute():
            return str((wd / cwd_spec).resolve())
        ap = p.resolve()
        for ed in (self._e._repo_spec or {}).get("editables", []):
            src = Path(ed["path"]).resolve()
            base = wd if ed["name"] in (".", "") else wd / ed["name"]
            try:
                rel = ap.relative_to(src)
            except ValueError:
                continue
            return str((base / rel).resolve())
        return str(ap)

    def materialize(self, node, workdir) -> None:
        """The full workdir build for one eval of `node` — the seed → node-files → assets triple
        `_evaluate` and both confirm paths (`_confirm_phase` / `_confirm_node`) each ran verbatim
        before the extraction. Order is load-bearing (see `_write_node_files`): node edits go on
        top of the seeded tree, and task assets win any name collision, last. Routed through the
        Engine's delegators so an instance-level monkeypatch of any step still intercepts it."""
        import shutil

        wd = Path(workdir).resolve()
        run_dir = Path(self._e.run_dir).resolve()
        if wd == run_dir or run_dir not in wd.parents:
            raise ValueError(f"refusing to materialize outside the run directory: {wd}")
        # A fresh lifecycle must start from the canonical seed + current node manifest, not an overlay
        # on files left by a previous generation. Stage-scoped reuse deliberately bypasses this method
        # in EvaluateMixin; every actual materialization is therefore safe to rebuild from scratch.
        if wd.exists():
            shutil.rmtree(wd)
        self._e._seed_workspace(wd)                # RepoTask: editable repo tree (ADR-7) …
        self._e._write_node_files(node, wd)         # … agent edits on top …
        self._e._write_assets(wd)                   # … task assets win any name collision
