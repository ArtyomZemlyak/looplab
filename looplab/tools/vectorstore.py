"""Pluggable vector store (I17, ADR-16). `VectorStore` is the seam; the ONLY implementation shipped
today is the dependency-free `InMemoryVectorStore` (brute-force cosine, no persistence — it
re-embeds on each rebuild). A persistent backend (LanceDB/Qdrant) is a documented FUTURE seam (see
the deferred-infra notes in the design docs); there is no LanceDB store or store-selection Settings
field yet, so "swap it in" is not a config change today.

`hash_embed` is a deterministic (hashlib-based) bag-of-words embedder for offline tests;
`LLMEmbedder` calls an OpenAI-compatible `/embeddings` endpoint directly (for example
Ollama with `nomic-embed-text`).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

Vector = list[float]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Credentialed embedding requests never follow a redirect to another authority."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirectHandler())
_TRUST_ENV_OPENER = urllib.request.build_opener(_NoRedirectHandler())


@dataclass
class Item:
    id: str
    vector: Vector
    payload: dict = field(default_factory=dict)


@dataclass
class Hit:
    id: str
    score: float
    payload: dict


class VectorStore(Protocol):
    """The two methods the seam actually requires of a backend.

    `delete` and `rebuild` were declared here too and had no production caller anywhere (doc 25
    TO-10). On a Protocol that is not merely dead code: the point of the seam is to state what a
    LanceDB/Qdrant backend must implement, and speculative methods make that contract wrong in both
    directions — a real backend is asked for machinery nothing calls, while the shapes a persistent
    store genuinely needs (a durable open/close, index-level compaction) are absent because nobody
    has written one yet. `InMemoryVectorStore` keeps its own `delete`/`rebuild` below; they are that
    class's API, not a promise every backend must keep.
    """

    def upsert(self, index: str, items: list[Item]) -> None: ...
    def search(self, index: str, query: Vector, k: int) -> list[Hit]: ...


def hash_embed(text: str, dim: int = 64) -> Vector:
    v = [0.0] * dim
    for tok in text.lower().split():
        # md5 as a cheap token -> bucket function, not an identity (doc 25 CO-08). It must be stable
        # ACROSS PROCESSES — Python's built-in `hash()` is salted per interpreter, so a persisted index
        # built in one process would not match a query embedded in the next.
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        v[h % dim] += 1.0
    return v


class LLMEmbedder:
    """Real semantic embeddings over any OpenAI-compatible `/embeddings` endpoint (Ollama
    `nomic-embed-text`, vLLM/SGLang, OpenAI…), replacing the lexical `hash_embed` bag-of-words when a
    model is configured. Dependency-free (stdlib urllib, so it uses the same proxy/CA env the chat
    client does). **Robust by construction:** it commits to ONE vector dimension for its lifetime, so
    `_cosine` never sees a dim mismatch — if a call fails (endpoint down / offline box), it returns a
    `hash_embed` fallback PADDED to that same dimension, and if the very first call fails it degrades
    to pure `hash_embed` at `dim_fallback`. So retrieval is never crashed by a flaky endpoint; it just
    quietly loses semantic quality. A single embedder instance must build AND query one index (same
    dim).

    **IT CARRIES A `CostAccountant`, and until 2026-09-02 it did not.** This posts to a paid
    endpoint with a bearer key, and `engine/costs.py::_CHILD_ATTRS` already walks `embed` — its own
    comment says both the abstractor and the embedder are "LIVE chat/embedding clients under the
    shipped defaults, each with its own `CostAccountant`". The abstractor had one; this did not, so
    the walk reached the object, found no `accountant`, and every embed call was spent and unbilled
    — invisible to the durable `llm_usage` ledger that `looplab tokens` reconciles against. The
    knowledge index re-embeds whenever the case store is appended to, so the residual is not small.
    Giving it the attribute is the whole wiring: no call site changes, because the walk was already
    looking for it.

    WHAT IS BILLED IS THE PROVIDER CALL, not a usable answer. `add` runs on any response that came
    back and parsed as JSON, before the body is validated — a batch whose rows disagree on dimension
    is discarded HERE and was still charged THERE, and `CostAccountant.add` already states this rule
    for the chat path ("a successful response with missing/malformed usage still increments it
    once"). A transport failure raises and is not billed; neither is the `hash_embed` fallback,
    which spends nothing. An endpoint that reports no `usage.cost` — most embedding endpoints —
    lands as an UNPRICED call, which `cost_is_reported` exists to keep distinguishable from free.
    """

    # Consecutive `_call` failures tolerated before this embedder stops trying the endpoint at all.
    # >1 so a single blip doesn't cost the whole run its semantic retrieval; small enough that a dead
    # endpoint costs a bounded number of `timeout`-length stalls rather than one per embed.
    _BREAKER_MISSES = 3

    def __init__(self, model: str, base_url: str = "http://localhost:11434/v1",
                 api_key: str = "x", timeout: float = 30.0, dim_fallback: int = 64,
                 trust_env: bool = False, accountant=None):
        # Imported here rather than at module scope for the reason `make_embedder` gives below: this
        # module is the dependency-free half of retrieval and is imported by offline paths that must
        # not pull the LLM client in. A failure to import it is not a reason to refuse to embed —
        # it costs the BILLING, not the retrieval — so the attribute degrades to None and the
        # accounting walk finds nothing, which is exactly the state this fixed.
        if accountant is None:
            try:
                from looplab.core.llm import CostAccountant
                accountant = CostAccountant()
            except Exception:  # noqa: BLE001 - telemetry construction never breaks retrieval
                accountant = None
        self.accountant = accountant
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or "x"
        self.timeout = timeout
        self.dim_fallback = dim_fallback
        self._opener = _TRUST_ENV_OPENER if trust_env else _DIRECT_OPENER
        self._dim: Optional[int] = None     # committed on first success (or first fallback)
        self._live: Optional[bool] = None    # None=untried, True=endpoint works, False=degraded to hash
        self._misses = 0                     # CONSECUTIVE `_call` failures; reset by any success

    def _call(self, texts: list[str]) -> Optional[list[Vector]]:
        """One batched POST /embeddings. Returns per-text vectors, or None on ANY failure (network,
        HTTP, bad body) so the caller degrades gracefully instead of crashing the run."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/embeddings",
                data=json.dumps({"model": self.model, "input": texts}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"},
            )
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                json.JSONDecodeError, http.client.HTTPException):
            # HTTPException covers IncompleteRead/BadStatusLine — a server dying mid-response
            # must degrade to the hash fallback, not crash role construction.
            return None
        # BILLED BEFORE THE BODY IS VALIDATED. Every `return None` below discards an answer the
        # provider already produced and charged for; billing at the bottom would make a malformed
        # batch read as a call that never happened. Same rule `CostAccountant.add` states for chat.
        self._bill(body)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            return None
        vecs: list[Vector] = []
        try:
            for row in data:
                emb = row.get("embedding") if isinstance(row, dict) else None
                if not isinstance(emb, list) or not emb:
                    return None
                vecs.append([float(x) for x in emb])
        except (TypeError, ValueError):    # non-numeric entries in a malformed body
            return None
        return vecs

    def _bill(self, body) -> None:
        """Commit one provider call to the accountant. Never raises.

        An embeddings response carries `usage.prompt_tokens`/`total_tokens` and no completion half;
        a gateway may add `cost`. Absent or malformed fields are simply not forwarded — `add` still
        counts the CALL, which is what makes an unpriced provider distinguishable from a free one
        (`core/llm.py::cost_is_reported`) instead of rolling up as zero spend.
        """
        accountant = getattr(self, "accountant", None)
        if accountant is None:
            return
        usage = body.get("usage") if isinstance(body, dict) else None
        payload: dict = {}
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
                value = usage.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    payload[key] = value
        try:
            accountant.add(payload.get("cost"), usage=payload or None)
        except Exception:  # noqa: BLE001 - the call already succeeded; telemetry never breaks it
            pass

    def _fallback(self, texts: list[str]) -> list[Vector]:
        d = self._dim or self.dim_fallback
        self._dim = d
        return [hash_embed(t, dim=d) for t in texts]

    def embed_many(self, texts: list[str]) -> list[Vector]:
        if not texts:
            return []
        if self._live is not False:                      # untried or known-live -> try the endpoint
            vecs = self._call(texts)
            if vecs is not None:
                self._live = True
                self._misses = 0
                # EVERY row's length, not just vecs[0]'s. A batch whose rows disagree with each
                # OTHER (row 0 at the committed dim, a later row at another) used to pass this guard
                # whole; the off-dim vectors were stored and then scored 0.0 forever in `_cosine`,
                # making those items permanently unreachable with no error anywhere. The docstring
                # promises `_cosine` never sees a mismatch — this is what makes that true.
                dim = len(vecs[0])
                if self._dim is None:
                    self._dim = dim
                if dim == self._dim and all(len(v) == dim for v in vecs):
                    return vecs
            else:
                # A breaker, not just a first-call verdict. `_live` used to pin only the FIRST call's
                # outcome, so an endpoint that died after one success left `_live` True forever and
                # every later embed paid the full `timeout` (30s by default) before falling back — a
                # memory rebuild embedding hundreds of notes one at a time stalled 30s per note.
                # Degrading is permanent on purpose, same as the first-call rule it generalizes:
                # mixing real and hash vectors in ONE index makes their cosines meaningless, so once
                # the endpoint has proven unreliable, consistency beats a half-populated index.
                self._misses += 1
                if self._live is None or self._misses >= self._BREAKER_MISSES:
                    self._live = False
        return self._fallback(texts)

    def embed(self, text: str) -> Vector:
        return self.embed_many([text])[0]

    def __call__(self, text: str) -> Vector:             # so it drops in wherever `hash_embed` is used
        return self.embed(text)


