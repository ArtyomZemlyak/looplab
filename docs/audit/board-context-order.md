# Goal and operator context in the fallback board window

**RESULT — isolated source implementation and CPU acceptance, 12 November 2026.**
No live proposal, model call, native command, training or runtime was changed.

The historical raw-lane window had no `_hyp_order`, so attempt rotation alone chose which
questions the proposer could see. A relevant question beyond the twenty-card/20k-character window
could remain invisible even when its topic was explicitly in the goal or operator direction.

`agents/board_context.py` supplies a deterministic lexical fallback to the existing shared
`next_board_prompt_cards`. It considers only the immutable seed statement actually shown for exact
hypothesis/Card binding. It uses Unicode NFKC/casefold words without translation, stemming, model
aliases or inferred axis equivalence. Matching term rarity downweights generic terms, repeated
words do not multiply scores, newest current operator text has precedence, then the goal. Context
is bounded to the latest six operator directions and 2000 characters per text. The canonical
operator filter excludes model-written deep-research advisories and retired directions.

An explicit predicted order retains the original payoff-order and tail-rotation behavior. With no
lexical signal, the original insertion rotation remains. No new setting, event type, paid ranking
call or selection authority is introduced. This advisory heuristic does not interpret negation,
prove semantic relevance, choose a model or predict experiment payoff. The full goal/directive
prompt and every source/protocol/admission guard still apply.

## Coverage and exact binding

Context leaders must not consume the entire seed budget forever. Before they are packed, one
eligible whole-card anchor is reserved by stable-ID rotation. Selected rows are displayed in context
order afterwards. On an unchanged eligible population/context, each card owns this anchor once per
full attempt cycle, including cards with no lexical match. This is not a guarantee under arbitrary
board churn, new goals or indefinitely restarted proposal counters. Seeds above4000 characters or
rows whose seed plus substitution clause cannot fit are excluded explicitly. No text is truncated,
no oversized row steals the anchor, and the existing twenty-card/20k-character limits remain.
Cards already being built remain excluded before any ranking, and the native distinct-belief
projection still owns deduplication. No Card status, score, source or evidence is changed.

The same helper feeds plain Researcher, tool-using Researcher, foresight fallback and board context.
Published per-call windows and exact seed/Card-ID binding are unchanged; foresight alternatives
continue the original proposal window rather than advancing the rotation again. Given the same
folded state and attempt, the helper produces the same window after replay. Contextual ties also
stay stable across dictionary insertion order.

## Behavioral acceptance

The regression constructs thirty genuine native Card event payloads and replays them. A relevant
Qwen question at index29 is first in the actual plain Researcher prompt, and a scripted non-paid
client's answer binds to its exact Card. Disabling only the new context ranking reproduces the old
attempt0 window: the question is absent and the same unseen Card claim is correctly nulled by the
existing binder. The native journal is byte-identical before/after both calls.

Additional cases cover operator recency, model/retired hint filtering, Unicode/casefold, immutable
seed versus display edits, explicit predicted order, no-signal fallback, full-cycle whole-card
coverage against long relevant leaders, oversized seeds/substitution clauses, in-flight exclusion
and replay/tie ordering. Existing actual Engine/foresight/brief tests retain their guards. This is
source-contract acceptance with controlled fixtures, not a measured gain in scientific quality,
coverage of every semantic axis or production utilization.
