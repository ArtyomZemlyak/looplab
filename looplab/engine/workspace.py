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


# THE NODE'S OWN BUILT SOURCE, and it is deliberately NOT one of the two fingerprints below.
# `_dir_fingerprint` (triage.py) answers "git HEAD, else (relpath, size, mtime_ns)", and BOTH of its
# branches are wrong for comparing a built node against the parent it claims to modify:
#
#   * a node workspace IS a git repo (`.git`, `.dvc`, `.gitmodules` are seeded into every one), so
#     the git branch fires and returns HEAD — blind to the uncommitted build edits that ARE the
#     experiment. `substrate_fingerprint` below says the same thing about the same blindness:
#     "a record that is confidently wrong, which is worse than one that says nothing".
#   * the fallback keys on `mtime_ns`, so two separately-created workspaces ALWAYS differ. A guard
#     built on it fires never.
#
# So this reads CONTENT, and only of the files a proposal can change. `experiments/` holds model
# checkpoints (hundreds of MB per node) and is written by the train, not by the build; the rest of
# the skip list is caches and editor droppings. MEASURED on e5small-dr-unified-v12: two of forty-
# seven parent edges across three runs are nodes whose source is byte-identical to their parent's,
# each having paid a full train to change nothing (#160).
_SOURCE_DIGEST_SUFFIXES = (".py", ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".sh")
_SOURCE_DIGEST_SKIP_DIRS = frozenset({
    "experiments", ".ipynb_checkpoints", "__pycache__", ".git", ".dvc", "node_modules",
    ".pytest_cache", ".mypy_cache", "wandb", "outputs",
})


