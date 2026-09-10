"""The durable research record: exact-span evidence, the retrieved literature, and the claim join.

Until doc 52 row 16 a Deep-Research memo's evidence was a URL plus a 200-character snippet
(`ResearchMemo.sources`) and its plan lived only inside one tool-loop context: nothing an operator
or a later run could re-check a verifier verdict against, nothing that survived a resume, and no
durable record of which papers a run had actually read (doc 28 DR-01 / DR-02; doc 51's
`retrieved-literature-is-never-durable`; doc 27's `inner-agent-phases-not-event-sourced`).

Four pure builders, all deterministic over what a tool RETURNED — never over what a model said
about it, which is the same line `metric_salvage.py` draws for the eval: the record is bytes the
engine observed.

* `evidence_item(...)` — one immutable `EvidenceItem`: an `id` minted from the kind, the locator
  and the sha256 of the FULL result text (so the same bytes from the same place get the same id
  in any run, and a changed page gets a different one), the `quote` (an exact span: the first
  `QUOTE_CHARS` characters of the result) and its provenance (the tool, the turn it was read on).
  `sha256` is over the whole result, so a verdict later re-checked against the quote can also be
  re-checked against the whole text the quote was cut from.
* `parse_literature(result)` — the papers an `arxiv_search` result rendered (`tools/literature.py`
  writes `N. title\\n   abstract`), each with a stable id over the title and a hash of the abstract.
* `bind_claims_to_evidence(claims, evidence)` — the deterministic join: a claim citing a URL is
  bound to every evidence item whose locator identity is that URL's, a claim citing a node id to
  every item read from that experiment. The model never chooses an evidence id; the record does.
* `number_fidelity(statement, …)` — the numbers half of that join: every decimal a claim QUOTES,
  matched against the recorded metrics of the experiments it CITES. A match, never a classifier
  (see the block above the function for why that distinction is the whole design), and it grades
  nothing — `trust/memo_verify.py::number_fidelity_report` aggregates it onto the memo's record.
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional

QUOTE_CHARS = 600
MAX_EVIDENCE_ITEMS = 64
MAX_LITERATURE_ITEMS = 32
EVIDENCE_KINDS = ("web", "literature", "experiment", "note", "memory", "tool")
# Tool name -> evidence kind. A name not listed is `tool`, which says only "a tool returned it".
_KIND_BY_TOOL = {
    "web_search": "web", "web_fetch": "web",
    "arxiv_search": "literature",
    "read_experiment": "experiment", "read_run_experiment": "experiment",
    "read_sibling_experiment": "experiment", "list_experiments": "experiment",
    "read_code": "experiment", "read_run_code": "experiment", "node_diff": "experiment",
    "kb_search": "note", "read_note": "note", "list_notes": "note", "grep": "note",
    "cross_run_search": "memory", "read_lessons": "memory", "list_lessons": "memory",
    "read_research_memo": "memory",
}
_ENTRY = re.compile(r"^\s*(\d+)\.\s+(.+?)\n\s+(.*?)(?=\n\s*\d+\.\s|\Z)", re.S | re.M)


def evidence_kind_for(tool: str) -> str:
    return _KIND_BY_TOOL.get(str(tool or ""), "tool")


def _sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()


def evidence_item(*, tool: str, locator: str, result: str, turn: int,
                  locator_identity: str = "", node_id: Optional[int] = None) -> dict:
    """One immutable evidence item over a tool result (see the module docstring)."""
    text = str(result or "")
    kind = evidence_kind_for(tool)
    digest = _sha(text)
    identity = str(locator_identity or locator or "")
    item = {
        "id": "ev-" + _sha(f"{kind}\n{identity}\n{digest}")[:24],
        "kind": kind,
        "tool": str(tool or "")[:64],
        "locator": str(locator or "")[:400],
        "quote": text[:QUOTE_CHARS],
        "sha256": digest,
        "bytes": len(text.encode("utf-8", errors="replace")),
        "turn": int(turn) if isinstance(turn, int) and turn >= 0 else 0,
    }
    if identity:
        item["locator_identity"] = identity[:400]
    if isinstance(node_id, int) and node_id >= 0:
        item["node_id"] = node_id
    return item


def parse_literature(result: str, *, query: str = "") -> list[dict]:
    """The papers an `arxiv_search` result rendered, or [] for a refusal / no-results answer."""
    text = str(result or "")
    if not text or text.startswith("("):
        return []
    out: list[dict] = []
    for match in _ENTRY.finditer(text):
        title = " ".join(match.group(2).split()).strip()
        abstract = " ".join(match.group(3).split()).strip()
        if not title:
            continue
        out.append({
            "id": "lit-" + _sha(title.lower())[:24],
            "title": title[:400],
            "abstract_sha256": _sha(abstract),
            "abstract_chars": len(abstract),
            "query": str(query or "")[:200],
            "tool": "arxiv_search",
        })
        if len(out) >= MAX_LITERATURE_ITEMS:
            break
    return out


def bind_claims_to_evidence(claims: Iterable[dict], evidence: Iterable[dict]) -> None:
    """Stamp `evidence_ids` on every claim from the items it can be bound to, IN PLACE."""
    by_identity: dict[str, list[str]] = {}
    by_node: dict[int, list[str]] = {}
    for item in evidence or ():
        if not isinstance(item, dict) or not item.get("id"):
            continue
        identity = item.get("locator_identity")
        if isinstance(identity, str) and identity:
            by_identity.setdefault(identity, []).append(item["id"])
        node = item.get("node_id")
        if isinstance(node, int):
            by_node.setdefault(node, []).append(item["id"])
    for claim in claims or ():
        if not isinstance(claim, dict):
            continue
        ids: list[str] = []
        for identity in claim.get("url_identities") or ():
            for eid in by_identity.get(str(identity), ()):
                if eid not in ids:
                    ids.append(eid)
        for node in claim.get("node_ids") or ():
            if isinstance(node, int):
                for eid in by_node.get(node, ()):
                    if eid not in ids:
                        ids.append(eid)
        claim["evidence_ids"] = ids


# ------------------------------------------------- DOES THE MEMO'S NUMBER COME FROM THE RUN'S OWN
#
# WHY THIS IS A MATCH AND NOT A CLASSIFIER (doc 52 row 32, second half). `trust/memo_verify.py::
# check_claims` declined to look at numbers at all, and its reason is on the record and still true:
# a research claim legitimately quotes non-metric decimals — an arXiv id (2506.12928), a percentage
# from a paper, a dataset size, a p-value — and NO regex can tell those from a metric, so a
# "confabulation" heuristic over them labels well-supported claims fabricated. MLReplicate measured
# 59 % of the numbers in accepted write-ups unsupported, so the question is worth asking; the way to
# ask it without a classifier is to stop classifying and start MATCHING: take every decimal the
# statement quotes and ask whether the CITED experiments' recorded metrics contain it.
#
# WHAT AN UNMATCHED NUMBER IS, AND IS NOT. It is not a fabrication and this module never says it is.
# A memo quoting a paper's 37.9 and an experiment's 0.8776 has one matched and one unmatched decimal
# and is entirely honest; a memo quoting a plateau from a SIBLING run has every number unmatched and
# is also honest about it. The three channels are the whole finding: `cited` (this number is one the
# cited experiments actually recorded), `run` (it is a metric of THIS run, but of an experiment the
# claim does not cite — a mis-attribution, and the one channel a reader could not get any other way)
# and `none` (it is not a metric of this run at all, which covers every legitimate foreign number as
# well as every invented one). The instrument RECORDS the three counts; nothing here grades a claim,
# and `unmatched` is deliberately not called anything else.
#
# ITS RECALL IS A FLOOR IN ONE DIRECTION AND EXACT IN THE OTHER. A `cited` match is exact: the
# metric, formatted to the number of decimal places the memo quoted, IS the quoted literal. An
# unmatched number is only "this run's terminal metrics do not contain it" — a metric printed in a
# log but never recorded, a number derived from two metrics (a delta, a ratio), and a sign the memo
# drops all read as unmatched. So a low match rate is not evidence of fabrication and a high one is
# not a clean bill of health, which is exactly why the block that carries these counts is read by
# nothing that decides.
#
# WHAT THE DENOMINATOR IS MADE OF, measured on the one real memo preserved in this tree
# (`tests/data/v8_research_memo.json`, `rubertlite-dr-unified-v8`, 8 claims): **21 decimals**, of
# which 6 are results (0.8776, 0.8835, 0.8173, 0.852, 0.728 and a second 0.8835), 2 are deltas
# (+0.03, 0.04) and **13 are hyperparameter VALUES** — weight decay 0.1, temperature 0.05, R-Drop
# alpha 0.5, OneCycle pct_start 0.2, a cosine threshold 0.264. Nothing separates those from a
# metric without the classifier this design refuses, so the recorded share has hyperparameters in
# its denominator by construction. That is the reason `fidelity` is an instrument reading rather
# than a grade, and the reason the `run` channel — a real metric of an experiment the claim does
# NOT cite — is the finding worth a reader's attention (`docs/audit/memo-number-fidelity.md`).
NUMBER_FIDELITY_VERSION = 1
MAX_QUOTED_NUMBERS = 32
NUMBER_MATCH_KINDS = ("cited", "run", "none")

# The two shapes a regex CAN tell apart, excluded by LEXICAL span rather than by judging the number:
# anything inside a URL or DOI, and an arXiv id (four digits, a dot, four or five digits, optional
# version suffix). Both are excluded rather than counted as unmatched, and the count of what was
# excluded rides on the record so the denominator can be checked. A metric that happens to be
# spelled like an arXiv id is indistinguishable from one, and this errs toward the smaller claim.
_EXCLUDED_SPANS = (
    re.compile(r"\b(?:https?://|www\.|arxiv\.org/|doi\.org/|10\.\d{4,9}/)\S*", re.I),
    re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b"),
)
# A DECIMAL, never a bare integer. The memos quote hyperparameters as integers by the dozen (batch
# 8192, 10 epochs, seed 42) and a recorded metric is a measured decimal, so admitting integers would
# fill the denominator with numbers nobody claims are results. A version triple (`1.2.3`) is refused
# by the two guards: `1.2` is followed by `.` and `2.3` is preceded by one.
_DECIMAL = re.compile(r"(?<![\w.])[-+]?\d{1,12}\.\d{1,10}(?![\w.])")


def _scan_numbers(statement: str) -> tuple[list[dict], int]:
    """`([{text, value, places, percent}, …], excluded)` — the decimals a statement quotes."""
    text = str(statement or "")
    spans = [(m.start(), m.end()) for pattern in _EXCLUDED_SPANS for m in pattern.finditer(text)]
    kept: list[dict] = []
    excluded = 0
    for match in _DECIMAL.finditer(text):
        if any(start < match.end() and match.start() < end for start, end in spans):
            excluded += 1
            continue
        literal = match.group(0)
        try:
            value = float(literal)
        except ValueError:                        # unreachable for this pattern; never raise here
            continue
        kept.append({
            "text": literal,
            "value": value,
            "places": len(literal.split(".", 1)[1]),
            # A memo that writes a fraction as a percentage is quoting the same measurement, so the
            # `%` immediately after the literal is read as scale rather than as a different number.
            "percent": text[match.end():match.end() + 1] == "%",
        })
        if len(kept) >= MAX_QUOTED_NUMBERS:
            break
    return kept, excluded


def quoted_numbers(statement: str) -> list[dict]:
    """Every decimal a claim statement quotes, minus the URL/arXiv spans (see `_scan_numbers`)."""
    return _scan_numbers(statement)[0]


def number_matches_metric(number: dict, metric) -> bool:
    """Is `metric` the number this statement quoted, AT THE PRECISION IT WAS QUOTED?

    `0.88` matches a recorded 0.8776 and `0.8776` does not match 0.8835 — the memo's own rounding is
    the tolerance, so nothing here has to invent one. The SIGN is part of the number: a quote that
    drops the minus of a negative metric reads as unmatched rather than as a match, because deciding
    that a memo "meant" the absolute value is exactly the guess this instrument exists to avoid.
    """
    try:
        value = float(metric)
    except (TypeError, ValueError):
        return False
    if value != value or value in (float("inf"), float("-inf")):    # NaN / inf: never a match
        return False
    places = number.get("places")
    places = places if type(places) is int and 0 <= places <= 10 else 0
    try:
        literal = f"{float(number['value']):.{places}f}"
    except (KeyError, TypeError, ValueError):
        return False
    if f"{value:.{places}f}" == literal:
        return True
    return bool(number.get("percent")) and f"{value * 100:.{places}f}" == literal


def number_fidelity(statement: str, *, cited_metrics: Iterable[tuple] = (),
                    other_metrics: Iterable[tuple] = ()) -> dict:
    """Where one claim's quoted decimals stand against the metrics it cites (see the block above).

    `cited_metrics` / `other_metrics` are `(node_id, metric)` pairs: the experiments this claim
    CITES, and the run's other recorded experiments. Pure and deterministic — the caller resolves
    which nodes are admissible evidence, this counts.

    Returns `{"v", "quoted", "matched", "elsewhere", "unmatched", "excluded", "values"}`, where
    `values` names each decimal and the channel it landed in. A claim that quotes no decimal
    reports `quoted: 0`, which is not a failure of anything.
    """
    numbers, excluded = _scan_numbers(statement)
    cited = [(nid, metric) for nid, metric in (cited_metrics or ())]
    other = [(nid, metric) for nid, metric in (other_metrics or ())]
    values: list[dict] = []
    matched = elsewhere = unmatched = 0
    for number in numbers:
        row = {"text": number["text"]}
        hit = next((nid for nid, metric in cited if number_matches_metric(number, metric)), None)
        if hit is not None:
            matched += 1
            row.update(match="cited", node_id=hit)
        else:
            near = next((nid for nid, metric in other
                         if number_matches_metric(number, metric)), None)
            if near is not None:
                elsewhere += 1
                row.update(match="run", node_id=near)
            else:
                unmatched += 1
                row["match"] = "none"
        values.append(row)
    return {"v": NUMBER_FIDELITY_VERSION, "quoted": len(numbers), "matched": matched,
            "elsewhere": elsewhere, "unmatched": unmatched, "excluded": excluded, "values": values}
