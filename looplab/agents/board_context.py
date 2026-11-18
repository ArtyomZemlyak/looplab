"""Deterministic lexical relevance for a board with no predicted payoff order.

This is a prompt ordering heuristic, not semantic constraint evaluation, an experiment choice,
measured utility or authority. Whole-card coverage/budgets are owned by state_brief's window.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

from looplab.core.hint_directives import operator_directive_texts

# Match Unicode words without translating/stemming or inventing model/axis equivalence.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STOP = frozenset('the and for from with into this that these those are was were have has will '
                  'our your their then than when where which what how can could should would '
                  'для что как это или при есть надо нужно этого этой которые чтобы также'.split())
_CONTEXT_CHARS = 2000


def _terms(text):
    return {word for word in _WORD.findall(unicodedata.normalize('NFKC', str(text or '')).casefold())
            if len(word) > 1 and not word.isdecimal() and word not in _STOP}


def rank_board_context(state, cards):
    """Rank whole seed statements by operator recency, then goal overlap; None if no signal.

    Current operator-only filtering is shared with the prompt itself. Model-written research
    advisories and retired directions never become operator priority through this path.
    Corpus rarity downweights generic terms, and sets prevent repeated words inflating a score.
    Stable ID breaks exact ties. The explicit predicted hyp_order is handled BEFORE this helper.
    """
    directives = operator_directive_texts(state.pending_hints)[-6:]
    contexts = [_terms(text[:_CONTEXT_CHARS]) for text in reversed(directives)]
    contexts.append(_terms(str(state.goal or "")[:_CONTEXT_CHARS]))
    if not any(contexts):
        return None
    wanted = set().union(*contexts)
    terms = {card.id: _terms(card.seed_statement) & wanted for card in cards}
    frequency = Counter(word for words in terms.values() for word in words)
    scores = {card.id: tuple(sum(max(1, 1024 // frequency[word]) for word in words & terms[card.id])
                            for words in contexts) for card in cards}
    if not any(any(value) for value in scores.values()):
        return None
    return sorted(cards, key=lambda card: (tuple(-x for x in scores[card.id]), card.id))
