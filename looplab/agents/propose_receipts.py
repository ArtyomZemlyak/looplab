"""THE RESEARCHER'S PROPOSE RECEIPT, BY CALL (doc 69 69.37): the scope a caller opens and a propose
notes its receipt into — read by the engine's proposal sites (`engine/node_build.py`,
`engine/novelty.py`), noted by `agents/agent.py` and the foresight panel. Apart from
`agents/roles.py`, which is the role CONTRACTS and the LLM roles on a size budget that asks where new
lines belong (`tests/test_role_module_split.py`)."""
from __future__ import annotations

import contextlib
import contextvars

from looplab.agents.roles import researcher_budget_exhausted

# THE RECEIPT'S CALL CHANNEL (doc 69 69.37). The role's two receipt attributes (read by
# `agents/roles.py::researcher_budget_exhausted`) live on a SHARED instance — the card lane and the
# offloaded serial build propose through one Researcher, and the foresight panel through one
# panel — so a caller that read them after its propose returned could read the
# OTHER call's receipt, written between the return and the read (critic crit_v53 N6). A propose
# notes its receipt into the scope its CALLER opened (`propose_receipt_scope`); the scope is a
# `ContextVar`, so a call on another thread or task notes into its own scope, never this one's.
_PROPOSE_RECEIPTS: contextvars.ContextVar = contextvars.ContextVar(
    "looplab_propose_receipts", default=None)


@contextlib.contextmanager
def propose_receipt_scope():
    """The receipts the proposes made INSIDE this block noted, in order — read with
    `scoped_budget_exhausted` once the block has returned."""
    box: list = []
    token = _PROPOSE_RECEIPTS.set(box)
    try:
        yield box
    finally:
        _PROPOSE_RECEIPTS.reset(token)


def note_propose_receipt(bound) -> None:
    """Which bound ended the propose that is returning — "" when the model emitted on its own terms —
    into the scope this call runs in. Outside any scope: nothing. A role that proposes several times
    for ONE answer (the foresight panel) notes the chosen candidate's receipt LAST."""
    box = _PROPOSE_RECEIPTS.get()
    if box is not None:
        box.append(str(bound or "").strip()[:32])


def scoped_budget_exhausted(box, researcher) -> str:
    """The receipt of the propose a `propose_receipt_scope` wrapped: the last one noted inside it.
    Only when nothing inside it noted one does it read the role's attributes
    (`researcher_budget_exhausted`) — a researcher that writes those alone."""
    if box:
        return box[-1]
    return researcher_budget_exhausted(researcher)
