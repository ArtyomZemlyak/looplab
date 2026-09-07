"""The AGENT-INVISIBLE search split for a real MLE-bench competition (doc 52 §5.1 row 3; AIRA₂'s D_search).

WHY THIS EXISTS. Until 2026-09-06 `engine/holdout.py::apply_host_grade` graded EVERY node's submission
against the private test answers and wrote that grade back as `res.metric`, and `build_holdout_idx`
returned an empty partition for the kind — so the search hill-climbed the private grade, the champion
was a max over N private draws (the 9-13-point oracle gap AIRA₂ cites), and the protocol the benchmark
states (no score feedback during a run; one final grade) was violated on the one path this repo calls
its credible benchmark. The run then published a test-selected number as if it were not.

WHAT IT IS. A deterministic slice of the PUBLIC train rows — chosen by `engine/triage.py::
_holdout_indices` over (row count, `holdout_fraction`, `search_epoch`), i.e. pinned by `run_started`
exactly like the generic host-graded holdout — is HIDDEN from the agent: removed from its `train.csv`,
appended to its `test.csv` with the target columns dropped, and added to its `sample_submission.csv`.
The candidate writes ONE `submission.csv` covering both populations, exactly as before. At search time
the host grades the hidden rows against answers carved here, with the competition's OWN grader
(`mlebench_grade.py --answers`); at finish the search champion's public-test rows are graded ONCE
against the private answers, and that grade is the run's `holdout_evaluated` metric and its medal
report. The private grade never reaches `res.metric` and never ranks anything.

WHAT CAN BE CARVED, and the refusal. The answers must be in the PRIVATE format — the sample
submission's columns — and `train.csv` does not spell that format in general (each competition's
`prepare.py` does). Two layouts are decidable from the files alone and cover the tabular family the
offline baselines support:
  * SCALAR — every target column of the sample submission is a column of `train.csv` (a regression
    target, a 0/1 label column, nomad's two energies): the answers are those columns verbatim;
  * ONE-HOT — the sample submission carries several target columns and exactly one train column
    (not the id) takes its values among those column names (spooky's `author` against
    `EAP,HPL,MWS`): the answers are that label one-hot, which is what the private file holds too.
Anything else raises `SplitUndecidable`, and the engine REFUSES the run at start (`ConfigRefusal`)
naming the two ways out — `holdout_fraction=0` for the explicit legacy protocol (every node graded on
the private answers, recorded as such in `host_grading.protocol`), or a competition whose layout
decides. A refusal, never a silent fallback: a run that cannot honour the protocol must say so, and
the campaign's reader must be able to tell the two protocols apart from the log.

IDS. Hidden rows keep their train ids. If any collides with a public test id the split is refused,
because filtering the submission by id (`filter_submission`) would then drop a public row from the
private grade or a public row into the search grade.

Layering: `adapters` — stdlib `csv` only, no pandas, no `mlebench` import; pure functions over asset
TEXT, so `engine/holdout.py` can carve at `Engine.__init__` and again on re-entry without a process.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Iterable, Optional


class SplitUndecidable(ValueError):
    """The answers for a hidden slice cannot be carved from the public files in the private format."""


@dataclass(frozen=True)
class Layout:
    train: str
    test: str
    sample: str
    id_col: str
    target_cols: tuple
    mode: str            # "scalar" | "onehot"
    label_col: Optional[str]   # the train column the one-hot answers are carved from (onehot only)


@dataclass(frozen=True)
class CarvedSplit:
    assets: dict          # what the agent sees: train minus the slice, test + slice, sample + ids
    answers_csv: str      # the hidden slice in the PRIVATE format (sample header), host-only
    hidden_ids: tuple     # the slice's ids, in train order


def _parse(text: str) -> tuple[list, list]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SplitUndecidable("an empty CSV has no header")
    header, body = rows[0], [r for r in rows[1:] if r]
    return header, body


def _render(header: list, rows: Iterable) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return out.getvalue()


def _asset_named(assets: dict, prefix: str) -> Optional[str]:
    names = sorted(n for n in assets if n.lower().startswith(prefix) and n.lower().endswith(".csv"))
    return names[0] if names else None


def layout(assets: dict) -> Layout:
    """Decide the split's shape from the public files, or refuse (`SplitUndecidable`)."""
    train = _asset_named(assets, "train")
    test = _asset_named(assets, "test")
    sample = _asset_named(assets, "sample_submission")
    if not (train and test and sample):
        raise SplitUndecidable(
            "the public files are not a train.csv / test.csv / sample_submission.csv triple")
    th, tb = _parse(assets[train])
    eh, _eb = _parse(assets[test])
    sh, _sb = _parse(assets[sample])
    if len(sh) < 2:
        raise SplitUndecidable("the sample submission has no target column")
    id_col, targets = sh[0], tuple(sh[1:])
    if id_col not in th or id_col not in eh:
        raise SplitUndecidable(f"the submission id column {id_col!r} is not in both train and test")
    if all(t in th for t in targets):
        return Layout(train, test, sample, id_col, targets, "scalar", None)
    if len(targets) >= 2:
        candidates = []
        tset = set(targets)
        for i, col in enumerate(th):
            if col == id_col or col in eh:
                continue
            values = {r[i] for r in tb if i < len(r) and r[i] != ""}
            if values and values <= tset:
                candidates.append(col)
        if len(candidates) == 1:
            return Layout(train, test, sample, id_col, targets, "onehot", candidates[0])
        if len(candidates) > 1:
            raise SplitUndecidable(
                f"{len(candidates)} train columns take their values among the submission's target "
                f"columns ({', '.join(candidates)}); the label column is ambiguous")
    raise SplitUndecidable(
        f"the sample submission's target columns {list(targets)} are neither train columns nor the "
        "one-hot of a single train label column, so the hidden rows' answers cannot be written in "
        "the private format")


