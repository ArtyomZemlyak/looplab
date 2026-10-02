"""Explicit run-owned upstream transaction: propose, measure, then stopped-engine CAS.

Claims precede work. A lost acknowledgement acknowledges the original body only;
an interrupted gate never re-executes implicitly. The operator can abandon that
claim, which grants no pass and requires a new action ID for a new check.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re

from looplab.core.atomicio import strict_atomic_write_bytes
from looplab.core.errors import UpstreamRefusal
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.core.scorer_boundary import BoundaryCapture
from looplab.core.upstream_evidence import gate_matches_policy
from looplab.engine.run_lifecycle import engine_alive, fresh_resume_launch_pending, run_lifecycle_lock
from looplab.engine.seed_archive import capture_seed_archive, verified_seed_archive
from looplab.engine.seed_base import selected_seed_base
from looplab.engine.upstream_gate import boundary_at, execute_gate, input_identity
from looplab.engine.upstream_spec import normalize_request
from looplab.engine.upstream_state import active_base, digest, events_for, node_signature, source_node, upstream_candidates
from looplab.engine.upstream_workspace import checked_overlay, git_at, maintainer_worktree, snapshot_worktree, verify_approved_candidate, write_overlay, owned_path
from looplab.events.eventstore import EventStore, interprocess_lock
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token


class UpstreamLane:
    def __init__(self, run_dir, task, settings):
        self.rd, self.task, self.settings = Path(run_dir).resolve(), task, settings
        self.task.bind_run_directory(self.rd)

    def read(self, expected_generation, *, offset=0, limit=40):
        from looplab.agents.maintainer import Maintainer
        events = self._current(expected_generation)
        active = active_base(events, self.task.seed_base)
        history = [{"seq": e.seq, "type": e.type, **self._view(e.data)} for e in events
                   if e.type.startswith("upstream_") or e.type == "base_advanced"]
        return {"version": 1, "generation": expected_generation,
            "enabled": self.task.upstream is not None, "active_base": active,
            "source_health": {"events": "complete"}, "history": history[offset:offset + limit],
            "next_offset": offset + limit if len(history) > offset + limit else None,
            "candidates": upstream_candidates(self.rd, self.task, events) if self.task.upstream else {"rows": [], "bounded": False, "limit": 200},
            "instruction": "Pause and wait for the engine to exit; propose, check, inspect the measured gate, advance explicitly, then resume. No automatic advancement.",
            "maintainer_instruction": Maintainer.instruction,
            "engine_running": engine_alive(self.rd)}

    def _current(self, generation):
        events = events_for(self.rd)
        if not generation or run_generation_token(events) != generation:
            raise UpstreamRefusal("run_generation_conflict", "Refresh the current run generation")
        start = next((e for e in events if e.type == "run_started"), None)
        if start is None or not isinstance(start.data.get("run_id"), str) or not start.data["run_id"]:
            raise UpstreamRefusal("upstream_source_unavailable", "events.jsonl has no launched run identity")
        if start is not None and start.data.get("upstream") != self.task.upstream:
            raise UpstreamRefusal("upstream_policy_changed", "Restore the launched upstream declaration or launch a new task")
        from looplab.engine.seed_base import enforce_initial_seed_base
        enforce_initial_seed_base(events, self.task.seed_base, self.task.upstream)
        return events

    @contextmanager
    def _mutation(self, body, retry_kinds=()):
        if self.task.upstream is None:
            raise UpstreamRefusal("upstream_disabled", "The operator must declare upstream probes in a newly launched pinned task")
        if not isinstance(body.get("action_id"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", body["action_id"]):
            raise UpstreamRefusal("upstream_action_invalid", "Use a stable bounded action_id")
        with run_lifecycle_lock(self.rd), interprocess_lock(self.rd / ".upstream.lock", required=True):
            events = self._current(body.get("expected_generation"))
            previous = self._retry(events, body, set(retry_kinds)) if retry_kinds else None
            acknowledged = previous is not None and previous.type not in ("upstream_proposal_started", "upstream_gate_started")
            if not acknowledged and (engine_alive(self.rd) or fresh_resume_launch_pending(self.rd)):
                raise UpstreamRefusal("upstream_engine_running", "Pause, wait for engine exit, and retry explicitly")
            yield events

    def _append(self, kind, payload):
        events = events_for(self.rd)
        return EventStore(self.rd / "events.jsonl").append(kind, payload,
            expected_last_seq=events[-1].seq, require_lock=True, require_durable=True)

    def _retry(self, events, body, kinds):
        all_matches = [e for e in events if (e.type.startswith("upstream_") or e.type == "base_advanced") and e.data.get("action_id") == body["action_id"]]
        if any(e.type not in kinds and e.type != "upstream_execution" for e in all_matches):
            raise UpstreamRefusal("upstream_action_conflict", "This action_id belongs to another operation")
        matches = [e for e in all_matches if e.type in kinds]
        if matches:
            if any(e.data.get("request_hash") != digest(body) for e in matches):
                raise UpstreamRefusal("upstream_action_conflict", "This action_id already belongs to a different exact body")
            return matches[-1]
        return None

    def _unresolved(self, events, proposal_id=None):
        completed = {e.data.get("action_id") for e in events if e.type in ("upstream_proposed", "upstream_proposal_failed", "upstream_gate_finished")}
        abandoned = {e.data.get("claim_action_id") for e in events if e.type == "upstream_gate_abandoned"}
        return [e for e in events if e.type in ("upstream_proposal_started", "upstream_gate_started") and e.data.get("action_id") not in completed | abandoned
                and (proposal_id is None or e.data.get("proposal_id") == proposal_id)]

    def _validate_shared_patch(self, body, implementation):
        from looplab.engine.activation import is_config_path
        patch_paths = set(body["files"]) | set(body["deleted"])
        shared_patch = set(implementation) | {p for p in patch_paths if not is_config_path(p)}
        # Use the declaration's portable path identity across separate overlays;
        # NTFS writes RUNNER.py onto runner.py. Retain exact request spelling.
        shared_keys = {p.casefold() for p in shared_patch}
        recipe_keys = {p.casefold() for p in set(body["recipe_files"]) | set(body.get("recipe_deleted", []))}
        if shared_keys & recipe_keys:
            raise UpstreamRefusal("upstream_capability_not_absorbed", "The source recipe cannot overwrite the shared implementation")
        probes = sum((self.task.upstream[k] for k in ("tests", "regressions", "repair_probes")), [])
        if any(shared_keys & {p.casefold() for p in probe["files"]} for probe in probes):
            raise UpstreamRefusal("upstream_probe_masks_capability", "Probes cannot replace the shared implementation; exercise the actual old/new base")

    def _proposal(self, events, proposal_id):
        event = next((e for e in events if e.type == "upstream_proposed" and e.data.get("proposal_id") == proposal_id), None)
        if event is None:
            raise UpstreamRefusal("upstream_proposal_missing", "Read a current proposed capability")
        p = event.data
        path = owned_path(self.rd, "upstream/proposals/" + p["proposal_id"] + "/manifest.json")
        raw = read_bounded_regular_file(path, 2 * 1024 * 1024 + 1)
        if raw is None or len(raw) > 2 * 1024 * 1024:
            raise UpstreamRefusal("upstream_manifest_unavailable", "Restore the original proposal manifest")
        manifest = json.loads(raw)
        if digest(manifest) != p["manifest_hash"]:
            raise UpstreamRefusal("upstream_manifest_changed", "Proposal manifest changed; publish a new proposal")
        # Fresh checks/CAS must revalidate proposals recorded before an admission
        # fix. Exact ACK recovery returns earlier, preserving the saved receipt.
        self._validate_shared_patch(manifest, p["capability_paths"])
        selected_seed_base(p["selector"])
        return p, manifest

    def propose(self, body):
        from looplab.agents.maintainer import Maintainer
        body = normalize_request("propose", body)
        with self._mutation(body, {"upstream_proposal_started", "upstream_proposed", "upstream_proposal_failed"}) as events:
            previous = self._retry(events, body, {"upstream_proposal_started", "upstream_proposed", "upstream_proposal_failed"})
            if previous:
                if previous.type == "upstream_proposal_started":
                    raise UpstreamRefusal("upstream_claim_unresolved", "Proposal claim has no completion; inspect its worktree and explicitly abandon before a new action")
                return self.receipt(previous)
            Maintainer().validate(body)
            if self._unresolved(events):
                raise UpstreamRefusal("upstream_claim_unresolved", "Operator must resolve the previous interrupted upstream claim before new work")
            spec = self.task.repo_spec()
            checked_overlay(spec, body["files"], body["deleted"])
            checked_overlay(spec, body["recipe_files"], body.get("recipe_deleted", []))
            if not self.task.upstream["tests"]:
                raise UpstreamRefusal("upstream_tests_required", "Declare at least one operator test command")
            for probe in sum((self.task.upstream[k] for k in ("tests", "regressions", "repair_probes")), []):
                checked_overlay(spec, probe["files"])
            node, receipt = source_node(events, body["source_node_id"])
            source_archive = verified_seed_archive(self.rd, receipt)
            if source_archive is None:
                raise UpstreamRefusal("upstream_source_unavailable", "Source seed archive changed")
            active = active_base(events, self.task.seed_base)
            if body.get("expected_base_revision") != active["revision"] or receipt["digest"] != active["selector"]["digest"]:
                raise UpstreamRefusal("upstream_base_conflict", "Source node and proposal must refer to the current base revision")
            advice = upstream_candidates(self.rd, self.task, events)["rows"]
            rows = [r for r in advice if r["node_id"] == node.id and r["hunk_hash"] in body.get("hunk_hashes", [])]
            if not rows or len(rows) != len(set(body.get("hunk_hashes", []))) or any(r["classification"] != "capability" for r in rows):
                raise UpstreamRefusal("upstream_nomination_invalid", "Select current reusable capability hunks; recipes and already promoted hunks cannot advance")
            patch_paths = set(body["files"]) | set(body["deleted"])
            nominated_paths = {r["path"] for r in rows}
            from looplab.engine.activation import is_config_path
            repair_recipes = {r["path"] for r in rows if r["origin"] == "repair" and r["pending_trigger_nodes"] and is_config_path(r["path"])}
            implementation = (nominated_paths - repair_recipes) | {p for p in patch_paths
                if not is_config_path(p) and p != body["documentation_path"]}
            # Generalization can add helpers absent from the nominated source.
            # Neither a probe nor the source recipe may replace those shared bytes;
            # naming code as documentation does not exempt it from this boundary.
            if not implementation or not implementation <= patch_paths:
                raise UpstreamRefusal("upstream_capability_not_absorbed", "Implement the nominated capability in the base; the source recipe cannot overwrite its implementation")
            self._validate_shared_patch(body, implementation)
            for row in rows:
                if row["pending_trigger_nodes"] and row["origin"] == "repair":
                    if not any(all(any(token in list(map(str.strip, probe["files"].get(path, "").splitlines())) for token in tokens)
                        for path, tokens in row["trigger_tokens"].items()) for probe in self.task.upstream["repair_probes"]):
                        raise UpstreamRefusal("upstream_trigger_required", "Declare a repair probe with the recorded failing pending recipe trigger")
            for name in set(node.files) | set(body["recipe_files"]):
                if name not in patch_paths | repair_recipes and node.files.get(name) != body["recipe_files"].get(name):
                    raise UpstreamRefusal("upstream_recipe_changed", "Keep source recipe files outside capability paths byte identical")
            if set(body.get("recipe_deleted", [])) - patch_paths != set(node.deleted) - patch_paths:
                raise UpstreamRefusal("upstream_recipe_changed", "Preserve source recipe deletions outside capability paths")
            proposal_id = "up_" + digest(body["action_id"])[:24]
            common = {"action_id": body["action_id"], "request_hash": digest(body), "proposal_id": proposal_id}
            self._append("upstream_proposal_started", common)
            try:
                work = maintainer_worktree(self.rd, spec, active["selector"], proposal_id)
                write_overlay(work, body["files"], body["deleted"])
                if boundary_at(work, spec["scorer_boundary"]) != boundary_at(source_archive, spec["scorer_boundary"]):
                    raise UpstreamRefusal("upstream_scorer_changed", "Maintainer changed the declared scorer")
                git_at(work, "add", "-f", "-A")
                git_at(work, "commit", "-m", body["summary"][:500])
                commit = git_at(work, "rev-parse", "HEAD")
                snapshot = snapshot_worktree(work, work.parent / "snapshot")
                capture = BoundaryCapture(spec["scorer_boundary"])
                base = capture_seed_archive(snapshot, self.rd / "base_snapshots", on_file=capture.add)
                base["scorer_boundary"] = capture.receipt(base)
                verify_approved_candidate(active["selector"], body["files"], body["deleted"], base)
                if verified_seed_archive(self.rd, base) is None or base["digest"] == receipt["digest"]:
                    raise UpstreamRefusal("upstream_candidate_unavailable", "Candidate archive is unavailable or duplicates the old base")
                strict_atomic_write_bytes(work.parent / "manifest.json", json.dumps(body, ensure_ascii=False).encode())
                seed = self._append("workspace_seeded", {"node_id": None, "materialized": [], "base_revision": base})
                payload = {"action_id": common["action_id"], "request_hash": common["request_hash"], "proposal_id": proposal_id, "selector": {"run_dir": str(self.rd), "event_seq": seed.seq, "digest": base["digest"]},
                    "old_selector": active["selector"], "expected_base_revision": active["revision"],
                    "manifest_hash": digest(body), "base_revision": base, "commit": commit,
                    "source_node_id": node.id, "source_signature": node_signature(node),
                    "source_recipe": {"files": body["recipe_files"], "deleted": body.get("recipe_deleted", [])},
                    "capability_paths": sorted(implementation),
                    "hunk_hashes": body["hunk_hashes"], "repair_trigger_nodes": sorted({n for r in rows for n in r["pending_trigger_nodes"] if r["origin"] == "repair"}),
                    "summary": body["summary"], "flag": body["flag"], "critic": body["critic"]}
                return self.receipt(self._append("upstream_proposed", payload))
            except Exception as exc:  # noqa: BLE001 — persist a failed proposal claim; never grant a gate
                self._append("upstream_proposal_failed", {"action_id": common["action_id"], "request_hash": common["request_hash"], "proposal_id": proposal_id, "code": getattr(exc, "code", "upstream_proposal_failed")})
                raise

    def check(self, body):
        body = normalize_request("check", body)
        with self._mutation(body, {"upstream_gate_started", "upstream_gate_finished"}) as events:
            previous = self._retry(events, body, {"upstream_gate_started", "upstream_gate_finished", "upstream_gate_abandoned"})
            if previous:
                if previous.type == "upstream_gate_started":
                    raise UpstreamRefusal("upstream_claim_unresolved", "Gate claim has no verdict; read its logs, then explicitly abandon before buying a new check")
                return self.receipt(previous)
            proposal, manifest = self._proposal(events, body["proposal_id"])
            if self._unresolved(events):
                raise UpstreamRefusal("upstream_claim_unresolved", "Operator must abandon the interrupted claim before a new check")
            node, _ = source_node(events, proposal["source_node_id"])
            self._assert_base(events, proposal)
            if node_signature(node) != proposal["source_signature"]:
                raise UpstreamRefusal("upstream_source_changed", "Source lifecycle changed; propose again")
            bound = input_identity(self.task, self.settings, node, proposal)
            common = {"action_id": body["action_id"], "request_hash": digest(body), "proposal_id": body["proposal_id"]}
            self._append("upstream_gate_started", {"action_id": common["action_id"], "request_hash": common["request_hash"], "proposal_id": body["proposal_id"], "input_identity": bound})
            def charge(row):
                self._append("upstream_execution", {"action_id": common["action_id"], "request_hash": common["request_hash"], "proposal_id": body["proposal_id"], "execution": row})
            try:
                result = execute_gate(self.rd, self.task, self.settings, node, proposal, manifest, body["action_id"], charge)
            except Exception as exc:  # noqa: BLE001 — a bought check settles failed, preserving execution charges
                bought = [e.data["execution"] for e in events_for(self.rd) if e.type == "upstream_execution" and e.data.get("action_id") == body["action_id"]]
                result = {"passed": False, "input_identity": bound, "code": getattr(exc, "code", "upstream_execution_failed"), "checks": [], "executions": bought, "eval_seconds": sum(r["seconds"] for r in bought)}
            evidence = digest(result)
            return self.receipt(self._append("upstream_gate_finished", {"action_id": common["action_id"], "request_hash": common["request_hash"], "proposal_id": body["proposal_id"], "result": result, "evidence_token": evidence}))

    def _assert_base(self, events, proposal):
        active = active_base(events, self.task.seed_base)
        if active["revision"] != proposal["expected_base_revision"]:
            raise UpstreamRefusal("upstream_base_conflict", "Current base superseded this proposal")

    def advance(self, body):
        body = normalize_request("advance", body)
        with self._mutation(body, {"base_advanced"}) as events:
            previous = self._retry(events, body, {"base_advanced"})
            if previous:
                return self.receipt(previous)
            proposal, _ = self._proposal(events, body["proposal_id"])
            self._assert_base(events, proposal)
            if body.get("expected_base_revision") != proposal["expected_base_revision"]:
                raise UpstreamRefusal("upstream_base_conflict", "Refresh the base CAS revision")
            node, _ = source_node(events, proposal["source_node_id"])
            gates = [e for e in events if e.type == "upstream_gate_finished" and e.data.get("proposal_id") == body["proposal_id"]]
            if self._unresolved(events):
                raise UpstreamRefusal("upstream_claim_unresolved", "Resolve the interrupted claim before advancing")
            if not gates or gates[-1].data.get("evidence_token") != body.get("evidence_token") or gates[-1].data["result"].get("passed") is not True:
                raise UpstreamRefusal("upstream_gate_required", "Read the latest measured passing gate and supply its exact evidence_token")
            result = gates[-1].data["result"]
            later = [e for e in events if e.seq > gates[-1].seq and e.type in ("upstream_gate_started", "upstream_gate_abandoned") and e.data.get("proposal_id") == body["proposal_id"]]
            if later or digest(result) != gates[-1].data["evidence_token"]:
                raise UpstreamRefusal("upstream_gate_required", "The latest gate is unresolved/abandoned or its evidence is inconsistent; check afresh")
            gate_event = gates[-1]
            charged = [e.data for e in events if e.type == "upstream_execution"
                       and e.data.get("action_id") == gate_event.data["action_id"]]
            score = node.task_metric if node.task_metric is not None else node.metric
            if (not gate_matches_policy(result, self.task.upstream, score,
                    repair_required=bool(proposal["repair_trigger_nodes"]))
                    or any(r.get("proposal_id") != body["proposal_id"]
                           or r.get("request_hash") != gate_event.data["request_hash"] for r in charged)
                    or [r.get("execution") for r in charged] != result["executions"]):
                raise UpstreamRefusal("upstream_gate_required", "The latest gate is incomplete or differs from its declared probes, tolerances or recorded executions; inspect the evidence and check afresh")
            if input_identity(self.task, self.settings, node, proposal) != result["input_identity"]:
                raise UpstreamRefusal("upstream_evidence_changed", "Task, config, source, environment, inputs or archive changed; check again")
            state = fold(events)
            if len(state.inject_requests) > state.injects_done or len(state.fork_requests) > state.forks_done or state.building is not None or any(n.status.value == "pending" and n.eval_activity_started for n in state.nodes.values()):
                raise UpstreamRefusal("upstream_work_pending", "Resolve admitted candidate/build/evaluation work before changing the base")
            git_at(self.rd / "upstream" / "git", "update-ref", "refs/looplab/base/" + proposal["selector"]["digest"], proposal["commit"])
            payload = {"action_id": body["action_id"], "request_hash": digest(body), "proposal_id": body["proposal_id"],
                "selector": proposal["selector"], "from_revision": proposal["expected_base_revision"],
                "source_node_id": node.id, "hunk_hashes": proposal["hunk_hashes"], "flag": proposal["flag"],
                "summary": proposal["summary"], "evidence_token": body["evidence_token"], "gate_seq": gates[-1].seq}
            return self.receipt(self._append("base_advanced", payload))

    def abandon(self, body):
        body = normalize_request("abandon", body)
        with self._mutation(body, {"upstream_gate_abandoned"}) as events:
            previous = self._retry(events, body, {"upstream_gate_abandoned"})
            if previous:
                return self.receipt(previous)
            if not isinstance(body.get("reason"), str) or not body["reason"].strip():
                raise UpstreamRefusal("upstream_recovery_invalid", "Explain the operator's interrupted-claim recovery")
            target = next((e for e in events if e.type in ("upstream_gate_started", "upstream_proposal_started") and e.data.get("action_id") == body.get("claim_action_id")), None)
            if target is None:
                raise UpstreamRefusal("upstream_claim_missing", "Name an actual interrupted claim")
            if target not in self._unresolved(events):
                raise UpstreamRefusal("upstream_claim_settled", "Only an actually unresolved claim can be abandoned")
            # An abandonment is audit only, never a gate pass or automatic resume.
            return self.receipt(self._append("upstream_gate_abandoned", {"action_id": body["action_id"],
                "request_hash": digest(body), "proposal_id": target.data["proposal_id"],
                "claim_action_id": body["claim_action_id"], "reason": body["reason"][:700]}))

    @staticmethod
    def _view(payload):
        """Keep retained source bytes in the journal/manifest, not in repeated API ACKs."""
        view = dict(payload)
        if "source_recipe" in view:
            recipe = view.pop("source_recipe")
            view["source_recipe_digest"] = digest(recipe)
            view["source_recipe_paths"] = sorted(set(recipe["files"]) | set(recipe["deleted"]))
        return view

    @staticmethod
    def receipt(event):
        return {"version": 1, "status": "succeeded" if event.type in ("upstream_proposed", "base_advanced", "upstream_gate_abandoned") or event.type == "upstream_gate_finished" and event.data["result"].get("passed") else "failed",
                "event_type": event.type, "seq": event.seq, **UpstreamLane._view(event.data)}
