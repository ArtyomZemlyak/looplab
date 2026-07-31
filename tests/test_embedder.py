"""T4 real embeddings (this session, Phase 2). `make_embedder` returns the dependency-free lexical
`hash_embed` by default (byte-identical to before), or an `LLMEmbedder` over an OpenAI-compatible
`/embeddings` endpoint when `embed_model` is set. The invariant that MATTERS: the embedder commits to
ONE vector dimension for its lifetime and degrades to a same-dim `hash_embed` fallback on any
endpoint failure, so `_cosine` never sees a dim mismatch and a flaky/offline endpoint never crashes
a run — it only loses semantic quality. All offline (no real network)."""
from __future__ import annotations

from looplab.core.config import Settings
from looplab.tools.knowledge_tools import KnowledgeTools
from looplab.tools.vectorstore import LLMEmbedder, _cosine, hash_embed, make_embedder


def test_default_is_hash_embed():
    assert make_embedder(Settings()) is hash_embed


def test_unreachable_endpoint_degrades_gracefully():
    s = Settings(embed_model="nomic-embed-text", embed_base_url="http://127.0.0.1:9/v1")  # closed
    emb = make_embedder(s)
    v1, v2 = emb("hello world"), emb("different text")
    assert len(v1) == len(v2) == 64                 # degraded to lexical dim, consistent
    assert _cosine(v1, v2) >= 0.0                    # usable, never raises


def test_live_then_failure_keeps_dimension():
    e = LLMEmbedder("m", "http://x/v1")
    e._call = lambda texts: [[0.1] * 768 for _ in texts]     # pretend a live 768-dim endpoint
    a = e.embed_many(["x", "y"])
    assert len(a) == 2 and len(a[0]) == 768 and e._dim == 768
    e._call = lambda texts: None                             # endpoint now fails
    b = e.embed("z")
    assert len(b) == 768                                     # fallback padded to the committed dim


def test_a_ragged_batch_is_rejected_not_stored_row_by_row():
    """Only row 0's length was checked against the committed dim, so a batch whose rows disagree with
    EACH OTHER passed whole. The off-dim vectors were then stored and scored 0.0 forever in
    `_cosine` — those items became permanently unreachable, with no error anywhere."""
    e = LLMEmbedder("m", "http://x/v1")
    e._call = lambda texts: [[0.1] * 768 for _ in texts]
    e.embed_many(["x"])                                      # commit the dimension
    assert e._dim == 768

    e._call = lambda texts: [[0.1] * 768, [0.1] * 512]       # row 0 fine, row 1 ragged
    out = e.embed_many(["x", "y"])
    assert {len(v) for v in out} == {768}, [len(v) for v in out]


def test_first_call_failure_commits_to_lexical():
    e = LLMEmbedder("m", "http://x/v1", dim_fallback=32)
    e._call = lambda texts: None                             # never works
    v = e.embed("anything")
    assert len(v) == 32 and e._live is False                 # sticky degrade, no repeated attempts


def test_an_endpoint_that_dies_after_a_success_trips_the_breaker_instead_of_stalling_forever():
    """`_live` used to pin only the FIRST call's outcome, so an endpoint that died after one success
    stayed "live" and every later embed paid the full 30s timeout before falling back — a memory
    rebuild of hundreds of notes stalled ~30s per note."""
    e = LLMEmbedder("m", "http://x/v1")
    attempts = []

    def _dead(texts):
        attempts.append(len(texts))
        return None

    e._call = lambda texts: [[0.1] * 8 for _ in texts]
    e.embed("warm")                                          # one success -> _live is True
    assert e._live is True

    e._call = _dead
    for _ in range(12):
        assert len(e.embed("z")) == 8                        # still answers, padded to the live dim
    assert len(attempts) <= 5, (
        f"the dead endpoint was called {len(attempts)} times over 12 embeds — the breaker never "
        "tripped, so every embed pays the full network timeout")
    assert e._live is False


def test_the_breaker_counts_CONSECUTIVE_failures_not_lifetime_ones():
    """A single blip must not cost the whole run its semantic retrieval."""
    e = LLMEmbedder("m", "http://x/v1")
    live = [[0.1] * 8]
    calls = []

    def _flaky(texts):
        calls.append(1)
        return None if len(calls) % 2 else live * len(texts)   # fail, ok, fail, ok, ...

    e._call = lambda texts: live * len(texts)
    e.embed("warm")                                           # the FIRST-call rule is separate
    e._call = _flaky
    for _ in range(12):
        e.embed("z")
    assert e._live is True and len(calls) == 12               # never tripped: no 3 in a row


def test_knowledge_tools_build_and_query_share_one_embedder(tmp_path):
    (tmp_path / "cv.md").write_text(
        "Cross validation: use k-fold to estimate generalization error.", encoding="utf-8")
    (tmp_path / "poly.md").write_text(
        "Polynomial regression: choose the degree and ridge lambda.", encoding="utf-8")

    class Fake:                                              # deterministic fixed-dim semantic stub
        dim = 8

        def __call__(self, t):
            import hashlib
            v = [0.0] * self.dim
            for w in t.lower().split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            return v

    kt = KnowledgeTools(str(tmp_path), embed=Fake())
    out = kt.execute("kb_search", {"query": "k-fold cross validation"})
    assert "cv.md" in out                                    # consistent dim -> real ranking, not all-zero
