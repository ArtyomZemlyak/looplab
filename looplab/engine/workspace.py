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
        # str OR bytes, mirroring the str/bytes branch the setup-provenance hash already uses
        # (`c.encode(...) if isinstance(c, str) else bytes(c)`). `write_text` raises TypeError on
        # bytes, so a task exposing a BINARY asset passed setup cleanly and then crashed every
        # single node materialization.
        for name, content in self._e._assets.items():
            if isinstance(content, str):
                (wd / name).write_text(content, encoding="utf-8")
            else:
                (wd / name).write_bytes(bytes(content))

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
            # build.) The check runs AFTER the editables are seeded and reads the REAL workspace, not the
            # source listdir, because only the materialized tree can actually shadow a mount. Reading the
            # source instead produced two false RuntimeErrors that aborted every node of a valid task:
            #   * `seed_mode="auto"` copies only git-TRACKED files, so a gitignored top-level `data/` —
            #     the standard layout, datasets are never committed — was "shadowing" a `data:` mount that
            #     in fact seeded fine;
            #   * `_mounts` listed EVERY reference, but only `ref.get("mount")` ones are materialized
            #     below, so a context-only reference collided with nothing yet still raised.
            # Post-seed `dst.exists()` is exactly the condition link_input/copy_input silently skip on, so
            # it has no false positives by construction and stays correct across resume.
            for ed in self._e._repo_spec.get("editables", []):
                dst = wd if ed["name"] in (".", "") else wd / ed["name"]
                mode = (ed.get("seed_mode") or self._e._seed_mode or "auto")
                n = self._e._seed_repo_tree(ed["path"], dst, ignore, mode)
                seeded.append(f"{ed['name']}[{mode}]:{'copytree' if n < 0 else str(n)+' tracked'}")
            _mounts = [m for m in ([r["name"] for r in self._e._repo_spec.get("references", [])
                                    if r.get("mount")]
                                   + list(self._e._repo_spec.get("data", {}))) if m]
            _root_ed = next((ed for ed in self._e._repo_spec.get("editables", [])
                             if ed.get("name") in (".", "")), None)
            if _root_ed:
                _clash = next((m for m in _mounts if (wd / m).exists()), None)
                if _clash is not None:
                    raise RuntimeError(
                        f"mount name {_clash!r} collides with a top-level entry of the root repo "
                        f"({_root_ed['path']}): the repo is seeded at the workspace root first, so the "
                        f"mount would be silently shadowed and the eval would read the repo's copy "
                        f"instead of the declared source. Rename the mount or the repo entry.")
            # The operator's PROTECTED files, materialized whatever the seed mode says (see
            # `seed_protected_files`). This position between the shadow guard and the mounts IS the
            # safety argument: BEFORE the guard, a protect entry like `datasets/labels.csv` would
            # manufacture a top-level `datasets/` and raise a FALSE collision that aborts every node of
            # a valid task; AFTER the mounts, the same entry would write THROUGH the read-only mount
            # symlink into the operator's original data. `reserved_top` covers the residue for the ROOT
            # editable (whose files land at the workspace root, where the mounts also live); a non-root
            # editable mounts under its own subdir, which `_names_distinct_and_safe` already keeps
            # disjoint from every mount name.
            for ed in self._e._repo_spec.get("editables", []):
                dst = wd if ed["name"] in (".", "") else wd / ed["name"]
                prot = self.seed_protected_files(
                    ed["path"], dst, ed.get("protect"),
                    reserved_top=(set(_mounts) if ed.get("name") in (".", "") else set()))
                if prot:
                    seeded.append(f"{ed['name']}:protected[{len(prot)}]:" + ",".join(prot[:5]))
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
        """Delegate to the shared evaluation/Developer candidate seeding rule."""
        from looplab.engine.workspace_seed import seed_repo_tree
        return seed_repo_tree(src, dst, ignore, mode)

    def seed_protected_files(self, src, dst, protect, *, reserved_top=()) -> list[str]:
        """Delegate to the shared evaluation/Developer protected-file rule."""
        from looplab.engine.workspace_seed import seed_protected_files
        return seed_protected_files(src, dst, protect, reserved_top=reserved_top)

    def link_input(self, src, dst) -> None:
        """Delegate to the shared evaluation/Developer input-mount rule.

        `self.copy_input` is threaded through as the fallback so an override of it covers BOTH
        branches — the `mount:false` copy in `seed_workspace` and the symlink-failure copy here.
        Without it the second silently escaped the seam (see `workspace_seed.link_input`)."""
        from looplab.engine.workspace_seed import link_input
        return link_input(src, dst, self.copy_input)

    def copy_input(self, src, dst, ignore=None) -> None:
        """Delegate to the shared evaluation/Developer input-copy rule."""
        from looplab.engine.workspace_seed import copy_input
        return copy_input(src, dst, ignore)

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