def make_embedder(settings) -> Callable[[str], Vector]:
    """Return a text→vector callable from config: `hash_embed` (zero-dep default, dim 64) when no
    `embed_model` is set — byte-identical to prior behavior — else an `LLMEmbedder` over the
    configured endpoint (falling back to `embed_base_url` or the shared `llm_base_url`). Never raises:
    a misconfigured/offline endpoint degrades to `hash_embed` at call time (see `LLMEmbedder`)."""
    # An embeddings endpoint is very often a DIFFERENT provider from the chat model, which is why the
    # whole connection is resolved as one: taking only the profile's KEY while keeping the endpoint
    # from `embed_base_url`/`llm_base_url` put one provider's live secret in an Authorization header
    # to another provider's host. `resolve_llm_target` binds the credential to the endpoint it came
    # with, so reading model/base_url/key off a single target cannot re-open that.
    try:
        from looplab.core.llm import (
            bound_api_key_for, client_kwargs_for, normalize_llm_base_url,
            resolve_llm_target, role_profile,
        )
        target = resolve_llm_target(settings, role="embed")
        embed_profile = role_profile(settings, "embed")
        profile_model = embed_profile.get("model")
    except Exception:  # noqa: BLE001 — invalid connection state degrades without touching a network
        return hash_embed
    # The gate stays "is an embedding model configured at all" — the resolved model can't serve as
    # one, since it falls back to the shared chat model. A profile bound to `embed` counts, so a
    # complete connection expressed only as a profile no longer silently leaves retrieval on
    # `hash_embed`; blank everywhere still means the zero-dependency lexical default.
    model = getattr(settings, "embed_model", None) or profile_model
    if not model:
        return hash_embed
    # A role/stage endpoint override deliberately drops a profile credential. Unlike a required chat
    # client, embeddings are optional, so fail closed to local hashing instead of probing the override
    # unauthenticated or aborting the run.
    if target.credential_mode == "none" and embed_profile.get("api_key_env"):
        return hash_embed
    try:
        kwargs = client_kwargs_for(target, role="embed")
        base = normalize_llm_base_url(target.base_url)
        key = bound_api_key_for(
            settings, base, api_key=kwargs.get("api_key"),
            api_key_base_url=kwargs.get("api_key_base_url"))
    except Exception:  # noqa: BLE001 — missing/mismatched credentials fail closed to lexical search
        return hash_embed
    return LLMEmbedder(
        model, base_url=base, api_key=key,
        trust_env=bool(getattr(settings, "llm_trust_env", False)))


