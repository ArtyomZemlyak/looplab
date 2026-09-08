"""The assistant's run-MUTATING provider: finalize/stop/resume, settings, reset/retag, delete.

Split out of `machine_runs_tools.py` on 2026-09-08 (doc 25 TO-02), which had grown to 1,988 lines
holding this provider, the read-only machine-runs view, the launch-proposal provider, the turn
mutation journal and the command adapter. This file is the one that DESTROYS things, and it now
reads as such: the two serve-owned dependency contracts it is injected with, the two shared fences
every delete path re-derives, and one method per verb.

Its dependencies are explicit by design — `RunLifecycleFns` and `TraceRewriteFns` are the
`serve`-owned primitives handed down by the composition root (doc 25 XP-03), the turn journal is
`turn_mutation_fence.py`, and every command-backed verb goes through
`run_command_adapter.py::_RunCommandAdapter`.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from looplab.core.models import RunState
from looplab.tools._base import fn_spec
from looplab.tools.node_purge_receipt import purge_operation_id
from looplab.tools.run_command_adapter import (
    _deletion_operation_id, _render_command_result, _render_deletion_result, _RunCommandAdapter)
from looplab.tools.turn_mutation_fence import (
    _exact_run_generation, _MutationRecoveryBlocked, _TurnMutationFence)


def _node_subtree(state: RunState, root_id: int) -> set[int]:
    """``root_id`` plus every descendant reachable through ``parent_ids``.

    Deleting a node alone would orphan its children's parent links, so every delete path resolves
    the whole subtree first — and all three of them (the plan, the commit, and the purge's
    re-verification) were carrying their own verbatim copy of this walk. They are not free to
    disagree: the purge compares its own answer against the approved one and refuses on a
    mismatch, so a copy that drifted would turn a correct approval into a permanent refusal.

    The graph is a DAG, not a tree — a merge node has several parents — so this is a fixpoint
    sweep rather than a recursive descent, and a node joins as soon as ANY parent is inside.
    """
    found = {root_id}
    changed = True
    while changed:
        changed = False
        for node in state.nodes.values():
            if node.id not in found and any(parent in found for parent in node.parent_ids):
                found.add(node.id)
                changed = True
    return found


def _node_lifecycle_unchanged(store, *, node_id: int, expected_tail: int,
                              generation: int) -> bool:
    """Whether the exact node lifecycle a permission card was formed against is still current.

    A confirm card can stay open indefinitely while another control resets, re-tags or tombstones
    the node underneath it, so approval is re-checked against the log immediately BEFORE the
    mutation is submitted. The fence is the whole log TAIL, not just the node: the operator
    approved an action against a run they were shown, and a sibling append changed that run.
    """
    from looplab.events.replay import fold

    events = store.read_all()
    tail = events[-1].seq if events else -1
    node = fold(events).nodes.get(node_id)
    return (tail == expected_tail and node is not None and not node.tombstoned
            and node.attempt == generation)


@dataclass(frozen=True)
class RunLifecycleFns:
    """The run-lifecycle primitives a run-MUTATING tool needs, as an explicit contract.

    Every field is a fence, not a convenience: the lifecycle lock is the only thing standing between
    a delete and a resume that has been claimed but has not yet taken `engine.lock`, and the two
    launch-pending predicates are what make that window observable. Naming them here means a
    caller can substitute them (a test, a different host) without `tools/` reaching upward into
    `serve/` — see doc 25 XP-03. Since 2026-09-08 the DEFAULT does not reach upward either: the
    five live in `looplab/engine/run_lifecycle.py`, below both packages, and `serve/` re-exports
    them. They were `serve`-owned only because that is where the server happened to write them.
    """
    engine_alive: Callable
    fresh_resume_launch_pending: Callable
    fresh_run_launch_pending: Callable
    run_lifecycle_lock: Callable
    run_config_write_lock: Callable


@dataclass(frozen=True)
class TraceRewriteFns:
    """Injected serving-layer transaction primitives used by irreversible node purge.

    ``tools`` is below ``serve`` in the package graph.  The assistant composition root supplies
    these callables so the tool can reuse trace-clear's descriptor-first filter and durable publish
    transaction without reaching upward into a private serving module.
    """
    prepare_filtered_snapshot: Callable
    digest_snapshot: Callable
    publish_prepared_snapshot: Callable


class RunControlTools:
    """Lets the assistant DRIVE an existing run's lifecycle — finalize, stop, resume, reset a node,
    delete a node, or delete the whole run. Lifecycle/engine commands go through the server-owned
    command service; only the deliberately separate destructive delete implementations edit storage
    here. Every verb first goes through `decide(mode, ...)` + the injected `approver` (a UI
    confirm-card), so it's denied in read-only `plan` mode, asks in default/acceptEdits, and runs inline
    only in `auto`. Destructive edits (delete node/run) additionally REFUSE while the run is live: all
    appenders serialize through EventStore, but a physical rewrite cannot safely race those appends."""

    def __init__(self, run_root, alive_fn: Optional[Callable[[Path], bool]] = None,
                 mode: str = "plan", approver: Optional[Callable] = None, *,
                 command_service=None, command_key_namespace: str = "",
                 mutation_journal_path=None, mutation_recovery: bool = False,
                 lifecycle: "Optional[RunLifecycleFns]" = None,
                 trace_rewrite: "Optional[TraceRewriteFns]" = None):
        self.run_root = Path(run_root)
        self.alive_fn = alive_fn
        self.mode = mode
        self.approver = approver
        # The serve-side run-lifecycle primitives, INJECTED (doc 25 XP-03). `tools/` sits below
        # `serve/` in the package map, and `serve/assistant.py` constructs this class — so reaching
        # up into `serve` from here closes a tools<->serve cycle that only function-local imports
        # were keeping open. Passing them in makes the dependency an explicit argument of the one
        # component that needs it. ``None`` keeps read/lifecycle controls usable for embedders that
        # construct this provider directly, while irreversible trace purge fails closed until the
        # host supplies the serving-layer transaction boundary.
        self._lifecycle = lifecycle
        self._trace_rewrite = trace_rewrite
        self._commands = _RunCommandAdapter(
            command_service, key_namespace=command_key_namespace)
        self._mutation_fence = (_TurnMutationFence(
            Path(mutation_journal_path), command_key_namespace, recovering=mutation_recovery)
            if mutation_journal_path is not None and command_key_namespace else None)

    def lifecycle(self) -> "RunLifecycleFns":
        """The injected run-lifecycle primitives, or the lazily-imported defaults.

        Resolved per call rather than in `__init__` so the default path keeps its historical import
        timing — these are only needed by the mutating tools, and importing the fences at
        construction would make every read-only assistant session pay for them.

        The default reaches DOWN into `looplab/engine/run_lifecycle.py` since 2026-09-08 (doc 25
        XP-03). It used to reach UP into `serve/engine_proc` + `serve/run_files`, which left the
        injection seam as the only thing standing between this module and a tools<->serve cycle:
        whenever nothing injected — every embedder, and the assistant's own default path — the
        cycle was there. `serve/engine_proc` and `serve/run_files` re-export every one of these
        names, so the server and this tool still share one implementation and one monkeypatch seam.
        """
        if self._lifecycle is not None:
            return self._lifecycle
        from looplab.engine import run_lifecycle
        return RunLifecycleFns(
            engine_alive=run_lifecycle.engine_alive,
            fresh_resume_launch_pending=run_lifecycle.fresh_resume_launch_pending,
            fresh_run_launch_pending=run_lifecycle.fresh_run_launch_pending,
            run_lifecycle_lock=run_lifecycle.run_lifecycle_lock,
            run_config_write_lock=run_lifecycle.run_config_write_lock,
        )

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("finalize_run",
                "Finalize a run: stop it AND wrap up (final report + cross-run lessons + cost roll-up). "
                "Use to END a run cleanly; the command service attaches the driver when needed.",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("stop_run",
                "Freeze a run (pause, NO wrap-up) — resumable later. Use to PAUSE without finalizing.",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("resume_run",
                "Resume a stopped/finished run through the durable singleton-owner handoff. Records "
                "the intent, lets a live/finalizing owner serve it or hand it off, and otherwise "
                "claims and launches the engine without requiring a separate UI action.",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("reset_node",
                "Re-run an existing node IN PLACE from a stage (no new node): 'eval' re-scores (keep the "
                "code), 'implement' re-runs only the Developer (keep the idea), 'propose' is a full redo. "
                "The command service resumes the run when needed.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 # No enum: the executor + HTTP route accept ANY pipeline stage name, and an enum here
                 # would make the model refuse legitimate stage resets (train, data_prep, …).
                 "stage": {"type": "string",
                           "description": "propose | implement | eval, or any eval-pipeline stage "
                           "name (train, data_prep, …) to re-run the pipeline from that stage"}},
                ["run_id", "node_id"]),
            fn_spec("retag_node",
                "Re-tag ONE experiment's CONCEPTS on a run — replace node #node_id's concept ids with the "
                "given `axis/slug` list (e.g. after you notice a mis-tag). Operator-authoritative: it wins "
                "over the Researcher's authored tags and the classifier, and is the per-run counterpart to "
                "the cross-run `concept_merge`/`concept_split` taxonomy edits. Pass every concept the node "
                "should carry (not a delta); an empty list clears its tags.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "concepts": {"type": "array", "items": {"type": "string"},
                              "description": "the full axis/slug concept id list for this node"}},
                ["run_id", "node_id", "concepts"]),
            fn_spec("set_run_concepts",
                "Set a run's BASE concept set — the common `axis/slug` concepts every node inherits unless "
                "it authors a delta. The engine seeds this from the first experiment; use this to correct or "
                "refine it. Last-write-wins; pass the full base list.",
                {"run_id": {"type": "string"},
                 "concepts": {"type": "array", "items": {"type": "string"},
                              "description": "the full axis/slug base concept id list for the run"}},
                ["run_id", "concepts"]),
            fn_spec("delete_node",
                "DELETE a node AND its descendants from a run. Default is an APPEND-ONLY tombstone: the "
                "subtree is logically removed (excluded from best-pick / breeding / re-eval) while its "
                "events stay in the log, so it's reversible and parent/chosen/archive refs stay valid. "
                "Pass purge=true for an IRREVERSIBLE physical compaction that also rewrites the log and "
                "removes spans + workdirs (backs the log up first). Refuses while the engine is live — "
                "stop the run first.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "purge": {"type": "boolean",
                           "description": "irreversibly rewrite the log + remove workdirs (default: "
                           "false = reversible tombstone)"}},
                ["run_id", "node_id"]),
            fn_spec("delete_run",
                "DELETE an entire run and all its artifacts. DESTRUCTIVE + irreversible. Refuses while "
                "the engine is live — stop the run first.",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("extend_budget",
                "Give a run MORE budget (and REOPEN it if it already finished, so the new budget is "
                "actually used). Set any of: add_nodes (N more experiment nodes), max_seconds (new "
                "wall-clock ceiling), max_eval_seconds (new cumulative-eval ceiling). The command "
                "service attaches the engine when needed and reports the observed outcome.",
                {"run_id": {"type": "string"},
                 "add_nodes": {"type": "integer", "description": "additive: N more experiment nodes"},
                 "max_seconds": {"type": "number", "description": "new whole-run wall-clock ceiling (s)"},
                 "max_eval_seconds": {"type": "number", "description": "new cumulative in-eval ceiling (s)"}},
                ["run_id"]),
            fn_spec("set_directive",
                "Give the run's agents a standing DIRECTIVE that steers the next proposals + code "
                "(e.g. 'use only sklearn', 'prefer lighter models', 'stop trying deep nets'). "
                "replace=true rewrites the single directive instead of accumulating.",
                {"run_id": {"type": "string"}, "text": {"type": "string"},
                 "replace": {"type": "boolean", "description": "replace all prior directives (default: append)"}},
                ["run_id", "text"]),
            fn_spec("set_trust_gate",
                "Change what a reward-hack / leakage flag does to the run: audit (surface only) · "
                "gate (a flagged node can't win and isn't bred from) · block (also fully infeasible). "
                "Applies immediately (last-write-wins) on the next fold.",
                {"run_id": {"type": "string"},
                 "trust_gate": {"type": "string", "enum": ["audit", "gate", "block"]}},
                ["run_id", "trust_gate"]),
        ]

    # ------------------------------------------------------------------ helpers
    def _rd(self, run_id) -> Optional[Path]:
        # Resolve a run_id to its dir, refusing traversal (must be a direct, existing child of run-root).
        rid = str(run_id or "").strip()
        if not rid or "/" in rid or "\\" in rid or rid.startswith("."):
            return None
        root = self.run_root.resolve()
        candidate = root / rid
        try:
            # Refuse aliases even when they happen to resolve to another direct child: direct mutation
            # paths (notably set_trust_gate) must never follow a run/events symlink outside the root.
            if candidate.is_symlink():
                return None
            rd = candidate.resolve()
            events = rd / "events.jsonl"
            if rd.parent != root or events.is_symlink() or not events.exists() \
                    or events.resolve().parent != rd:
                return None
        except OSError:
            return None
        return rd

    def _gate(self, name: str, rid: str, rd: Path, verb: str, *,
              scope: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
        # Returns a "declined/disabled" string to short-circuit, or None to proceed.
        from looplab.tools.perm_modes import decide_action, refusal_for
        action = {"tool": name, "tool_kind": "run_control", "label": f"{name} {rid}",
                  "verb": verb, "preview": f"{name}({rid})", "run_id": rid,
                  "scope": dict(scope or {"run_id": rid})}
        denied = ("(run control is disabled in read-only plan mode — switch to "
                  "default/acceptEdits/auto.)")
        # `refusal_for` rather than `authorize`: the generation must be captured BETWEEN the deny
        # short-circuit and the approval round-trip, so the mutation fence describes the run as it was
        # before the user was asked. A deny also returns NO generation — nothing was fenced.
        decision = decide_action(self.mode, action)
        if decision == "deny":
            return denied, None
        generation = (None if self._mutation_fence is not None and self._mutation_fence.recovering
                      else self._commands.run_generation(rd))
        return refusal_for(decision, self.approver, action,
                           denied=denied, declined=f"{name} {rid}"), generation

    def _live(self, rd: Path) -> bool:
        """Is a run's engine actively writing its log? The flock probe is primary, but on FUSE / NFS / S3
        mounts flock can wrongly report "not live" — so ALSO trip on a fresh-write backstop: a run that
        is neither paused nor finished AND whose events.jsonl was appended in the last 30s is treated as
        live (the engine and serialized control writers keep the log fresh). This gates the destructive
        delete_node/delete_run so they can't rewrite the log out from under a live engine even when flock
        lies. Conservative: a genuinely crashed run (stale mtime) still deletes."""
        try:
            if self.alive_fn and self.alive_fn(rd):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            import time as _time
            from looplab.events.eventstore import EventStore
            from looplab.events.replay import fold
            evp = rd / "events.jsonl"
            st = fold(EventStore(evp).read_all())
            if st.finished or st.paused:
                return False                              # a settled run is safe to act on
            return (_time.time() - evp.stat().st_mtime) < 30.0   # recent write on an unsettled run -> live
        except Exception:  # noqa: BLE001
            return False

    @contextmanager
    def _mutation_intent(self, name: str, rid: str, rd: Path, data: dict, *, command_backed: bool,
                         expected_generation: Optional[str]):
        """Stage one canonical run mutation before any command/event/storage side effect."""
        key = ""
        generation = ""
        if self._mutation_fence is not None:
            key, generation = self._mutation_fence.claim(
                {"tool": name, "run_id": rid, "data": data}, command_backed=command_backed,
                expected_generation=expected_generation)
        else:
            generation = _exact_run_generation(expected_generation)
        yield key, generation

    # ------------------------------------------------------------------ dispatch
    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        rid = str(args.get("run_id") or "").strip()
        rd = self._rd(rid)
        if rd is None:
            if (name == "delete_run" and self._mutation_fence is not None
                    and self._mutation_fence.recovering):
                try:
                    return self._recover_delete_run(rid)
                except _MutationRecoveryBlocked as e:
                    return f"(run mutation blocked: code={e.code}; {e})"
                except Exception as e:  # noqa: BLE001 - a tool error must never crash the loop
                    return f"(tool error in {name}: {e})"
            return f"(no such run: {rid!r})"
        try:
            if name in ("finalize_run", "stop_run", "resume_run"):
                return self._control(name, rid, rd)
            if name == "reset_node":
                return self._reset_node(rid, rd, args)
            if name == "retag_node":
                return self._retag_node(rid, rd, args)
            if name == "set_run_concepts":
                return self._set_run_concepts(rid, rd, args)
            # One method per verb: the outer dispatch used to hand three unrelated settings verbs to
            # a single `_settings`, which then re-dispatched on the same name it was just given.
            if name in ("extend_budget", "set_directive", "set_trust_gate"):
                return getattr(self, f"_tool_{name}")(name, rid, rd, args)
            if name == "delete_node":
                return self._delete_node(rid, rd, args)
            if name == "delete_run":
                return self._delete_run(rid, rd)
        except _MutationRecoveryBlocked as e:
            return f"(run mutation blocked: code={e.code}; {e})"
        except Exception as e:  # noqa: BLE001 — a tool error must never crash the loop
            return f"(tool error in {name}: {e})"
        return f"(unknown tool: {name})"

    def _recover_delete_run(self, rid: str) -> str:
        if not self._commands.durable_deletion_available or self._mutation_fence is None:
            raise _MutationRecoveryBlocked(
                "run_deletion_service_unavailable",
                "The exact deletion receipt cannot be recovered in this process.")
        if (not rid or "/" in rid or "\\" in rid or rid.startswith(".")
                or Path(rid).name != rid):
            raise _MutationRecoveryBlocked(
                "run_deletion_identity_invalid", "The durable deletion run id is invalid.")
        key, generation, data = self._mutation_fence.claim_recovery("delete_run", rid)
        expected_tail = data.get("expected_tail")
        if type(expected_tail) is not int or expected_tail < -1:
            raise _MutationRecoveryBlocked(
                "run_deletion_identity_invalid", "The durable deletion tail is invalid.")
        rd = self.run_root.resolve() / rid
        result = self._commands.begin_or_resume_deletion(
            rd, operation_id=_deletion_operation_id(key),
            expected_generation=generation, expected_seq=expected_tail)
        return _render_deletion_result(result, rid)

    def _control(self, name: str, rid: str, rd: Path) -> str:
        from looplab.events.types import EV_PAUSE, EV_RESUME, EV_RUN_ABORT
        etype, data, verb = {
            "finalize_run": (EV_RUN_ABORT, {"reason": "finalized"}, f"finalize run {rid} (stop + wrap up)"),
            "stop_run": (EV_PAUSE, {}, f"stop (freeze) run {rid}"),
            "resume_run": (EV_RESUME, {}, f"resume run {rid}"),
        }[name]
        blocked, formed_generation = self._gate(name, rid, rd, verb, scope={"run_id": rid})
        if blocked:
            return blocked
        with self._mutation_intent(
                name, rid, rd, {"event_type": etype, "data": data},
                command_backed=True, expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, etype, data, idempotency_key=key, expected_generation=generation)
        return _render_command_result(record, name=name, run_id=rid, completed=verb)

    def _tool_extend_budget(self, name: str, rid: str, rd: Path, args: dict) -> str:
        """Raise a LIVE run's node/time budget by appending the same EV_BUDGET_EXTEND the UI writes."""
        import math

        from looplab.events.types import EV_BUDGET_EXTEND

        data: dict = {}
        for k in ("add_nodes", "max_seconds", "max_eval_seconds"):
            v = args.get(k)
            if v is None:
                continue
            try:
                data[k] = int(v) if k == "add_nodes" else float(v)
            except (TypeError, ValueError):
                return f"({k} must be a number)"
            if k != "add_nodes" and not math.isfinite(data[k]):
                return f"({k} must be a finite number — nan/inf would disable the budget)"
        if not data:
            return "(extend_budget needs at least one of add_nodes / max_seconds / max_eval_seconds)"
        if data.get("add_nodes", 1) <= 0:      # a negative/zero delta SHRINKS the budget, not extends
            return "(add_nodes must be a positive count of MORE experiment nodes)"
        blocked, formed_generation = self._gate(
            name, rid, rd, f"extend budget of {rid}: {data}",
            scope={"run_id": rid, **data})
        if blocked:
            return blocked
        with self._mutation_intent(
                name, rid, rd, {"event_type": EV_BUDGET_EXTEND, "data": data},
                command_backed=True,
                expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, EV_BUDGET_EXTEND, data, idempotency_key=key,
                expected_generation=generation)
        return _render_command_result(
            record, name=name, run_id=rid, completed=f"budget extended for {rid}: {data}")

    def _tool_set_directive(self, name: str, rid: str, rd: Path, args: dict) -> str:
        """Record a standing directive for a LIVE run (EV_HINT), gated like every other mutation."""
        from looplab.events.types import EV_HINT

        text = " ".join(str(args.get("text") or "").split())
        if not text:
            return "(set_directive needs a non-empty text)"
        blocked, formed_generation = self._gate(
            name, rid, rd, f"directive for {rid}: {text[:60]}",
            scope={"run_id": rid,
                   "text_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                   "replace": bool(args.get("replace"))})
        if blocked:
            return blocked
        data = {"text": text, "replace": bool(args.get("replace"))}
        with self._mutation_intent(
                name, rid, rd, {"event_type": EV_HINT, "data": data},
                command_backed=True,
                expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, EV_HINT, data, idempotency_key=key,
                expected_generation=generation)
        return _render_command_result(
            record, name=name, run_id=rid, completed=f"directive recorded for {rid}: {text[:80]!r}")

    def _tool_set_trust_gate(self, name: str, rid: str, rd: Path, args: dict) -> str:
        """Set the trust gate.

        The only settings verb that is NOT command-backed: it writes the event and mirrors
        `config.snapshot.json` directly, so a later RESUME re-enters with the new gate and the
        settings panel does not show a stale value. It stays this way until the trust gate joins the
        server's control registry, at which point it becomes a submit like the other two.

        The WRITE is `events/trust_gate.py::apply_trust_gate`, the same one the config PUT uses.
        This path used to spell its own and had drifted on all three of its properties — it appended
        unconditionally (so confirming a gate that already held grew the log by a row claiming a
        change nobody made), with no tail CAS and no writer lock. Only the refusal is phrased here.
        """
        from looplab.events.trust_gate import (
            GATE_WRITE_ALREADY_SET, GATE_WRITE_CONTENDED, TRUST_GATE_VALUES, apply_trust_gate,
        )
        from looplab.events.types import ASSISTANT_APPENDABLE, EV_TRUST_GATE_CHANGED

        # Invariant #1's assistant seam, declared at the site like its two thread-side siblings.
        assert EV_TRUST_GATE_CHANGED in ASSISTANT_APPENDABLE

        tg = str(args.get("trust_gate") or "").strip().lower()
        if tg not in TRUST_GATE_VALUES:
            return "(trust_gate must be audit | gate | block)"
        blocked, formed_generation = self._gate(
            name, rid, rd, f"set trust_gate={tg} for {rid}",
            scope={"run_id": rid, "trust_gate": tg})
        if blocked:
            return blocked
        with self._mutation_intent(
                name, rid, rd, {"trust_gate": tg}, command_backed=False,
                expected_generation=formed_generation) as (_key, generation):
            with self._commands.mutation_guard(
                    rd, "set the trust gate", expected_generation=generation) as rd:
                # Mirror the UI PUT /config path: the fold already applies the event, but also update
                # config.snapshot.json so a later RESUME re-enters with the new gate and the settings panel
                # doesn't show a stale value (the two mutation paths must not drift). Best-effort.
                snap = rd / "config.snapshot.json"
                if snap.exists():
                    # Global order is config -> events. Reset takes the same pair in that order,
                    # preventing an event->config/config->event deadlock while also closing the
                    # reset-marker race for this dual write.
                    with self.lifecycle().run_config_write_lock(snap):
                        outcome = apply_trust_gate(rd, tg, source="assistant")
                        if outcome != GATE_WRITE_CONTENDED:
                            try:
                                import json as _json
                                from looplab.core.atomicio import atomic_write_text
                                cfg = _json.loads(snap.read_text(encoding="utf-8"))
                                cfg["trust_gate"] = tg
                                atomic_write_text(snap, _json.dumps(cfg, indent=2))
                            except (OSError, ValueError):
                                pass
                else:
                    outcome = apply_trust_gate(rd, tg, source="assistant")
                if outcome == GATE_WRITE_CONTENDED:
                    # The refusal is phrased here, not in the shared writer: this surface answers an
                    # LLM, so it says what to do next rather than returning a status code.
                    return (f"(run {rid} changed while the trust gate was being saved — "
                            f"refresh and retry)")
                if outcome == GATE_WRITE_ALREADY_SET:
                    # Say it, rather than reporting a change that did not happen. The row is what a
                    # later audit reads; claiming one nobody made is the defect this closes.
                    return f"(trust_gate was already {tg} for {rid} — nothing recorded)"
                return f"(trust_gate set to {tg} for {rid})"

    def _reset_node(self, rid: str, rd: Path, args: dict) -> str:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        from looplab.events.types import EV_NODE_RESET
        try:
            nid = int(args.get("node_id"))
        except (TypeError, ValueError):
            return "(reset_node needs an integer node_id)"
        stage = str(args.get("stage") or "eval").strip()
        if not stage or len(stage) > 64:      # propose|implement|eval OR an eval-pipeline stage name
            return "(stage must be a non-empty stage name)"
        store = EventStore(rd / "events.jsonl")
        inspected_events = store.read_all()
        expected_tail = inspected_events[-1].seq if inspected_events else -1
        state = fold(inspected_events)
        node = state.nodes.get(nid)
        if node is None:
            return f"(no node #{nid} in {rid})"
        if node.tombstoned:
            return f"(node #{nid} in {rid} is tombstoned and cannot be reset)"
        generation = node.attempt
        blocked, formed_generation = self._gate(
            "reset_node", rid, rd, f"reset node #{nid} of {rid} from {stage}",
            scope={"run_id": rid, "node_id": nid, "generation": generation, "stage": stage})
        if blocked:
            return blocked
        # Permission can stay open while another control changes the node. Reject that stale scope
        # before handing the exact lifecycle generation to the command sequencer.
        if not _node_lifecycle_unchanged(
                store, node_id=nid, expected_tail=expected_tail, generation=generation):
            return f"(node #{nid} or run intent changed while awaiting permission — refresh and retry)"
        data = {"node_id": nid, "generation": generation, "from_stage": stage}
        with self._mutation_intent(
                "reset_node", rid, rd, {"event_type": EV_NODE_RESET, "data": data},
                command_backed=True, expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, EV_NODE_RESET, data, idempotency_key=key,
                expected_generation=generation)
        return _render_command_result(
            record, name="reset_node", run_id=rid,
            completed=f"node #{nid} of {rid} re-run from {stage}")

    def _retag_node(self, rid: str, rd: Path, args: dict) -> str:
        # PART V (D): let the assistant re-tag ONE node's concepts — the operator's per-run concept edit,
        # now available to the operator's assistant. Reuses the existing EV_CONCEPT_TAG_EDITED control event
        # (folds to node_concepts with OPERATOR provenance, wins over authored/classifier tags), through the
        # same command funnel + generation fence + permission gate as reset_node. The server normalizes and
        # caps the concept ids, so submit raw and surface any 400/409 through the command result.
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        from looplab.events.types import EV_CONCEPT_TAG_EDITED
        try:
            nid = int(args.get("node_id"))
        except (TypeError, ValueError):
            return "(retag_node needs an integer node_id)"
        raw = args.get("concepts")
        if not isinstance(raw, list):
            return ("(retag_node needs a `concepts` list of axis/slug ids, "
                    'e.g. ["loss/contrastive", "regularization/r-drop"])')
        concepts = [str(c) for c in raw]
        store = EventStore(rd / "events.jsonl")
        inspected = store.read_all()
        expected_tail = inspected[-1].seq if inspected else -1
        node = fold(inspected).nodes.get(nid)
        if node is None:
            return f"(no node #{nid} in {rid})"
        if node.tombstoned:
            return f"(node #{nid} in {rid} is tombstoned and cannot be re-tagged)"
        node_gen = node.attempt
        blocked, formed_generation = self._gate(
            "retag_node", rid, rd, f"re-tag node #{nid} of {rid}",
            scope={"run_id": rid, "node_id": nid, "generation": node_gen, "concepts": concepts})
        if blocked:
            return blocked
        # Reject a stale subject that changed while the confirm card was open (same fence as reset_node).
        if not _node_lifecycle_unchanged(
                store, node_id=nid, expected_tail=expected_tail, generation=node_gen):
            return f"(node #{nid} or run intent changed while awaiting permission — refresh and retry)"
        data = {"node_id": nid, "node_generation": node_gen, "concepts": concepts}
        with self._mutation_intent(
                "retag_node", rid, rd, {"event_type": EV_CONCEPT_TAG_EDITED, "data": data},
                command_backed=True, expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, EV_CONCEPT_TAG_EDITED, data, idempotency_key=key,
                expected_generation=generation)
        return _render_command_result(
            record, name="retag_node", run_id=rid,
            completed=f"node #{nid} of {rid} re-tagged with {len(concepts)} concept(s)")

    def _set_run_concepts(self, rid: str, rd: Path, args: dict) -> str:
        # PART V (D): set a run's BASE concept set (EV_RUN_CONCEPTS, last-write-wins). The engine seeds this
        # once from the first node; the assistant can override/refine it. Run-scoped (no node fence); the
        # server normalizes/caps the ids. Nodes then author only deltas vs this base.
        from looplab.events.types import EV_RUN_CONCEPTS
        raw = args.get("concepts")
        if not isinstance(raw, list):
            return ("(set_run_concepts needs a `concepts` list of axis/slug ids, "
                    'e.g. ["model/transformer", "loss/contrastive"])')
        concepts = [str(c) for c in raw if str(c)]
        if not concepts:
            # An EMPTY base is indistinguishable from "never seeded", so while concept_run_base is on the
            # engine cadence would re-seed it from the first node and silently undo the clear. Replace the
            # base with a real set instead; to disable run-base authoring entirely, turn off concept_run_base.
            return ("(set_run_concepts needs at least one concept — an empty base is re-seeded by the engine. "
                    "Pass the base set you want, or disable concept_run_base to stop run-base authoring.)")
        blocked, formed_generation = self._gate(
            "set_run_concepts", rid, rd, f"set the base concepts of {rid}",
            scope={"run_id": rid, "concepts": concepts})
        if blocked:
            return blocked
        data = {"concepts": concepts}
        with self._mutation_intent(
                "set_run_concepts", rid, rd, {"event_type": EV_RUN_CONCEPTS, "data": data},
                command_backed=True, expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, EV_RUN_CONCEPTS, data, idempotency_key=key, expected_generation=generation)
        return _render_command_result(
            record, name="set_run_concepts", run_id=rid,
            completed=f"run {rid} base concepts set ({len(concepts)})")

    def _delete_node(self, rid: str, rd: Path, args: dict) -> str:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        try:
            nid = int(args.get("node_id"))
        except (TypeError, ValueError):
            return "(delete_node needs an integer node_id)"
        purge = bool(args.get("purge"))
        if self._live(rd):
            return f"(run {rid} is LIVE — stop it before physically rewriting its event log)"
        evp = rd / "events.jsonl"
        store = EventStore(evp)
        events = store.read_all()
        st = fold(events)
        if nid not in st.nodes:
            return f"(no node #{nid} in {rid})"
        subtree = _node_subtree(st, nid)
        expected_tail = events[-1].seq if events else -1
        verb = "PURGE (physical, irreversible)" if purge else "tombstone"
        blocked, formed_generation = self._gate(
            "delete_node", rid, rd, f"{verb} node(s) {sorted(subtree)} of {rid}",
            scope={"run_id": rid, "node_id": nid, "subtree": sorted(subtree), "purge": purge})
        if blocked:
            return blocked
        with self._mutation_intent(
                "delete_node", rid, rd,
                {"node_id": nid, "subtree": sorted(subtree), "purge": purge,
                 "expected_tail": expected_tail},
                command_backed=False, expected_generation=formed_generation) as (key, generation):
            pass
        # The purge's operation id comes FROM the journal key, which is doc 34 D-01's first question
        # answered: the turn that stages the intent is the thing that owns the id, and a recovered
        # turn reconstructs the same key and therefore the same id. `key` is "" for an embedder that
        # constructs this provider with no journal, and `purge_operation_id` mints a uuid4 there.
        operation_id = purge_operation_id(key) if purge else ""
        with self._commands.destructive_guard(
                rd, "delete node", expected_generation=generation) as canonical:
            if self._live(canonical):
                return f"(run {rid} is LIVE — stop it before physically rewriting its event log)"
            return self._commit_delete_node_snapshot(
                rid, canonical, nid, subtree, expected_tail, purge=purge,
                operation_id=operation_id, expected_generation=generation)

    def _commit_delete_node_snapshot(self, rid: str, rd: Path, nid: int,
                                     subtree: set[int], expected_tail: int, *, purge: bool,
                                     operation_id: str = "",
                                     expected_generation: str = "") -> str:
        from looplab.events.eventstore import EventStore, EventStoreConcurrencyError, _interprocess_lock
        from looplab.events.replay import fold
        from looplab.events.types import ASSISTANT_APPENDABLE, EV_NODE_TOMBSTONED
        lifecycle = self.lifecycle()

        evp = rd / "events.jsonl"

        # The launch claim/Popen/child-lock gap is fenced only by the lifecycle lock. Acquire it after
        # approval, reject a fresh pending launch, then take engine.lock before the event-log CAS.
        with lifecycle.run_lifecycle_lock(rd):
            if (lifecycle.fresh_resume_launch_pending(rd)
                    or lifecycle.fresh_run_launch_pending(rd)):
                return f"(run {rid} is launching — retry delete after the engine settles)"
            if lifecycle.engine_alive(rd):
                return f"(run {rid} became LIVE while awaiting permission — stop it and retry)"
            if purge:
                return self._purge_node_snapshot(
                    rid, rd, nid, subtree, expected_tail,
                    operation_id=operation_id, expected_generation=expected_generation)
            with _interprocess_lock(rd / "engine.lock"):
                store = EventStore(evp)
                events = store.read_all()
                tail = events[-1].seq if events else -1
                state = fold(events)
                current_subtree = _node_subtree(state, nid) if nid in state.nodes else set()
                if current_subtree != subtree:
                    return (f"(delete scope changed while awaiting permission: approved "
                            f"{sorted(subtree)}, now {sorted(current_subtree)}; review and approve "
                            f"again)")
                if (tail != expected_tail or nid not in state.nodes
                        or (state.nodes[nid].tombstoned and not purge)):
                    return f"(run {rid} changed while awaiting permission — refresh and retry)"
                # Invariant #1's assistant seam, declared. This provider is neither the engine nor
                # a control intent, and `node_tombstoned` has no other writer in the tree — so the
                # exception is stated at the site, exactly as the two thread-side seams state theirs.
                assert EV_NODE_TOMBSTONED in ASSISTANT_APPENDABLE
                try:
                    store.append(
                        EV_NODE_TOMBSTONED, {"node_ids": sorted(subtree)},
                        expected_last_seq=expected_tail)
                except EventStoreConcurrencyError:
                    return f"(run {rid} changed before delete could commit — refresh and retry)"

        state = fold(EventStore(evp).read_all())
        live_left = sum(1 for node in state.nodes.values() if not node.tombstoned)
        return (f"(tombstoned node(s) {sorted(subtree)} of {rid} — logically deleted, log intact + "
                f"reversible; {live_left} live nodes left, best now #{state.best_node_id}. "
                f"Use purge=true for an irreversible physical compaction.)")

    # DEFERRED DECISION D-01 (docs/34), RESOLVED 2026-09-08. This was an irreversible multi-file
    # transaction with NO durable receipt while its three siblings (reset, deletion, trace clear)
    # all kept one, so a death between the event-log rewrite and the span publish left a renumbered
    # log whose `seq` no longer matched the spans sidecar, with nothing on disk saying an operation
    # was in flight. It now writes one — `tools/node_purge_receipt.py`, the SCHEMA half of the same
    # `core/receipt.py` protocol the other three use, NOT a fourth protocol — and the two answers
    # doc 34 said this code could not give itself are stated there: the operation id comes from the
    # turn's own mutation journal, and recovery REFUSES rather than resumes. Read docs/34 D-01
    # before changing what a phase means.
    def _purge_node_snapshot(self, rid: str, rd: Path, nid: int,
                             subtree: set[int], expected_tail: int, *,
                             operation_id: str = "", expected_generation: str = "") -> str:
        """Physically compact exactly the stopped tree snapshot the operator approved."""
        import json
        import shutil

        from looplab.core.atomicio import atomic_write_text
        from looplab.core.trace_append import SPAN_APPEND_JOURNAL_NAME
        from looplab.events.eventstore import EventStore, _interprocess_lock, iter_event_jsonl
        from looplab.events.replay import fold
        from looplab.events.span_index import invalidate, span_destructive_write_guard
        from looplab.tools.node_purge_receipt import (
            NodePurgeReceiptError, advance_purge_receipt, describe_purge_recovery,
            prepare_purge_receipt, purge_receipt_path, save_purge_receipt,
            unresolved_purge_receipts)

        evp = rd / "events.jsonl"
        spans = rd / "spans.jsonl"
        # Lock order matches the engine (singleton first, event append second). If a resume won the
        # liveness-check race, wait for it to release engine.lock and then fail the tail CAS; if purge
        # wins, no child can enter while the source-of-truth logs are rewritten. The span-index guard
        # is the same third lock used by reset/archive: a cold trace read cannot publish offsets for
        # the pre-purge inode behind this rewrite.
        with (_interprocess_lock(rd / "engine.lock"),
              _interprocess_lock(Path(str(evp) + ".lock")),
              span_destructive_write_guard(spans, required=True)):
            self._commands._reject_unresolved_reset(rd, "purge nodes")
            # An earlier purge of THIS RUN that never reached `succeeded` is a fail-closed fence on
            # every later one, and it is the whole point of the receipt: the log is run-global, so a
            # half-applied compaction of one subtree is not something a purge of another may be
            # layered on top of. An unreadable receipt RAISES out of here rather than being skipped
            # — "no operation" and "an operation whose record I cannot read" must never collapse.
            try:
                stalled = unresolved_purge_receipts(rd)
            except NodePurgeReceiptError as exc:
                return (f"(run {rid} has a node-purge receipt that cannot be read ({exc}) — "
                        "refusing irreversible purge; inspect the run directory)")
            if stalled:
                # The oldest one is described because it is the one whose backup holds the run as it
                # was; the COUNT is stated beside it so a reader is never shown one of several and
                # left to think it is the only one.
                oldest = min(stalled, key=lambda row: row[1]["created_at"])[1]
                more = f" ({len(stalled)} unresolved in total)" if len(stalled) > 1 else ""
                return (f"(run {rid} has an unresolved node purge{more} — refusing irreversible "
                        "purge. " + describe_purge_recovery(oldest) + ")")
            source_store = EventStore(evp)
            events = source_store.read_all()
            source_bytes = evp.read_bytes()
            torn_nonblank_tail = bool(
                source_bytes
                and not source_bytes.endswith(b"\n")
                and source_bytes.rsplit(b"\n", 1)[-1].strip()
            )
            # Purge is an irreversible compaction, not an implicit log repair. The historical raw
            # parser failed before rewriting any malformed complete row; preserve that posture for a
            # corrupt batch and additionally refuse a torn nonblank tail that EventStore legitimately
            # hides from ordinary replay.
            if source_store.divergence is not None or torn_nonblank_tail:
                return (
                    f"(run {rid} event log has an invalid or torn tail — refusing irreversible "
                    "purge; repair or restore the log first)"
                )
            actual_tail = events[-1].seq if events else -1
            state = fold(events)
            if actual_tail != expected_tail or nid not in state.nodes:
                return f"(run {rid} changed while awaiting permission — refresh and retry)"

            current_subtree = _node_subtree(state, nid)
            if current_subtree != subtree:
                return (f"(delete scope changed while awaiting permission: approved "
                        f"{sorted(subtree)}, now {sorted(current_subtree)}; review and approve again)")

            # Work over the logical Events already decoded above. ``append_many`` is stored as one
            # physical batch envelope; filtering physical rows would retain the whole transaction when
            # only one nested member names the purged node. Rewriting logical rows also removes the
            # internal storage wrapper while preserving every surviving event and sequence.
            recs = list(iter_event_jsonl(evp))
            kept = [record for record in recs
                    if not (isinstance(record.get("data"), dict)
                            and record["data"].get("node_id") in subtree)]
            # RENUMBER to a dense 0..N-1 run. Filtering alone left a seq GAP after every purged
            # event — the trailing `pause`, later sibling nodes — and the event store's dense fence
            # (`event_sequence_continues`: "no legitimate workflow produces a monotonic gap") reads
            # that as CORRUPTION: `read_all` silently drops every surviving event past the gap, and
            # the next append or resume raises EventLogCorruptionError. A purge could brick the run
            # it was cleaning. This whole path is already a full rewrite of the log (the backup taken
            # below is what makes it recoverable), so renumbering costs nothing extra, and seq is
            # POSITIONAL identity — nothing in the fold keys off an absolute value.
            for position, record in enumerate(kept):
                record["seq"] = position

            # Trace rows can contain credentials, prompts and host paths. Use the same descriptor-
            # first, root-aware streaming filter as HTTP trace-clear instead of following a link or
            # materialising a multi-GB sidecar. Besides explicit per-span node ids, this removes
            # unstamped children from legacy traces whose ROOT belongs to the purged subtree. Invalid
            # complete rows and a torn EOF remain byte-for-byte, so purge never turns an uncommitted
            # crash suffix into a committed JSONL record.
            trace_rewrite = self._trace_rewrite
            if trace_rewrite is None:
                return (
                    f"(run {rid} trace rewrite service is unavailable — refusing irreversible "
                    "purge; retry through the owner assistant service)"
                )
            prepared_trace = None
            try:
                prepared_trace = trace_rewrite.prepare_filtered_snapshot(spans, subtree)
                current_trace = trace_rewrite.digest_snapshot(spans)
            except Exception as exc:  # noqa: BLE001 - soft-fail this assistant tool before writes
                if prepared_trace is not None:
                    prepared_trace.cleanup()
                detail = getattr(exc, "detail", None)
                code = detail.get("code") if isinstance(detail, dict) else type(exc).__name__
                return (
                    f"(run {rid} trace sidecar is unavailable or unsafe ({code}) — refusing "
                    "irreversible purge; restore the private run-owned spans.jsonl first)"
                )
            if current_trace != prepared_trace.source:
                prepared_trace.cleanup()
                return (
                    f"(run {rid} trace sidecar changed while purge was prepared — refusing "
                    "irreversible purge; refresh and retry)"
                )

            # APPEND-ONLY backups: the name used to be keyed only by the root node id, so a second
            # purge of the SAME nid — a scope-change retry, or an id reused after a purge on resume —
            # silently overwrote the safety receipt for an IRREVERSIBLE operation. Find the first
            # free suffix instead; the unnumbered name stays as-is so existing backups keep working.
            _backup = rd / f"events.jsonl.bak-del{nid}"
            _n = 2
            while _backup.exists():
                _backup = rd / f"events.jsonl.bak-del{nid}.{_n}"
                _n += 1

            # Eviction is harmless if a later source write fails, while doing it first makes a
            # conflicting directory/unremovable projection fail before the irreversible event-log
            # rewrite. The guard prevents a reader from rebuilding either projection in this gap.
            try:
                invalidate(spans)
                (rd / "spans.index.jsonl").unlink(missing_ok=True)
                (rd / SPAN_APPEND_JOURNAL_NAME).unlink(missing_ok=True)
            except OSError:
                prepared_trace.cleanup()
                return (
                    f"(run {rid} trace projections could not be retired — refusing irreversible "
                    "purge; repair the run-owned trace sidecars first)"
                )

            # THE RECEIPT, published after the last step that can still refuse and before the first
            # one this transaction cannot take back, so a REFUSED purge leaves no record and fences
            # nothing. It names the backup chosen above — recovery reads its restore source out of
            # the record rather than guessing at a `bak-del<N>` suffix — plus the operation, the
            # subtree and the tail this attempt was approved against.
            receipt_path = purge_receipt_path(rd, operation_id) if operation_id else None
            receipt = None
            if receipt_path is not None:
                try:
                    receipt = save_purge_receipt(receipt_path, prepare_purge_receipt(
                        rd, operation_id=operation_id, node_id=nid, subtree=subtree,
                        expected_generation=expected_generation, expected_seq=expected_tail,
                        backup=_backup.name))
                except NodePurgeReceiptError as exc:
                    prepared_trace.cleanup()
                    return (f"(run {rid} purge receipt could not be published durably ({exc}) — "
                            "refusing irreversible purge; nothing was rewritten)")

            def _advance(phase: str) -> None:
                """Record that *phase* COMPLETED. Lagging is the safe direction and is deliberate:
                a receipt that failed to advance under-states progress, so the next attempt refuses
                and a human looks — where an over-stated phase would say a step happened that did
                not."""
                nonlocal receipt
                if receipt_path is not None and receipt is not None:
                    receipt = advance_purge_receipt(receipt_path, receipt, phase)

            try:
                shutil.copy(evp, _backup)
                _advance("backed_up")
                atomic_write_text(evp, "".join(json.dumps(record) + "\n" for record in kept))
                _advance("log_rewritten")
                if prepared_trace.temporary is not None:
                    trace_rewrite.publish_prepared_snapshot(prepared_trace, spans)
                _advance("trace_published")
                for deleted_id in subtree:
                    shutil.rmtree(rd / "nodes" / f"node_{deleted_id}", ignore_errors=True)
                _advance("workdirs_removed")
                _advance("succeeded")
            finally:
                prepared_trace.cleanup()

        remaining = fold(EventStore(evp).read_all())
        broken = sorted({parent for node in remaining.nodes.values() for parent in node.parent_ids
                         if parent not in remaining.nodes})
        operation = f"; operation {operation_id}" if operation_id else ""
        return (f"(deleted node(s) {sorted(subtree)} from {rid}; {len(remaining.nodes)} nodes left, "
                f"best now #{remaining.best_node_id}, broken parent links: {broken or 'none'}. "
                f"Backup: {_backup.name}{operation})")

    def _delete_run(self, rid: str, rd: Path) -> str:
        if self._live(rd):
            return f"(run {rid} is LIVE — stop it first before deleting)"
        from looplab.events.eventstore import EventStore

        events = EventStore(rd / "events.jsonl").read_all()
        expected_tail = events[-1].seq if events else -1
        blocked, formed_generation = self._gate(
            "delete_run", rid, rd, f"DELETE the entire run {rid} (irreversible)",
            scope={"run_id": rid, "expected_tail": expected_tail})
        if blocked:
            return blocked
        if not self._commands.durable_deletion_available:
            raise _MutationRecoveryBlocked(
                "run_deletion_service_unavailable",
                "Durable run deletion is unavailable; no run files were modified.")
        with self._mutation_intent(
                "delete_run", rid, rd, {"expected_tail": expected_tail},
                command_backed=True, expected_generation=formed_generation) as (key, generation):
            result = self._commands.begin_or_resume_deletion(
                rd, operation_id=_deletion_operation_id(key),
                expected_generation=generation, expected_seq=expected_tail)
            return _render_deletion_result(result, rid)
