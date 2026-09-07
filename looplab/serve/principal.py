"""WHO IS ASKING — the request principal, and the one portfolio-visibility decision (doc 52 row 29).

The server has two credentials and, until 2026-09-06, no NAME for the party holding one: the owner
token (`owner_token.py`, a per-DEPLOYMENT credential) and a review capability (`reviews.py`, one run,
read-only). `serve/assistant.py` mounted the cross-run providers — the portfolio-wide concept,
claims and atlas READS, and the taxonomy governance verbs — on `cross_run_enabled`, a boolean
computed from `Settings` alone: one process-wide feature flag standing where an authorization
decision belongs, so every caller that reached the owner Assistant received the same unbound
portfolio (its own note called this the multi-user gap on the shared hub).

This module gives the party a name and moves the decision to it:

  * `Principal` is stamped on `request.state` by the auth middlewares in `server.py` — `owner` for a
    request that presented the owner token, `local` for the historical unauthenticated single-user
    plane (no token resolved), `review` for a review capability (with its link id), `anonymous`
    for a request that presented nothing on the small open surface;
  * `portfolio_access(principal, settings)` is the ONE decision that mounts the portfolio providers:
    the storage (`memory_dir`) and the switch (`cross_run_read_tools`) are necessary, and the
    principal is the third clause — a `review` or `anonymous` principal never reads the portfolio,
    whatever the flag says. Evaluated PER TURN from the principal the route captured, and pinned
    on a standing watch at arming so a wake-up runs as the party that armed it;
  * a caller that passes no principal is `anonymous` — fail closed. A missing identity is not an
    owner.

WHAT THIS IS NOT. The owner token is a per-deployment credential, not per-user identity or RBAC
(`server.py` says so beside its middleware), so two people sharing one hub and one token are still
one `owner` principal here. The change is structural: the decision is now a function of the
request's authenticated party, which is the seam a per-user identity plugs into, instead of a
process-wide constant no identity could ever reach.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

OWNER = "owner"
LOCAL = "local"
REVIEW = "review"
ANONYMOUS = "anonymous"
KINDS = (OWNER, LOCAL, REVIEW, ANONYMOUS)

# `request.state` attribute the middlewares write and `request_principal` reads.
STATE_ATTR = "principal"


@dataclass(frozen=True)
class Principal:
    """The authenticated party behind one request (or one pinned standing watch)."""

    kind: str
    review_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"principal kind must be one of {KINDS}, not {self.kind!r}")
        if self.kind != REVIEW and self.review_id is not None:
            raise ValueError("only a review principal carries a review id")

    @property
    def may_read_portfolio(self) -> bool:
        """The owner plane — the token holder, or the local single-user plane without a token."""
        return self.kind in (OWNER, LOCAL)


OWNER_PRINCIPAL = Principal(OWNER)
LOCAL_PRINCIPAL = Principal(LOCAL)
ANONYMOUS_PRINCIPAL = Principal(ANONYMOUS)


def review_principal(record) -> Principal:
    """The principal a resolved review capability record names."""
    link = record.get("id") if isinstance(record, dict) else None
    return Principal(REVIEW, str(link) if link else "")


def stamp(request, principal: Principal) -> None:
    setattr(request.state, STATE_ATTR, principal)


def request_principal(request) -> Principal:
    """The principal a middleware stamped, or `anonymous` when none did (fail closed)."""
    try:
        value = getattr(request.state, STATE_ATTR)
    except AttributeError:
        return ANONYMOUS_PRINCIPAL
    return value if isinstance(value, Principal) else ANONYMOUS_PRINCIPAL


def coerce(value) -> Principal:
    """A `Principal`, a kind string (as a watch record pins it), or nothing -> a Principal."""
    if isinstance(value, Principal):
        return value
    if isinstance(value, str) and value in KINDS and value != REVIEW:
        return Principal(value)
    return ANONYMOUS_PRINCIPAL


def portfolio_access(principal, settings) -> tuple[bool, str]:
    """`(allowed, why)`: may this principal read the cross-run portfolio under these settings?

    Three clauses, each stated in `why` when it refuses: the storage must exist (`memory_dir`),
    the operator's switch must be on (`cross_run_read_tools`), and the principal must be on the
    owner plane. The first two are the historical gate; the third is what makes the answer a
    property of the caller rather than of the process.
    """
    mdir = getattr(settings, "memory_dir", None) if settings is not None else None
    if not mdir:
        return False, "no memory_dir is configured"
    if not getattr(settings, "cross_run_read_tools", False):
        return False, "cross_run_read_tools is off"
    party = coerce(principal)
    if not party.may_read_portfolio:
        return False, f"a {party.kind} principal may not read the portfolio"
    return True, f"the {party.kind} principal may read the portfolio"
