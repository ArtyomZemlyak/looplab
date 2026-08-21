"""APPLIED PARAMS — what the node's CONFIGURATION said, bound at the metric read.

THE THIRD SIDE OF THE METRIC RECORD, and the asymmetry between the three is the design:

    metric_subject   the OUTPUT: which artifact this number is a claim ABOUT.
    metric_inputs    the INPUTS:  which bytes it was measured AGAINST.
    applied_params   the COORDINATES: what the configuration that ran actually said the
                     declared parameters were worth.

`Idea.params` is a PROPOSAL. `params_style: "none"` means the engine applies nothing — the Developer
realises the proposal by editing the repo, and it legitimately DEVIATES when it hits a real
constraint. The champion of `e5small-dr-unified-v2` (RECALL@100 0.793426) is recorded at batch 8192 /
accumulation 2 / 15 epochs and its own committed config says 512 / 32 / 3, with the reason written
into the file beside each one:

    n_epochs: 3     # cut from 15: 3x703 steps x ~10.8s/it ~ 6.3h fits the 10h budget with margin
    batch_size: 512 # halved again to fit H200 under R-Drop's 8 concurrent forwards; 32x accumulation

**THAT IS THE BEHAVIOUR WE WANT.** The defect is that nothing reconciled the two, so every reader —
the surrogate, the panel, the proxy, the archive's niches, the novelty distance,
`search/operators.py::merge_idea`'s arithmetic, the exported notebook, and the "Best so far" line the
Researcher is shown — placed that result at coordinates it never occupied. This module SURFACES the
divergence beside the number. It refuses nothing, excludes nothing and cannot cost a node its
terminal: a node that adjusted for a real constraint must still run and must still be allowed to win.
What must not survive is a record attributing its number to parameters it never used.

TWO AUTHORITIES, and the weaker one is the one that fires today.

  `resolved`  — a configuration the EVAL PROCESS ITSELF WROTE during this attempt, elected by an
                operator-declared `eval.metric.applied_config_glob`. It is the STRONGER source
                because it is post-resolution: defaults filled in, environment and command-line
                layered, types coerced. Measured on this box, the resolved document carries 170
                leaves against the input config's 149.
  `committed`  — the carriers the engine staged for this node, re-read from the workdir at the metric
                read. Total on this corpus (all 52 nodes with a config carrier) and needs no
                declaration, which is why it exists at all: a record that only fires when the
                operator has declared something is the `metric_subject` literal-path defect again.

WHY BOTH, WITH NUMBERS. Over `runs/`, the two agree on 341 of 345 declared keys where both answer.
The four where they do not are the whole argument:

  * `rubertlite-dr-unified-v8` node 8 declares batch 8192 / 15 epochs, its committed carrier AGREES
    with the declaration, and the config the process resolved says **4096 / 8**. No reading of the
    bytes the engine holds can see that node; only the resolved document can.
  * `e5small-dr-unified-v2` node 0 declares batch 8192, its carrier says 512, and the process
    resolved **2048** — three different numbers for one parameter.

AND WHY THE STRONGER ONE MUST BE DECLARED RATHER THAN DISCOVERED. A pattern is the only way to name
a path the operator cannot write down (`runtime/metric_subject.py` spends a section on this: the
directory segment is `run_name`, chosen by the agent, with ten distinct spellings across 17 nodes).
But the pattern binds ONLY on a UNIQUE match, and here that rule is not theoretical: 28 of the 52
nodes hold MORE THAN ONE `**/final/config.yaml`, and on 8 of them the matches DISAGREE — on
`train.training.batch_size` and `gradient_accumulation_steps` specifically, because the training
stage and the scoring stage each resolved their own. A pattern that picked one would record a number
nobody chose, which is strictly worse than recording the committed carrier and saying so. So: 0
matches is `missing`, 2+ is `ambiguous`, both are REFUSALS that fall back to `committed` with the
reason on the record — never a tie-break, never a guess.

FRESHNESS IS ENFORCED ON THE RESOLVED TIER AND NOT ON THE COMMITTED ONE, and the split is the same
one `metric_subject` and `metric_inputs` make. A resolved config is by definition something THIS
attempt produced, so one that predates the attempt is a previous attempt's and must not be elected.
A committed carrier is by definition something staged BEFORE the attempt, so a freshness floor would
refuse every one of them.

WHAT IT DOES NOT CLAIM. Exactly the bound `engine/repair_verify.py` states one package over: this is
a statement about a DOCUMENT, not about an execution. A key the loader never reads, a section a
different code path ignores, an environment variable that wins over the file — none of them are
visible to any reader of bytes. The record says which file it read and what that file said, and a
`resolved` record says the process wrote it. Absence is silence, never a certificate.

OPEN[applied-config-glob-undeclared] no task on this box declares `applied_config_glob`, so the
`resolved` tier — the ONLY reader that can see a value the eval process settled for itself — is inert
and every record here is `committed` authority. It is the `metric_subject` literal-path shape one
declaration over, and the cost is measurable: `rubertlite-dr-unified-v8` node 8 declares batch 8192 /
15 epochs, its committed carrier AGREES with the declaration, and the config the process resolved says
4096 / 8 — a node no reading of committed bytes can ever caveat. Closing it is one line in the repo
task's `eval.metric`, plus a run that records `authority: "resolved"` on a real node.
proof:absent:applied_config_glob@examples

DECLINED[cli-vs-carrier-contradiction] a rung reporting that a stage's `--dotted.path=<n>` argv token
contradicts the committed carrier's value for the same path. The pair is real and it is exactly the
env/CLI precedence hazard: the CANDIDATE repo's own `settings_customise_sources` (in
`vectorizer-unified`, outside this tree) returns
`(init, YAML, env, dotenv, CLI)` and pydantic-settings takes the FIRST as highest priority, so the
YAML silently beats anything set on the command line for a key the YAML defines. measured: 10 argv
tokens across every event log on disk name a ≥2-part dotted path their node's own carrier also
defines, and exactly 1 contradicts it — `rubertlite-dr-unified-v8` node 9's
`--train.training.n_epochs 10` against a carrier saying 15, on a node that recorded 0.761773
(— docs/guide/tasks.md). Refused on two grounds and the second is decisive. (1) The rung cannot say WHICH declaration decided: on that
very node the CLI LOST for `n_epochs` (the resolved config reads 15) and WON for
`--train.training.scheduler.type onecycle` (the resolved config carries the whole onecycle block),
from one argv, one source ordering, one process — the difference being only whether the YAML defines
the key, which is a fact about pydantic-settings' merge and not about anything the engine holds. A
report of "these two contradict" at the one site that already has the answer is worse than no report.
(2) Deciding it properly means the engine modelling a third-party library's source-precedence
semantics from the candidate's own `config.py`, which is a version-dependent semantic reading of
agent-authored code — the exact thing the deterministic tier is defined by not doing. The ANSWER is
already built and needs no new rung: with `applied_config_glob` declared, that node's record reads
`n_epochs: 15` at `resolved` authority beside a declaration of 10, so the record shows the CLI lost
without LoopLab knowing one thing about pydantic.
"""
from __future__ import annotations

