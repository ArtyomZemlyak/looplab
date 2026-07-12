"""Audit & trust emitters (telemetry events, workdir-tamper audit, redaction, leakage blocks)
for the engine — extracted from orchestrator.py as a MIXIN: `class Engine(AuditMixin, …)`
inherits these methods unchanged, so there is ZERO call-site churn and `self` here IS the
engine. The method bodies are verbatim moves and read engine attributes freely (`store`,
`developer`, `researcher`, `_assets`, `_redact_output`, `crash_after`), exactly as they did
inside the class — several are exercised on bare `Engine.__new__(Engine)` instances by tests,
which a mixin preserves.

The cluster: the per-node audit-event emitters (`_emit_agent_report` / `_emit_role_telemetry` /
`_emit_hypothesis_ranked` / `_emit_foresight_selected`), the protected-file tamper audit
(`_audit_workdir_writes`), output redaction (`_redact`), the crash-injection test hook
(`_maybe_crash`), and the leakage detector set (`_leakage_blocks`)."""
from __future__ import annotations

import os
from pathlib import Path

from looplab.events.types import (EV_AGENT_VALIDATED, EV_DATA_LEAKAGE, EV_FORESIGHT_SELECTED,
                                  EV_HYPOTHESIS_RANKED, EV_NODE_EVALUATED)
from looplab.trust.leakage import target_leakage, temporal_leakage, train_test_contamination


