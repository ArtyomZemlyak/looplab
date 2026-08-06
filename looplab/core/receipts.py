"""The two rules every health RECEIPT shares: what counts as a receipt COUNT, and how a receipt
SURVIVES a projection of the rows it describes.

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


class ReceiptRows(list):
    """A list of rows whose PROJECTIONS carry the health receipts describing where the rows came from.

    Doc 25 EM-09 found three independent spellings of this shape — ``_ClaimSourceRows`` and
    ``_ClaimAssessmentRows`` (`engine/claims_health.py`) and ``_CapsuleRows``
    (`engine/concept_capsules.py`) — each re-deriving "copy the rows, carry the receipt" in its own
    filter helper. Only the CARRYING is shared here, and deliberately only that:

    * What each receipt MEANS, and what its consistency rules are, stays with the receipt. Those are
      three different vocabularies (`read_health` / `claim_source`+`research_source` /
      `source_health`) validated by three different sanitizers.
    * What an ABSENT receipt defaults to stays with the subclass, because the three genuinely
      disagree and that disagreement is load-bearing. Measured on the shipped code:
      ``_ClaimSourceRows()`` defaults to a complete-and-empty receipt, ``_CapsuleRows()`` to
      ``source_store_complete=True``, and ``_ClaimAssessmentRows()`` to ``None`` — which is not a
      default receipt at all but a third state meaning "no aggregate was carried, fall back to the
      per-row copies" (`claims_retrieval._claim_claim_source_summary`), whose own fallback is
      fail-CLOSED. Collapsing those onto one default would turn one of "we could not read the
      store" / "the store is complete" / "look at the rows" into another.
    * How a receipt is INHERITED when rows arrive as a plain list also stays with the subclass:
      ``_claim_source_rows`` re-validates and conservatively ADDS newly visible schema failures to a
      carried receipt, ``_dedup_valid_capsules`` takes the per-field max against the duplicates it
      finds itself. Neither is a copy.

    What IS shared is the hazard the finding named: a plain list operation over one of these silently
    produces a bare ``list``, and every consumer's ``getattr(rows, "<receipt>", None)`` probe then
    reads absence — i.e. "we could not read the store" quietly becomes "the store is complete".
    ``filter``/``map``/slicing below are the receipt-preserving spellings of the three operations
    that used to drop it, and ``__slots__`` closes the other half of the finding: an attribute this
    class does not declare can no longer be stashed on an instance at all, so a caller who needs to
    attach evidence to a snapshot has to declare a field that projections then carry.

    ``sorted()`` is deliberately NOT given a receipt-preserving sibling: no caller sorts one of these
    snapshots (they sort the plain list they are built from, before wrapping), and an unused
    projection API is one more thing to keep honest for no reader.
    """

    __slots__ = ()

    #: Every attribute an instance carries — the registry `filter`/`map`/slicing project through,
    #: and which `tests/test_receipt_rows.py` pins two-way against `__slots__` and against the
    #: assignments in `__init__`. A field missing from here is silently dropped by every projection,
    #: which is exactly the class of defect this base exists to remove; each name must therefore also
    #: be a keyword parameter of the subclass's `__init__`, because that is how a projection rebuilds.
    CARRIED_FIELDS: tuple[str, ...] = ()

    def carried(self) -> dict:
        """The receipts this snapshot carries, keyed as the constructor takes them."""
        return {name: getattr(self, name) for name in self.CARRIED_FIELDS}

    def filter(self, predicate) -> "ReceiptRows":
        """Rows matching *predicate*, still carrying every receipt. Filtering NARROWS the view, it
        never repairs the source — a scoped query over a quarantined store is still quarantined."""
        return type(self)((row for row in self if predicate(row)), **self.carried())

    def map(self, project) -> "ReceiptRows":
        """Each row through *project*, still carrying every receipt. Same rule as `filter`: a
        row-shape projection cannot make an incompletely-read source complete."""
        return type(self)((project(row) for row in self), **self.carried())

    def __getitem__(self, key):
        # A slice of a list subclass returns a bare `list`, which is the silent-drop the whole class
        # exists to prevent — and a slice is how display caps are spelled, so the receipt is exactly
        # what the capped view still needs. An integer index returns the row, unchanged.
        if isinstance(key, slice):
            return type(self)(list.__getitem__(self, key), **self.carried())
        return list.__getitem__(self, key)
