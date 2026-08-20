"""METRIC INPUTS — bind a recorded number to the CONTENT IDENTITY of what it was measured AGAINST.

THE MIRROR IMAGE OF `metric_subject.py`, and the asymmetry between the two is the whole design.

    metric_subject   the OUTPUT side: which artifact this number is a claim ABOUT.
                     It must be CONFINED to the node's own workdir and it must be FRESH — a metric's
                     subject is by definition something THIS node produced, so an absolute path or a
                     file predating the attempt is never one.

    metric_inputs    the INPUT side: which bytes this number was measured AGAINST.
                     It must NOT be confined and it must NOT be fresh. An input is by definition
                     something the node did NOT produce and legitimately SHARES with every other
                     node and every other run — the test set, the product index, a mounted corpus.
                     Requiring it inside the workdir would refuse every real one; requiring it newer
                     than the attempt would refuse all of them by construction.

Both halves are bound by `metric_subject.bind_one`, which already takes an injected `confine` and an
optional freshness floor. This module supplies "no confinement, no floor" and reuses everything else —
`file_identity`, the size/digest ceiling, the directory shape, the closed unbound vocabulary. There is
ONE binder in the tree and two policies over it, rather than two binders that will drift.

WHY THE DECLARATION MAY NAME AN ABSOLUTE PATH, stated because it is the rule `metric_subject` spends
a paragraph refusing. `EvalSpec` is the OPERATOR's spec — `adapters/repo_task.py` says so in its own
docstring ("the agent does not author this") and the entrypoint protection exists to keep it that
way. A candidate cannot add an entry here, so an absolute path is the operator naming a file they
own, not a node reaching outside its sandbox. The measured corpus makes it necessary rather than
merely permissible: the dense-retrieval task's test set and product index live at
`/home/jovyan/data/dr-local/v2/{test,smkt_all.index}.parquet`, reached through a `VS_LOCAL_DATA_ROOT`
env var, and are mounted into no workdir at all.

WHAT THIS BUYS, MEASURED ON THAT TASK (2026-08-20, derived from the eval path in
`/home/jovyan/data/vectorizer-unified`):

  * `test.parquet` is 62,920,840 bytes and carries **no version marker of any kind** — its parquet
    key-value metadata holds only `ARROW:schema`, and its `split` column is the constant "test".
  * `smkt_all.index.parquet` is 37,785,295 bytes and 641,261 rows, and carries **no manifest, no
    checksum and no row-count assertion**.
  * So both are IDENTIFIED BY PATH ALONE, and replacing either in place is undetectable from every
    artifact the eval writes. A corpus that grew makes recall@100 strictly harder; a test set that
    changed makes the number a different quantity. Neither is a fact about the model and neither
    leaves a trace.

A digest of those two files is the entire fix for that pair, and it is path-independent: the same
index reached through a mount, a symlink and an absolute path binds to ONE identity, and two
different indexes written to ONE path bind to two.

GLOBS ARE DELIBERATELY NOT SUPPORTED HERE, unlike on the subject side. `subject_glob` exists because
the pipeline names its OWN output directory and the operator cannot write the literal at submit time
(measured: 10 distinct agent-chosen directory names across 17 nodes). An input is the opposite case —
the operator chose the path, it is stable across the whole run, and a pattern would introduce exactly
the `ambiguous` outcome (`metric_subject.UNBOUND_REASONS`) on the one declaration whose whole job is
to be unambiguous. Declare the file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from looplab.runtime.metric_subject import MAX_SUBJECTS, bind_one

# The same ceiling the subject side uses. An eval with more than eight declared inputs is not a
# comparability record, it is a manifest, and the cost is a full digest of each one per node eval.
MAX_INPUTS = MAX_SUBJECTS

# Longest declaration accepted. A path longer than this is a bug in the spec, not a file.
_MAX_PATH_CHARS = 4096


def _input_path(workdir, rel):
    """Resolve one declared input. `None` refuses the declaration (-> `escapes`).

    THE ONE THING IT REFUSES is a path that cannot name a file at all: empty, over-long, or holding
    a NUL byte (which `os.stat` raises `ValueError` on rather than `OSError`, i.e. it would escape
    `bind_one`'s own containment and reach the eval worker uncaught). Everything else resolves —
    absolute as itself, relative against the workdir — because refusing an operator's absolute path
    here would refuse every real input on this box. See the module docstring for why that is safe.
    """
    text = str(rel or "")
    if not text or len(text) > _MAX_PATH_CHARS or "\x00" in text:
        return None
    path = Path(text)
    return path if path.is_absolute() else Path(workdir) / path


def bind_inputs(inputs, workdir) -> Optional[dict]:
    """The `metric_provenance.eval_inputs` record for one eval, or `None` when none was declared.

    Shape (additive; every key is optional to a reader and every old log has none of it):

        {"inputs_bound": bool,
         "inputs": [ {path, bound, identity, size, mtime_ns, kind, digest, digest_mode, reason?} … ]}

    `None` — not an empty record — when the operator declared nothing. The distinction is the same
    one `engine/comparability.py` turns on: "no inputs were declared" is `unknown`, and an empty
    record would be a key over nothing that every other empty record equals.

    `inputs_bound` is True only when EVERY declared input bound, and the reason for the FIRST failure
    is reported. A key over a half-bound set would digest "the files we could read", so two runs that
    each failed to read a different file would hash the same material — which is why
    `comparability.measured_material` refuses a partial record outright rather than weakening it.

    NO FRESHNESS FLOOR (`since=None`), which is not an oversight: an input predates the attempt by
    definition. `bind_one`'s `stale` slug is a claim about an artifact nobody chose to reuse, and
    every input is an artifact everybody chose to reuse.
    """
    rels = [entry for entry in (inputs or [])
            if isinstance(entry, str) and entry.strip()][:MAX_INPUTS]
    if not rels:
        return None
    rows = [bind_one(workdir, rel, since=None, confine=_input_path) for rel in rels]
    bad = next((row for row in rows if not row.get("bound")), None)
    record: dict = {"inputs_bound": bad is None, "inputs": rows}
    if bad is not None:
        record["unbound_reason"] = bad.get("reason") or "unreadable"
    return record


def input_declaration(spec) -> list:
    """The operator's `eval.inputs` list, filtered to usable strings. `[]` when absent.

    Filtered HERE rather than trusted, for the reason `eval_dispatch` filters `metric.subject`:
    `_grandfathered` reloads a recorded `task.snapshot.json` WITHOUT re-validating it, so the
    pydantic guard on `EvalSpec` is not total over what reaches a binder, and a non-string entry
    becomes `Path(workdir) / 123` — an uncaught TypeError out of the eval worker, i.e. a node with
    no terminal that re-dies on every resume.
    """
    if not isinstance(spec, dict):
        return []
    declared = spec.get("inputs")
    if not isinstance(declared, (list, tuple)):
        return []
    return [entry for entry in declared if isinstance(entry, str) and entry.strip()][:MAX_INPUTS]


def unreadable_input_note(record: Optional[dict]) -> str:
    """One sentence naming the fix when a declared input did not bind. `""` when it did.

    Reuses `metric_subject`'s per-reason message table rather than minting a second one — the slugs
    are the same closed vocabulary and the fixes are the same fixes — but says INPUT, because
    "the declared subject is not there" sends an operator to look at the wrong side of the eval.
    """
    if not isinstance(record, dict) or record.get("inputs_bound"):
        return ""
    row = next((entry for entry in (record.get("inputs") or []) if not entry.get("bound")), {})
    reason = str(record.get("unbound_reason") or "unreadable")
    path = row.get("path") or ""
    return (f"the declared evaluation INPUT {path!r} could not be identified ({reason}), so this "
            "number carries no evidence of what it was measured against and is not comparable with "
            "any other run's.")
