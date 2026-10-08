"""The copied-base receipt and the `workspace_seeded` row it names: ONE binder (doc 72 §9).

A completed attempt's `metric_provenance.base_revision` says "these were the editable bytes this
lifecycle was seeded with, archived under this digest, recorded by the seed event at seq N". Three
readers act on that sentence — the `/seed-files` route (`serve/seed_files.py`), the upstream lane's
source eligibility (`engine/upstream_state.py::_source_receipt`) and `looplab export-git`'s
`Looplab-Base-Reference` trailer (`events/git_export.py`) — and a fourth reads the receipt half
alone (`serve/node_comparison.py`'s `base_unknown` / `base_different`). Each had spelled the rule
itself, and the copies had drifted: the git projection re-checked the fields by hand, never asked
whether the archive was `stored` at `base_snapshots/<digest>`, so it printed
`Looplab-Base-Reference: sha256=…` for a receipt the route and the upstream lane both refuse.

So the rule lives here, once, and it is the STRICTEST union of the copies, because a weaker reader
is a reader that vouches for what a stricter one refused:

* the receipt half (`receipt_archive_digest`): `seed_archive_digest` (version 1, the seeded-editables
  scope, `complete`, a lowercase 64-hex digest, an archive `stored` at its canonical path), every
  identity integer exact and inside JSON's safe range (`node_comparison`'s bound — a value past
  2**53-1 cannot round-trip a JS reader, and nothing real is that large), and the receipt naming
  THIS node and THIS lifecycle;
* the event half (`bind_seed_receipt`): the row at `seed_event_seq` IS a `workspace_seeded` row,
  lands before the node's terminal, names the node with an exact int, binds the lifecycle by
  `events/replay_ctx.py::event_generation_binds` — the ONE answer to "does this row's stamp bind that
  generation", which the git projection had re-spelled as a bare `!=` — and records the SAME archive
  identity (`seed_archive_digest` of its own `base_revision`, plus `file_count` and `bytes`).

The generation predicate is HANDED IN (`generation_binds=`) rather than imported or re-spelled:
`core` may not import `events`, and a third spelling of that rule here is exactly the drift this
module exists to end. Every caller passes `event_generation_binds` itself.

Pure: no I/O, no event-type import (`core` imports nothing above itself, so the type is the literal
`events/types.py::EV_WORKSPACE_SEEDED` spells). Whether the bytes are still on disk is a different
question, answered only by `engine/seed_archive.py::verified_seed_archive`. Node ELIGIBILITY
(status, tombstone, feasibility, a measured score) stays with each caller: it is what the caller
does with a bound base, not whether the base is bound.
"""
from __future__ import annotations

import re
from typing import Callable, Mapping, NamedTuple

ARCHIVE_DIR = "base_snapshots"
SEED_EVENT_TYPE = "workspace_seeded"
JSON_SAFE_INT_MAX = 2 ** 53 - 1
_IDENTITY_INTS = ("node_id", "generation", "seed_event_seq", "file_count", "bytes")

RECEIPT_UNUSABLE = "receipt_unusable"   # the receipt itself does not name this lifecycle's archive
SEED_UNBOUND = "seed_unbound"           # the receipt is well-formed but its seed row does not bind it


def seed_archive_digest(receipt) -> str | None:
    """The recorded archive's canonical identity, or None for an unusable reference."""
    if not isinstance(receipt, dict) or receipt.get("complete") is not True:
        return None
    if (type(receipt.get("version")) is not int or receipt["version"] != 1
            or receipt.get("scope") != "seeded_editables_before_mounts_and_overlay"
            or any(type(receipt.get(k)) is not int or receipt[k] < 0 for k in ("file_count", "bytes"))):
        return None
    digest, archive = receipt.get("digest"), receipt.get("archive")
    if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(archive, dict) or type(archive.get("version")) is not int
            or archive["version"] != 1
            or archive.get("status") != "stored"
            or archive.get("path") != f"{ARCHIVE_DIR}/{digest}"):
        return None
    return digest


def node_base_receipt(node):
    """The raw `base_revision` a node's provenance records, or None. Never validated here."""
    provenance = getattr(node, "metric_provenance", None) if node is not None else None
    return provenance.get("base_revision") if isinstance(provenance, dict) else None


def receipt_archive_digest(receipt, *, node_id, generation) -> str | None:
    """The receipt half: its archive digest when it names lifecycle `(node_id, generation)`."""
    digest = seed_archive_digest(receipt)
    if (digest is None
            or any(type(receipt.get(k)) is not int or not 0 <= receipt[k] <= JSON_SAFE_INT_MAX
                   for k in _IDENTITY_INTS)
            or receipt["node_id"] != node_id or receipt["generation"] != generation):
        return None
    return digest


class SeedReceiptBinding(NamedTuple):
    """`digest` is set exactly when the receipt is bound; otherwise `reason` says which half failed."""
    receipt: object
    digest: str | None
    reason: str | None
    seed: object = None


def bind_seed_receipt(receipt, events_by_seq: Mapping, *, node_id, generation,
                      terminal_event_seq,
                      generation_binds: Callable[[dict, int], bool]) -> SeedReceiptBinding:
    """Bind a recorded copied-base receipt to the `workspace_seeded` row it names.

    `events_by_seq` maps a seq to its event (any mapping holding at least the seed rows; the row's
    own `type` is checked here, so a caller need not pre-filter). `terminal_event_seq` is the node's
    terminal seq for the lifecycle being asked about. `generation_binds` is
    `events/replay_ctx.py::event_generation_binds` (see the module docstring for why it is passed).
    """
    digest = receipt_archive_digest(receipt, node_id=node_id, generation=generation)
    if digest is None:
        return SeedReceiptBinding(receipt, None, RECEIPT_UNUSABLE)
    seq = receipt["seed_event_seq"]
    seed = events_by_seq.get(seq)
    data = getattr(seed, "data", None)
    seeded = data.get("base_revision") if isinstance(data, dict) else None
    if (seed is None or getattr(seed, "type", None) != SEED_EVENT_TYPE
            or getattr(seed, "seq", None) != seq or not isinstance(data, dict)
            or type(terminal_event_seq) is not int or seq >= terminal_event_seq
            or type(data.get("node_id")) is not int or data["node_id"] != node_id
            or not generation_binds(data, generation)
            or seed_archive_digest(seeded) != digest
            or any(seeded[k] != receipt[k] for k in ("file_count", "bytes"))):
        return SeedReceiptBinding(receipt, None, SEED_UNBOUND, seed)
    return SeedReceiptBinding(receipt, digest, None, seed)
