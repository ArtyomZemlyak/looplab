"""The speculation-gate CALIBRATION envelope — extracted from orchestrator.py (doc 25 ES-01).

ES-01 measured ~220 lines of nested closures inside `Engine.__init__` plus two module-level helpers
that "none of which touch the run loop". They validate the one runtime that the calibrated
speculation evidence actually measured, and they are the reason a caller cannot obtain the
depth-waiver by constructing `Engine` directly with arbitrary roles and settings.

Extracting them is not only about `__init__`'s length. As closures over twenty construction locals
the envelope could not be stated, so it could not be unit-tested: the only way to exercise a single
rule in it was to build a whole Engine and read one string out of a `ConfigRefusal` message.
`CalibrationRuntime` names those twenty inputs once, and the two functions below are now ordinary
functions with a truth table — which is the bar CLAUDE.md sets for a rule buried where no caller can
reach it ("a rule nobody can state is a rule nobody reviews").

The bodies are the closures' own text, unchanged: the only edits are the signature lines, the
rebinding preamble, and an AST-located `self` -> `engine` rename. A reworded policy check is a
changed policy check, and this envelope refuses runs.

The three lazy imports inside `narrow_runtime_envelope_errors` stay function-local exactly as they
were — `ToyTask`, `SubprocessSandbox`, `GreedyTree` and `hash_embed` are compared by EXACT type, so
they are patch-visible identities, and hoisting them to module scope would also give `engine` a new
module-level dependency on `adapters`/`runtime`/`search`/`tools` for a code path most runs skip.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import orjson

from looplab.core.errors import ConfigRefusal
from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_PROFILE_DIGEST,
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
    SPECULATION_POLICY_SCOPE,
    SPECULATION_CALIBRATION_SEEDS,
    canonical_speculation_toy_task,
    speculation_runtime_scope_digest,
)


@dataclass(frozen=True)
class CalibrationRuntime:
    """Exactly the construction inputs the narrow envelope is allowed to look at.

    One field per local the closure captured, under the same name, so the check bodies did not have
    to be rewritten and a future reader can still diff them against the closure they replaced.
    `read_option`/`option_fields` are `Engine.__init__`'s own `EngineOptions` accessors: the profile
    comparison must see what the ENGINE resolved (defaults, aliases, snapshot round-trips), not a
    second reading of the same knobs.
    """

    option_fields: frozenset
    read_option: Callable[[str], object]
    recorded_runtime_scope: Optional[str]
    card_driven_selection: object
    max_nodes: object
    speculation_depth: object
    task: object
    researcher: object
    developer: object
    policy: object
    sandbox: object
    crash_after: object
    strategist: object
    deep_researcher: object
    report_writer: object
    developer_factory: object
    onboarder: object
    proxy_scorer: object
    lesson_abstractor: object
    dep_installer: object


def _stable_effective_gpu_inventory(raw) -> list[dict]:
    """Canonical, resume-stable projection of ``effective_gpu_inventory``.

    Free memory is deliberately excluded: it changes while unrelated jobs start and is not machine
    identity.  The effective helper already applies CUDA_VISIBLE_DEVICES; this projection preserves its
    logical indices and stable hardware identity only.
    """
    if not isinstance(raw, list):
        return []
    stable: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            return []
        index = item.get("index")
        uuid = item.get("uuid")
        pci_bus_id = item.get("pci_bus_id")
        name = item.get("name")
        total = item.get("mem_total_mib")
        driver_version = item.get("driver_version")
        cuda_driver_version = item.get("cuda_driver_version")
        if (type(index) is not int or index < 0 or not isinstance(name, str)
                or not name.strip() or type(total) is not int or total <= 0
                or not isinstance(uuid, str) or not uuid.strip()
                or not isinstance(pci_bus_id, str) or not pci_bus_id.strip()
                or not isinstance(driver_version, str) or not driver_version.strip()
                or type(cuda_driver_version) is not int or cuda_driver_version <= 0):
            return []
        stable.append({
            "index": index,
            "uuid": uuid.strip(),
            "pci_bus_id": pci_bus_id.strip(),
            "name": name.strip(),
            "mem_total_mib": total,
            "driver_version": driver_version.strip(),
            "cuda_driver_version": cuda_driver_version,
        })
    stable.sort(key=lambda row: row["index"])
    if (
        len({row["index"] for row in stable}) != len(stable)
        or len({row["uuid"] for row in stable}) != len(stable)
        or len({row["pci_bus_id"] for row in stable}) != len(stable)
    ):
        return []
    return stable


def engine_authored_artifacts(engine) -> bool:
    """Does the ENGINE itself author every eval artifact in this run, rather than an agent?

    THE ONE PLACE THAT ASSERTION IS MADE, and the reason it is made from engine state rather than
    from the artifact. `core/calibration.py::engine_declared_extra_metric_keys` names the extra-metric
    keys the engine's own CUDA probe prints, and until 2026-08-14 the whole grant was a byte-exact
    prefix match against the code the sandbox was about to run — i.e. against `solution.py`, which on
    a repo run is written by an external coding agent. The prefix is a public constant in the shipped
    tree, so that authenticated nothing: prefix + one `print` earned the `engine` tag, and it survived
    `auto_extra_metrics=false`, the operator's "authenticated only" switch. Nothing derivable from an
    artifact the candidate writes can say who wrote it. This can, because it reads the engine's OWN
    role wiring — state no candidate can reach, in a run no candidate participates in.

    True in exactly one configuration: the speculation-calibration profile, whose Developer is the
    engine's own probe splicer. `agents/roles.py::ToyObjectiveDeveloper.implement` splices the probe
    UNCONDITIONALLY when `calibration_gpu_probe` is set, and that flag is deliberately not a
    Settings/env/UI knob — `cli/__init__.py::_make_calibration_roles` is its only writer. So in that
    run every artifact is engine-authored, and in every other run this is False and the fail-safe
    `auto` stands.

    EXACT TYPE, no wrapper and no subclass, matching `_calibration_role_pair_errors` right below —
    which is the same rule for the same object and refuses the calibration run outright if it does
    not hold. Total and fail-closed: any engine without a readable developer answers False.
    """
    from looplab.agents.roles import ToyObjectiveDeveloper

    developer = getattr(engine, "developer", None)
    return (type(developer) is ToyObjectiveDeveloper
            and getattr(developer, "calibration_gpu_probe", False) is True)


def _calibration_role_pair_errors(task, researcher, developer) -> list[str]:
    """Validate the two default-off purpose flags without accepting wrappers/subclasses."""
    from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher

    errors: list[str] = []
    if type(researcher) is not ToyResearcher:  # exact: a wrapper could make live/model calls
        errors.append("researcher must be the exact ToyResearcher")
    else:
        if getattr(researcher, "calibration_concepts", False) is not True:
            errors.append("ToyResearcher.calibration_concepts must be true")
        if (researcher.bounds != task.bounds or researcher.step != task.step
                or researcher.seed != task.seed):
            errors.append("ToyResearcher must match the calibrated task bounds/step/seed")
    if type(developer) is not ToyObjectiveDeveloper:
        errors.append("developer must be the exact ToyObjectiveDeveloper")
    else:
        if getattr(developer, "calibration_gpu_probe", False) is not True:
            errors.append("ToyObjectiveDeveloper.calibration_gpu_probe must be true")
        if developer.noise != 0.0:
            errors.append("ToyObjectiveDeveloper noise must be zero")
    return errors


def narrow_runtime_envelope_errors(engine, rt: CalibrationRuntime) -> tuple[list[str], str]:
    """Validate the one runtime that calibration evidence actually measured."""
    # Rebind the captured construction inputs under their original names, so the envelope below
    # is the SAME text it was as a closure — a reworded policy check is a changed policy check.
    _fields, _opt = rt.option_fields, rt.read_option
    _speculation_runtime_scope_sha256 = rt.recorded_runtime_scope
    card_driven_selection = rt.card_driven_selection
    max_nodes = rt.max_nodes
    speculation_depth = rt.speculation_depth
    task = rt.task
    researcher = rt.researcher
    developer = rt.developer
    policy = rt.policy
    sandbox = rt.sandbox
    crash_after = rt.crash_after
    strategist = rt.strategist
    deep_researcher = rt.deep_researcher
    report_writer = rt.report_writer
    developer_factory = rt.developer_factory
    onboarder = rt.onboarder
    proxy_scorer = rt.proxy_scorer
    lesson_abstractor = rt.lesson_abstractor
    dep_installer = rt.dep_installer
    import sys
    from looplab.adapters.toytask import ToyTask
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree
    from looplab.tools.vectorstore import hash_embed

    errors: list[str] = []
    option_renames = {"policy": "policy_name"}
    for setting, expected in SPECULATION_CALIBRATION_PROFILE_SETTINGS.items():
        option_name = option_renames.get(setting, setting)
        if option_name not in _fields:
            continue
        actual = _opt(option_name)
        try:
            # EngineOptions retains schema-native tuples while snapshots contain JSON arrays.
            matches_profile = orjson.dumps(
                actual, option=orjson.OPT_SORT_KEYS) == orjson.dumps(
                    expected, option=orjson.OPT_SORT_KEYS)
        except (TypeError, ValueError, orjson.JSONEncodeError):
            matches_profile = False
        if not matches_profile:
            errors.append(f"{setting} must be {expected!r}, got {actual!r}")

    if card_driven_selection is not True:
        errors.append("card_driven_selection must be exactly true")
    if type(max_nodes) is not int or not 1 <= max_nodes <= 64:
        errors.append("max_nodes must be an integer in 1..64")
    if type(speculation_depth) is not int or not 0 <= speculation_depth <= 64:
        errors.append("speculation_depth must be an integer in 0..64")
    if not engine.run_dir.name.strip():
        errors.append("run directory must have a non-empty run id")

    expected_scope = ""
    if type(max_nodes) is int and type(speculation_depth) is int:
        try:
            expected_scope = speculation_runtime_scope_digest({
                **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
                "max_nodes": max_nodes,
                "speculation_depth": speculation_depth,
                "speculation_gate_receipt": engine.speculation_gate_receipt,
            })
        except ValueError as exc:
            errors.append(f"runtime scope could not be constructed: {exc}")
    if (
        not expected_scope
        or _speculation_runtime_scope_sha256 != expected_scope
    ):
        errors.append(
            "runtime scope digest must match the source-owned full Settings profile "
            "and live max_nodes")

    if type(task) is not ToyTask:
        errors.append("task must be the exact offline ToyTask")
    else:
        try:
            canonical_speculation_toy_task(task, require_seed_set=True)
        except ValueError as exc:
            errors.append(str(exc))
        errors.extend(_calibration_role_pair_errors(task, researcher, developer))
    if (
        type(policy) is not GreedyTree
        or policy.n_seeds != len(SPECULATION_CALIBRATION_SEEDS)
        or policy.max_nodes != max_nodes
        or policy.debug_depth != 1
        or policy.enable_merge is not True
        or policy.merge_every != 3
        or policy.max_merges != 2
        or policy.ablate_every != 0
        or policy.operator_bandit is not False
    ):
        errors.append("policy must be the canonical bounded GreedyTree")
    if (
        type(sandbox) is not SubprocessSandbox
        or sandbox.python != sys.executable
        or sandbox.max_output_bytes != 64_000
        or sandbox.mem_bytes is not None
        or sandbox.fsize_bytes is not None
    ):
        errors.append("sandbox must be the exact default trusted-local SubprocessSandbox")
    for name, value in (
        ("strategist", strategist), ("deep_researcher", deep_researcher),
        ("report_writer", report_writer), ("developer_factory", developer_factory),
        ("onboarder", onboarder), ("proxy_scorer", proxy_scorer),
        ("lesson_abstractor", lesson_abstractor), ("dep_installer", dep_installer),
    ):
        if value is not None:
            errors.append(f"{name} must be disabled")
    if engine._embedder is not hash_embed:
        errors.append("embedder must be the offline hash embedder")
    if not callable(engine.role_factory):
        errors.append("role_factory must provide isolated calibrated Toy roles")
    if crash_after is not None:
        errors.append("crash_after is forbidden in the calibrated runtime")
    return errors, expected_scope


def guard_calibrated_role_factory(engine, task) -> None:
    original_role_factory = engine.role_factory

    def _calibrated_role_factory():
        pair = original_role_factory()
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise RuntimeError("calibrated role_factory must return one role pair")
        pair_errors = _calibration_role_pair_errors(task, pair[0], pair[1])
        if pair_errors:
            raise RuntimeError(
                "calibrated role_factory escaped the purpose envelope: "
                + "; ".join(pair_errors))
        return pair

    engine.role_factory = _calibrated_role_factory


def _receipt_refusal_detail(receipt_path, validated) -> str:
    """Why the calibrated toy lane is about to refuse this run — a suffix, never a decision.

    The refusal above states a DISJUNCTION ("stale, invalid, non-GPU, policy-mismatched, or does not
    pass the current gates") and, for an operator, that is five different problems wearing one
    sentence. Measured on this box's own 2026-08-04 receipt: it had been revoked along FOUR
    independent axes at once (the `Settings` field set the archived config snapshots are compared
    against, the installed-distribution fingerprint, the whole-source implementation digest, and the
    visible GPU inventory), and two agents inspecting the code that week each named the source
    digest as the cause because nothing could be asked which one had moved.

    Two cases, and they are genuinely different questions. A receipt that did not REVALIDATE is
    diagnosed by re-running the ordered checklist that rejected it — a second derivation, taken only
    here, on a path that is already raising and ending the run, so the admitting path is unchanged.
    A receipt that revalidated and then failed a LANE requirement is diagnosed locally: nothing
    expensive is involved, the mismatch is a field compare.

    Never raises: a diagnostic that can abort the refusal it is explaining would replace a stated
    refusal with a traceback.
    """
    try:
        if validated is None:
            from looplab.search.speculation_quality import (
                speculation_gate_receipt_rejection,
            )
            reason = speculation_gate_receipt_rejection(receipt_path)[1]
        else:
            reason = next(
                (
                    text for ok, text in (
                        (validated.get("require_gpu") is True,
                         "the receipt was not measured with require_gpu"),
                        (bool(validated.get("gpu_inventory")),
                         "the receipt carries no GPU inventory"),
                        (validated.get("policy_scope") == SPECULATION_POLICY_SCOPE,
                         f"receipt policy scope is {validated.get('policy_scope')!r}, "
                         f"expected {SPECULATION_POLICY_SCOPE!r}"),
                        (validated.get("calibration_profile_digest")
                         == SPECULATION_CALIBRATION_PROFILE_DIGEST,
                         "receipt calibration profile digest is not this build's"),
                        (validated.get("workload_scope") == "quadratic_toy",
                         f"receipt workload scope is {validated.get('workload_scope')!r}, "
                         "expected 'quadratic_toy'"),
                        (isinstance(validated.get("implementation_digest"), str)
                         and bool(validated.get("implementation_digest")),
                         "receipt carries no implementation digest"),
                    ) if not ok
                ),
                "",
            )
    except Exception:  # noqa: BLE001 — the suffix is descriptive; an unreadable receipt drops it
        return ""
    return f" — {reason}" if reason else ""


def admit_speculation_lane(engine, rt: CalibrationRuntime, gate_receipt) -> None:
    """Decide, once, which speculation lane this construction is entitled to, and stamp its identity.

    Extracted from `Engine.__init__` (doc 25 XP-06): at 207 lines this single `if` was the largest
    block in an 822-line constructor, and it is not construction at all — it is the gate's admission
    DECISION, sibling to the envelope above it. Nothing after it in `__init__` reads any of its
    locals (measured), and its whole output is the ten `_speculation_*` attributes it stamps, so the
    cut is exact rather than a convenient place to stop.

    The three lanes it chooses between are, in order: the calibration BOOTSTRAP (validate the narrow
    envelope at the library boundary, so a caller cannot obtain the depth waiver by constructing
    `Engine` directly with arbitrary roles), the calibrated REPLAY lane (the only lane a receipt
    binds anything to, because it is the only one whose workload the receipt actually measured), and
    the PRODUCT lane (the operator's setting is the whole authority, and a lane token a
    receipt-authorized log can never match keeps re-entry from reinterpreting one lane as the other).

    Body verbatim from the constructor apart from the `self` -> `engine` rename and this preamble.
    """
    _gate_receipt = gate_receipt
    task, max_nodes = rt.task, rt.max_nodes
    _calibration_runtime = rt
    if engine._speculation_gate_calibration:
        # Validate the bootstrap at the library boundary.  A caller cannot obtain the waiver by
        # constructing Engine directly with arbitrary roles/settings, and no run artifact exists
        # yet when these checks execute.
        calibration_errors, expected_runtime_scope = narrow_runtime_envelope_errors(
            engine, _calibration_runtime)
        if engine.speculation_gate_receipt is not None:
            calibration_errors.append("speculation_gate_receipt must be unset")

        # The CLI has already created engine.lock.  An empty events file is also harmless; every
        # material snapshot/artifact is forbidden because copied evidence must not bootstrap a run.
        if engine.run_dir.exists():
            unexpected = sorted(
                path.name for path in engine.run_dir.iterdir()
                if path.name not in {"engine.lock", "events.jsonl"}
            )
            event_path = engine.run_dir / "events.jsonl"
            if unexpected:
                calibration_errors.append(
                    "run directory contains stale material: " + ", ".join(unexpected))
            if event_path.exists() and event_path.stat().st_size:
                calibration_errors.append("events.jsonl must be exactly empty")

        try:
            from looplab.core.hardware import effective_gpu_inventory
            gpu_inventory = _stable_effective_gpu_inventory(effective_gpu_inventory())
        except Exception:  # noqa: BLE001 — an uninventoriable box is no GPU, which the calibration gate refuses
            gpu_inventory = []
        if not gpu_inventory:
            calibration_errors.append(
                "effective CUDA_VISIBLE_DEVICES GPU inventory must be non-empty")
        if calibration_errors:
            raise ConfigRefusal(
                "speculation gate calibration profile mismatch: "
                + "; ".join(calibration_errors)
            )

        guard_calibrated_role_factory(engine, task)
        engine._speculation_gate_admitted = True  # depth=0 baseline and depth>0 treatment
        # SpeculationMixin's live enablement also requires a non-empty internal admission token.
        # This value is never serialized as a receipt digest for calibration evidence; the durable
        # authority is the explicit profile/GPU/seed envelope below.
        engine._speculation_gate_receipt_digest = SPECULATION_CALIBRATION_PROFILE_DIGEST
        engine._speculation_policy_scope = SPECULATION_POLICY_SCOPE
        engine._speculation_calibration_profile_digest = (
            SPECULATION_CALIBRATION_PROFILE_DIGEST
        )
        engine._speculation_calibration_gpu_inventory = gpu_inventory
        engine._speculation_calibration_seed = task.seed
        engine._speculation_runtime_scope_sha256 = expected_runtime_scope
        from looplab.search.speculation_quality import speculation_implementation_digest
        engine._speculation_implementation_digest = speculation_implementation_digest()
    elif engine.card_driven_selection and engine.speculation_depth > 0:
        # WHY THIS IS ADMITTED ON ANY TaskAdapter (2026-08-04 operator decision; this block used
        # to additionally require `workload_scope == "quadratic_toy"` AND `type(task) is ToyTask`,
        # so every real Dataset/Repo/Command workload was refused and the public knob only ever
        # replayed its own benchmark):
        #
        # Card speculation pre-builds the code for the experiment PREDICTED to be selected next.
        # A build whose prediction misses is DISCARDED BEFORE IT EVER RUNS — the discard is a
        # `superseded` terminal appended from `_drop_stale_speculation`, which only reaches a node
        # that is still `pending` on a fresh fold, is not in `eval_inflight`, and carries no
        # durable eval-start boundary — so no sandbox, no workdir and no GPU second was ever spent
        # on it. (The first two are IN-MEMORY facts and a resumed process has neither; the
        # boundary, `events/types.py::EV_NODE_EVAL_STARTED`, is the durable one that survives a
        # kill, and without it the refund is refused.) Its whole cost is one Developer call.
        # The toy-only fence was never about that cost. It existed because a discarded build still
        # consumed a slot of the run's NODE budget, so the same budget bought fewer real
        # experiments: measured at equal task/seed/budget with speculation the only variable, the
        # baseline evaluated 12/12 nodes while the depth-1 treatment evaluated 9/12 with three
        # `superseded` discards and finished ~2.6% worse. That 2.6% IS the `normalized_regret` the
        # calibration gate bounds — the gate protected the EXPERIMENT BUDGET, not search
        # correctness. `core/models.py::is_unevaluated_speculative_discard` now refunds
        # exactly that slot (and `_hard_node_reservation_limit` refunds the matching physical
        # reservation), so the harm the evidence measured is gone and demanding per-workload
        # evidence that "the harm is small" is ceremony.
        #
        # THE PROPERTY THIS ARGUMENT DEPENDS ON — a speculative miss is provably cheap ONLY while
        # it never consumed a real evaluation. If any future path lets a speculative build reach
        # an evaluation before its selection is confirmed, that is real GPU time and MUST NOT be
        # admitted on this reasoning. It is asserted, not hoped for, at the single dispatch funnel:
        # `engine/evaluate.py::_evaluate` -> `_assert_speculative_selection_confirmed`.
        if not engine.run_dir.name.strip():
            raise ConfigRefusal("positive Card speculation requires a non-empty run id")
        if engine._policy_name != SPECULATION_POLICY_SCOPE:
            # Not a workload fence: the speculative freshness test asks the POLICY for the
            # counterfactual next action, and `greedy` is the one policy whose counterfactual the
            # Card scorer/selector was built and measured against.
            raise ConfigRefusal(
                f"Card speculation requires policy={SPECULATION_POLICY_SCOPE!r}, "
                f"got {engine._policy_name!r} — set `-s policy={SPECULATION_POLICY_SCOPE}`, or "
                f"turn speculation off with `-s speculation_depth=0`"
            )
        from looplab.adapters.toytask import ToyTask
        _calibrated_replay = bool(engine.speculation_gate_receipt) and type(task) is ToyTask
        if engine.speculation_gate_receipt:
            # A receipt is OPTIONAL now, but it is still revalidated end-to-end whenever it is
            # supplied (schema, thresholds, self-digest, current implementation and environment
            # digests, and a full recomputation from its own raw paired run dirs) — a stale or
            # forged one is never silently honoured.
            #
            # WHAT REFUSAL MEANS depends on what the receipt is authorizing. On the calibration
            # toy it IS the authority, so a bad receipt is a hard `ValueError`. On a real
            # workload it authorizes nothing (the operator's setting is the authority), so a hard
            # raise at `Engine.__init__` would take a whole Repo/GPU run down over an attestation
            # the run does not need — and with `card_driven_selection` defaulting True that is a
            # trap, not a safety property. There the receipt is DECLINED instead: the run
            # proceeds in the product lane, and `run_started` durably records the product lane
            # token, so the log never claims an attestation that did not hold.
            #
            # ...AND A HONOURED ONE BINDS NOTHING THERE EITHER. This block used to carry the
            # receipt's identity into `run_started` on ANY workload, which meant a receipt that
            # was valid at run start pinned `speculation_implementation_digest` — the whole-source
            # digest — on a real Repo/GPU run. The next `pip install -U` (or any source edit)
            # revoked the receipt, the resume fell into the product lane with an empty digest, and
            # the re-entry equality check refused the run FOREVER; dropping the receipt from the
            # resume command did not help, because that lands in the same product lane. That is
            # precisely the trap the product lane below exists to avoid, so a receipt supplied on
            # a workload it did not measure is now inert in BOTH directions: it never authorizes
            # (it already did not) and it never pins.
            from looplab.search.speculation_quality import (
                speculation_task_profile_digest,
                validated_speculation_gate_receipt,
            )
            # STAYS `validated_speculation_gate_receipt`, deliberately. It is a monkeypatch SEAM
            # roughly a dozen tests reach through, and calling the reason-returning sibling here
            # instead resolves the real function while every `monkeypatch.setattr(quality,
            # "validated_speculation_gate_receipt", …)` silently stops reaching the engine — the
            # narrowing CLAUDE.md names for the `fold` seam, one module over. The diagnosis is taken
            # on the REFUSAL path only (`_receipt_refusal_detail`), which is already fatal, so the
            # admitting path pays for exactly one derivation as before (doc 25 SE-01).
            _gate_receipt = validated_speculation_gate_receipt(
                engine.speculation_gate_receipt,
            )
            if (
                _gate_receipt is None
                or _gate_receipt.get("require_gpu") is not True
                or not _gate_receipt.get("gpu_inventory")
                or _gate_receipt.get("policy_scope") != SPECULATION_POLICY_SCOPE
                or _gate_receipt.get("calibration_profile_digest")
                != SPECULATION_CALIBRATION_PROFILE_DIGEST
                or _gate_receipt.get("workload_scope") != "quadratic_toy"
                or not isinstance(_gate_receipt.get("implementation_digest"), str)
                or not _gate_receipt.get("implementation_digest")
            ):
                if _calibrated_replay:
                    raise ConfigRefusal(
                        "speculation_gate_receipt is stale, invalid, non-GPU, "
                        "policy-mismatched, or does not pass the current "
                        "scorer/search-quality gates"
                        + _receipt_refusal_detail(
                            engine.speculation_gate_receipt, _gate_receipt)
                    )
                _gate_receipt = None
        if _calibrated_replay and _gate_receipt is not None:
            # CALIBRATED REPLAY LANE — the ONLY lane a receipt binds anything to, because it is
            # the only one whose workload the receipt actually measured. Re-running the
            # benchmark's own workload under its own receipt gets the full narrow envelope: the
            # exact Settings profile, roles, policy, sandbox, treatment depth, node budget and
            # runtime-scope digest the evidence was measured under. Weakening this lane would let
            # a receipt earned on one measured runtime authorize a different one under the same
            # name — and this lane's own workload is the toy the receipt measured, so pinning the
            # source digest here costs nothing: a re-measurement is minutes, not a lost GPU run.
            runtime_errors, expected_runtime_scope = narrow_runtime_envelope_errors(
                engine, _calibration_runtime)
            if (
                runtime_errors
                or type(_gate_receipt.get("admitted_depth")) is not int
                or _gate_receipt.get("admitted_depth") != engine.speculation_depth
                or type(_gate_receipt.get("admitted_max_nodes")) is not int
                or _gate_receipt.get("admitted_max_nodes") != max_nodes
                or _gate_receipt.get("runtime_scope_sha256") != expected_runtime_scope
                # The receipt is scoped to the shipped quadratic adapter, not merely to an
                # arbitrary TaskAdapter/subclass that can spoof the same model_dump while
                # executing a different workload.
                or _gate_receipt.get("task_profile_sha256")
                != speculation_task_profile_digest(task)
            ):
                # `runtime_errors` is the per-rule truth table `narrow_runtime_envelope_errors`
                # already computed and this refusal used to throw away, and the four receipt-vs-run
                # comparisons above are facts this frame is holding both sides of. Without them an
                # operator whose `max_nodes` was off by one read the same sentence as one whose
                # receipt had expired. Reported, never consulted: the condition above is unchanged,
                # and every term below is already in hand — nothing is re-derived to say it.
                # The condition above is an `or` chain, so a truthy `runtime_errors` SHORT-CIRCUITS
                # before `speculation_task_profile_digest(task)` — which RAISES on a non-canonical
                # ToyTask, and that raise is a different, more specific refusal the envelope is
                # meant to surface. Re-deriving it eagerly here to describe the failure would
                # replace that refusal with this one, so the term is resolved defensively and simply
                # drops out of the description when it cannot be computed.
                try:
                    _task_profile = speculation_task_profile_digest(task)
                except Exception:  # noqa: BLE001 — resolved defensively; the term drops out of the description when it cannot be computed
                    _task_profile = _gate_receipt.get("task_profile_sha256")
                mismatches = [
                    text for ok, text in (
                        (type(_gate_receipt.get("admitted_depth")) is int
                         and _gate_receipt.get("admitted_depth") == engine.speculation_depth,
                         f"receipt admitted depth {_gate_receipt.get('admitted_depth')!r}, "
                         f"this run requests {engine.speculation_depth!r}"),
                        (type(_gate_receipt.get("admitted_max_nodes")) is int
                         and _gate_receipt.get("admitted_max_nodes") == max_nodes,
                         f"receipt admitted max_nodes "
                         f"{_gate_receipt.get('admitted_max_nodes')!r}, "
                         f"this run requests {max_nodes!r}"),
                        (_gate_receipt.get("runtime_scope_sha256") == expected_runtime_scope,
                         "receipt runtime scope digest is not this runtime's"),
                        (_gate_receipt.get("task_profile_sha256") == _task_profile,
                         "receipt task profile digest is not this task's"),
                    ) if not ok
                ]
                detail = [*runtime_errors, *mismatches]
                raise ConfigRefusal(
                    "speculation_gate_receipt is stale, invalid, non-GPU, "
                    "policy/depth-mismatched, runtime-scope/max-nodes-mismatched, or does "
                    "not pass the current scorer/search-quality gates"
                    + (" — " + "; ".join(detail) if detail else "")
                )
            guard_calibrated_role_factory(engine, task)
            engine._speculation_runtime_scope_sha256 = expected_runtime_scope
            engine._speculation_gate_receipt_digest = _gate_receipt["self_digest"]
            engine._speculation_implementation_digest = _gate_receipt["implementation_digest"]
        else:
            from looplab.search.speculation_quality import (
                speculation_product_authority_digests,
            )
            # PRODUCT LANE — the operator's setting is the whole authority. What stays pinned is
            # the run's own search TREATMENT (selector, depth, policy scope) plus a lane token a
            # receipt-authorized log can never match, so re-entry cannot reinterpret one lane's
            # speculative prefix as the other's. A receipt supplied on a workload it did not
            # measure lands HERE, valid or not: it authorized nothing, so it pins nothing, and the
            # run stays resumable with or without it on the command line.
            #
            # It deliberately does NOT pin `speculation_implementation_digest`. That digest hashes
            # every shipped Python file, so any source edit — a comment, a `pip install -U` —
            # would make a half-finished Repo/GPU run permanently unresumable. It is an EVIDENCE
            # identity for the lanes that claim evidence; this lane claims none, and the
            # speculative prefix it writes is folded by the ordinary forward-compatible rules
            # (invariant #5) like every other part of the log.
            engine._speculation_product_lane = True
            # Head = the token this run MINTS; the tail is superseded spellings of the same
            # identity that re-entry still accepts, so a schema bump cannot strand a run that is
            # already underway (see `speculation_product_authority_digests`).
            _product_tokens = speculation_product_authority_digests(
                policy_scope=SPECULATION_POLICY_SCOPE,
                task_kind=str(getattr(task, "kind", "") or ""),
            )
            engine._speculation_gate_receipt_digest = _product_tokens[0]
            engine._speculation_product_authority_tokens = frozenset(_product_tokens)
        engine._speculation_policy_scope = SPECULATION_POLICY_SCOPE
        engine._speculation_gate_admitted = True
