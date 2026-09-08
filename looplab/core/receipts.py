"""The three rules every health RECEIPT shares: what counts as a receipt COUNT, what FIELDS a given
receipt is made of, and how a receipt SURVIVES a projection of the rows it describes.

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


def receipt_field_set(*fields: str) -> tuple[str, ...]:
    """Declare, ONCE, the exact field names one receipt is made of (doc 25 EM-12).

    EM-12's resolution shared the LEAF (`bounded_receipt_count`) and deliberately left each
    receipt's consistency predicate with its receipt, because those are domain logic carrying
    load-bearing comments. What it explicitly left open is the other half of the finding's last
    sentence: **nothing forced a receipt's WRITER and its READER to agree on the FIELD SET.** Both
    ends spelled it out independently — the writer as literal keys in the dict it returns, the
    reader as a local ``keys = (...)`` tuple it checks presence against — so a field added to one
    end is invisible at the other, and the failure is silent in the worst direction: a reader that
    still finds every field it knows about reports a receipt as complete while the writer has
    started emitting something it never reads.

    That is a registry problem, the shape the other duck-typed seams in this repo solve, so the
    answer is a registry: this returns the declaration both ends then CONSUME —
    `receipt_payload` refuses to build a row that is not exactly these fields, and
    `receipt_presence` answers the reader's all-or-nothing presence question from the same tuple.
    Neither end can be changed alone without the other going red.

    Refusals rather than coercion, because a declaration is read once at import and then trusted:
    an empty declaration (a receipt with no fields is not a receipt), a duplicate name (the writer
    would emit one key and the reader would count two), and a non-string or empty name.
    """
    if not fields:
        raise ValueError("a receipt declares at least one field")
    for name in fields:
        if not isinstance(name, str) or not name:
            raise ValueError(f"receipt field names are non-empty strings: {name!r}")
    if len(set(fields)) != len(fields):
        raise ValueError(f"receipt field names are unique: {fields!r}")
    return tuple(fields)


def receipt_payload(fields: tuple[str, ...], values: dict) -> dict:
    """The WRITER's half: *values* as a receipt row, refusing anything but exactly *fields*.

    This is what makes the declaration binding rather than decorative. A writer that grows a field
    without declaring it, or declares one without emitting it, raises HERE — at the moment the row
    is built, in the writer's own process — instead of shipping a durable row whose reader silently
    ignores the new field or reads it as absent.

    Raises ``ValueError`` (not an `OperatorRefusal`): a mismatch is a defect in the code, never a
    fact about the operator's input, and the receipt's callers already fail closed on ValueError
    from their own consistency checks.

    Values are NOT validated here — what a given field may hold is the receipt's own rule
    (`bounded_receipt_count`, a bool, a nested count bounded by another field). Only the SHAPE is.
    """
    missing = [name for name in fields if name not in values]
    extra = [name for name in values if name not in fields]
    if missing or extra:
        raise ValueError(
            "receipt payload does not match its declared field set"
            + (f"; missing {missing}" if missing else "")
            + (f"; undeclared {extra}" if extra else ""))
    return {name: values[name] for name in fields}


def receipt_presence(row: object, fields: tuple[str, ...]) -> str:
    """The READER's half: ``"absent"`` / ``"partial"`` / ``"complete"`` for *fields* in *row*.

    Every additive receipt in this repo reads its presence the same way and for the same reason,
    and each one had written the three-line ``any``/``all`` dance out by hand:

    * ``absent`` — a row written BEFORE the receipt existed. Its observations are still valid; only
      the receipt's question is unanswerable, so callers return their legacy "unknown" default.
    * ``partial`` — some fields but not all. No writer ever produced that, so the row is corrupt or
      forged and the caller fails closed. Accepting it would let a truncated receipt claim
      completeness on the fields that survived.
    * ``complete`` — every declared field is present, and the caller may read them.

    Presence, not validity: ``key in row`` only. Whether the VALUES are sane is the receipt's own
    predicate, which stays at the receipt.
    """
    if not isinstance(row, dict):
        return "absent"
    present = sum(1 for name in fields if name in row)
    if present == 0:
        return "absent"
    return "complete" if present == len(fields) else "partial"


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
