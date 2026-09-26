"""SEED A NEW RUN FROM A PRIOR ONE (doc 67 67.2, `Settings.seed_from_run`): the prior run's champion
— or a named node — becomes the new run's first experiment, with the evaluation contract beside it.

WHY. Rounds were chained by hand: `NEXT_RUN.md` walks an operator through copying the previous run's
task snapshot, and every `e5small_v12/13/14.json` round started its search from scratch — node 0's
median was 27 against a prior champion of 169 (doc 60 item 2.3). The server could already import a
sibling's experiment into a LIVE run (`serve/control_validation.py::_import_cross_run_source`,
HTTP/UI only), but no launch form made a prior champion the ROOT of a new run, and nothing said
whether the imported number could even be read on the new run's scale.

WHAT IT DOES. At launch, before the engine starts, `looplab run` (which the web start route spawns
too) resolves the setting and, on a FRESH run directory only, appends one operator `inject_node`
intent: the node's snapshot exactly as the server's import builds it
(`events/node_import.py::node_import_payload`), its `origin` receipt marked `seed_from_run`, plus the
EVALUATION-CONTRACT verdict (`engine/eval_contract.py`): `same`, `different` (with the sentence
naming which facet differs) or `unknown` — tri-state, `unknown` never a guess. The engine serves it
as it serves every inject, before its first creation turn, so the seed is evaluated under THIS run's
protocol: the source's metric rides the receipt as provenance and is never this run's number. An
existing run directory is never seeded (the setting is recorded in `config.snapshot.json` and is
inert on every later `run` or `resume` of it).

A CONTROL INTENT, NOT A DOMAIN EVENT. The CLI appends only what the UI may (invariant 1), and an
inject appended before the engine starts is the tested pattern
(`tests/test_control.py::test_inject_node_creates_and_evaluates`). The resume idempotency is the
inject queue's own: `inject_done` advances the cursor, so a crash between the append and the build
serves it on the next resume and never twice.

SPEC GRAMMAR. `PATH` or `PATH#NODE`. PATH is a run directory — absolute, relative to the working
directory, or a sibling run's id under the new run's own runs root (the web UI's run ids). Without
`#NODE` the source's CHAMPION (`RunState.best()`) is taken. A refusal is a `ConfigRefusal`: the
operator's own input, one line at exit 2, before anything is created.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from looplab.core.errors import ConfigRefusal
from looplab.engine.eval_contract import (comparable, contract_for_run_dir, contract_notice)
from looplab.events.eventstore import EventStore
from looplab.events.node_import import node_import_payload
from looplab.events.replay import fold


@dataclass(frozen=True)
class SeedSource:
    """A resolved, validated seed: the source run directory and the node to import."""
    run_dir: Path
    node_id: int
    payload: dict


def _source_dir(path_part: str, out: Path) -> Path:
    raw = Path(path_part).expanduser()
    candidates = [raw] if raw.is_absolute() else [raw, Path(out).parent / raw]
    for candidate in candidates:
        if (candidate / "events.jsonl").is_file():
            return candidate
    raise ConfigRefusal(
        f"seed_from_run: no run directory at {path_part!r} (looked for an events.jsonl at "
        + " and ".join(repr(str(c)) for c in candidates) + ")")


def _portable_relative(name) -> bool:
    """A file the import may carry: a relative, portable name with no `..` — the engine's own
    materializer contains every write again (`engine/workspace.py`), this refuses early and says so."""
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        return False
    parts = PurePosixPath(name).parts
    return not (PurePosixPath(name).is_absolute() or ":" in name
                or any(part in ("", ".", "..") for part in parts))


def resolve_seed(spec: str, out: Path) -> SeedSource:
    """Resolve and validate `spec` against the new run directory `out`, reading the source's own log.
    Raises `ConfigRefusal` naming what is wrong; never touches `out`."""
    text = str(spec or "").strip()
    path_part, _sep, node_part = text.partition("#")
    if not path_part:
        raise ConfigRefusal("seed_from_run: empty — name a run directory, optionally `#<node>`")
    source = _source_dir(path_part, out)
    if source.resolve() == Path(out).resolve():
        raise ConfigRefusal(f"seed_from_run: {path_part!r} is this run's own directory")
    state = fold(EventStore(source / "events.jsonl").read_all())
    if node_part:
        try:
            node_id = int(node_part)
        except ValueError:
            raise ConfigRefusal(f"seed_from_run: node {node_part!r} is not an integer id") from None
        node = state.nodes.get(node_id)
        if node is None:
            raise ConfigRefusal(f"seed_from_run: run {source.name} has no node #{node_id}")
    else:
        node = state.best()
        if node is None:
            raise ConfigRefusal(
                f"seed_from_run: run {source.name} has no champion to seed from; name a node with "
                "`#<id>`")
        node_id = node.id
    if node.tombstoned or node_id in state.aborted_nodes:
        raise ConfigRefusal(f"seed_from_run: node #{node_id} of run {source.name} is "
                            + ("tombstoned" if node.tombstoned else "aborted"))
    payload = node_import_payload(state, node_id, source.name)
    if not (payload["code"] or payload["files"]):
        raise ConfigRefusal(f"seed_from_run: node #{node_id} of run {source.name} carries no code "
                            "and no files — there is nothing to import")
    unsafe = sorted(str(name) for name in [*payload["files"], *payload["deleted"]]
                    if not _portable_relative(name))
    if unsafe:
        raise ConfigRefusal(f"seed_from_run: node #{node_id} of run {source.name} names file(s) "
                            f"outside a node workspace: {', '.join(unsafe[:3])}")
    return SeedSource(run_dir=source, node_id=node_id, payload=payload)


def seed_intent(seed: SeedSource, out: Path) -> tuple[dict, str, str]:
    """`(inject_node payload, contract verdict, contract sentence)` for a fresh run at `out`, whose
    `task.snapshot.json` is already published: the receipt compares the two runs' OWN recorded
    declarations, the one read every later reader of either run makes."""
    this, other = contract_for_run_dir(out), contract_for_run_dir(seed.run_dir)
    verdict = {True: "same", False: "different", None: "unknown"}[comparable(this, other)]
    note = contract_notice(this, other, other_run_id=seed.run_dir.name)
    payload = dict(seed.payload)
    payload["parent_id"] = None
    payload["origin"] = {**payload["origin"], "seed_from_run": True, "eval_contract": verdict,
                         **({"eval_contract_note": note} if note else {})}
    return payload, verdict, note


def seed_summary(seed: SeedSource, verdict: str, note: str) -> str:
    """The one line `looplab run` prints when it seeds."""
    metric = seed.payload["origin"].get("metric")
    return (f"seeded from run {seed.run_dir.name} #{seed.node_id}"
            + (f" (its metric there: {metric})" if metric is not None else "")
            + f" — evaluation contract: {verdict}" + (f". {note}" if note else "")
            + ". It is evaluated here under this run's own protocol.")


def seed_ignored_note(spec: Optional[str]) -> str:
    return (f"seed_from_run={spec!r} ignored: this run directory already has events, and only a "
            "fresh run is seeded")