def source_tree_digest(root) -> str:
    """A content digest over a node workspace's SOURCE files, or "" when there is nothing to read.

    Deterministic and order-independent: every eligible file contributes `relpath\0sha256` to one
    sorted list, which is then hashed. Two workspaces agree iff every source file agrees, so a
    build that changed nothing is exactly a digest collision with its parent.

    Best-effort by construction — an unreadable file contributes its path and the marker `unread`
    rather than raising, because this is a diagnostic and may never cost a node its evaluation.
    """
    import hashlib
    from pathlib import Path
    base = Path(root)
    if not base.is_dir():
        return ""
    rows: list[str] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if any(part in _SOURCE_DIGEST_SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if path.suffix.lower() not in _SOURCE_DIGEST_SUFFIXES:
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:  # noqa: BLE001 - a diagnostic may never raise on a node's behalf
            digest = "unread"
        rows.append(f"{rel.as_posix()}\0{digest}")
    if not rows:
        return ""
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


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

    def workspace_source_paths(self) -> list[str]:
        """The real filesystem paths behind `workspace_fingerprint`'s LABEL keys.

        The fingerprint keys on `editable:<name>` / `data:<name>` / `ref:<name>` because it is READ
        BY LABEL. `orchestrator._dirty_inputs` keys on the PATH — it does `Path(src)` and runs
        `git -C <root> status --porcelain` — so handing it the fingerprint MAP resolved every source
        to `Path("editable:foo").parent`, i.e. `"."`, the engine's own CWD. Two records were wrong
        for the one reason: `run_started.dirty_inputs` enumerated LoopLab's own checkout (or, off a
        git tree, nothing at all) instead of the operator's repo, and `substrate_fingerprint` — whose
        entire job is to notice a fix promoted into the editable tree mid-run — read CLEAN for every
        such promotion and let `comparability` answer SAME across it.

        ONE derivation of "which trees are this run's sources", so the label spelling and the path
        spelling cannot drift apart again. Same sources, same order, as the fingerprint above.
        """
        if not self._e._repo_spec:
            return []
        paths: list[str] = []
        for ed in self._e._repo_spec.get("editables", []):
            paths.append(str(ed["path"]))
        for _name, spec in self._e._repo_spec.get("data", {}).items():
            paths.append(str(spec["path"] if isinstance(spec, dict) else spec))
        for ref in self._e._repo_spec.get("references", []):
            if ref.get("mount"):
                paths.append(str(ref["path"]))
        return paths

    def substrate_fingerprint(self) -> dict:
        """The editable source tree a NUMBER was produced on — HEAD *and* the uncommitted work.

        `workspace_fingerprint` above is `git rev-parse HEAD` per source, and its own sibling
        (`orchestrator._dirty_inputs`) says in its docstring that HEAD "is blind to uncommitted
        work". That is fine for its job — detecting that the operator's repo MOVED between a run's
        start and a resume — and it is not fine for this one.

        The gesture this exists to catch is an operator promoting a fix into the editable repo
        mid-run, which `looplab repair-candidates` explicitly urges them to do, and the ordinary way
        to do that is to EDIT THE WORKING TREE. On a HEAD-only digest both sides of that edit read
        identical and `comparability` would answer SAME while `_SUBSTRATE_NOTICE` asserted the
        opposite — a record that is confidently wrong, which is worse than one that says nothing.

        So the porcelain list and the bounded diff digest ride along. Best-effort exactly like the
        fingerprint it extends: a source that cannot be read contributes nothing rather than raising,
        because this record may never cost a node its terminal. Cheap enough only because it is
        called off the event loop, once per node terminal — see the call site.
        """
        base = self.workspace_fingerprint()
        if not base:
            return {}
        try:
            # The PATHS, never `base`: `base` is keyed by LABEL and `_dirty_inputs` reads its keys as
            # filesystem paths — see `workspace_source_paths` for what that cost.
            dirty = self._e._dirty_inputs(self.workspace_source_paths())
        except Exception:  # noqa: BLE001 — an unreadable tree contributes no dirty evidence
            dirty = None
        # "COULD NOT READ" IS NOT "NOTHING TO READ", and returning `base` for both said the second
        # about the first. `base` IS the spelling of a clean tree — the argument is spelled out
        # sixteen lines below for the sibling digest-failure branch — so a node whose
        # `git status --porcelain` raised (an index.lock, a mid-rebase tree, an EIO on the geesefs
        # mount this box measures 105-950 ms absent-file `lstat`s on) recorded itself as CLEAN and
        # then read SAME against a genuinely clean one, which is exactly the confidently-wrong
        # record this discriminator exists to refuse. Only a successfully read, EMPTY enumeration
        # may return `base`.
        #
        # It shares the `"unknown"` marker with that branch rather than minting a second word,
        # because the two mean the same thing to every reader: the engine cannot say what this tree
        # held. Note the marker is a CONSTANT, so two unreadable trees still compare equal to each
        # other — it makes them refusable against a CLEAN tree, which is the case that was wrong,
        # and nothing here can distinguish two trees it failed to read.
        if dirty is None:
            return {**base, "dirty": "unknown"}
        if not dirty:
            return base
        # A DIGEST of the enumeration, not the enumeration: the caller hashes this into one opaque
        # token, and carrying a few hundred porcelain lines through `metric_provenance` on every
        # node would bloat the durable record for bytes nobody reads back.
        #
        # Through the SHARED tail (`core/jsonutil.py::canonical_json_digest`, doc 25 CO-08) and not a
        # local `sha256(canonical_json(...))`: the hand-rolled copy called `.encode()` on a value that
        # `canonical_json` already returns as BYTES, so every dirty tree raised into the call site's
        # `except Exception: _substrate = None` and recorded no substrate at all — silently absent on
        # exactly the uncommitted-edit case this function's own docstring says it exists for.
        from looplab.core.jsonutil import canonical_json_digest
        digest = canonical_json_digest(dirty)
        # That tail fails CLOSED (None) for a value with no canonical form. "Dirty, but undigestable"
        # must not fall back to `base`, because `base` IS the spelling of a clean tree: such a node
        # would then read SAME against a genuinely clean one, which is the confidently-wrong record
        # this discriminator exists to refuse. An opaque marker keeps it refusable.
        return {**base, "dirty": digest[:16] if digest else "unknown"}

    def seed_workspace(self, workdir) -> None:
        """RepoTask (ADR-7): materialize the editable repo tree(s) into the eval workdir, plus
        any runtime-mounted reference repos and data files. Phase 4: each editable repo is
        mounted at its own subdir (name=".") -> workspace root). The agent's `Node.files` edits
        are applied on top by `_write_node_files`; task assets win last. No-op for non-repo
        tasks."""
        if not self._e._repo_spec:
            return
        from looplab.engine.workspace_seed import SeedOps, seed_candidate_workspace
        wd = Path(workdir)
        sp = (self._e.tracer.span("seed_workspace") if self._e.tracer is not None
              else __import__("contextlib").nullcontext(None))
        with sp as _h:
            # THE ORDER lives in `workspace_seed.seed_candidate_workspace` (its docstring holds the
            # safety argument for it, and `MountCollision` the one for the guard), because the
            # Developer's disposable candidate has to be materialized the same way and had a second
            # hand-written copy of this sequence until 2026-08-19. What stays HERE is what only the
            # engine has: the span, the domain event, and the seams — the primitives are passed as
            # this seeder's own bound methods so `Engine._seed_repo_tree` / `_link_input` /
            # `copy_input` remain the patch points they have always been.
            rows = seed_candidate_workspace(
                self._e._repo_spec, wd, seed_mode=(self._e._seed_mode or "auto"),
                ops=SeedOps(seed_repo_tree=self._e._seed_repo_tree,
                            seed_protected_files=self.seed_protected_files,
                            link_input=self._e._link_input,
                            copy_input=self.copy_input))
            seeded: list[str] = []
            for row in rows:
                if row["kind"] == "editable":
                    seeded.append(f"{row['name']}[{row['mode']}]:" + (
                        "copytree" if row["count"] < 0 else f"{row['count']} tracked"))
                elif row["kind"] == "protected":
                    seeded.append(f"{row['name']}:protected[{len(row['files'])}]:"
                                  + ",".join(row["files"][:5]))
                elif row["kind"] == "reference":
                    seeded.append(f"ref:{row['name']}->link")
                else:
                    seeded.append(f"data:{row['name']}->{row['action']}")
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
