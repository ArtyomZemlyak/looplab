"""The run's SETUP PHASE: what a run does once, before its first node (review 2026-09-22, ENG1-04
step 3).

`_setup_phase` is the preflight between entering a run and the search. It appends the identity anchor
`run_started` (only `if not state.run_id`, so a resume that re-enters setup after a crash just past it
re-runs the REST of preflight without minting a second anchor), writes AGENTS.md, pins the data
provenance, records the host grading, profiles the dataset, records the distribution shift and runs
the leakage hard-stop — its steps inside one `setup` span, between `setup_started` and the FOLDED
`setup_finished` that the completion gate (`setup_done`) reads. On a resume it records a changed
workspace or environment instead of pretending the run is reproducible. It folds once, the leakage
stop's own re-read before its final-report CAS, through `shared.py::engine_fold`, so a test that
patches `orchestrator.fold` still steers it.

Its helpers came with it:

* `_setup_manifest` — the P0-3 content-addressed digest `setup_finished` binds completion to, so a
  pre-node resume re-runs preflight when the material it verified has changed;
* `_env_fingerprint` — the P0-5 interpreter + library identity `run_started.env` pins and a resume
  compares against;
* `_dirty_inputs` — the P0-5 dirty-input enumeration `run_started.dirty_inputs` records. It has a
  second caller, `workspace.py::substrate_fingerprint` (every node's substrate digest), which reaches
  it through the Engine (`self._e._dirty_inputs`), so nothing there changed;
* `_task_declared_env` — the module-level predicate behind `run_started.eval_env_absent_from_task`.

The two module constants `_dirty_inputs` reads, `_DIFF_DIGEST_CAP` and `_DIRTY_STATUS_TIMEOUT_S`,
moved WITH it rather than staying behind as seams. A function looks its globals up in the module
that defines the FUNCTION, and a test patches the first and imports the second, so both have to be
spelled on this module. `orchestrator.py` deliberately keeps no copy of either, nor of
`_task_declared_env`: a stale `monkeypatch.setattr(orchestrator, "_DIFF_DIGEST_CAP", …)` is then an
AttributeError, not a patch that silently reaches nothing — the failure step 0 removed for `fold`.

WHAT STAYS IN `orchestrator.py`: `_enter_run`, which decides WHETHER setup runs (never inside a
half-finished finalization); the workspace delegators (`_workspace_fingerprint` & co.), registered
`FORWARDED_SUBOBJECT_MEMBERS` of the Engine body; and the Phase-3 spec gate (`_run_spec_gates`,
`_activate_spec`), which runs on every loop turn. The values `run_started` PINS are
`reentry.py::ReentryMixin`'s (`_run_start_pinned_values`, `_run_start_settled_widths`), beside the
re-entry checks that read them back: this module appends the row they ride in and decides none of
them.

The bodies are byte-for-byte what `orchestrator.py` held (moved by AST line range and asserted
verbatim), and in a mixin `self` IS the Engine, so no call site changed.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Iterable
from pathlib import Path

from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT
from looplab.core.models import RunState
from looplab.core.profile import profile_dataset
from looplab.core.setup_identity import setup_config_hash, setup_manifest_digest
from looplab.engine.shared import engine_fold as fold
from looplab.events.types import (EV_DATA_PROFILED, EV_DATA_PROVENANCE, EV_ENV_CHANGED,
                                  EV_HOST_GRADING, EV_RUN_STARTED, EV_SETUP_FINISHED,
                                  EV_SETUP_STARTED, EV_SETUP_STEP, EV_WORKSPACE_CHANGED)
from looplab.tools.agents_md import generate_agents_md


# P0-5 dirty-input diff digest: the byte ceiling on how much of `git diff HEAD` is hashed before the
# digest is marked truncated (`~`). A real code diff is far under this; beyond it we're diffing a
# tracked data/generated file, where buffering the whole patch would spike run-start memory (a latent
# OOM) and a truncated "did-it-change" signal is enough. Module-level so an operator/test can retune.
_DIFF_DIGEST_CAP = 8 * 1024 * 1024
# The wall the dirty-input enumeration gives `git status --porcelain`. NAMED, and named HERE, because
# the harness has to bound its environment probe by the SAME number: a probe more patient than the
# code it certifies passes on a loaded box while the product's call times out, and the test then
# reports an environment fact as a product defect (measured 2026-08-28 — the probe allowed 120 s
# against this 10, and the guard went red anyway). One spelling, imported by
# `tests/test_setup_completion.py::_fixture_git_reports_dirty`, so the two cannot drift apart again.
_DIRTY_STATUS_TIMEOUT_S = 10


def _task_declared_env(task) -> bool:
    """Does the TASK itself declare the environment its eval needs?

    `RepoTask.eval.env` is the durable home (it rides `task.snapshot.json`); a `Settings.eval_env`
    is the launch-time override that does not. Duck-typed because not every task model has an
    `eval` section, and a task that declares nothing is the common, correct case for tasks whose
    eval needs no environment at all — this only ever qualifies a run that IS using one.
    """
    eval_spec = getattr(task, "eval", None)
    return bool(getattr(eval_spec, "env", None))


class SetupPhaseMixin:
    """The one-time preflight — `run_started`, provenance, profiling, the leakage stop — and its
    resume-time drift records."""

    def _setup_phase(self, state: RunState) -> None:
        # Per-RUN reset of the dep-install circuit breaker: it is a module global, so in the long-lived
        # `looplab ui` server a run that latched (egress blip) would leave auto-install disabled for the
        # next run in the same process until some pip call happens to respond.
        try:
            from looplab.runtime.deps import reset_install_latch
            reset_install_latch()
        except Exception:  # noqa: BLE001 - best-effort; a missing helper must not block setup
            pass
        # A mount the root repo would shadow fails EVERY node's seed; say so before anything is
        # spent (`workspace_seed.preflight_mount_collision`).
        if self._repo_spec:
            from looplab.engine.workspace_seed import preflight_mount_collision
            preflight_mount_collision(self._repo_spec, seed_mode=(self._seed_mode or "auto"))
        # SETUP-COMPLETION GATE (arch-review §3 P0-3): gate on `setup_done` (folded from
        # setup_finished), NOT on run_id. run_started is appended in the MIDDLE of this block — before
        # AGENTS.md/provenance/host-grading/profiling and the leakage hard-stop — so a crash right
        # after it used to make every later resume skip the rest of preflight (leakage included)
        # forever. Gating on setup_done re-runs the body until it actually completes. Legacy logs that
        # never emitted setup_finished but already reached a node (or finished) are treated as
        # set-up-complete via `state.nodes`/`state.finished`, so they never re-run setup.
        # P0-3 material re-verification: on a PRE-node resume, re-run preflight if setup completed
        # against a DIFFERENT material manifest than we now hold (edited config / changed data or
        # workspace) — the `setup_done` boolean alone would skip the leakage/grounding checks on the
        # changed inputs. Only pre-node (a node present => the run is underway; mid-run drift is handled
        # by workspace_changed below). Re-running records a fresh setup_finished with the new manifest,
        # so this can never loop. Old logs (no recorded manifest) keep the pure-boolean behavior.
        _setup_stale = bool(state.setup_done and not state.nodes and state.setup_manifest
                            and self._setup_manifest() != state.setup_manifest)
        if not (state.setup_done or state.nodes or state.finished) or _setup_stale:
            # SETUP PHASE (task + data), an explicit, ONLINE-watchable phase: the pre-node work
            # (fingerprint the workspace, hash data provenance, profile columns, write AGENTS.md) is
            # otherwise silent between run_started and the first node. `setup_started` +/ `setup_step`
            # + `setup_finished` events land in the activity feed live, and a `setup` span (node_id=-1)
            # captures the trace so the UI's Setup pseudo-node shows what happened. setup_finished is
            # now folded (setup_done); the others stay pure observability.
            _su_t0 = time.time()
            self.store.append(EV_SETUP_STARTED,
                              {"phase": "task+data", "repo": bool(self._repo_spec),
                               "goal": (self.task.goal or "")[:200]})
            def _su_step(step: str, **detail):
                self.store.append(EV_SETUP_STEP, {"step": step, **detail})
            with self.tracer.span("setup", new_trace=True, node_id=-1) as _su:
                def _ev(name, **kv):
                    if _su is not None:
                        _su.event(name, **kv)
                cfg_hash = setup_config_hash(self.task.model_dump(mode="json"))
                # Reproducibility (item #4): pin the editable repo(s)+data fingerprint at start so a
                # resume can tell whether the source workspace changed underneath.
                _ev("workspace_fingerprint")
                wf = self._workspace_fingerprint()
                _su_step("workspace fingerprint", sources=list(wf.keys()))
                # run_started is the one-time identity anchor: append it only if it isn't already
                # recorded, so a resume RE-ENTERING setup after a crash-right-after-run_started (P0-3)
                # re-runs the REST of preflight (leakage) without minting a second run_started.
                if not state.run_id:
                    self.store.append(
                        EV_RUN_STARTED,
                        {
                            "run_id": self.run_dir.name,
                            # Display ids are only unique inside a run root. Cross-run memory uses this
                            # persisted incarnation id so roots named ``run_local`` cannot overwrite or
                            # self-exclude one another.
                            "run_uid": secrets.token_hex(16),
                            "task_id": self.task.id,
                            "goal": self.task.goal,
                            "direction": self.task.direction,
                            "config_hash": cfg_hash,
                            "workspace": wf,
                            # P0-5 environment identity: pin the interpreter + key-lib versions so a
                            # resume can flag a library upgrade that breaks bit-reproducibility.
                            "env": self._env_fingerprint(),
                            # P0-5 dirty-input enumeration: which repo files were uncommitted at start
                            # (repo tasks only; a clean/non-repo run records []). Provenance on top of
                            # the workspace content hash in `wf`.
                            # The source PATHS, not `wf` — `wf` is keyed by label and this reads its
                            # keys as paths (see `workspace.workspace_source_paths`).
                            "dirty_inputs": (self._dirty_inputs(self.workspace.workspace_source_paths())
                                             if self._repo_spec else []),
                            # T2 trust enforcement: recorded here so the pure fold applies the same
                            # gate on replay/resume (config isn't available to `replay.fold`). Absent in
                            # old logs -> "audit" -> byte-identical legacy selection.
                            "trust_gate": self.trust_gate,
                            # Holdout and verifier policy are immutable run-start semantics. Re-entry
                            # restores this shared contract from the fold rather than accepting a later
                            # snapshot edit that would mix incomparable scores or selection rules.
                            **self._run_start_pinned_values(),
                            # F1d: the run-level DECLARED ENVIRONMENT, when there is one. ABSENT
                            # otherwise, which keeps the default `run_started` payload BYTE-IDENTICAL
                            # — the same discipline `_run_start_pinned_values` follows for the Card
                            # selector flag, and here it is load-bearing twice over:
                            # `search/speculation_quality.py::_CALIBRATION_RUN_STARTED_FIELDS`
                            # compares the payload's key SET for equality, so an unconditional new key
                            # would revoke every issued calibration receipt, and the calibration
                            # profile declares no environment.
                            **({"eval_env": dict(self._eval_env)} if self._eval_env else {}),
                            # …AND WHETHER THE TASK ITSELF CARRIES IT. A SETTING rides
                            # `config.snapshot.json`, so a RESUME reproduces it (invariant #6) — but
                            # it does NOT ride `task.snapshot.json`, and the documented way to start
                            # the next run on this box is to COPY that snapshot. So a value that
                            # lives only in the launch line survives every resume and is lost by the
                            # one operation an operator actually performs between runs.
                            #
                            # MEASURED: `eval.env` is null on EVERY task file on this box (v11, v12,
                            # v13 and all three snapshots). v12 was launched from such a copy without
                            # the flag, every node crashed in `botocore ListObjects` on an unset
                            # `VS_LOCAL_DATA_ROOT`, each paid a triage+repair to rediscover the local
                            # corpus, and node 14 died of it (#147). `adapters/repo_task.py::eval.env`
                            # is where the tree already says this fact belongs — "a fact about the
                            # TASK", "every node inherits it instead of each one spending a repair
                            # attempt rediscovering it".
                            #
                            # PRESENT ONLY WHEN THE SETTING IS CARRYING A FACT THE TASK DOES NOT, so
                            # a healthy run's payload stays byte-identical and
                            # `speculation_quality._CALIBRATION_RUN_STARTED_FIELDS` keeps its key-set
                            # equality — the calibration profile declares no environment, so this key
                            # can never appear for it, by the same argument the line above makes.
                            # A NOTICE, NOT A REFUSAL: the run is correct, its successor is the one
                            # at risk.
                            **({"eval_env_absent_from_task": True}
                               if self._eval_env and not _task_declared_env(self.task) else {}),
                            # The setting NAMES the operator spelled explicitly at launch (never
                            # values; credentials cannot be spelled there at all). ABSENT when there
                            # are none, so the default payload stays byte-identical, and never on the
                            # calibration lane, whose receipt pins this payload's KEY SET
                            # (`speculation_quality._CALIBRATION_RUN_STARTED_FIELDS`). The record is
                            # what makes an explicit `-s max_parallel=1` an operator width pin the
                            # Strategist cannot override (`engine/widths.py::operator_width_axes`),
                            # for the whole run and after resume.
                            **({"explicit_settings": list(self._explicit_settings)}
                               if self._explicit_settings and not self._speculation_gate_calibration
                               else {}),
                            # The SETTLED widths, not their AUTO sentinel: re-entry must never
                            # re-derive this run's execution treatment from a different box.
                            **self._run_start_settled_widths(),
                            # …and WHETHER the pinned depth resolved that sentinel. The pin alone
                            # cannot say, and only an AUTO run may ratchet itself down, so leaving
                            # this in the process let a later `looplab run <dir>` under the shipped
                            # `-1` default settle a SPELLED treatment to 0, irreversibly.
                            "speculation_depth_auto": bool(
                                getattr(self, "_speculation_depth_auto", False)),
                            "select_verifier_contract": VERIFIER_SELECTION_CONTRACT,
                        },
                    )
                # AGENTS.md (I18): run-level task-contract provenance. Repo backends receive their
                # task-specific brief directly and retain a seed repo's own AGENTS.md; this manifest
                # mirrors that contract without being copied over repository-owned instructions.
                # Runtime lines remain honest: capable tasks get the auto-install capability sentence,
                # offline/synthetic tasks stay numpy+stdlib (task_runtime_caps returns None for those).
                from looplab.core.hardware import detect_gpu, task_runtime_caps
                _md_caps = task_runtime_caps(self.task, auto_install=self._auto_install_deps,
                                             gpu=detect_gpu() if self._auto_install_deps else None)
                (self.run_dir / "AGENTS.md").write_text(
                    generate_agents_md(self.task, runtime_caps=_md_caps), encoding="utf-8")
                _ev("agents_md")
                _su_step("wrote AGENTS.md")
                # D4 data provenance: pin a content hash of every task asset/dataset into the run so a
                # result is tied to the exact data (repo tasks also pin via `workspace`). Reproducibility.
                prov = {name: hashlib.sha256(
                            c.encode("utf-8") if isinstance(c, str) else bytes(c)).hexdigest()[:16]
                        for name, c in (self._assets or {}).items()}
                if prov:
                    self.store.append(EV_DATA_PROVENANCE, {"assets": prov})
                    _ev("data_provenance", n=len(prov))
                    _su_step("data provenance", assets=list(prov))
                # Out-of-process host-side grading active: record WHICH scorer + how many held-out labels
                # (NEVER the labels themselves — the log is readable). Surfaced in the Trust panel.
                if self._host_grader is not None:
                    hg = self._host_grader
                    evt = {
                        "scorer": hg.get("scorer", "rmse"),
                        "predictions": self._graded_output_name()}
                    if hg.get("kind") == "mlebench":          # real MLE-bench: answers live in the
                        evt["competition"] = hg.get("competition")   # mle-bench data dir, never here —
                        # WHICH PROTOCOL this run scores under (doc 52 §5.1 row 3), so the reader of
                        # the log can tell a search-split number from a test-selected one.
                        evt["protocol"] = ("search_split" if self._search_hidden_ids
                                           else "private_per_node")
                        evt["n_hidden"] = len(self._search_hidden_ids)
                        # so there is no in-memory label list to count; n_labels=0 would mislead the Trust
                        # panel into "nothing held out". Omit it; `competition` signals host-held answers.
                    else:
                        evt["n_labels"] = len(hg.get("labels") or [])
                    self.store.append(EV_HOST_GRADING, evt)
                # Grounding pre-phase (I16): profile the dataset if the task exposes one.
                cols = getattr(self.task, "columns", None)
                if callable(cols):
                    self.store.append(EV_DATA_PROFILED, {"columns": profile_dataset(cols())})
                    _ev("data_profiled")
                    _su_step("data profiled")
                # Distribution shift (docs/BACKLOG.md §15): record how far the deployment sample is
                # from the training one, from the SAME declared data the two rungs around it read.
                # Advisory and appended BEFORE the gate below on purpose — a run the leakage gate
                # aborts is exactly a run whose operator wants to see what the data looked like.
                self._record_distribution_shift()
                # Leakage-first grounding (I9): if the task exposes split/feature/target/time
                # data and a leak is detected, refuse to run — don't produce results on leaky data.
                leakage_blocked = self._leakage_blocks()
            # P0-3: bind this completion to the material it verified (reuse the wf computed above), so a
            # later resume can tell "done for THIS material" from "done for material that has changed".
            self.store.append(EV_SETUP_FINISHED, {"seconds": round(time.time() - _su_t0, 3),
                                                  "manifest": self._setup_manifest(wf=wf)})
            if leakage_blocked:
                # Preserve `_setup_phase`'s direct-call contract while using the same final-report
                # CAS as every other completion. If a control races this append, run()'s top-level
                # leakage gate refolds and retries instead of losing the intent.
                setup_events = self.store.read_all()
                setup_state = fold(setup_events)
                setup_seq = setup_events[-1].seq if setup_events else -1
                self._finish_with_report_if_quiescent(
                    setup_state, {"reason": "leakage"}, after_seq=setup_seq)
        elif self._repo_spec and state.workspace and not state.workspace_changed:
            # Resume (item #4): the editable workspace is copied fresh each node, so if the
            # operator's repo changed since the run started, later nodes silently evaluate a
            # DIFFERENT codebase. Record it instead of pretending the run is reproducible.
            now = self._workspace_fingerprint()
            if now != state.workspace:
                self.store.append(EV_WORKSPACE_CHANGED, {"was": state.workspace, "now": now})
        # P0-5 environment drift: on ANY resume where an env was pinned at run start, flag a Python/
        # library change — a run continued after an upgrade is no longer bit-reproducible, so record it
        # instead of pretending it is. Diagnostic-only (mirrors workspace_changed). state.env is None on
        # the first run (run_started is appended mid-setup, after this fold) and on old logs -> skipped.
        if state.env is not None and not state.env_changed:
            # `not state.env_changed` (F18): emit the drift note ONCE. Without the folded-flag gate a
            # run resumed repeatedly after an env upgrade re-appended an identical env_changed every time.
            _cur_env = self._env_fingerprint()
            if _cur_env != state.env:
                self.store.append(EV_ENV_CHANGED, {"was": state.env, "now": _cur_env})

    def _setup_manifest(self, wf: "dict | None" = None) -> str:
        """P0-3 content-addressed setup: a stable digest of the MATERIAL the task+data preflight
        verified — the config hash, the workspace fingerprint, and the data-asset provenance. Binds
        `setup_done` to the exact inputs so a pre-node resume re-runs preflight (leakage!) when they
        changed rather than trusting a stale boolean. Deterministic (pure content hashes), so an
        unchanged workspace yields the recorded digest and never loops. `wf` may be passed to reuse an
        already-computed fingerprint.

        The hashing itself is `core/setup_identity.setup_manifest_digest` — the quality reader
        re-derives this exact digest to prove calibration evidence came from the shipped writer, and
        `search` may not import the engine, so the derivation lives where both can reach it
        (doc 25 SE-01)."""
        wf = self._workspace_fingerprint() if wf is None else wf
        prov = {name: hashlib.sha256(
                    c.encode("utf-8") if isinstance(c, str) else bytes(c)).hexdigest()[:16]
                for name, c in (self._assets or {}).items()}
        return setup_manifest_digest(self.task.model_dump(mode="json"), wf, prov)

    def _env_fingerprint(self) -> dict:
        """Use the same source-owned environment identity as the quality receipt validator.

        A calibration run is pinned here and re-read later by ``speculation_quality``; two nearly
        identical package lists would make valid local evidence impossible to revalidate (or, worse,
        omit a broken direct dependency from one side).  The shared helper is metadata-only and never
        touches the network.
        """
        from looplab.search.speculation_quality import speculation_environment_fingerprint
        return speculation_environment_fingerprint()

    def _dirty_inputs(self, sources: "Iterable[str] | None") -> "list | None":
        """P0-5 dirty-input enumeration: for each git-repo workspace source, the uncommitted-file LIST
        (`git status --porcelain`) plus a bounded DIGEST of the actual diff vs HEAD (`git diff HEAD`) —
        the EXPLICIT record of which inputs differ from a clean checkout AND a content fingerprint of
        HOW, on top of the HEAD-SHA the workspace fingerprint pins (which is blind to uncommitted work).
        The digest (not the diff TEXT) is stored on purpose: it detects a changed dirty-content across
        runs WITHOUT leaking a secret a raw patch could carry (a pasted key, an edited .env) into the
        world-readable log.

        Corner-case behavior (all best-effort — a source never fails the run):
          * A heavy UNTRACKED artifact costs nothing: `git diff HEAD` never emits untracked files, so
            only its NAME lands in the porcelain list. A heavy TRACKED+modified text file would make
            git stream a giant patch, so the diff is hashed INCREMENTALLY and capped at
            `_DIFF_DIGEST_CAP` — the engine never buffers the whole patch, and an over-cap digest is
            marked `~` (truncated) so a reader knows the tail was not seen.
          * A gitignored file is INVISIBLE here BY DESIGN — porcelain skips it and the repo fingerprint
            is HEAD-only, so declared-non-source scratch (`runs/`, `__pycache__`, `model.pkl`, `.env`)
            never pollutes the enumeration (and `.env`'s secret never enters the log). A gitignored
            path that is genuinely a run INPUT should be mounted as a `data:` source, where
            `_shallow_fingerprint` covers it outside git's ignore rules.
          * Multiple sources under one repo share a single diff (computed once per resolved root).
        Bounded output: <=500 porcelain lines x 200 chars, and one capped digest per repo root.

        `sources` is an iterable of FILESYSTEM PATHS — never the workspace fingerprint, whose keys are
        the `editable:<name>` LABELS this function would resolve to `Path(".")`. A dict still works
        (iterating one yields its keys) so the four tests that pass `{path: {}}` are unchanged, but
        callers should hand it `workspace.workspace_source_paths()`."""
        import os
        import subprocess
        import time
        from looplab.runtime.sandbox import git_subprocess_env

        git_env = git_subprocess_env()

        def _diff_digest(root: str) -> "str | None":
            # Incrementally hash `git diff HEAD` (staged + unstaged) so a multi-GB tracked-file diff
            # never lands in memory: raw fd reads, an 8 MiB byte cap, and a wall-clock deadline.
            proc = None
            try:
                proc = subprocess.Popen(["git", "-C", root, "diff", "HEAD"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        env=git_env)
                fd = proc.stdout.fileno()
                h, read, truncated, deadline = hashlib.sha256(), 0, False, time.monotonic() + 15
                while read < _DIFF_DIGEST_CAP:
                    if time.monotonic() > deadline:
                        truncated = True
                        break
                    chunk = os.read(fd, min(65536, _DIFF_DIGEST_CAP - read))
                    if not chunk:
                        break                                       # EOF: the whole diff was hashed
                    h.update(chunk)
                    read += len(chunk)
                else:
                    truncated = bool(os.read(fd, 1))                # bytes remained past the cap
                return (h.hexdigest()[:16] + ("~" if truncated else "")) if read else None
            except Exception:  # noqa: BLE001 — no HEAD / git error / decode: keep the file list only
                return None
            finally:
                if proc is not None:
                    try:
                        proc.stdout.close()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        proc.terminate()                            # stop git if we bailed mid-stream
                        proc.wait(timeout=5)
                    except Exception:  # noqa: BLE001
                        try:
                            proc.kill()
                        except Exception:  # noqa: BLE001
                            pass

        out: list = []
        digests: dict = {}                                          # resolved-root -> digest (once)
        # A SOURCE WE COULD NOT READ MAKES THE WHOLE ENUMERATION UNKNOWN, and until 2026-08-30 it
        # made it CLEAN. `workspace.py::substrate_fingerprint` guards this call with
        # `except Exception: dirty = None` and records `"dirty": "unknown"`, naming an index.lock, a
        # mid-rebase tree, an EIO and a timeout — and not one of them could reach that branch,
        # because this loop swallowed every per-source exception with a bare `pass` and returned the
        # same `[]` a genuinely clean tree returns. A wedged geesefs mount (a 10 s wall against this
        # box's 105-950 ms lstats) therefore recorded the bare-HEAD fingerprint, byte-equal to a
        # clean checkout, and `comparability` could certify SAME across a substrate change — exactly
        # the confidently-wrong record that branch exists to refuse. Driven by monkeypatching
        # `subprocess.run` to raise TimeoutExpired.
        #
        # ONLY AN EXCEPTION COUNTS AS UNREADABLE — a NONZERO exit deliberately does not. `git status`
        # answers 128 for "not a git repository", which is the ordinary condition of a plain data
        # mount; treating that as unknown would put nearly every run into the unknown branch and say
        # nothing. So the residue is stated rather than hidden: a source whose git exits nonzero for
        # a reason OTHER than not-being-a-repo still folds into the clean reading, and separating
        # those needs git's own stderr text, which is a weaker signal than the exception this raises.
        #
        # `None` REACHES BOTH CALLERS CORRECTLY: the fingerprint takes its documented `unknown`
        # branch, and `run_started.dirty_inputs` records null instead of an empty list that would
        # claim nothing was dirty.
        unreadable = False
        for src in sorted(sources or ()):
            try:
                p = Path(src)
                root = str(p if p.is_dir() else p.parent)
                r = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                                   capture_output=True, text=True,
                                   timeout=_DIRTY_STATUS_TIMEOUT_S, env=git_env)
                dirty = [ln[:200] for ln in r.stdout.splitlines() if ln.strip()][:500]
                if r.returncode == 0 and dirty:
                    entry = {"source": src, "dirty": dirty}
                    if root not in digests:
                        digests[root] = _diff_digest(root)
                    if digests[root] is not None:
                        entry["diff_digest"] = digests[root]
                    out.append(entry)
            except Exception:  # noqa: BLE001 — git missing / timeout / EIO: this source is UNREADABLE
                unreadable = True
        return None if unreadable else out
