"""PART V §22.4 (Phase 2): the owner assistant's CONCEPT-TAXONOMY editing toolset.

Lets the assistant EDIT the shared cross-run concept taxonomy — merge/rename one concept into another,
purge it, split a coarse concept into finer ones, or clear a prior policy — through the SAME append-only,
reversible governance ledger (`looplab.engine.concept_registry`) the `/api/cross-run/concept-*` endpoints
and the taxonomy steward use. Nothing here is destructive-in-place: every edit is an append to
`concept_aliases.jsonl` / `concept_splits.jsonl`, reversible by a later `*_clear`. READS are always
available; every MUTATION is gated by the assistant permission mode + approver (exactly like `remember` /
the write tools), so a read-only `plan` session can inspect the taxonomy but never edit it.

The registry functions are imported LAZILY inside `execute` (mirroring CrossRunTools), so tools/ never
takes an import-time dependency on engine/. Every `execute` returns a STRING and soft-fails — an operator
error (empty source, self-link, cycle, purged/non-existent concept) becomes a readable message, never a
raised exception (the ToolProvider contract, tools/_base.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from looplab.tools._base import fn_spec
from looplab.tools.perm_modes import (
    DEFAULT_MODE, approval_allows, decide_action, default_approver)


class ConceptGovernanceTools:
    """Cross-run concept-taxonomy governance for the assistant tool-loop. Reads always; mutations are
    mode+approver gated. `role`-free: the taxonomy is one shared portfolio artifact. Never raises."""

    def __init__(self, memory_dir: str | Path | None, *, mode: str = DEFAULT_MODE,
                 approver=None, actor: str = "assistant"):
        self.dir = str(memory_dir) if memory_dir else None
        self.mode = mode
        self.approver = approver or default_approver
        self.actor = str(actor or "assistant")

    # --- specs --------------------------------------------------------------
    def specs(self) -> list[dict]:
        if not self.dir:
            return []
        specs = [fn_spec(
            "concept_taxonomy",
            "Show the current EDITABLE cross-run concept taxonomy: active merges/renames (aliases), "
            "purges (tombstones), and splits. Read this before editing so you mutate the LIVE canonical "
            "concept, not a stale or already-aliased slug.",
            {}, [])]
        if self.mode == "plan":
            return specs            # read-only session: inspect the taxonomy, never edit it
        specs += [
            fn_spec(
                "concept_merge",
                "MERGE/RENAME one concept into another across the whole portfolio: declare `from_concept` "
                "is really `to_concept` (append-only, reversible with concept_edit_clear). Use it to fold a "
                "near-duplicate or rename a concept. Both must be live canonical concepts (see "
                "concept_taxonomy). Editing shared cross-run state — asks for approval outside auto mode.",
                {"from_concept": {"type": "string", "description": "The concept to retire (axis/slug id)."},
                 "to_concept": {"type": "string", "description": "The concept it becomes (axis/slug id)."}},
                ["from_concept", "to_concept"]),
            fn_spec(
                "concept_purge",
                "PURGE/tombstone one concept: drop `concept` from all cross-run views (reversible with "
                "concept_edit_clear). Use it for a junk or mis-tagged concept. Editing shared cross-run "
                "state — asks for approval outside auto mode.",
                {"concept": {"type": "string", "description": "The concept to purge (axis/slug id)."}},
                ["concept"]),
            fn_spec(
                "concept_split",
                "SPLIT a coarse `from_concept` into finer concepts per RULES. `rules` is an ordered list of "
                "{\"to\": \"axis/slug\", \"when_any\": [term, ...]}: for each run, the FIRST rule whose terms "
                "appear among that run's sibling concept tokens wins; `default` (optional) catches the rest. "
                "Append-only, reversible with concept_edit_clear. Editing shared cross-run state — asks for "
                "approval outside auto mode.",
                {"from_concept": {"type": "string", "description": "The coarse concept to split."},
                 "rules": {"type": "array", "description": "Ordered [{to, when_any:[...]}] rules.",
                           "items": {"type": "object"}},
                 "default": {"type": "string", "description": "Optional fallback concept for unmatched runs."}},
                ["from_concept", "rules"]),
            fn_spec(
                "concept_edit_clear",
                "UNDO the active merge/purge (kind='alias') or split (kind='split') policy for one concept, "
                "through an append-only clear record. Use it to revert a prior edit.",
                {"concept": {"type": "string", "description": "The source concept whose policy to clear."},
                 "kind": {"type": "string", "enum": ["alias", "split"],
                          "description": "'alias' clears a merge/purge; 'split' clears a split."}},
                ["concept", "kind"]),
        ]
        return specs

    # --- helpers ------------------------------------------------------------
    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # NOTE: we deliberately DO NOT pass a registry `action_id`. That field is a per-request idempotency
    # TOKEN, not a content hash — the registry returns the FIRST receipt (and appends nothing) for a repeat
    # id+payload, and RAISES on a repeat id with a changed payload. A content-hashed id would (a) silently
    # no-op a legitimate re-apply after a clear (merge A->B; clear A; merge A->B would leave A unmerged while
    # reporting success) and (b) reject a same-arity split re-edit. Concept edits are last-write-wins in
    # EFFECT, so letting each call append fresh is correct: a repeated edit is a harmless no-op-in-effect and
    # a changed edit takes effect. (Reviewed 2026-07-17.)

    def _gate(self, name: str, label: str, preview: str, scope: dict) -> Optional[str]:
        """Apply the shared permission policy to one mutation. Returns None to PROCEED, else a message."""
        action = {"tool": name, "tool_kind": "concept_edit", "label": label,
                  "verb": "edit the shared cross-run concept taxonomy", "path": str(self.dir),
                  "preview": preview[:4000], "scope": scope}
        decision = decide_action(self.mode, action)
        if decision == "deny":
            return (f"({name} is disabled in read-only plan mode. Switch to default/acceptEdits/auto to "
                    "edit the shared concept taxonomy.)")
        if decision == "ask" and not approval_allows(self.approver(action) or "deny"):
            return f"(declined by the user: {label})"
        return None

    # --- dispatch -----------------------------------------------------------
    def execute(self, name: str, args: dict) -> str:
        if not self.dir:
            return "(no memory_dir configured — the cross-run concept taxonomy is unavailable)"
        args = args or {}
        try:
            if name == "concept_taxonomy":
                return self._taxonomy()
            # Plan mode is enforced in ONE place — each mutation calls _gate first, and decide_action
            # denies a concept_edit in plan (specs() also hides the verbs from the schema). No separate
            # plan-check here, so there is a single consistent refusal path/message.
            if name == "concept_merge":
                return self._merge(str(args.get("from_concept") or ""), str(args.get("to_concept") or ""))
            if name == "concept_purge":
                return self._purge(str(args.get("concept") or ""))
            if name == "concept_split":
                return self._split(str(args.get("from_concept") or ""),
                                   args.get("rules"), str(args.get("default") or ""))
            if name == "concept_edit_clear":
                return self._clear(str(args.get("concept") or ""), str(args.get("kind") or ""))
            return f"(unknown tool: {name})"
        except Exception as exc:  # noqa: BLE001 — ToolProvider contract: never raise from execute
            return f"(concept edit error: {exc})"

    def _taxonomy(self) -> str:
        from looplab.engine.concept_registry import (
            _TOMBSTONE, load_concept_aliases, load_concept_splits)
        aliases = load_concept_aliases(self.dir) or {}
        splits = load_concept_splits(self.dir) or {}
        # A purge is stored as the _TOMBSTONE sentinel target (a truthy string), NOT "" — classify by it.
        merges = sorted((f, t) for f, t in aliases.items() if t and t != _TOMBSTONE)
        purges = sorted(f for f, t in aliases.items() if t == _TOMBSTONE)
        lines = [f"Cross-run concept taxonomy: {len(merges)} merge(s), {len(purges)} purge(s), "
                 f"{len(splits)} split(s)."]
        if merges:
            lines.append("Merges (from -> to): " + ", ".join(f"{f} -> {t}" for f, t in merges[:40]))
        if purges:
            lines.append("Purged: " + ", ".join(purges[:40]))
        if splits:
            lines.append("Splits: " + ", ".join(sorted(splits)[:40]))
        if not (merges or purges or splits):
            lines.append("(no taxonomy edits yet — the raw per-run concept slugs are the vocabulary)")
        return "\n".join(lines)

    def _merge(self, src: str, dst: str) -> str:
        if not src.strip() or not dst.strip():
            return "(concept_merge needs both from_concept and to_concept)"
        gate = self._gate("concept_merge", f"merge {src} -> {dst}", f"{src} -> {dst}",
                          {"from": src, "to": dst})
        if gate is not None:
            return gate
        from looplab.engine.concept_registry import record_concept_alias
        rec = record_concept_alias(self.dir, from_concept=src, to_concept=dst, by=self.actor,
                                   at=self._now(), require_existing=True)
        return (f"merged '{rec.get('from')}' -> '{rec.get('to')}' "
                f"(governance revision {rec.get('governance_revision')})")

    def _purge(self, concept: str) -> str:
        if not concept.strip():
            return "(concept_purge needs a concept)"
        gate = self._gate("concept_purge", f"purge {concept}", concept, {"purge": concept})
        if gate is not None:
            return gate
        from looplab.engine.concept_registry import record_concept_alias
        rec = record_concept_alias(self.dir, from_concept=concept, to_concept="", by=self.actor,
                                   at=self._now(), require_existing=True)
        return f"purged '{rec.get('from')}' (governance revision {rec.get('governance_revision')})"

    def _split(self, src: str, rules, default: str) -> str:
        if not src.strip():
            return "(concept_split needs a from_concept)"
        if not isinstance(rules, (list, tuple)) or not rules:
            return "(concept_split needs a non-empty `rules` list of {to, when_any:[...]})"
        gate = self._gate("concept_split", f"split {src}", src, {"split": src, "n_rules": len(rules)})
        if gate is not None:
            return gate
        from looplab.engine.concept_registry import record_concept_split
        rec = record_concept_split(self.dir, from_concept=src, rules=list(rules), default=default,
                                   by=self.actor, at=self._now(), require_existing=True)
        # Report the STORED rule count (the registry drops inert rules), not the raw input count.
        n_stored = len(rec.get("rules") or [])
        return (f"split '{rec.get('from')}' into {n_stored} rule(s) "
                f"(governance revision {rec.get('governance_revision')})")

    def _clear(self, concept: str, kind: str) -> str:
        if not concept.strip():
            return "(concept_edit_clear needs a concept)"
        if kind not in ("alias", "split"):
            return "(concept_edit_clear needs kind='alias' (merge/purge) or kind='split')"
        gate = self._gate("concept_edit_clear", f"clear {kind} for {concept}", concept,
                          {"clear": concept, "kind": kind})
        if gate is not None:
            return gate
        from looplab.engine.concept_registry import clear_concept_alias, clear_concept_split
        clearer = clear_concept_alias if kind == "alias" else clear_concept_split
        # require_existing: clearing a concept with NO active policy is an operator error (a spurious clear
        # record + a false "cleared" receipt otherwise) — matches the /cross-run clear endpoints.
        rec = clearer(self.dir, from_concept=concept, by=self.actor, at=self._now(), require_existing=True)
        return (f"cleared {kind} policy for '{rec.get('from')}' "
                f"(governance revision {rec.get('governance_revision')})")
