"""SEED A NEW RUN FROM A PRIOR ONE (doc 67 67.2, `Settings.seed_from_run`): the prior run's champion
— or a named node — becomes the new run's first experiment, with the evaluation contract beside it.

WHY. Rounds were chained by hand: `benchmarks/NEXT_RUN.md` walks an operator through copying the previous run's
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
the canonical `<run dir>#<node>` it resolved to (`SeedSource.canonical_spec`), the spec an operator
reads and may clear — and is inert on every later `run` or `resume` of it. A Replay does not resolve
that spec again: it re-seeds from the log's own first row, frozen before the archive
(`serve/reset_route.py::_freeze_birth_seed`, `frozen_seed_payload`).

A CONTROL INTENT, NOT A DOMAIN EVENT. The CLI appends only what the UI may (invariant 1), and an
inject appended before the engine starts is the tested pattern
(`tests/test_control.py::test_inject_node_creates_and_evaluates`). The resume idempotency is the
inject queue's own: `inject_done` advances the cursor, so a crash between the append and the build
serves it on the next resume and never twice. The web start record accepts that one row ahead of the
engine's identity anchor (`serve/start_record.py::has_first_run_started`).

REFUSED BEFORE ANYTHING IS CREATED — a `ConfigRefusal` (the operator's own input: one line at exit 2),
and on the web start route the same refusal as an `invalid_seed` 422 from `/api/start` — which
`/api/validate` answers as `{ready: false, status: 422}` (`serve/launch.py::preflight_start`): a
source that is not a run, or is this run; a source log with a mid-file
corruption, whose fold would silently read a prefix — and so rank a different champion — where
`looplab resume` refuses it; no champion, or a champion taken across the scale (the source ranks the
other direction: name the node); a missing, tombstoned or aborted node; a node with nothing to
import; a file name that could leave a node workspace (`events/node_import.py::
portable_relative_name`, the server's own rule); and whatever the engine's inject validation refuses
(`engine/node_build.py::_prepare_injected_node`, asked here rather than copied, so a `debug` champion
is not reported as seeded and then dropped by an `inject_failed` row). Critic 2026-09-26, each
driven.

THE VERDICT. `same` is a claim, so it is earned, never defaulted. Both TASKS are read as their
adapters dump them — the CLI records a task as written and the web route as dumped, so a raw
comparison read one evaluation as two. The evaluation contract (`engine/eval_contract.py`) decides
`different`, naming the facet; it is a PARTIAL key (reader, command, declared paths), so `same`
also needs the two declarations to agree on every field but the goal, the task id and the direction
— stages, the eval `env`, a timeout — or the verdict is `unknown` and names them (critic
2026-09-26). The contract leaves run-level facts out on purpose — its other readers gate on them.
The seed has no such gate, so `seed_verdict` compares three of them itself: the DIRECTION (a
different one is `different`, provably: the seed's standing there is the reverse of its standing
here), the declared `eval_env` and the `holdout_fraction` (a difference, or a source that never
recorded one, turns `same` into `unknown` and is named — it may, not must, change the scale).

SPEC GRAMMAR. `PATH` or `PATH#NODE`. PATH is a run directory — absolute, relative to the working
directory, or a sibling run's id under the new run's own runs root. A server admits only a run of
ITS runs root, by its own run-directory rule (`locate`, `serve/launch.py::server_seed_locator`). A
trailing `#<integer>` names the node when the
text before it names a run, else the whole text is the path, so a run whose name holds a `#` stays
addressable. Without `#NODE` the source's CHAMPION (`RunState.best()`) is taken. Surrounding
whitespace is not part of a spec, and a blank one is off (`core/config.py::Settings`).

A SEED IS A FACT OF BIRTH. `birth_seed_row` reads it off the run's own first row, and a later
`looplab run` of the directory records that and nothing else. A Replay re-seeds from that ROW, frozen
before anything is archived (`serve/reset_route.py::_freeze_birth_seed`) and appended verbatim by its
child (`frozen_seed_payload`) — never re-resolved by path, which re-seeded whatever the path held by
then: a deleted source stranded the run, a Replayed one handed back another experiment under the
same node id (critic 2026-09-26). A per-run config edit may clear the seed, so a Replay starts
unseeded, never change it (`serve/routers/runs.py::_put_run_config_locked`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from looplab.core.errors import ConfigRefusal
from looplab.engine.eval_contract import (TASK_SNAPSHOT, comparable, contract_from_task,
                                          contract_notice)
from looplab.engine.shared import engine_fold as fold
from looplab.events.eventstore import EventStore
from looplab.events.node_import import (NAME_ESCAPES, NAME_NOT_TEXT, node_import_payload,
                                         portable_relative_name)
from looplab.events.types import EV_INJECT_NODE

_NODE_SUFFIX = re.compile(r"\s*(-?\d+)\s*")
_RANKS = {"max": "maximizes", "min": "minimizes"}
# The task fields that cannot change what a number MEANS, so two declarations differing only there
# still earn `same`: the goal prose, the operational task id (`engine/eval_contract.py::EvalContract`
# argues both) and the direction, which `seed_verdict` judges in its own clause. Everything else a
# task declares is compared — a field added later is compared by default, so a new field can only
# ever turn `same` into `unknown`, never the reverse.
_NOT_EVALUATION = frozenset({"goal", "id", "direction"})
_NAME_DEFECTS = {NAME_ESCAPES: "outside a node workspace",
                 NAME_NOT_TEXT: "that are not a plain file name (empty, over 512 characters or "
                                "holding a control character)"}


@dataclass(frozen=True)
class SeedSource:
    """A resolved, validated seed: the source run directory — RESOLVED, its identity rather than the
    path as typed — the node to import, and the run-level facts `seed_verdict` compares."""
    run_dir: Path
    node_id: int
    payload: dict
    named: bool = False                 # `#<node>` was given; else the source's champion was taken
    direction: str = ""
    # None = the source never RECORDED one (a log older than the fact), which `seed_verdict` reads
    # as unknown — never as agreement with this run's value.
    eval_env: Optional[dict] = field(default_factory=dict)
    holdout_fraction: Optional[float] = None

    @property
    def canonical_spec(self) -> str:
        """The spec this seed resolved to, which resolves to the same node again."""
        return f"{self.run_dir}#{self.node_id}"


# A server's own run-directory rule (`serve/launch.py::server_seed_locator`): the resolved run a
# path part names under ITS runs root, or None. The engine never imports `serve`, so it is handed in.
SeedLocator = Callable[[str], Optional[Path]]


def _candidates(path_part: str, out: Path) -> list[Path]:
    raw = Path(path_part).expanduser()
    return [raw] if raw.is_absolute() else [raw, Path(out).parent / raw]


def _locate(path_part: str, out: Path, locate: Optional[SeedLocator]) -> Optional[Path]:
    """The resolved run directory `path_part` names, or None — also for a name the filesystem
    cannot take (a NUL, an over-long component, a `~user` with no home), which is not a run rather
    than a crash (critic 2026-09-26: a NUL answered the web route with a 500, a long component the
    CLI with a trace)."""
    if not path_part:
        return None
    try:
        if locate is not None:
            return locate(path_part)
        for candidate in _candidates(path_part, out):
            if (candidate / "events.jsonl").is_file():
                return candidate.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    return None


def _not_a_run(text: str, head: Optional[str], out: Path, locate: Optional[SeedLocator]) -> str:
    """Refusal for a spec that names no run. `head` is the path part of a `PATH#<node>` spec, which
    was looked up first — named too, so the operator sees every spelling that was tried."""
    if locate is not None:
        # No host path is echoed on the web route: the refusal says what was asked, not what exists.
        return f"seed_from_run: {text!r} is not a run under this server's runs root"
    try:
        looked = [c for part in ([head] if head else []) + [text] for c in _candidates(part, out)]
    except (OSError, ValueError, RuntimeError):
        looked = []
    return (f"seed_from_run: no run directory at {text!r}"
            + (f" nor at {head!r}" if head else "")
            + (" (looked for an events.jsonl at " + " and ".join(repr(str(c)) for c in looked) + ")"
               if looked else ""))


def _engine_refusal(payload: dict, *, external_harness: bool = False,
                    repo_spec: Optional[dict] = None) -> Optional[str]:
    """What the engine's own inject validation refuses this PARENTLESS request with, or None.

    `node_build.py::_prepare_injected_node` is pure — no provider, Developer, filesystem or log
    effect — and for a request with no parents it reads nothing off its `self` but the rules below.
    Asking IT, not a copy of its rules, is what keeps the seed's refusals the engine's.

    `external_harness` / `repo_spec` are the NEW run's, as its engine will hold them: the engine's
    external branch reads both first — the "needs a Developer" rule (a seed carries its own
    ready-made artefact, so it has nothing to refuse there) and the candidate-surface rule over the
    run's own repository. `resolve_seed` asks with the defaults, which are the internal engine's;
    `check_seed_surface` asks again with the run's real mode once the task is known (critic
    2026-09-29: a stand-in pinned to internal admitted a seed an external engine then refused)."""
    from looplab.core.models import RunState
    from looplab.engine.card_reservation import CardReservationMixin
    from looplab.engine.node_build import NodeBuildMixin

    class _ParentlessInject:
        _build_parent_snapshot = staticmethod(CardReservationMixin._build_parent_snapshot)
        _implementation_ref = staticmethod(CardReservationMixin._implementation_ref)

    stand_in = _ParentlessInject()
    stand_in.external_harness = bool(external_harness)
    stand_in._repo_spec = repo_spec if repo_spec is not None else {}
    try:
        NodeBuildMixin._prepare_injected_node(
            stand_in, RunState(), {**payload, "parent_id": None})
    except ValueError as exc:
        return str(exc)
    return None


def check_seed_surface(seed: "SeedSource", task, *, external_harness: bool) -> None:
    """Refuse, at LAUNCH, a seed the new run's EXTERNAL engine would refuse at its first inject: a
    file its repository's edit surface does not allow, or a file overlay on a task with no
    repository. An internal run needs nothing here (`resolve_seed` already asked its rules).

    At launch and never in `Settings`: the pair is legal — a code-only seed of a script task seeds
    an external run fine (critic 2026-09-29, driven) — and a `Settings` rule would refuse every
    snapshot a previous build wrote with both, the run's own resume and config routes included.

    `task` is the validated task ADAPTER. `None` means the caller holds none to ask (an embedder's
    injected validator that returns a plain dict, `serve/launch.py::preflight_start`): nothing is
    known of its repository, so the engine's own inject validation stays the only judge."""
    if not external_harness or task is None:
        return
    rs = getattr(task, "repo_spec", None)
    refusal = _engine_refusal(seed.payload, external_harness=True,
                              repo_spec=rs() if callable(rs) else {})
    if refusal:
        raise ConfigRefusal(
            f"seed_from_run: node #{seed.node_id} of run {seed.run_dir.name} cannot seed this "
            f"external_harness run: {refusal}")


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
                 locate: Optional[SeedLocator] = None) -> SeedSource:
    """Resolve and validate `spec` against the new run directory `out`, reading the source's own log.
    `direction` is the NEW run's (the champion pick refuses across it); `locate` is a server's own
    run-directory rule, admitting only a run of ITS runs root (`serve/launch.py::
    server_seed_locator`) — None is the CLI's filesystem lookup. Raises `ConfigRefusal`; never
    touches `out`."""
    text = str(spec or "").strip()
    head, sep, tail = text.rpartition("#")
    head = head.strip()
    node_match = _NODE_SUFFIX.fullmatch(tail) if sep else None
    if not text or (node_match and not head):
        raise ConfigRefusal("seed_from_run: empty — name a run directory, optionally `#<node>`")
    source = _locate(head, out, locate) if node_match else None
    named = int(node_match.group(1)) if source is not None else None
    if source is None:
        source = _locate(text, out, locate)
    if source is None:
        if sep and not node_match and _locate(head, out, locate) is not None:
            raise ConfigRefusal(f"seed_from_run: node {tail.strip()!r} is not an integer id")
        raise ConfigRefusal(_not_a_run(text, head if node_match else None, out, locate))
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
    defects: dict = {}
    for fname, content in payload["files"].items():
        portable, defect = portable_relative_name(fname)
        if portable is None:
            defects.setdefault(defect, []).append(repr(fname))
        else:
            files[portable] = content
    for fname in payload["deleted"]:
        portable, defect = portable_relative_name(fname)
        if portable is None:
            defects.setdefault(defect, []).append(repr(fname))
        else:
            deleted.append(portable)
    if defects:
        # Named by the defect the server's own rule found (critic 2026-09-26: an over-long or
        # control-character name was reported as "outside a node workspace").
        found = [f"{_NAME_DEFECTS.get(defect, defect)}: {', '.join(sorted(names)[:3])}"
                 for defect, names in sorted(defects.items())]
        raise ConfigRefusal(f"seed_from_run: node #{node_id} of run {name} names file(s) "
                            + "; ".join(found))
    payload = {**payload, "files": files, "deleted": deleted}
    refusal = _engine_refusal(payload)
    if refusal:
        raise ConfigRefusal(
            f"seed_from_run: node #{node_id} of run {name} cannot be injected: {refusal}")
    return SeedSource(run_dir=source, node_id=node_id, payload=payload, named=named is not None,
                      direction=str(state.direction or ""),
                      eval_env=_recorded_eval_env(source, state),
                      holdout_fraction=getattr(state, "holdout_fraction", None))