class AuditMixin:
    """The engine's audit/trust-emitter cluster. See the module docstring for the mixin
    convention (`self` is the Engine)."""

    def _emit_agent_report(self, node_id: int) -> None:
        """External-agent audit (ADR-7): if the Developer validated its output (a
        `ValidatingDeveloper`), record the verdict as an `agent_validated` event so each
        node carries a trail of how the external coding agent performed. No-op for
        plain developers (no `last_report`).

        Safe because node *creation* (`_create_node` / `_ablate`) is awaited sequentially
        in the main loop and never inside the parallel `evals` task group, so the shared
        `developer.last_report` set just above always belongs to `node_id`."""
        report = getattr(self.developer, "last_report", None)
        if report is not None:
            data = {"node_id": node_id, **report.summary()}
            extra = getattr(self.developer, "audit_extra", None)
            if callable(extra):
                data.update(extra())
            self.store.append(EV_AGENT_VALIDATED, data)

    def _emit_role_telemetry(self, role, attr: str, event_type: str, node_id: int) -> None:
        """Append `event_type` from a role's predictive-telemetry attr (a dict set during
        propose/implement), stamped with `node_id`, then CONSUME it (reset to None). Like
        `_emit_agent_report` this relies on sequential node creation for correctness; the consume adds
        a further guard specific to these predictive channels — a following non-propose action (merge /
        debug-repair, which never re-predicts) then finds None and can't re-emit a stale pick for the
        wrong node. No-op when the attr is absent/None (the role didn't predict for this node)."""
        pick = getattr(role, attr, None)
        if isinstance(pick, dict):
            pick = dict(pick)   # copy before consuming; strip the captured op-trace ids out of the data
            tid, sid = pick.pop("_trace_id", None), pick.pop("_span_id", None)
            # The ranking LLM ran DURING propose in its own named span (captured there); stamp the event
            # with THAT trace so the UI scopes it to just the ranking, not the whole node.
            self.store.append(event_type, {"node_id": node_id, **pick}, trace_id=tid, span_id=sid)
            setattr(role, attr, None)

    def _emit_hypothesis_ranked(self, node_id: int) -> None:
        """FOREAGENT board prioritization audit: if the active Researcher (a `ForesightPanelResearcher`)
        predicted an order over the OPEN-hypothesis board while proposing THIS node, record it as a
        `hypothesis_ranked` event — the analysis + selection trace the UI surfaces (kanban order + the
        model's `reason`)."""
        self._emit_role_telemetry(self.researcher, "last_hyp_priority", EV_HYPOTHESIS_RANKED, node_id)

    def _emit_foresight_selected(self, node_id: int) -> None:
        """FOREAGENT predict-before-execute audit: when the world model picked WHICH candidate becomes
        this node — the best of K generated ideas (the researcher panel) or of N code implementations
        (best-of-N) — record the ranking + confidence + the model's reasoning as a `foresight_selected`
        event. Without it the choice and its discarded alternatives vanish (only the winner survives in
        `node_created`)."""
        self._emit_role_telemetry(self.developer, "last_foresight_pick", EV_FORESIGHT_SELECTED, node_id)
        self._emit_role_telemetry(self.researcher, "last_foresight", EV_FORESIGHT_SELECTED, node_id)

    def _audit_workdir_writes(self, workdir, protected: set) -> list[dict]:
        """4.4: after an eval, flag any PROTECTED/frozen file (assets/answer keys) whose on-disk state
        no longer matches what the engine wrote — a runtime tamper the static code scan can't see. Pure
        host-side check feeding `reward_hack_suspected`.

        FAIL-CLOSED (arch-review §4 P1-6): the audit must never report CLEAN when it could not actually
        VERIFY the file. So a protected file we placed that is now MISSING (a deletion — `os.remove`,
        which the static scan misses) is a hard `protected_missing` signal; one that is UNREADABLE
        (invalid bytes / permission) is `protected_unreadable`; and if the whole audit throws
        unexpectedly it emits an advisory `protected_audit_unavailable` rather than an empty (=clean)
        list. Only a file we have no baseline for (`original is None`) is genuinely un-judgeable and
        skipped. `protected_missing`/`protected_unreadable` are HARD (gate/block); the whole-audit
        failure stays advisory (a transient FS error should surface, not exclude an honest node)."""
        sigs: list[dict] = []
        try:
            wd = Path(workdir)
            for name in protected:
                original = self._assets.get(name)
                if original is None:
                    continue                        # no baseline placed -> genuinely un-judgeable
                p = wd / name
                try:
                    if not p.is_file():
                        # The engine placed this protected file; it is now GONE. A deletion is a tamper
                        # the static write-scan never sees (os.remove/os.unlink/Path.unlink). Never clean.
                        sigs.append({"signal": "protected_missing",
                                     "detail": f"protected file '{name}' was deleted at runtime"})
                        continue
                    # Compare as TEXT for str assets: `_write_assets` writes them via `Path.write_text`
                    # (text mode translates '\n' -> os.linesep), so a raw-BYTES compare would flag EVERY
                    # honest eval where os.linesep != '\n' (Windows CRLF) as a tamper. Bytes byte-exact.
                    if isinstance(original, str):
                        try:
                            got = p.read_text(encoding="utf-8")
                        except (OSError, UnicodeDecodeError):
                            # Unreadable protected input is NOT clean: an eval that corrupts the answer
                            # key to invalid bytes must not read as untampered (the old code set got=None
                            # and fell through to tampered=False). Surface it as a hard signal.
                            sigs.append({"signal": "protected_unreadable",
                                         "detail": f"protected file '{name}' is unreadable after the eval"})
                            continue
                        tampered = got != original
                    else:
                        tampered = p.read_bytes() != bytes(original)
                    if tampered:
                        sigs.append({"signal": "protected_write",
                                     "detail": f"protected file '{name}' was modified at runtime"})
                except Exception:  # noqa: BLE001 — one file's odd error must not abort the whole audit
                    sigs.append({"signal": "protected_unreadable",
                                 "detail": f"protected file '{name}' could not be audited"})
        except Exception:  # noqa: BLE001 — an audit failure must never fail the eval NOR read as clean
            sigs.append({"signal": "protected_audit_unavailable",
                         "detail": "the protected-file audit did not complete"})
        return sigs

    def _redact(self, text: str) -> str:
        """B3: mask secrets in an output tail before it is persisted, when redaction is enabled."""
        if not self._redact_output or not text:
            return text
        from looplab.trust.redact import redact_secrets
        return redact_secrets(text)

    def _maybe_crash(self) -> None:
        if self.crash_after is None:
            return
        n_eval = sum(1 for e in self.store.read_all() if e.type == EV_NODE_EVALUATED)
        if n_eval >= self.crash_after:
            os._exit(137)  # simulate kill -9 (no cleanup, no run_finished)

    def _leakage_blocks(self) -> bool:
        """Leakage-first gate (I9): run the detectors on whatever split/feature/target/
        timestamp data the task exposes via `leakage_inputs()`. Emit a verdict; return
        True (abort) if a hard leak is found. Tasks without the method are skipped."""
        fn = getattr(self.task, "leakage_inputs", None)
        if not callable(fn):
            return False
        inp = fn() or {}
        verdicts = []
        if "train_rows" in inp and "test_rows" in inp:
            verdicts.append(train_test_contamination(inp["train_rows"], inp["test_rows"]))
        if "features" in inp and "target" in inp:
            verdicts.append(target_leakage(inp["features"], inp["target"]))
        if "train_timestamps" in inp and "test_timestamps" in inp:
            verdicts.append(temporal_leakage(inp["train_timestamps"], inp["test_timestamps"]))
        leak = any(v.get("leak") for v in verdicts)
        self.store.append(EV_DATA_LEAKAGE, {"leak": leak, "verdicts": verdicts})
        return leak
