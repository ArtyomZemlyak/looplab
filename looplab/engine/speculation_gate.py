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

from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
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
