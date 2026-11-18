# Atomic raw proposal publication

RESULT 2026-11-11 — isolated source acceptance; no deployment claim.

A prepared raw proposal previously committed `card_added` and then published its buffered
novelty/governance events separately. A crash between them could expose a selectable Card without
the audit that accompanied that proposal. The stager now commits a new Card and its audit in one
bounded, tail-fenced `EventStore.append_many` envelope. The existing event types, contiguous
sequences and each producer's trace/span are preserved. No second event journal is introduced.
Other staging lanes already publish their audit before publishing a Card. An existing-Card reuse
records the repeated proposal without minting another Card; an attach remains a handoff, and stale
proposals retain their existing refusal policy. Historical logs are not rewritten or claimed repaired.

`tests/test_raw_stage_audit_atomic.py` drives the real raw-stage consumer and stager, reconstructs
every physical byte prefix of its new batch, and proves that a partial batch exposes no Card or
partial audit. A complete batch exposes one selection-ready Card with both audit events and their
original spans. Additional cases inject a competing writer at the tail CAS, a write failure before
commit, a lost response after commit, and malformed per-record contexts. The existing append
race/critical-section, retry/attach, stale-refusal and proposal publication tests remain required.
