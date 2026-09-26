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
verdict `seed_verdict` returns: `same`, `different` (with the sentence naming which facet differs) or
`unknown` — tri-state, `unknown` never a guess. The engine serves it as it serves every inject,
before its first creation turn, so the seed is evaluated under THIS run's protocol: the source's
metric rides the receipt as provenance and is never this run's number. An existing run directory is
never seeded and nothing is resolved for it: the setting is recorded in `config.snapshot.json` — as
the canonical `<run dir>#<node>` it resolved to (`SeedSource.canonical_spec`), so a Replay of the run
seeds the same node — and is inert on every later `run` or `resume` of it.

A CONTROL INTENT, NOT A DOMAIN EVENT. The CLI appends only what the UI may (invariant 1), and an
inject appended before the engine starts is the tested pattern
(`tests/test_control.py::test_inject_node_creates_and_evaluates`). The resume idempotency is the
inject queue's own: `inject_done` advances the cursor, so a crash between the append and the build
serves it on the next resume and never twice. The web start record accepts that one row ahead of the
engine's identity anchor (`serve/start_record.py::has_first_run_started`).

REFUSED BEFORE ANYTHING IS CREATED — a `ConfigRefusal` (the operator's own input: one line at exit 2),
and on the web start route the same refusal as a 422 from `/api/validate` (`serve/launch.py::
preflight_start`): a source that is not a run, or is this run; a source log with a mid-file
corruption, whose fold would silently read a prefix — and so rank a different champion — where
`looplab resume` refuses it; no champion, or a champion taken across the scale (the source ranks the
other direction: name the node); a missing, tombstoned or aborted node; a node with nothing to
import; a file name that could leave a node workspace (`events/node_import.py::
portable_relative_name`, the server's own rule); and whatever the engine's inject validation refuses
(`engine/node_build.py::_prepare_injected_node`, asked here rather than copied, so a `debug` champion
is not reported as seeded and then dropped by an `inject_failed` row). Critic 2026-09-26, each
driven.

THE VERDICT. The evaluation contract (`engine/eval_contract.py`) compares what the two TASKS declare
and leaves the run-level facts out on purpose — its other readers gate on them. The seed has no such
gate, so `seed_verdict` compares three of them itself: the DIRECTION (a different one is `different`,
provably: the seed's standing there is the reverse of its standing here), the declared `eval_env` and
the `holdout_fraction` (a difference turns `same` into `unknown` and is named — it may, not must,
change the scale).

SPEC GRAMMAR. `PATH` or `PATH#NODE`. PATH is a run directory — absolute, relative to the working
directory, or a sibling run's id under the new run's own runs root; the web start route admits only a
run of ITS runs root (`confine_to`). A trailing `#<integer>` names the node when the text before it
names a run, else the whole text is the path, so a run whose name holds a `#` stays addressable.
Without `#NODE` the source's CHAMPION (`RunState.best()`) is taken.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from looplab.core.errors import ConfigRefusal
from looplab.core.pathsafe import validate_run_child
from looplab.engine.eval_contract import (comparable, contract_for_run_dir, contract_from_task,
                                          contract_notice)
from looplab.engine.shared import engine_fold as fold
from looplab.events.eventstore import EventStore
from looplab.events.node_import import node_import_payload, portable_relative_name

_NODE_SUFFIX = re.compile(r"\s*(-?\d+)\s*")
_RANKS = {"max": "maximizes", "min": "minimizes"}


@dataclass(frozen=True)
class SeedSource:
    """A resolved, validated seed: the source run directory — RESOLVED, its identity rather than the
    path as typed — the node to import, and the run-level facts `seed_verdict` compares."""
    run_dir: Path
    node_id: int
    payload: dict
    named: bool = False                 # `#<node>` was given; else the source's champion was taken
    direction: str = ""
    eval_env: dict = field(default_factory=dict)
    holdout_fraction: Optional[float] = None

    @property
    def canonical_spec(self) -> str:
        """The spec this seed resolved to, which resolves to the same node again."""
        return f"{self.run_dir}#{self.node_id}"


def _locate(path_part: str, out: Path, confine_to: Optional[Path]) -> Optional[Path]:
    """The resolved run directory `path_part` names, or None."""
    if not path_part:
        return None
    if confine_to is not None:
        raw = Path(path_part)
        child = validate_run_child(confine_to, raw if raw.is_absolute() else path_part,
                                   must_exist=True)
        if child.defect is None and child.path is not None and (
                child.path / "events.jsonl").is_file():
            return child.path
        return None
    raw = Path(path_part).expanduser()
    for candidate in [raw] if raw.is_absolute() else [raw, Path(out).parent / raw]:
        if (candidate / "events.jsonl").is_file():
            return candidate.resolve()
    return None


def _not_a_run(text: str, out: Path, confine_to: Optional[Path]) -> str:
    if confine_to is not None:
        # No host path is echoed on the web route: the refusal says what was asked, not what exists.
        return f"seed_from_run: {text!r} is not a run under this server's runs root"
    raw = Path(text).expanduser()
    looked = [raw] if raw.is_absolute() else [raw, Path(out).parent / raw]
    return (f"seed_from_run: no run directory at {text!r} (looked for an events.jsonl at "
            + " and ".join(repr(str(c)) for c in looked) + ")")


def _engine_refusal(payload: dict) -> Optional[str]:
    """What the engine's own inject validation refuses this PARENTLESS request with, or None.

    `node_build.py::_prepare_injected_node` is pure — no provider, Developer, filesystem or log
    effect — and for a request with no parents it reads nothing off its `self` but the two static
    rules below. Asking IT, not a copy of its rules, is what keeps the seed's refusals the engine's."""
    from looplab.core.models import RunState
    from looplab.engine.card_reservation import CardReservationMixin
    from looplab.engine.node_build import NodeBuildMixin

    class _ParentlessInject:
        _build_parent_snapshot = staticmethod(CardReservationMixin._build_parent_snapshot)
        _implementation_ref = staticmethod(CardReservationMixin._implementation_ref)

    try:
        NodeBuildMixin._prepare_injected_node(
            _ParentlessInject(), RunState(), {**payload, "parent_id": None})
    except ValueError as exc:
        return str(exc)
    return None


def check_seed_direction(seed: SeedSource, direction: Optional[str]) -> None:
    """Refuse a CHAMPION taken across the scale: the source ranks the other direction, so its best is
    the other end of this run's scale. A named node is the operator's own pick — `seed_verdict` then
    says `different` and why. `looplab run` asks this again once Genesis has settled the direction."""
    if seed.named or not direction or not seed.direction or direction == seed.direction:
        return
    raise ConfigRefusal(
        f"seed_from_run: run {seed.run_dir.name} {_RANKS.get(seed.direction, seed.direction)} its "
        f"metric and this run {_RANKS.get(direction, direction)} it, so its champion is the best of "
        "the other end of the scale; name the node to seed with `#<id>`")


def resolve_seed(spec: str, out: Path, *, direction: Optional[str] = None,
                 confine_to: Optional[Path] = None) -> SeedSource:
    """Resolve and validate `spec` against the new run directory `out`, reading the source's own log.
    `direction` is the NEW run's (the champion pick refuses across it); `confine_to` admits only a
    run of that runs root (the web start route). Raises `ConfigRefusal`; never touches `out`."""
    text = str(spec or "").strip()
    head, sep, tail = text.rpartition("#")
    head = head.strip()
    node_match = _NODE_SUFFIX.fullmatch(tail) if sep else None
    if not text or (node_match and not head):
        raise ConfigRefusal("seed_from_run: empty — name a run directory, optionally `#<node>`")
    source = _locate(head, out, confine_to) if node_match else None
    named = int(node_match.group(1)) if source is not None else None
    if source is None:
        source = _locate(text, out, confine_to)
    if source is None:
        if sep and not node_match and _locate(head, out, confine_to) is not None:
            raise ConfigRefusal(f"seed_from_run: node {tail.strip()!r} is not an integer id")
        raise ConfigRefusal(_not_a_run(text, out, confine_to))
    if source == Path(out).resolve():
        raise ConfigRefusal(f"seed_from_run: {text!r} is this run's own directory")
    name = source.name
    store = EventStore(source / "events.jsonl")
    div = store.divergence
    if div:
        raise ConfigRefusal(
            f"seed_from_run: run {name}'s events.jsonl is corrupted at line {div['corrupt_line']} — "
            f"{div['dropped_lines']} later record(s) would be dropped, so what the run ranks cannot "
            f"be read; run `looplab repair-log` on it first")
    state = fold(store.read_all())
    if named is not None:
        node = state.nodes.get(named)
        if node is None:
            raise ConfigRefusal(f"seed_from_run: run {name} has no node #{named}")
        node_id = named
    else:
        check_seed_direction(SeedSource(run_dir=source, node_id=-1, payload={},
                                        direction=str(state.direction or "")), direction)
        node = state.best()
        if node is None:
            raise ConfigRefusal(
                f"seed_from_run: run {name} has no champion to seed from; name a node with `#<id>`")
        node_id = node.id
    if node.tombstoned or node_id in state.aborted_nodes:
        raise ConfigRefusal(f"seed_from_run: node #{node_id} of run {name} is "
                            + ("tombstoned" if node.tombstoned else "aborted"))
    payload = node_import_payload(state, node_id, name)
    if not (payload["code"] or payload["files"] or payload["deleted"]):
        raise ConfigRefusal(f"seed_from_run: node #{node_id} of run {name} carries no code, no "
                            "files and no deletions — there is nothing to import")
    files: dict = {}
    deleted: list = []
    unsafe: list = []
    for fname, content in payload["files"].items():
        portable, _defect = portable_relative_name(fname)
        if portable is None:
            unsafe.append(str(fname))
        else:
            files[portable] = content
    for fname in payload["deleted"]:
        portable, _defect = portable_relative_name(fname)
        if portable is None:
            unsafe.append(str(fname))
        else:
            deleted.append(portable)
    if unsafe:
        raise ConfigRefusal(f"seed_from_run: node #{node_id} of run {name} names file(s) "
                            f"outside a node workspace: {', '.join(sorted(unsafe)[:3])}")
    payload = {**payload, "files": files, "deleted": deleted}
    refusal = _engine_refusal(payload)
    if refusal:
        raise ConfigRefusal(
            f"seed_from_run: node #{node_id} of run {name} cannot be injected: {refusal}")
    return SeedSource(run_dir=source, node_id=node_id, payload=payload, named=named is not None,
                      direction=str(state.direction or ""),
                      eval_env=dict(getattr(state, "eval_env", None) or {}),
                      holdout_fraction=getattr(state, "holdout_fraction", None))


def seed_verdict(seed: SeedSource, task: dict, *, direction: Optional[str], eval_env,
                 holdout_fraction) -> tuple[str, str]:
    """`(verdict, sentence)` for seeding a run whose task is `task` (its canonical dict — what
    `task.snapshot.json` records) and whose run-level facts are the keywords. See the module
    docstring: the task contract first, then the three run-level facts outside it."""
    this, other = contract_from_task(task), contract_for_run_dir(seed.run_dir)
    verdict = {True: "same", False: "different", None: "unknown"}[comparable(this, other)]
    notes = [contract_notice(this, other, other_run_id=seed.run_dir.name)]
    if direction and seed.direction and direction != seed.direction:
        verdict = "different"
        notes.append(f"DIFFERENT DIRECTION: run {seed.run_dir.name} "
                     f"{_RANKS.get(seed.direction, seed.direction)} its metric and this run "
                     f"{_RANKS.get(direction, direction)} it, so the seed's standing there is the "
                     "reverse of its standing here.")
    facets = []
    mine, theirs = dict(eval_env or {}), dict(seed.eval_env or {})
    if mine != theirs:
        keys = sorted(str(k) for k in set(mine) | set(theirs) if mine.get(k) != theirs.get(k))
        facets.append("eval_env (" + ", ".join(keys[:5]) + ")")
    if (seed.holdout_fraction is not None and holdout_fraction is not None
            and float(seed.holdout_fraction) != float(holdout_fraction)):
        facets.append(f"holdout_fraction ({float(seed.holdout_fraction):g} there, "
                      f"{float(holdout_fraction):g} here)")
    if facets:
        if verdict == "same":
            verdict = "unknown"
        notes.append("The runs also differ in " + "; ".join(facets) + " — outside the evaluation "
                     "contract, so the two numbers may not share a scale.")
    return verdict, " ".join(note for note in notes if note)


def seed_intent(seed: SeedSource, out: Path, task: dict, *, direction: Optional[str], eval_env,
                holdout_fraction) -> tuple[dict, str, str]:
    """`(inject_node payload, verdict, sentence)` for a fresh run at `out` whose task is `task`.

    The receipt carries the RESOLVED source directory (`run_dir`) beside the source's name; `run_id`
    — which the UI links as a sibling run and the portfolio map draws as a `seeded_from` edge — is
    kept only when the source IS a sibling of `out`, so a run seeded from outside its runs root is
    never shown as seeded from an unrelated run that happens to share the name."""
    verdict, note = seed_verdict(seed, task, direction=direction, eval_env=eval_env,
                                 holdout_fraction=holdout_fraction)
    payload = dict(seed.payload)
    payload["parent_id"] = None
    origin = {**payload["origin"], "seed_from_run": True, "run_dir": str(seed.run_dir),
              "eval_contract": verdict, **({"eval_contract_note": note} if note else {})}
    if seed.run_dir.parent != Path(out).resolve().parent:
        origin.pop("run_id", None)
    payload["origin"] = origin
    return payload, verdict, note


def seed_summary(seed: SeedSource, verdict: str, note: str) -> str:
    """The one line `looplab run` prints when it seeds (and the web preflight shows)."""
    metric = seed.payload["origin"].get("metric")
    return (f"seeded from run {seed.run_dir.name} #{seed.node_id}"
            + (f" (its metric there: {metric})" if metric is not None else "")
            + f" — evaluation contract: {verdict}" + (f". {note}" if note else "")
            + ". It is evaluated here under this run's own protocol.")


def seed_ignored_note(spec: Optional[str]) -> str:
    return (f"seed_from_run={spec!r} ignored: this run directory already has events, and only a "
            "fresh run is seeded")