def cosine(a: Vector, b: Vector) -> float:
    if len(a) != len(b):
        # Mismatched dims (e.g. a store populated with hash_embed=64 then queried with a 768-dim
        # LLM embedder) would silently rank on the truncated overlap; refuse instead.
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# Back-compat alias for pre-rename importers (orchestrator, tests).
_cosine = cosine


class InMemoryVectorStore:
    """Default offline impl. Brute-force cosine; fine at LoopLab's scale (hundreds–
    low-thousands of notes, per ADR-16's benchmark analysis)."""

    def __init__(self) -> None:
        self._idx: dict[str, dict[str, Item]] = {}
        # The index dicts are shared mutable state, and the engine runs concurrent-research and
        # llm_parallel WORKER THREADS. `search` used to iterate `store.values()` unguarded, so a
        # concurrent `upsert`/`delete` raised RuntimeError("dictionary changed size during
        # iteration"). Held only around the cheap dict ops — the (relatively slow) cosine scan below
        # runs OUTSIDE it, over a snapshot, so a search can never block a writer.
        self._lock = threading.Lock()

    def upsert(self, index: str, items: list[Item]) -> None:
        with self._lock:
            store = self._idx.setdefault(index, {})
            for it in items:
                store[it.id] = it

    def search(self, index: str, query: Vector, k: int) -> list[Hit]:
        with self._lock:
            items = list((self._idx.get(index) or {}).values())
        # Drop non-positive scores: `cosine` returns 0.0 on a DIMENSION MISMATCH (a query embedded at a
        # different dim than the stored vectors — e.g. the embedding endpoint died mid-run and queries
        # fell back to hash_embed), so without this filter search would return k ARBITRARY notes at
        # score 0 tie-broken by id, presented to the model as relevant. An orthogonal (0-similarity)
        # hit is likewise not a real match.
        hits = [h for h in (Hit(it.id, cosine(query, it.vector), it.payload)
                            for it in items) if h.score > 0.0]
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits[:k]

    def get(self, index: str, id: str) -> Optional[Hit]:
        with self._lock:
            it = (self._idx.get(index) or {}).get(id)
        return Hit(it.id, 1.0, it.payload) if it else None

    def delete(self, index: str, ids: list[str]) -> None:
        with self._lock:
            store = self._idx.get(index, {})
            for i in ids:
                store.pop(i, None)

    def rebuild(self, index: str) -> None:
        # In-memory store has no derived state to rebuild; LanceDB re-derives from
        # canonical knowledge/*.md here.
        with self._lock:
            self._idx.setdefault(index, {})
