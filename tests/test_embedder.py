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


def test_first_call_failure_commits_to_lexical():
    e = LLMEmbedder("m", "http://x/v1", dim_fallback=32)
    e._call = lambda texts: None                             # never works
    v = e.embed("anything")
    assert len(v) == 32 and e._live is False                 # sticky degrade, no repeated attempts


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