def _recorded_eval_env(source: Path, state) -> Optional[dict]:
    """The declared environment the source's evals ran under, or None when it never recorded one.

    `run_started` carries `eval_env` only when one was declared (so the default payload stayed
    byte-identical), which leaves `{}` meaning either "declared none" or "a log older than the
    field" (critic 2026-09-26: the second read as agreement). The run's own `config.snapshot.json`
    decides: a build that knew the field wrote the key, whatever its value."""
    recorded = dict(getattr(state, "eval_env", None) or {})
    if recorded:
        return recorded
    try:
        snapshot = json.loads((source / "config.snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {} if isinstance(snapshot, dict) and "eval_env" in snapshot else None


def _canonical_task(task) -> tuple[Optional[dict], str]:
    """`(task as its adapter dumps it, "")` — the one form two declarations compare in (critic
    2026-09-26: the CLI records the task as written, `cmd:` spellings and all, and the web start
    route as the adapter dumps it, so the same evaluation read as a different one) — or `(None,
    why)` for a declaration no adapter accepts, e.g. a dataset task whose data is gone.
    `existing_run=True`: the source's is a recorded snapshot."""
    if not isinstance(task, dict):
        return None, "no task snapshot"
    from looplab.adapters.tasks import validate_task
    try:
        return validate_task(dict(task), existing_run=True).model_dump(mode="json"), ""
    except Exception as exc:  # noqa: BLE001 — a declaration no adapter reads is UNKNOWN, never a difference
        return None, _refusal_cause(exc)


def _refusal_cause(exc: Exception) -> str:
    """WHY an adapter refused a declaration, in one bounded line. A pydantic refusal's first line is
    only its count ("1 validation error for DatasetTask"), which names no cause (critic 2026-09-26),
    so its first error is named instead: where, and what."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = (errors() or [None])[0]
        except (TypeError, ValueError, IndexError):  # an `errors` that is not pydantic's: fall back
            first = None
        if isinstance(first, dict) and first.get("msg"):
            where = ".".join(str(part) for part in first.get("loc") or ())
            return ((f"{where}: " if where else "") + str(first["msg"]).strip())[:200]
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:200]


def _normalized_declaration(task) -> Optional[dict]:
    """A declaration in the composable schema's canonical SPELLING (`adapters/task_schema.py::
    normalize_task`) — `cmd:`/`reader:` converge on `eval.command`/`eval.metric.kind` — without an
    adapter's validation, so a side no adapter reads can still be compared; None when it is not a
    task at all."""
    if not isinstance(task, dict):
        return None
    from looplab.adapters.task_schema import normalize_task
    try:
        return normalize_task(dict(task))
    except Exception:  # noqa: BLE001 — a declaration that is not a task says nothing, it is UNKNOWN
        return None


def _task_snapshot(run_dir: Path) -> Optional[dict]:
    try:
        return json.loads((Path(run_dir) / TASK_SNAPSHOT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _declaration_differences(mine: dict, theirs: dict) -> list[str]:
    """The task fields the two canonical declarations disagree on, `eval.<key>` inside the eval."""
    out = []
    for key in sorted((set(mine) | set(theirs)) - _NOT_EVALUATION):
        a, b = mine.get(key), theirs.get(key)
        if a == b:
            continue
        if key == "eval" and isinstance(a, dict) and isinstance(b, dict):
            out.extend(f"eval.{sub}" for sub in sorted(set(a) | set(b)) if a.get(sub) != b.get(sub))
        else:
            out.append(key)
    return out


def seed_verdict(seed: SeedSource, task: dict, *, direction: Optional[str], eval_env,
                 holdout_fraction) -> tuple[str, str]:
    """`(verdict, sentence)` for seeding a run whose task is `task` and whose run-level facts are the
    keywords. Both tasks are compared as their adapters dump them: the evaluation contract decides
    `different` (`engine/eval_contract.py`, naming the facet), and `same` is earned only when the two
    declarations agree on every field but `_NOT_EVALUATION` — a contract key is a partial key, and
    two tasks it calls equal may still declare different stages or an eval `env` (critic
    2026-09-26). Anything that differs, or that either side cannot state, is `unknown`, named."""
    name = seed.run_dir.name
    raw_theirs = _task_snapshot(seed.run_dir)
    (mine, why_mine), (theirs, why_theirs) = _canonical_task(task), _canonical_task(raw_theirs)
    notes, facets = [], []
    if mine is None or theirs is None:
        # A side no adapter reads — the source's dataset moved, say — still DECLARES what it read
        # and ran: the declared contract decides `different` on its own facets (critic 2026-09-26: a
        # proven difference read `unknown` once the source's data was gone), and `same` is never
        # claimed from a declaration half read. BOTH sides in ONE spelling, the composable schema's
        # (critic 2026-09-26, driven): an adapter's dump beside a raw `cmd:{…}`/`reader:` snapshot
        # read as an empty reader and command, and an identical declaration came out `different`
        # with invented facets. A side that is not a task at all decides nothing.
        this_decl, other_decl = _normalized_declaration(task), _normalized_declaration(raw_theirs)
        verdict = "unknown"
        if this_decl is not None and other_decl is not None:
            this, other = contract_from_task(this_decl), contract_from_task(other_decl)
            if comparable(this, other) is False:
                verdict = "different"
                notes.append(contract_notice(this, other, other_run_id=name))
        notes.append(("This run's task" if mine is None else f"Run {name}'s task snapshot")
                     + f" could not be read as a task ({why_mine or why_theirs}), so the two "
                     "declarations cannot be compared in full.")
    else:
        this, other = contract_from_task(mine), contract_from_task(theirs)
        if comparable(this, other) is False:
            verdict = "different"
            notes.append(contract_notice(this, other, other_run_id=name))
        else:
            verdict = "same"
            differs = _declaration_differences(mine, theirs)
            if differs:
                facets.append("their task declarations (" + ", ".join(differs[:6])
                              + (", …" if len(differs) > 6 else "") + ")")
    if direction and seed.direction and direction != seed.direction:
        verdict = "different"
        notes.append(f"DIFFERENT DIRECTION: run {name} "
                     f"{_RANKS.get(seed.direction, seed.direction)} its metric and this run "
                     f"{_RANKS.get(direction, direction)} it, so the seed's standing there is the "
                     "reverse of its standing here.")
    ours = dict(eval_env or {})
    if seed.eval_env is None:
        facets.append("eval_env (not recorded there)")
    elif ours != seed.eval_env:
        keys = sorted(str(k) for k in set(ours) | set(seed.eval_env)
                      if ours.get(k) != seed.eval_env.get(k))
        facets.append("eval_env (" + ", ".join(keys[:5]) + ")")
    if seed.holdout_fraction is None:
        facets.append("holdout_fraction (not recorded there)")
    elif holdout_fraction is not None and float(seed.holdout_fraction) != float(holdout_fraction):
        facets.append(f"holdout_fraction ({float(seed.holdout_fraction):g} there, "
                      f"{float(holdout_fraction):g} here)")
    if facets:
        if verdict == "same":
            verdict = "unknown"
        notes.append("The runs also differ in " + "; ".join(facets) + " — so the two numbers may "
                     "not share a scale.")
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
    objective = seed.payload["origin"].get("source_objective")
    note = str(note or "").strip()
    return (f"seeded from run {seed.run_dir.name} #{seed.node_id}"
            + (f" (its metric there: {metric})" if metric is not None else "")
            # doc 68 68.2: a source an operator retargeted RANKS by another metric, so its champion
            # is that metric's; the number above is its task metric (`node_import_payload`).
            + (f" — that run ranks by {objective!r} (an operator retarget), so its champion is "
               f"that metric's; the number is its task metric" if objective else "")
            + f" — evaluation contract: {verdict}. "
            + (note + ("" if note.endswith(".") else ".") + " " if note else "")
            + "It is evaluated here under this run's own protocol.")


def seed_row_spec(data) -> str:
    """The canonical `<run dir>#<node>` a seed row's `origin` receipt names, or "" for any row that
    is not a well-formed seed — the one reading of a seed row's identity."""
    origin = data.get("origin") if isinstance(data, dict) else None
    if not isinstance(origin, dict) or origin.get("seed_from_run") is not True:
        return ""
    run_dir, node_id = origin.get("run_dir"), origin.get("node_id")
    if (not isinstance(run_dir, str) or not run_dir or isinstance(node_id, bool)
            or not isinstance(node_id, int)):
        return ""
    return f"{run_dir}#{node_id}"


def birth_seed_row(events) -> Optional[dict]:
    """The seed row a run was BORN with — its seq-0 `inject_node` marked `seed_from_run` — as its
    payload, or None. The seed is a fact of the run's first row (`serve/start_record.py::
    _launch_seed_intent` admits it only at seq 0); a later inject with the same marking is not one."""
    first = next(iter(events or ()), None)
    data = getattr(first, "data", None)
    if (getattr(first, "type", None) != EV_INJECT_NODE or getattr(first, "seq", None) != 0
            or not seed_row_spec(data)):
        return None
    return dict(data)


def birth_seed_row_at(run_dir) -> Optional[dict]:
    """`birth_seed_row` of the run at `run_dir`, reading only its log's FIRST record — or None, also
    for a log whose first record cannot be read: a Replay stays usable on a damaged log, which is
    often why it is replayed."""
    from looplab.core.models import Event
    from looplab.events.eventstore import iter_event_jsonl
    try:
        first = next(iter(iter_event_jsonl(Path(run_dir) / "events.jsonl")), None)
        return birth_seed_row([Event(**first)] if isinstance(first, dict) else [])
    except (OSError, ValueError, TypeError):
        return None


def recorded_seed_spec(events) -> str:
    """The canonical spec a run was BORN seeded from, read off its own log, or "". A later `looplab
    run` of the same directory — whatever it passes — records this and nothing else in
    `config.snapshot.json` (critic 2026-09-26: the snapshot was overwritten with the new
    invocation's value, dropping or inventing a seed)."""
    return seed_row_spec(birth_seed_row(events))


def frozen_seed_payload(raw: bytes) -> dict:
    """The seed row a Replay froze for its `looplab run` child (`serve/reset_route.py::
    _freeze_birth_seed`), checked for the shape a seed row carries — every key the child appends,
    `code` a string or null (`events/node_import.py::node_import_payload` writes `snode.code or
    None`, and a repo node's code IS empty: its edits travel in `files`); `ConfigRefusal`
    otherwise. The child appends it VERBATIM rather than resolving a source by path again: the run
    is re-seeded with the node it was born with even when the source has since been deleted, moved
    or replayed into different nodes under the same ids (critic 2026-09-26). The server asks this
    same question of the row BEFORE it commits a Replay, so a row the child would refuse is a 409
    on an intact run, never a stranded archive (critic 2026-09-26, driven: every repo-task seed)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigRefusal(f"the Replay's frozen seed is unreadable: {exc}") from exc
    if (not isinstance(data, dict) or not seed_row_spec(data)
            or not all(key in data for key in _SEED_ROW_KEYS)
            or not isinstance(data["idea"], dict)
            or not (data["code"] is None or isinstance(data["code"], str))
            or not isinstance(data["files"], dict) or not isinstance(data["deleted"], list)
            or data["parent_id"] is not None):
        raise ConfigRefusal("the Replay's frozen seed is not a seed row")
    return data


# Every key the Replay child appends off a frozen seed (`cli/run_cmds.py::_open_and_drive`).
_SEED_ROW_KEYS = ("idea", "code", "files", "deleted", "origin", "parent_id")


def seed_ignored_note(spec: Optional[str]) -> str:
    return (f"seed_from_run={spec!r} ignored: this run directory already has events, and only a "
            "fresh run is seeded")