from typing import Optional

from looplab.core import param_carriers
from looplab.runtime.metric_subject import bind_one, resolve_glob

# Authority vocabulary, strongest first. A REGISTRY (CLAUDE.md): `engine/champion_caveats.py` and the
# UI both read these strings, and a typo'd literal would silently turn "the process resolved this"
# into an unknown authority that every reader falls through on.
APPLIED_RESOLVED = "resolved"
APPLIED_COMMITTED = "committed"
APPLIED_AUTHORITIES = (APPLIED_RESOLVED, APPLIED_COMMITTED)

# Why the `resolved` tier did not bind. `not_declared` is the state every task on this box is in and
# is deliberately a NAMED state rather than an absent key — "nobody declared a resolved config" and
# "the declared one was ambiguous" send an operator to two different places.
RESOLVED_REFUSALS = ("not_declared", "missing", "ambiguous", "stale", "unreadable", "escapes")

# A coordinate two of this node's own carriers state DIFFERENT numbers for. Deliberately its own
# word beside `param_carriers.UNRESOLVED_*`: `ambiguous` means one document names the coordinate
# twice, `conflict` means two carriers name it once each and disagree, and the remedies are
# different — the first is a vaguer declaration, the second is a repo that sets one value in two
# places. It is never settled here; see the conflict rule in `bind_applied_params`.
UNRESOLVED_CONFLICT = "conflict"