def train_row_count(assets: dict) -> int:
    """How many train rows the split is drawn over (0 when the layout is not a train CSV)."""
    train = _asset_named(assets, "train")
    if not train:
        return 0
    try:
        _h, body = _parse(assets[train])
    except SplitUndecidable:
        return 0
    return len(body)


def carve(assets: dict, hidden: Iterable[int]) -> CarvedSplit:
    """Hide the train rows at the given 0-based body indices; return what the agent sees, the
    answers the host keeps, and the hidden ids. Pure: the same inputs carve the same split."""
    lay = layout(assets)
    hidden_idx = {int(i) for i in hidden}
    th, tb = _parse(assets[lay.train])
    eh, eb = _parse(assets[lay.test])
    sh, sb = _parse(assets[lay.sample])
    id_i = th.index(lay.id_col)
    kept, hid = [], []
    for i, row in enumerate(tb):
        (hid if i in hidden_idx else kept).append(row)
    if not hid:
        raise SplitUndecidable("no hidden rows: the partition names no train row")
    if not kept:
        raise SplitUndecidable("the partition hides every train row")
    hidden_ids = tuple(r[id_i] if id_i < len(r) else "" for r in hid)
    if any(not h for h in hidden_ids) or len(set(hidden_ids)) != len(hidden_ids):
        raise SplitUndecidable("a hidden train row has no id, or two share one")
    e_id = eh.index(lay.id_col)
    public_ids = {r[e_id] for r in eb if e_id < len(r)}
    clash = sorted(set(hidden_ids) & public_ids)
    if clash:
        raise SplitUndecidable(f"train ids collide with public test ids ({clash[:3]}…)")
    # The hidden rows as TEST rows: the test header's columns, taken from the train row where the
    # column exists, never a target column (a test file that carries a blank target keeps it blank).
    tpos = {c: i for i, c in enumerate(th)}
    tset = set(lay.target_cols) | ({lay.label_col} if lay.label_col else set())
    hidden_test = []
    for r in hid:
        hidden_test.append([("" if c in tset or c not in tpos or tpos[c] >= len(r) else r[tpos[c]])
                            for c in eh])
    # The sample rows for the hidden ids copy the first sample row's placeholder values.
    placeholder = list(sb[0][1:]) if sb and len(sb[0]) == len(sh) else ["0"] * (len(sh) - 1)
    hidden_sample = [[h, *placeholder] for h in hidden_ids]
    # The answers, in the private format.
    if lay.mode == "scalar":
        cols = [tpos[c] for c in lay.target_cols]
        answers = [[r[id_i], *[(r[c] if c < len(r) else "") for c in cols]] for r in hid]
    else:
        li = tpos[lay.label_col]
        answers = [[r[id_i], *["1" if (li < len(r) and r[li] == c) else "0" for c in lay.target_cols]]
                   for r in hid]
    out = dict(assets)
    out[lay.train] = _render(th, kept)
    out[lay.test] = _render(eh, eb + hidden_test)
    out[lay.sample] = _render(sh, sb + hidden_sample)
    return CarvedSplit(assets=out, answers_csv=_render(sh, answers), hidden_ids=hidden_ids)


def filter_submission(text: str, ids: Iterable[str], *, keep: bool) -> str:
    """The submission restricted to (`keep=True`) or purged of (`keep=False`) the given ids, by its
    first column — the id column, by the sample submission's convention. The header is kept."""
    ids = set(ids)
    header, body = _parse(text)
    rows = [r for r in body if r and ((r[0] in ids) if keep else (r[0] not in ids))]
    return _render(header, rows)
