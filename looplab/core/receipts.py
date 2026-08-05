"""What counts as a RECEIPT COUNT — the one bounded-integer rule the receipt validators share.

Doc 25 EM-12 found ~8 hand-rolled receipt validators repeating the same idioms. Most of what they
repeat is not shareable: each receipt's consistency predicate (``total == retained + omitted``,
``complete == (omitted == 0)``, and `concept_steward`'s two-axis source rule) is domain logic with
load-bearing comments, and folding those into a generic spec table would hide the part a reader
actually needs. What IS shareable is the leaf: the guard on a single count field.

That guard had DIVERGED, which is the reason this module exists rather than a comment:

* `claims_health` and `memory` spell it ``type(value) is int`` — which rejects every ``int``
  subclass, ``bool`` among them.
* `concept_steward` spells it ``isinstance(value, int) and not isinstance(value, bool)`` — which
  rejects ``bool`` specifically and ACCEPTS any other ``int`` subclass.

The two agree on everything JSON can produce, so no shipped log distinguished them; they disagree on
an in-process ``class Count(int)``. One canonical rule now answers it, and the STRICT spelling wins
for the same reason the fold uses it on untrusted event data: a receipt is durable evidence, an
``int`` subclass can override ``__eq__``/``__le__``, and a bound that a value can talk its way past
is not a bound. Nothing in the repo constructs receipt counts as a subclass, so this tightens
`concept_steward` without changing any behaviour a real caller can reach.

`bool` is called out separately in the docstring below because it is the trap next door:
``isinstance(True, int)`` is ``True``, so a receipt reading ``{"rows_total": true}`` passes any guard
built from ``isinstance`` alone and then arithmetics as ``1``.
"""
from __future__ import annotations


def bounded_receipt_count(value: object, maximum: int) -> bool:
    """Whether *value* is an exact non-negative ``int`` no greater than *maximum*.

    Exact means ``type(value) is int``: not ``bool`` (``isinstance(True, int)`` is ``True``, and a
    receipt whose count is ``true`` would arithmetic as ``1``), and not an ``int`` subclass, which
    could override the comparisons this bound is expressed in.

    Returns a bool rather than a normalized value because the callers differ on what to do next —
    some return ``None`` for the whole receipt, some substitute ``0`` and record
    ``receipt_known=False``. That decision belongs to the receipt, not to its leaf guard.
    """
    return type(value) is int and 0 <= value <= maximum
