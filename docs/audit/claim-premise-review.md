# Explicit premises and authored refutation reviews

The historical `claim-refutation-undercut` proposal conflated two different edges. A Card's
`parent_card_id` files an experiment under a research direction; it does not assert a logical
premise. A `tested` verdict means evaluated without improvement, not refuted. The September count
of 17 directions with evidenced children established neither logical dependency nor refutation.
Those incident records and the refused one-Card trigger remain in BACKLOG; no historical result is
relabelled and no unsuccessful child automatically refutes its direction or siblings.

The new optional `claim_reviewed` native command records explicit logical premise IDs and an authored
interpretation of the subject Card's own usable measured evidence. An assessment may be `refuted` or
`inconclusive`, or null to withdraw it. It requires exact own-evidence node IDs, finite evaluated
outcomes and current attempts, excluding substituted, aborted and trust-excluded evidence. Failed
runs, absence of improvement and unrelated children cannot substitute for these receipts. The
server binds immutable seed identity and native metric/source/protocol/terminal/attempt stamps; it
does not prove the author's scientific interpretation, data availability or protocol compatibility.

Each review explicitly replaces the subject's whole declaration: at most 4 logical premises and 16
evidence nodes, short 512-character reason, bounded 16 KiB native review. Empty premises remove edges;
null assessment withdraws the interpretation. Explicit cycles and borrowed evidence are refused.
The existing generation, command idempotency and strict-tail CAS path checks exact prior review,
claim identities and evidence again before publication. A reset, source/metric/provenance or objective
change makes the old assessment stale. Unsupported latest records are unknown, never fallback to an
old refutation. Original native events remain the historical evidence.

Only a direct, explicitly declared logical premise with a current authored `refuted` assessment
produces an `undercut` explanation notice. It says to reassess the dependent explanation: that child
may be true for a different reason. It changes no child measurement, verdict, research filing,
champion, rank, selection readiness, budget, notification or source/GPU/Root approval gate. An
undercut child is not itself refuted and cannot transmit a fabricated refutation to its descendants.
Missing/stale/unsupported premise assessments remain explicitly visible in `premise_status`.

The bounded public Card field, Researcher board context and read-only UI notice use the same native
projection. The notice separates authorship from proof and adds no action or approval control. Old
logs omit the optional field and preserve exact model/public shapes and existing replay goldens.
If the public Card budget omits a field, its existing projection receipt reports that loss; use the
review event sequence with the generation-bound log pager to read the original bounded record.

Acceptance covers public command and strict-tail CAS races, actual native CPU evaluations,
idempotency, process restart and incremental/full replay parity. Negative cases cover self/cyclic
edges, borrowed/no measurement, bool IDs, malformed and future records, reset/retarget staleness and
explicit withdrawal. UI checks render interpretation and unknown state, escape text and perform no
HTTP work. This is isolated source acceptance, not deployment or evidence that authored reviews are
scientifically correct or improve research utility. Existing scientific/workflow measurement bounds
remain open.