# Committed carriers read for one node. A working set naming more configuration documents than this
# is a repository, not a configuration; over the ceiling the rest are unread, which can only
# UNDER-report. The largest real working set on this box names ONE.
MAX_COMMITTED_CARRIERS = 8

# Declared keys the record will carry. `PARAM_OVERRIDE_CAP` bounds the DIVERGENT rows on the durable
# repair event; this bounds the whole applied map, which is the larger object. The biggest real
# declaration on this box is 18 keys.
MAX_APPLIED_KEYS = 64

# Bytes read from one carrier. A configuration document larger than this is not a configuration, and
# over the bound the tail is unread — which can only ever UNDER-report, the direction every rung here
# fails in. The largest real carrier on this box is 18,102 bytes, so the ceiling is ~230x it.
MAX_CARRIER_BYTES = 4 * 1024 * 1024


def declared_numeric_params(params) -> dict:
    """`{key: float}` for the declared coordinates this rung can compare — `{}` for the rest.

    THE SAME THREE BOUNDS `engine/repair_verify.py::declared_param_overrides` applies, deliberately
    re-stated rather than imported: `runtime` may not import `engine`, and the alternative — the
    engine passing a pre-filtered map — would put the rule at the call site where no test can reach
    it. `tests/test_applied_params.py` pins the two against each other.

      * at least two dotted parts (a bare `lr` is a word, not a path);
      * numeric and finite (a computed or non-finite coordinate is not comparable);
      * not a bool (`True` is `isinstance(int)` and would report an agreement nobody wrote).
    """
    out: dict = {}
    for key, value in sorted((params or {}).items()):
        if not isinstance(key, str):
            continue
        parts = tuple(p for p in key.split(".") if p)
        if len(parts) < 2:
            continue
        number = param_carriers.finite_number(value)
        if number is None:
            continue
        out[key] = number
        if len(out) >= MAX_APPLIED_KEYS:
            break
    return out


_KIND_PYTHON = "python"
_KIND_DOCUMENT = "document"


def _carrier_kind(path):
    """`_KIND_PYTHON` / `_KIND_DOCUMENT` for a carrier this record can read, else None.

    MIRRORS `engine/repair_verify.py::_carrier_kind` and is a second spelling for one reason only:
    `runtime` may not import `engine`. `tests/test_param_carriers.py` pins the two to agree on every
    registered suffix, which is what stops the mirror from becoming a drift.
    """
    name = str(path or "")
    if name.endswith(".py"):
        return _KIND_PYTHON
    if param_carriers.is_document_carrier(name):
        return _KIND_DOCUMENT
    return None


def _offer(readings: dict, key: str, value: float, rel: str, line: int, how: str) -> None:
    """Record one carrier's reading of one coordinate. FIRST file to state a given VALUE keeps it.

    Deduplicating on the value and not on the key is the whole point: two carriers that AGREE
    collapse to one reading and settle the coordinate, two that disagree stay two and become a
    `conflict`. A first-wins-by-key loop cannot tell those apart.
    """
    readings.setdefault(key, {}).setdefault(float(value), (rel, int(line), how))


