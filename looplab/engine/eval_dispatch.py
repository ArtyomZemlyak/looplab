"""Eval dispatch (run-setup gating, data binds, the eval entrypoint, sweep collapse) for the
engine — extracted from orchestrator.py as a MIXIN: `class Engine(EvalDispatchMixin, …)`
inherits these methods unchanged, so there is ZERO call-site churn and `self` here IS the
engine. The method bodies are verbatim moves and read engine attributes freely (`_eval_spec`,
`_repo_spec`, `_run_setup_lock`, `trust_mode`, `sandbox`, `store`, …), exactly as they did
inside the class. `_run_eval` is instance-monkeypatched by tests — a mixin preserves that seam.

Runtime deps (`command_eval`, `_run_argv`, `_to_float`) stay method-local so monkeypatching
through their source modules keeps working."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from looplab.core.models import normalize_extra_metrics
from looplab.engine.shared import effective_researcher_eval_timeout
from looplab.events.replay import fold
from looplab.events.types import (EV_RUN_SETUP_FINISHED, EV_RUN_SETUP_STARTED,
                                  SETUP_THREAD_APPENDABLE)

# THE engine sentinel (engine/options.py): `_evaluate` passes it into `_run_eval` positionally
# (as `next_start`), so the identity check here MUST see the same object the orchestrator uses.
from looplab.engine.options import _UNSET

_LOG = logging.getLogger(__name__)

# How many declared requirement lines the durable `deps_declared` receipt carries. Well past any
# real file (the live dense-retrieval repo declares 21) and small enough that a pathological
# generated requirements.txt cannot bloat every reader of the event log. Overflow is COUNTED, never
# silently dropped — see `_record_declared_deps`.
_DECLARED_PINS_CAP = 400

# How many DISTINCT dependency declarations one run may install (the run's own baseline counts as
# the first). The digest gate and the seen-set already stop repetition; this bounds VARIETY, which
# is what an agent that rewrites requirements.txt every node produces. A module constant rather than
# a `Settings` field, matching its sibling bound `engine/triage.py::_MAX_DEP_ROUNDS`: both are
# runaway guards on the same shared interpreter, and neither is a knob an operator tunes — the knob
# is `auto_install_deps`, which turns the whole capability off.
_MAX_NODE_DEP_SYNCS = 8


class EvalDispatchMixin:
    """The engine's eval-dispatch cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    def _ensure_run_setup(self) -> None:
        """Run the eval's RUN-LEVEL `run_setup` exactly ONCE, before the first eval — e.g. a one-time
        dependency install into the shared interpreter (the autonomy default when deps are stable
        across experiments). Distinct from per-node `setup`, which reinstalls before EVERY eval. Runs
        in the first editable repo's SOURCE dir so `-r requirements.txt` resolves; output streams to
        `run_setup.log`. A non-zero/timed-out run_setup ABORTS the run (the env would be unusable).
        Only in trusted_local (an untrusted/docker eval is a fresh container — use per-node `setup`).
        When `run_setup` is unset the command is DERIVED from what the repo declares
        (`_settle_declared_deps`) — LoopLab preparing its own environment is the product promise, and
        an operator who has to know their repo ships a `requirements.txt` is the bug. No-op only when
        the repo declares nothing we act on. The in-process guard is set only AFTER a successful install, so
        a concurrent worker on the lock-free fast path blocks on the lock rather than racing ahead to
        evaluate against a half-installed interpreter.

        Honest delivery contract: EXACTLY-once for a command that reported success, AT-LEAST-once
        across a kill that lands between `run_setup_started` and `run_setup_finished`. LoopLab cannot
        make an arbitrary operator command transactional, and refusing to re-run would leave the
        interpreter permanently half-installed with no way forward, so the repeat happens — but it is
        stamped `after_interrupted_attempt` in the log instead of being indistinguishable from a first
        attempt. Give `run_setup` an idempotent command (a plain `pip install` is) whenever the repeat
        would otherwise cost money or mutate shared state."""
        if self._run_setup_done:
            return
        # Serialize the check-then-set: parallel eval worker threads would otherwise all see
        # _run_setup_done == False and launch pip (not concurrency-safe) N times into one interpreter.
        with self._run_setup_lock:
            if self._run_setup_done:
                return
            cmd = self._settle_declared_deps(list((self._eval_spec or {}).get("run_setup") or []))
            if not cmd or self.trust_mode != "trusted_local":
                self._run_setup_done = True
                return
            # Crash-safe exactly-once across resume (arch-review §5 P2): if THIS run_setup command
            # already completed successfully in a prior process (folded from `run_setup_finished`),
            # skip it — don't re-install deps on every resume. The in-memory flag above still guards
            # the concurrent case within one process; this closes the cross-process/resume gap.
            from looplab.core.models import run_setup_key
            if run_setup_key(cmd) in fold(self.store.read_all()).run_setup_done:
                self._run_setup_done = True
                return
            # Set the in-memory guard only AFTER the install SUCCEEDS. The install runs while THIS lock
            # is held, so a concurrent worker that reached the lock-free fast path (top) still sees the
            # flag False mid-install and blocks on `_run_setup_lock` here — waiting for the install to
            # finish instead of racing ahead and evaluating against an unprepared interpreter. A failed
            # install leaves the flag False and re-runs (a non-zero run_setup aborts the run anyway, and
            # the fold records run_setup_done only for exit_code==0, so resume also re-runs a failed one).
            # The Declaration travels only when the command is OURS: an operator's own `run_setup`
            # is run exactly as written or not at all, so it is never rewritten into a reduced form.
            self._do_run_setup(cmd, declared=(self._declared_deps()
                                              if getattr(self, "_deps_setup_derived", False)
                                              else None))
            self._run_setup_done = True

    # The closed vocabulary of what a run DID about its repo's declared dependencies. A bare string
    # is what `deps_declared.action` carries into the durable log, so a typo'd literal here would be
    # invisible to every reader; `tests/test_declared_deps.py` drives the table from this tuple.
    DECLARED_DEPS_ACTIONS: tuple[str, ...] = (
        "installed",             # nothing operator-authored; we derived and ran the `-r` install
        "operator_run_setup",    # the operator wrote their own run_setup — it wins, untouched
        "auto_install_disabled",  # auto_install_deps=false: the operator owns this environment
        "refused_untrusted_tier",  # a declaration exists but this tier runs --network none
        "nothing_declared",      # nothing we act on (`observed` may still name what we saw)
        "installed_partial",     # …minus lines no index serves; a REVERSIBLE policy, see _do_run_setup
        "node_resync",           # a node's declaration differs from the run's — the Developer's ask
        "node_resync_failed",    # …and installing it did not succeed (the node evals unchanged)
        "node_resync_capped")    # …and this run has spent `_MAX_NODE_DEP_SYNCS`; refused

    def _repo_deps_root(self) -> str:
        """The directory whose dependency declaration governs this run: the first editable repo's
        SOURCE dir. The SAME rule `_do_run_setup` uses for its cwd, and deliberately so — the derived
        command is `-r <basename>`, which only resolves if it runs where the file was found.

        EMPTY when there is no editable repo, and `find_declaration` answers "nothing" for an empty
        root. `_do_run_setup` falls back to the run dir instead, because a cwd must exist for an
        operator's arbitrary command — but a run DIRECTORY is not a repo, and reading a declaration
        out of one would let a stray `requirements.txt` beside `events.jsonl` govern installs."""
        # `getattr`, not attribute access: this is reached from `_ensure_run_setup`, whose callers
        # include non-repo tasks and the test doubles that stand in for them — an Engine without a
        # repo spec is a legitimate shape here, and reading it unguarded turned "this task declares
        # no dependencies" into an AttributeError from inside a setup thread.
        eds = (getattr(self, "_repo_spec", None) or {}).get("editables", [])
        return eds[0]["path"] if eds else ""

    def _node_deps_root(self, workdir) -> str:
        """Where the SAME repo's declaration lives inside a node's workdir — the mirror of
        `_repo_deps_root`. The single-repo shorthand mounts at the workspace root (`name == "."`);
        an `editables` entry mounts under its own subdir, so the file the agent edited is at
        `<workdir>/<name>/requirements.txt`, not at the workspace root."""
        eds = (self._repo_spec or {}).get("editables", [])
        name = (eds[0].get("name") or ".") if eds else "."
        base = Path(str(workdir))
        return str(base if name in (".", "") else base / str(name).rstrip("/"))

    def _declared_watchlist(self) -> list:
        """The distributions whose versions a receipt reports on: exactly the ones the repo
        declares. Not "everything installed" — that is thousands of rows per event and most of it is
        transitive noise; not the `run_started.env` list either, which is a FIXED 17-package
        calibration identity that happens to contain none of this repo's pins."""
        decl = self._declared_deps()
        return sorted(decl.pins) if decl.found else []

    def _env_versions(self, watch) -> dict:
        from looplab.runtime import deps
        return deps.installed_versions(watch, python=getattr(self.sandbox, "python", None)) if watch else {}

    def _env_delta_since(self, watch, before) -> dict:
        """What moved, measured against `before`. Bounded by construction — one entry per DECLARED
        distribution at most, and the declaration itself is capped at read time."""
        from looplab.runtime import deps
        return deps.version_delta(before, self._env_versions(watch))

    def _sync_node_deps(self, workdir) -> None:
        """Install this NODE's dependency declaration when the Developer changed it.

        THE DELIBERATE-CHANGE PATH. The owner's requirement is that a Developer wanting a new
        library or a different version rewrites the requirements file and the deps get reinstalled.
        The write half already worked — `requirements.txt` is an ordinary file in the edit surface
        (under the composable `repo:` default `["**/*"]`; NOT under the direct `RepoTask` default
        `["**/*.py"]` — see `_record_declared_deps`, which reports which). The read half did not
        exist: nothing re-read the file, so an edit was inert and the agent's only feedback was the
        same ImportError it started with.

        WHAT STOPS IT THRASHING THE ENVIRONMENT, in order:
          1. **A content DIGEST, not a timestamp** — the fast path. A node whose declaration is
             byte-identical to the run's does nothing at all: no set, no subprocess, no event, no
             lock. That is the overwhelmingly common case (every node inherits the file unchanged),
             so the steady-state cost of this hook is one small read and a sha256. It is deliberately
             NOT an independent CORRECTNESS bound: rule 2 below reaches the same answer for the same
             input, which is why deleting the comparison is behaviour-preserving and no test catches
             it (checked by mutation, 2026-08-07). What it buys is that the common path never
             contends for `_dep_lock` — a node that did not touch the file must not wait behind
             another node's multi-minute pip, which is the same reason `crash_repair._prepare_env`
             parses its candidates before taking that lock.
          2. **A per-run SET of digests already installed**, seeded with the run's own baseline. This
             is the rule; 1 is its shortcut. It additionally covers a node that REVERTS to an earlier
             file, so an agent oscillating between two dependency sets pays once for each rather than
             once per node. Checked and extended UNDER `_dep_lock` with 3 and the install: it is a
             check-then-act over run-global state, and two eval workers reach it concurrently.
          3. **A hard per-run cap** (`_MAX_NODE_DEP_SYNCS`). 1 and 2 bound repetition, not variety:
             an agent that appends a distinct comment line each node produces a new digest every
             time. The cap is what makes that terminate, and the refusal is RECORDED so it reads as
             a bound being hit rather than the edit being ignored.
          4. **`_dep_lock`.** pip is not concurrency-safe and this is a shared interpreter; the
             crash-time installer already serializes on this lock, so the two cannot interleave.

        WHAT THIS IS NOT: a per-node environment. The install lands in the interpreter EVERY
        concurrent eval shares, so a node that changes a version changes it under its siblings.
        That is not a new hazard — `crash_repair` has pip-installed into this same shared
        interpreter from a worker thread since it was written — but it is the reason the honest
        answer here is a per-node venv, which is deliberately out of scope. The bound above plus the
        `deps_declared` receipt make the exposure visible and finite rather than silent.

        Best-effort throughout: a failed install is recorded and the node evaluates against the
        unchanged environment (its own crash then drives the normal repair path). It must not abort
        the RUN the way a failed run-setup does — a run-setup failure means the operator's
        environment never came up, while this one means one node's speculative dependency edit did
        not take."""
        if not getattr(self, "_auto_install_deps", False):
            return
        from looplab.runtime import deps
        base = self._declared_deps()
        node_decl = deps.find_declaration(self._node_deps_root(workdir))
        if not node_decl.found or node_decl.digest == base.digest:
            return                       # gate 1: unchanged — the common path, and it costs nothing
        argv = deps.declaration_argv(node_decl, python=getattr(self.sandbox, "python", None))
        from looplab.runtime.sandbox import _run_argv
        log = str(Path(self.run_dir) / "run_setup.log")   # same log as run-setup: one install story
        # Watch the UNION of what the run declares and what this node declares: a node that DROPS a
        # pin moves that distribution too, and watching only the node's list would miss it.
        watch = sorted(set(self._declared_watchlist()) | set(node_decl.pins))
        # Gates 2 and 3 are CHECK-THEN-ACT over run-global state and share `_dep_lock` with the
        # install, so two eval workers whose nodes both changed the file cannot both pass the cap or
        # both install. Gate 1 above is what keeps this lock off the common path — a node that did
        # not touch the file must never wait behind a multi-minute pip, which is the same reason
        # `crash_repair._prepare_env` parses its candidates before contending for this lock.
        with self._dep_lock:                              # gate 4: serialize with the crash installer
            seen = self._deps_synced_digests
            if not seen and base.digest:
                seen.add(base.digest)     # the run's own baseline is the first of the allowance
            if node_decl.digest in seen:
                return                    # gate 2: this exact file was already installed this run
            seen.add(node_decl.digest)
            if len(seen) > _MAX_NODE_DEP_SYNCS:
                self._record_declared_deps(node_decl, "node_resync_capped", [])
                return                    # gate 3: variety bound
            before = self._env_versions(watch)
            rc, out, err, timed = _run_argv(argv, node_decl.root, self._dep_install_timeout,
                                            log_path=log)
            delta = self._env_delta_since(watch, before)
        if rc != 0 or timed:
            _LOG.warning("node dependency re-sync failed (exit=%s, timed_out=%s); the node evaluates "
                         "against the unchanged environment — see %s", rc, timed, log)
        # The declaration we CACHE stays the run's baseline: the node's file is what a node asked
        # for, not what the repo declares, and adopting it would make the next node's diff read
        # against a sibling's speculative edit rather than against the source tree.
        self._record_declared_deps(node_decl, "node_resync" if (rc == 0 and not timed)
                                   else "node_resync_failed", argv, delta=delta)

    def _declared_deps(self):
        """This run's `runtime.deps.Declaration`, read once and cached.

        Cached because it is consulted twice for different reasons and must give the same answer to
        both: here, to decide what to install, and at CRASH time
        (`crash_repair.py::_install_missing`), to keep a bare-name install from moving a package the
        repo pinned. If the two reads disagreed — because the Developer rewrote the file mid-run —
        the run would install one set of pins and enforce another, which is a worse failure than
        either policy alone. Lazily initialized rather than set in `Engine.__init__`: this is a
        filesystem read, ~170 test call sites construct the Engine directly, and a run whose
        `_ensure_run_setup` never fires should never pay for it."""
        decl = getattr(self, "_deps_declaration", None)
        if decl is None:
            from looplab.runtime import deps
            decl = deps.find_declaration(self._repo_deps_root())
            self._deps_declaration = decl
        return decl

    def _settle_declared_deps(self, operator_cmd: list) -> list:
        """Decide what this run installs from the repo's own declaration, record the decision, and
        return the argv `_ensure_run_setup` should run.

        THE COMPOSITION RULE: an explicit operator `run_setup` WINS ENTIRELY. Not prepended, not
        merged, not appended. Someone who spelled a setup command meant it, and all three ways of
        combining are wrong in a way the operator cannot undo — a prepend double-installs for the
        (common) operator whose command already IS `pip install -r requirements.txt`, and an operator
        who deliberately curated an environment against the repo's pins has no spelling left that
        means "do not install these". The derived command is a DEFAULT, and a default that overrides
        an explicit value is not a default.

        The three gates, in the order they are asked:
          * `auto_install_deps` (default true) is the operator's one switch for "LoopLab may change
            my environment". It already gates the crash-time installer; gating this the same way is
            what makes the switch mean one thing. Note it is ALSO where the operator says "run
            nothing" now that an empty `run_setup` no longer means that.
          * the tier: installs are `trusted_local` only, because the Docker tiers run
            `--network none` and must not mutate a shared image. A detected declaration there is a
            STATED refusal — the receipt says `refused_untrusted_tier` — rather than a silent skip,
            because "we found your pins and could not honour them" is exactly the thing an operator
            debugging a version mismatch needs to be told.
          * whether the tree declares anything we act on at all.

        Failure is LOUD by design and inherits `_do_run_setup`'s existing contract: a non-zero
        `-r` install raises and aborts the run. Degrading to "the agent will work it out from
        tracebacks" is what produced 2,345 repair attempts on `runs/rubert-dr-0804`."""
        from looplab.runtime import deps
        decl = self._declared_deps()
        if not decl.found and not operator_cmd:
            action, cmd = "nothing_declared", []          # nothing to do at all
        elif self.trust_mode != "trusted_local":
            # There IS setup to do and this tier cannot do it. Asked BEFORE "who authored the
            # command", because the answer does not depend on that: `_ensure_run_setup` has always
            # skipped an operator's own `run_setup` here too, and recording that as
            # `operator_run_setup` would claim we ran a command we did not.
            action, cmd = "refused_untrusted_tier", []
        elif operator_cmd:
            action, cmd = "operator_run_setup", list(operator_cmd)
        elif not getattr(self, "_auto_install_deps", False):
            # Reached only on a trusted tier, so `_auto_install_deps` being False here can only mean
            # the operator switched `auto_install_deps` off — a different fact from the tier refusal
            # above, and it leads to a different fix.
            action, cmd = "auto_install_disabled", []
        else:
            action = "installed"
            cmd = deps.declaration_argv(decl, python=getattr(self.sandbox, "python", None))
        assert action in self.DECLARED_DEPS_ACTIONS
        # Whether the command about to run is OURS. Only a derived command may be retried in reduced
        # form (`_do_run_setup`); an operator's own is run exactly as written or not at all.
        self._deps_setup_derived = (action == "installed")
        self._record_declared_deps(decl, action, cmd)
        return cmd

    def _record_declared_deps(self, decl, action: str, cmd: list, *, delta=None, dropped=None) -> None:
        """Append the once-per-run `deps_declared` receipt: what the repo asked for, what we did.

        This is the row that would have made `runs/rubert-dr-0807` legible. Its log records that
        `pytorch-lightning` was installed and nothing else; the repo pinned 1.5.1, the container held
        2.6.5, and no artifact of that run carries either number. `run_started.env` does not help —
        its `libs` map is a FIXED 17-package list owned by the speculation-quality receipt validator,
        and pytorch-lightning is not in it (nor sentence-transformers, faiss, tensorboard,
        accelerate, datasets). Changing THAT list is not an option: it is the calibration identity a
        published receipt is revalidated against, so widening it silently revokes every issued
        receipt. Hence a separate, purpose-built row.

        DIAGNOSTIC (fold-ignored) and appended from the eval WORKER THREAD outside `_write_lock`,
        beside the `SETUP_THREAD_APPENDABLE` pair below. Invariant #1's question for a non-folded
        event is "does any reader key on its position?", and the answer here is no by construction:
        `speculation.py::_proposal_authority_seq` excludes `DIAGNOSTIC_EVENTS` wholesale, and
        `evaluate.py::_durable_dep_rounds` keys on `deps_installed` alone. Membership is asserted at
        the append site the way the train-monitor asserts it.

        Idempotent across resume ON THE FACTS: a re-append is skipped only when an existing row
        already carries this digest AND this action. A resume that finds a DIFFERENT digest has
        genuinely re-observed a rewritten declaration and must say so — that row is the audit trail
        for the Developer-requested change."""
        from looplab.events.types import DIAGNOSTIC_EVENTS, EV_DEPS_DECLARED
        # Bounded: a declaration is operator/agent-authored text and this row is durable. The pins
        # are the load-bearing part (they are what a version dispute is settled against), so they are
        # carried whole up to a cap, with the overflow COUNTED rather than silently dropped.
        pins = dict(list(decl.pins.items())[:_DECLARED_PINS_CAP])
        data = {
            "root": decl.root, "file": decl.filename, "digest": decl.digest, "action": action,
            "pins": pins, "pin_count": len(decl.pins),
            "pins_truncated": len(decl.pins) > _DECLARED_PINS_CAP,
            # The directives (`-e .`, `--index-url …`, `-r other.txt`) are the part of the file we
            # do NOT follow. Naming them is what stops the receipt reading as "these 21 lines are
            # everything the repo asked for" when a `-r base.txt` pointed somewhere we never looked.
            "directives": [str(d)[:200] for d in decl.directives[:_DECLARED_PINS_CAP]],
            # Declarations we saw and deliberately left alone (pyproject.toml, environment.yml …).
            "observed": list(decl.observed),
            "command": list(cmd),
            # Only the node-resync path measures a delta here; the run-setup install reports its own
            # on `run_setup_finished`, which is where its exit code lives — one install, one row.
            "env_delta": dict(delta or {}),
            # Declarations we could not honour, per line, verbatim from pip. Written from the SAME
            # list `run_setup_finished.dropped_requirements` carries (one producer,
            # `_retry_without_unsatisfiable`) — it appears here too because this row is the one that
            # NAMES the decision (`installed_partial`), and a decision that does not carry its own
            # evidence sends the reader to another event to find out what it means.
            "dropped": list(dropped or []),
        }
        try:
            prior = [e for e in self.store.read_all() if e.type == EV_DEPS_DECLARED]
        except Exception:  # noqa: BLE001 - a receipt must never be what takes a run down
            prior = []
        if prior:
            last = prior[-1].data or {}
            if last.get("digest") == decl.digest and last.get("action") == action:
                return
        assert EV_DEPS_DECLARED in DIAGNOSTIC_EVENTS
        self.store.append(EV_DEPS_DECLARED, data)

    def _do_run_setup(self, cmd: list, declared=None) -> None:
        from looplab.core.models import run_setup_key
        from looplab.runtime import deps
        from looplab.runtime.sandbox import _run_argv
        # ONE root rule, shared with the declaration reader — but an arbitrary operator command
        # needs a cwd that EXISTS, so a repo-less run falls back to the run dir (see the docstring
        # there for why the declaration reader must NOT make that fallback).
        cwd = self._repo_deps_root() or str(self.run_dir)
        to = float((self._eval_spec or {}).get("run_setup_timeout", 1800.0))
        # A prior process appended this command's `run_setup_started` and never appended its finish:
        # its side effects may be complete, partial, or absent and no receipt can say which. The
        # attempt receipt is what makes that ambiguity SURVIVE the crash — see the delivery contract
        # in `_ensure_run_setup`. Stamping the repeat keeps `run_setup.log` and the event log honest
        # about how many times an arbitrary command actually ran.
        interrupted = run_setup_key(cmd) in fold(self.store.read_all()).run_setup_open
        if interrupted:
            _LOG.warning("run_setup %r is being re-executed after an interrupted attempt: its side "
                         "effects may be applied twice", cmd)
        # These are FOLDED events appended from an eval WORKER THREAD (`_run_eval` runs under
        # `anyio.to_thread` from `_evaluate`), so they sit outside `_write_lock` and outside
        # BACKGROUND_APPENDABLE / DIAGNOSTIC_EVENTS / the per-node parallel-build seam. That is
        # invariant #1's remaining thread-side exception, and it is now a REGISTRY rather than a
        # prose claim: `SETUP_THREAD_APPENDABLE` states the conditions (append serializes bytes,
        # `_run_setup_lock` makes this once-per-run, the fold keys run_setup_open/done purely BY
        # COMMAND), the asserts below pin membership at the append sites the way the train-monitor
        # pins DIAGNOSTIC_EVENTS, and tests/test_setup_thread_appendable.py guards both ends.
        assert EV_RUN_SETUP_STARTED in SETUP_THREAD_APPENDABLE
        assert EV_RUN_SETUP_FINISHED in SETUP_THREAD_APPENDABLE
        self.store.append(EV_RUN_SETUP_STARTED,
                          {"command": cmd, "cwd": cwd, "after_interrupted_attempt": interrupted})
        log = str(Path(self.run_dir) / "run_setup.log")
        # What the environment held for the DECLARED distributions before this command ran. The
        # `-r` install honours the repo's pins, and on the live testbed honouring them DOWNGRADES a
        # package (`pytorch_lightning==1.5.1` into a container carrying 2.6.5). That is a policy
        # question the operator owns, and one they cannot even be asked if no artifact records that
        # it happened — so the delta is measured here rather than inferred later. Measured around
        # an OPERATOR's command too: their `pip install -r …` moves the same versions.
        watch = self._declared_watchlist()
        before = self._env_versions(watch)
        rc, out, err, timed = _run_argv(cmd, cwd, to, log_path=log)
        # ONE DEAD LINE IN A REAL REPO'S requirements.txt MUST NOT STOP THE LAB.
        #
        # "The install could not run" and "one declared line has no distribution anywhere" are
        # different failures, and only the first is a reason to refuse to start. Live and measured
        # 2026-08-07: the dense-retrieval testbed declares `ecom-mlflow` at requirements.txt:24,
        # nothing on this box serves it (no PIP_INDEX_URL, no pip.conf), and it is imported only from
        # `test*.py` files the eval pipeline never executes — so under a whole-file contract every
        # rubert run aborts at run start over a package no node would have imported.
        #
        # So: retry ONCE without exactly the unresolvable lines, and record per line which and why.
        # WHAT STAYS FATAL, deliberately — a pip that crashed, a dead or missing index, any non-zero
        # exit with no per-requirement complaint, a declaration too tangled to rewrite faithfully
        # (`deps.can_reduce`), an unresolvable name the declaration does not even contain (a
        # TRANSITIVE dep: dropping the line that pulled it is not what the operator asked), and the
        # reduced set failing in its turn. `unsatisfied_requirements` returning {} is what makes all
        # of those fall straight through to the raise below, unchanged.
        #
        # THIS IS A BEHAVIOUR CHOICE AND IT IS THE OWNER'S TO OVERRULE — recorded as such in the
        # receipt vocabulary (`installed_partial`) and in docs/guide/configuration.md. It is not a
        # silent degrade: if a dropped package IS needed, the run fails at the point of use with a
        # traceback naming it, and `run_setup_finished.dropped_requirements` is the run-start record
        # explaining exactly why it was absent — which is the thing that was missing before.
        dropped: list = []
        if rc != 0 and not timed and declared is not None:
            dropped, retried = self._retry_without_unsatisfiable(declared, out, err, cwd, to, log)
            if retried is not None:
                rc, out, err, timed = retried
        # Carry the command so the fold can key the exactly-once record on it (arch-review §5 P2).
        # Both output tails go through `self._redact`, like every sibling tail (evaluate.py's
        # stdout_tail/stderr, train_monitor's reason). This one is persisted to the DURABLE event
        # log, so under `redact_output=True` — the recommended untrusted-tier posture — a run_setup
        # command's stderr (pip echoing an index URL with an inline token, a git error carrying URL
        # userinfo, a traceback quoting an env var) used to land verbatim and stay there; the
        # RuntimeError below re-leaked the same bytes into the failure message.
        # `env_delta` is an ADDITIVE data field on a FOLDED event (invariant 5): old logs simply do
        # not carry it and every reader defaults it, and the fold has never read this payload beyond
        # `command`/`exit_code`. Empty dict = "nothing the repo declares moved", which is a real and
        # useful answer (a re-run of an already-satisfied install), distinct from an absent field.
        # `dropped_requirements` sits beside `env_delta` on purpose: both answer "what environment
        # did this run actually get?", both are per-item and machine-readable, and an operator must
        # be able to see "we honoured 20 of 21 declarations, `ecom-mlflow` has no distribution"
        # without opening a log. A stderr tail cannot be read by a tool.
        self.store.append(EV_RUN_SETUP_FINISHED,
                          {"command": cmd, "exit_code": rc, "timed_out": timed,
                           "stderr_tail": self._redact((err or "")[-2000:]),
                           "env_delta": self._env_delta_since(watch, before),
                           "dropped_requirements": dropped})
        if rc != 0 or timed:
            # Name the requirements pip refused, when it named any. "run_setup failed, see the log"
            # and "your repo declares ecom-mlflow and nothing serves it" are the same event and not
            # the same message — and this branch is now reached only when the reduced retry did not
            # apply or did not help, which is exactly when the operator needs the name most.
            unsat = deps.unsatisfied_requirements((err or "") + (out or ""))
            named = ("; no distribution for: " + ", ".join(sorted(unsat))) if unsat else ""
            raise RuntimeError(f"run_setup failed (exit={rc}, timed_out={timed}){named}; see {log}\n"
                               + self._redact((err or out or "")[-500:]))

    def _retry_without_unsatisfiable(self, declared, out, err, cwd, to, log):
        """Retry the DERIVED declaration install once, minus the lines pip could not resolve.

        Returns `(dropped records, retry result | None)`. `None` means no retry was attempted and
        the caller's original failure stands — see the conditions in `_do_run_setup`, which is where
        the policy is stated. The dropped records are returned even when the retry then fails, so
        the log says what was tried.

        The reduced file is written into the RUN DIR, never into the operator's repo: a run must not
        mutate the source tree it is reading, and `run_setup.log` records the file pip actually read.
        It is only safe to move the file because `deps.can_reduce` has already refused any
        declaration whose directives name relative paths."""
        from looplab.runtime import deps
        from looplab.runtime.sandbox import _run_argv
        unsat = deps.unsatisfied_requirements((err or "") + (out or ""))
        if not unsat or not deps.can_reduce(declared):
            return [], None
        # Only lines the DECLARATION itself carries. An unresolvable TRANSITIVE dependency names a
        # distribution the operator never wrote down, and the only way to drop it would be to drop
        # whichever declared line pulled it in — a guess about intent, so we refuse and stay fatal.
        droppable = [n for n in unsat if n in declared.pins]
        if not droppable or len(droppable) >= len(declared.pins):
            return [], None            # nothing to drop, or nothing would be left to install
        records = [{"name": n, "line": declared.pins[n],
                    "reason": "no distribution found", "pip": unsat[n]} for n in sorted(droppable)]
        try:
            src = Path(declared.root) / declared.filename
            reduced = Path(self.run_dir) / "requirements.reduced.txt"
            reduced.write_text(
                deps.reduced_requirements(src.read_text(encoding="utf-8", errors="replace"),
                                          droppable), encoding="utf-8")
        except OSError as e:           # unreadable source / unwritable run dir -> the original stands
            _LOG.warning("could not write a reduced declaration (%s); the original failure stands", e)
            return [], None
        _LOG.warning("dependency install retrying without %d unresolvable declaration(s): %s",
                     len(droppable), ", ".join(sorted(droppable)))
        argv = deps.declaration_argv(declared, python=getattr(self.sandbox, "python", None))
        argv = argv[:-1] + [str(reduced)]      # same command, pointed at the reduced copy
        result = _run_argv(argv, cwd, to, log_path=log)
        if result[0] == 0 and not result[3]:
            self._record_declared_deps(declared, "installed_partial", argv, dropped=records)
        return records, result

    def _data_binds(self, workdir) -> Optional[list]:
        """(host_path, read_only) binds for the untrusted tier: every data/reference source that was
        actually materialized as a SYMLINK in `workdir`, bound at its own absolute path inside the
        container (the workspace bind carries only the symlink, which would otherwise dangle there).
        read_only unless the data source's `edit` permission grants in-place writes — so `edit:false`
        is enforced at the MOUNT layer for sandboxed runs, not just in the agent-facing write-tool
        gate. (The trusted_local tier runs on the host and keeps only the tool gate; documented in
        docs/guide/tasks.md.)

        Only ACTUAL symlinks are bound (arch-review §4 P1-8): a source that was copied IN — either
        `mount:false`, or a `mount:true` source on a host where `os.symlink` fell back to a copy
        (Windows without the symlink privilege) — already lives inside the /work bind, so re-binding
        it is redundant, and on Windows its drive-letter path has no valid Linux container target. `workdir`
        is already seeded (`_materialize` runs before `_run_eval`), so the symlink check is reliable."""
        wd = Path(workdir)

        def _linked(name) -> bool:
            try:
                return (wd / name).is_symlink()
            except OSError:
                return False

        binds: list = []
        for name, spec in (self._repo_spec or {}).get("data", {}).items():
            if isinstance(spec, dict):                    # DataSpec dict | bare path (back-compat)
                if spec.get("mount", True) and spec.get("path") and _linked(name):
                    binds.append((spec["path"], not spec.get("edit", False)))
            elif spec and _linked(name):
                binds.append((spec, True))
        for ref in (self._repo_spec or {}).get("references", []):
            if ref.get("mount") and ref.get("path") and _linked(ref["name"]):
                binds.append((ref["path"], True))         # references are read-only by definition
        return binds or None

    def _run_eval(self, node, workdir, env=None, profile=None, cancel=None, start_stage=_UNSET):
        """Eval dispatcher: RepoTask runs the operator's command + reads its metric;
        otherwise the classic solution.py sandbox path. Both return a `RunResult`, so all
        downstream metric/exit/timeout checks are identical.

        Phase 2: the command is built with an eval profile (smoke/full — `profile` arg, else
        the Researcher's `idea.eval_profile`) and, when params_style=cli_overrides, the
        node's params as `key=value` overrides.

        `start_stage`: which pipeline stage to run FROM (earlier stages reused). Default `_UNSET`
        derives it from `node.rerun_stage` (the operator node_reset seam). The inline-repair loop
        passes an EXPLICIT value (a stage name to reuse-into, or None for a full re-run) computed by
        its safe-reuse predicate — passing explicitly avoids the transient `rerun_stage` being reset
        by the loop's re-fold."""
        if self._eval_spec:
            from looplab.runtime import command_eval
            es = self._eval_spec
            self._ensure_run_setup()             # one-time run-level dep install (before the first eval)
            prof = profile or (node.idea.eval_profile if node is not None else None)
            # A7 Strategist fidelity override: when the active strategy pins smoke/full and the node
            # didn't request a profile, use the strategy's. An explicit `profile` arg (confirm=full)
            # always wins. "adaptive" leaves _strategy_fidelity None => the Idea's own profile.
            if prof is None and self._strategy_fidelity in ("smoke", "full"):
                prof = self._strategy_fidelity
            params = node.idea.params if node is not None else {}
            cmd, timeout = command_eval.build_command(es, params, prof)
            root = str(Path(workdir).resolve())               # repo/workdir root
            # The Developer's deliberate-change path: if THIS node's dependency declaration differs
            # from the one the run installed, install it before the eval. Digest-gated, so the
            # unchanged case (every node that did not touch the file) costs one read — see
            # `_sync_node_deps` for the four bounds and for why this is not a per-node environment.
            self._sync_node_deps(root)
            stages = self._resolve_stages(root, es, params,   # cmd-authoritative pipeline (+ %params% per stage)
                                          score_cmd=cmd, score_timeout=timeout)  # profile/timeout survive pipeline mode
            check_fn = (self._stage_check_fn(node)            # Phase 3: inter-stage verify (only if any stage asks)
                        if stages and any(s.get("check") for s in stages) else None)
            cwd = self._sandbox_cwd(workdir, es.get("cwd", "."))
            # PREFLIGHT the resolved chain for a PROTECTED script the workdir doesn't hold, BEFORE any
            # stage runs. The one failure the repair loop structurally cannot fix (the agent may not
            # write a protected path) was also the most expensive to discover: the `score` stage is
            # LAST, so a node paid for its whole train and then died in 0.06s on
            # `python: can't open file '…/looplab_eval.py'`. Reported as a plain failed RunResult
            # rather than raised: an exception here escapes the eval worker and leaves the node with
            # NO terminal event (invariant 2), re-raising on every resume.
            _unrunnable = self._unrunnable_protected_scripts(
                [(s.get("name"), s.get("command")) for s in stages] if stages
                else [("score", cmd)], root, cwd)
            if _unrunnable:
                return command_eval.RunResult(
                    exit_code=2, stdout="", metric=None, timed_out=False,
                    stderr="\n".join(self.PROTECTED_SCRIPT_MISSING.format(stage=st, script=sc)
                                     for st, sc in _unrunnable))
            # untrusted tier (Phase 4): sandbox the eval in docker, mounting the workspace
            # root so the cwd subdir + host metric reading line up. Fails loudly w/o docker.
            # Symlink-mounted data/reference sources ride along as same-path binds (the /work
            # bind alone leaves their symlinks dangling in the container) — read-only unless the
            # source's `edit` permission grants writes (mount-layer enforcement of edit:false).
            wrap = (command_eval.make_docker_wrap(
                        root, self.docker_image,
                        mem=self.sandbox_memory or None, cpus=self.sandbox_cpus or None,
                        runtime=("runsc" if self.trust_mode == "hostile" else None),
                        binds=self._data_binds(workdir),
                        env=env)   # forward LOOPLAB_EVAL_SEED etc. into the container (per-eval env)
                    if self.trust_mode in ("untrusted", "hostile") else None)
            # The operator's declared metric SUBJECT, filtered to strings HERE rather than trusted:
            # `_grandfathered` reloads a recorded `task.snapshot.json` WITHOUT re-validating it, so
            # the pydantic guard on `EvalSpec.metric` is not total over what reaches this line, and a
            # non-string entry becomes `Path(workdir) / 123` -> an uncaught TypeError out of the eval
            # worker (no node terminal, re-dying on every resume).
            _mspec = es.get("metric") if isinstance(es.get("metric"), dict) else {}
            _subject = [s for s in (_mspec.get("subject") or []) if isinstance(s, str) and s.strip()]
            res = command_eval.run_command_eval(
                cmd, cwd, timeout, es["metric"], env,
                setup=es.get("setup") or None, setup_timeout=es.get("setup_timeout", 600.0),
                setup_cwd=root,                               # deps install at the repo root
                cross_check=es.get("cross_check"),            # Phase 4 drift cross-check …
                drift_tolerance=float(es.get("drift_tolerance", 1e-6)),
                enforce_drift=(self.eval_trust_mode == "ratify_freeze_drift"),
                wrap=wrap,
                metrics=es.get("metrics") or None,            # #5 multi-objective …
                constraints=es.get("constraints") or None,
                tracer=self.tracer,                           # child spans: setup/command/read
                cancel=cancel,                                # operator mid-eval node_abort
                log_dir=root,                                 # live setup.log/eval.log in the node workdir
                stages=stages,                                # multi-stage pipeline (Phase 1); None = single command
                start_stage=((node.rerun_stage if node is not None else None)
                             if start_stage is _UNSET else start_stage),  # Phase 2: re-run from a stage
                stall_cap=self.eval_stall_timeout_s,          # #6: operator-set silence-before-kill cap (0 = off)
                check_fn=check_fn,                            # Phase 3: optional inter-stage agentic verify
                # METRIC PROVENANCE: what the number is a claim ABOUT. Gated on the rung so `off` is
                # byte-identical to the behaviour before this shipped — and so a RESUMED pre-2026-08-13
                # run (which `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` pins to `off`) does not acquire a
                # different record mid-log. The binding is a READ; whether an unbound metric is a
                # violation is decided at the terminal, in `engine/evaluate.py`.
                subject=(_subject if str(getattr(self, "metric_subject", "audit") or "audit") != "off"
                         else None))
        else:
            # Intra-node sweep nodes run a whole grid in one process, so they need ~N× the
            # single-eval budget. `sweep_timeout_mult` scales the wall-clock for sweep nodes only;
            # _kill_tree + the mid-eval cancel watcher still bound a runaway. (The RepoTask path
            # gets its per-profile timeout from build_command above.)
            timeout = self.timeout
            if node is not None and node.idea.is_sweep:
                timeout = self.timeout * self.sweep_timeout_mult
            # Researcher-sized per-node budget (e.g. a neural-net / large-ensemble idea that needs longer
            # than the run default) — honored ONLY when the governance matrix grants the researcher the
            # `timeout` setting; otherwise the run-wide budget stands. This is the "auto" per-node mode.
            # The same governed canonicalization feeds native-batch identity and execution. Keeping
            # this as the single consumer prevents admission from fingerprinting a timeout we ignore.
            etv = effective_researcher_eval_timeout(
                self, node.idea if node is not None else None)
            if etv is not None:
                timeout = etv
            res = self.sandbox.run(node.code, str(workdir), timeout, env, cancel=cancel)
        # Intra-node sweep: if the solution reported a grid of trials, collapse them into the node's
        # scalar `metric` (the best feasible trial under the task direction) so fold/best-selection/
        # improve are untouched. Done BEFORE host grading so a host grader still has the final say on
        # the best trial's predictions file. The full trial list rides along on `res.trials`.
        if res.trials:
            self._apply_sweep_best(res)
        # Out-of-process host-side grading (general): override the (ignored) self-reported metric with
        # the HOST's score of the candidate's predictions. Applied for BOTH the command-eval and the
        # sandbox path, so a task that exposes host_grader() is always host-scored — and so EVERY
        # sandbox-path eval (normal AND the multi-seed confirm pass, both call _run_eval) is graded
        # the same way. host_grader takes precedence: its score replaces any self-reported metric.
        if self._host_grader is not None:
            res = self._apply_host_grade(res, workdir)
        return res

    def _apply_sweep_best(self, res):
        """Collapse an intra-node sweep's `res.trials` into the node's scalar `metric`: pick the
        best trial that produced a usable (finite) metric, under the task direction. Keeping
        `metric` a single number means fold, best-selection, confirm and `improve` treat a sweep
        node like any other; the trials are audit/UI only. No usable trial -> no metric (the node
        fails like an empty run, so a sweep where every config crashed can't pass)."""
        from looplab.runtime.sandbox import _to_float
        # Candidate stdout is untrusted input. A syntactically valid `{"trials":[1]}` must fail the
        # node, not raise AttributeError in the host engine. Keep only object-shaped trial records;
        # replay independently validates them into Trial models later.
        trials = [t for t in (res.trials or []) if isinstance(t, dict)]
        scored = [(t, _to_float(t.get("metric"))) for t in trials]
        scored = [(t, m) for t, m in scored if m is not None]
        if not scored:
            res.metric = None
            return
        chooser = min if self.task.direction == "min" else max
        best_t, best_m = chooser(scored, key=lambda tm: tm[1])
        res.metric = best_m
        extra = normalize_extra_metrics(best_t.get("extra_metrics"))
        if extra:
            res.extra_metrics = {**normalize_extra_metrics(res.extra_metrics), **extra}
