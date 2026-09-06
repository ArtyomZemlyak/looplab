"""The durable research record: exact-span evidence, the retrieved literature, and the claim join.

Until doc 52 row 16 a Deep-Research memo's evidence was a URL plus a 200-character snippet
(`ResearchMemo.sources`) and its plan lived only inside one tool-loop context: nothing an operator
or a later run could re-check a verifier verdict against, nothing that survived a resume, and no
durable record of which papers a run had actually read (doc 28 DR-01 / DR-02; doc 51's
`retrieved-literature-is-never-durable`; doc 27's `inner-agent-phases-not-event-sourced`).

Three pure builders, all deterministic over what a tool RETURNED — never over what a model said
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