def _read(path) -> Optional[str]:
    """One carrier's text, or None. Bounded, and total over everything a filesystem can raise."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_CARRIER_BYTES)
        return raw.decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def _resolved_carrier(workdir, pattern, *, since, confine=None):
    """The eval-written configuration one declared pattern elects, or `(None, <refusal>)`.

    `resolve_glob` and `bind_one` and NOT a second walk: a pattern here must get exactly the
    containment, identity, digest and freshness treatment `metric_subject` gives one, or the two
    would drift into two different ideas of what a workdir-relative match is.
    """
    matches = resolve_glob(workdir, pattern)
    if matches is None:
        return None, "unreadable"
    if not matches:
        return None, "missing"
    if len(matches) > 1:
        return None, "ambiguous"
    row = bind_one(workdir, matches[0], since=since, confine=confine)
    if not row.get("bound"):
        return None, str(row.get("reason") or "unreadable")
    row["glob"] = str(pattern)
    return row, ""


def bind_applied_params(params, workdir, *, carriers=(), applied_config_glob=None,
                        since: Optional[float] = None, confine=None) -> Optional[dict]:
    """The `metric_provenance.applied_params` record for one eval, or `None`.

    `None` — not an empty record — when the node declares no comparable coordinate or no carrier
    could be read. The distinction matters for the same reason it does on the inputs side: an empty
    record is a claim ("the configuration said nothing about anything you declared") and absence is
    the honest answer when nothing was looked at. **A count that cannot tell "all agreed" from
    "nothing was checked" is the vacuous green this whole rung exists to abolish**, so the record
    always carries `checked` beside `diverged`.

    Shape (additive; every key is optional to a reader and every log written before today has none
    of it — invariant #5):

        {"authority": "resolved" | "committed",
         "carrier": {path, identity, size, digest, digest_mode, kind, glob?},
         "checked": int,                       # declared coordinates the carrier ANSWERED
         "declared": int,                      # declared coordinates that were comparable at all
         "applied": {key: float},              # what the carrier says each answered coordinate is
         "diverged": [{param, declared, applied, line, match}],
         "unresolved": {key: "absent" | "ambiguous"},
         "resolved_refused": str}              # why the stronger tier did not bind, when it did not

    Never raises for anything a filesystem or a malformed document can do: a record may not cost a
    node its terminal, which is `metric_inputs`' rule and `metric_salvage`'s before it.
    """
    declared = declared_numeric_params(params)
    if not declared:
        return None

    refused = "not_declared"
    row = None
    if isinstance(applied_config_glob, str) and applied_config_glob.strip():
        row, refused = _resolved_carrier(workdir, applied_config_glob.strip(),
                                         since=since, confine=confine)
    authority = APPLIED_RESOLVED if row is not None else APPLIED_COMMITTED

    # THE CARRIER SET. On the `resolved` tier it is the one document the pattern elected. On the
    # `committed` tier it is the engine's own staged carriers, re-read FROM THE WORKDIR rather than
    # taken from `node.files` — this record is about what was applied at the metric read, and the
    # committed bytes are already in the log for anyone asking the other question. A carrier the eval
    # rewrote mid-run is exactly the case the two answers differ on, and it is the interesting one.
    if row is not None:
        wanted = [(str(row.get("path") or ""), row)]
    else:
        wanted = [(str(p), None) for p in (carriers or [])
                  if isinstance(p, str) and _carrier_kind(p)][:MAX_COMMITTED_CARRIERS]
    if not wanted:
        return None

    applied: dict = {}
    where: dict = {}
    unresolved: dict = {}
    read_rows: list = []
    # key -> {value: (file, line, how)} — EVERY reading, from every carrier, before anything is
    # settled. Accumulated rather than first-wins because "two carriers disagree" is a fact the
    # record owes the reader and a first-wins loop destroys it silently.
    readings: dict = {}
    conflicts: list = []
    for rel, bound in wanted:
        target = bind_one(workdir, rel, since=None, confine=confine) if bound is None else bound
        if not target.get("bound"):
            read_rows.append(target)
            continue
        text = _read(_carrier_path(workdir, rel, confine))
        if text is None:
            target = dict(target)
            target["bound"] = False
            target["reason"] = "unreadable"
            read_rows.append(target)
            continue
        read_rows.append(target)
        kind = _carrier_kind(rel)
        paths = (param_carriers.python_numeric_paths(text) if kind == _KIND_PYTHON
                 else param_carriers.document_numeric_paths(rel, text))
        for key in declared:
            parts = tuple(p for p in key.split(".") if p)
            if kind == _KIND_PYTHON:
                # TARGET-FIRST, the Python rule: the tree is incomplete, so several assignments may
                # reach one coordinate and every one of them is a real assignment. Two values from
                # one file is already a disagreement this record may not settle.
                hits = param_carriers.resolve_declaration_python(paths, parts)
                for value, line in sorted(hits.items()):
                    _offer(readings, key, value, rel, int(line), param_carriers.MATCH_SUFFIX)
                if not hits and key not in readings:
                    unresolved.setdefault(key, param_carriers.UNRESOLVED_ABSENT)
                continue
            got, line, how = param_carriers.resolve_declaration(paths, parts)
            if got is None:
                # An `ambiguous` refusal OUTRANKS a later `absent`: it is a fact about the
                # DECLARATION, not about one file, and letting the next document settle it would be
                # exactly the tie-break `resolve_declaration` refuses.
                if unresolved.get(key) != param_carriers.UNRESOLVED_AMBIGUOUS:
                    unresolved[key] = how
                continue
            _offer(readings, key, got, rel, int(line), how)
            unresolved.pop(key, None)

    # THE CONFLICT RULE, and it is the one thing this record must not get wrong. Two carriers of ONE
    # node that state DIFFERENT numbers for one declared coordinate are not a tie to break: static
    # bytes cannot order a YAML load against a `.py` assignment that mutates the loaded object, and
    # the corpus proves the naive reading ("the config file is the config") is WRONG — measured over
    # `runs/`, 14 coordinates conflict, 9 on nodes that recorded a metric, and on the two the run's
    # own RESOLVED config settles uniquely the PYTHON carrier is what ran (v8 node 8: the config says
    # 8192 / 15 epochs, `train.py` says 4096 / 8, the process resolved 4096 / 8). Picking the
    # document would have published the champion's number at coordinates it never occupied — the
    # exact defect this module exists to end, committed by the module itself.
    #
    # So a conflicted coordinate is NOT in `applied`; it rides in `conflicts` with EVERY reading and
    # the file each came from, and `unresolved` names it `conflict`. Surfaced, never settled.
    for key, seen in readings.items():
        if len(seen) == 1:
            value = next(iter(seen))
            rel, line, how = seen[value]
            applied[key] = value
            where[key] = (rel, line, how)
            unresolved.pop(key, None)
        else:
            unresolved[key] = UNRESOLVED_CONFLICT
            conflicts.append({"param": key, "declared": declared[key],
                              "readings": [{"applied": v, "file": seen[v][0], "line": seen[v][1]}
                                           for v in sorted(seen)]})
    if not applied and not conflicts:
        return None

    diverged = [{"param": key, "declared": declared[key], "applied": applied[key],
                 "file": where[key][0], "line": where[key][1], "match": where[key][2]}
                for key in sorted(applied) if applied[key] != declared[key]]
    # `checked` AND `declared` both ride, always. A record carrying only `diverged: []` cannot tell
    # "every declared coordinate was compared and they all agree" from "no carrier answered a single
    # one of them", and those are the two states this whole mechanism exists to keep apart.
    record: dict = {"authority": authority,
                    "carriers": read_rows,
                    "declared": len(declared),
                    "checked": len(applied),
                    "applied": dict(sorted(applied.items())),
                    "diverged": diverged}
    if conflicts:
        record["conflicts"] = sorted(conflicts, key=lambda row: row["param"])
    if unresolved:
        record["unresolved"] = dict(sorted(unresolved.items()))
    if authority != APPLIED_RESOLVED:
        record["resolved_refused"] = refused
    return record


def _carrier_path(workdir, rel, confine):
    """The filesystem path one carrier name resolves to under the SAME containment `bind_one` used.

    Reached separately because `bind_one` returns an identity record and not a path; re-deriving it
    with a bare `Path(workdir) / rel` would read a file the containment rule had already refused.
    """
    if confine is not None:
        return confine(workdir, rel)
    from looplab.runtime.metric_subject import _fallback_confine
    return _fallback_confine(workdir, rel)


def applied_divergence_note(record: Optional[dict]) -> str:
    """One sentence naming what the record found. `""` when there is nothing to say.

    Deliberately says COORDINATES and not "wrong": the node may have adjusted for a real constraint
    and its number stands. What the sentence is for is the reader who is about to reuse the
    configuration, or to breed from it.
    """
    if not isinstance(record, dict) or not record.get("diverged"):
        return ""
    rows = record.get("diverged") or []
    first = rows[0] if isinstance(rows[0], dict) else {}
    where = str(first.get("file") or "the applied configuration")
    return (f"{len(rows)} of the {record.get('checked', 0)} declared parameter(s) this eval's "
            f"configuration answers are recorded at a value it does not carry — e.g. "
            f"{first.get('param')} declared {first.get('declared')}, {where} says "
            f"{first.get('applied')}. The number stands; the COORDINATES it is filed under do not.")
