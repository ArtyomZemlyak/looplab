# Chat-first UI & always-on command bar — deep research

> Research question: how to design a chat-first UI centered on an always-present command/chat bar
> (command-palette + conversational-input hybrid), and how to transition gracefully from that inline
> bar into a full assistant view — with concrete variants for LoopLab.
>
> Method: fan-out web search (5 angles) → 22 sources fetched → 10 falsifiable claims → 3-vote
> adversarial verification (kill on 2/3 refutes). Result: **9 confirmed, 1 refuted.**

## TL;DR

Only the **intent-routing mechanics** survived adversarial verification. The requested breadth on
**bar placement/anchoring, the inline→full transition UX, and non-Copilot product examples was NOT
verified** — treat those parts of the variants below as design judgment, not cited fact.

Recommendation for LoopLab: **one persistent bar, intent-routed** — a "create a run" intent hands off
to the Genesis planner; "fix / steer / ask" routes to the assistant thread — using `/` for named
actions and `@` / `#` for scoping/context (the GitHub Copilot Chat model), backed by a **fast
pre-router** (explicit prefixes + embedding/heuristic classifier, *not* an LLM call per keystroke) and
**explicit conversation-state** to govern escalation from the inline bar to the full assistant.

---

## Verified findings (cited)

### 1. One bar disambiguates many intents via three orthogonal prefixes — `/ @ #` (HIGH, 3-0)
GitHub Copilot Chat ships exactly this model:
- **`/` slash commands** = fixed named actions with fixed descriptions (`/explain`, `/fix`, `/tests`,
  `/new`, `/fixTestFailure`, `/help`) — not free-form conversation.
- **`@` mentions** = select a scoped participant/agent (`@workspace`, `@terminal`, `@vscode`,
  `@github`, `@azure`); on GitHub.com the web `@` instead **attaches entities** (repos, files, issues,
  PRs, discussions) as context (shipped June 2025).
- **`#` references** = attach specific context (`#file`, `#selection`, `#function`, symbols, lines),
  **decoupled from the natural-language question**.

For LoopLab this maps to: `/` for named actions (`/new-run`, `/approve`, `/ablate`, `/fix`), `@` to
scope to an agent/run (`@genesis`, `@run`), `#` to attach context (`#node-12`, `#run-xyz`, `#log`).
Sources: docs.github.com/copilot/chat-cheat-sheet; code.visualstudio.com/docs/copilot/chat.

### 2. Don't LLM-route every input (HIGH, 3-0 / routing-agent 2-1)
LLM-based routing "can be slow and expensive, since every decision is an LLM call," and with many
tools the LLM "may misclassify or hallucinate a wrong function." Accuracy drops as tool count grows
(Berkeley Function Calling Leaderboard; "Less is More," arXiv 2411.15399 → trimming the tool set
raised accuracy to 87%; "lost in the middle" position bias). A dedicated, focused router/coordinator
is more accurate. Sources: gist mkbctrl; arXiv 2411.15399; arXiv 2605.24660.

### 3. Embedding-based semantic routing = instant intent classification, no runtime LLM (HIGH, 3-0)
Pre-encode example utterances per intent, then classify a new query by nearest-neighbor similarity in
embedding space — no runtime LLM generation. Documented latency ~5000 ms (LLM classifier) → ~100 ms.
Sources: github.com/aurelio-labs/semantic-router; arXiv 2502.00409. (Rule-based <1 ms, embeddings
~5 ms, ML classifier 50–100 ms, LLM 500–2000 ms per other latency data in the finding.)

### 4. Multi-turn continuity is the hard part of escalation (HIGH, 3-0)
Terse follow-ups ("correct", "yes please proceed", "makes sense") "can be very pinpointed to previous
agent response" and need prior context to route correctly — but naively concatenating that context
"might skew up the embedding representation," degrading routing. **"Midflow intent switching"** is a
distinct failure mode needing its own conversational-state handling (Towards AI WhatsApp-pipeline
case: escalations dropped ~38%→17% after adding explicit conversation state alongside intent routing).
Sources: gist mkbctrl; arXiv 2602.07338; arXiv 2602.16935.

### Refuted — do NOT cite
Specific precision/cost numbers from a "car-sales chatbot" semantic-routing case study (0-3 refuted).

---

## What was NOT verified (open questions / caveats)

The strongest caveat is **scope mismatch**: only routing/continuity mechanics and one richly
documented product (Copilot) survived. The following were requested but are unverified here — the
variants below rely on design judgment for them:

1. **Bar placement/anchoring** for a canvas-heavy `@xyflow/react` app (top header vs bottom-docked vs
   floating centered overlay) and how to stay always-visible without stealing canvas space.
2. **Inline-bar → full-assistant transition UX** (expand-in-place vs slide-in drawer vs full page) and
   how typed input + conversation continuity is preserved across escalation.
3. **Non-Copilot exemplars** (Raycast/Linear/Superhuman palettes, Slack/Notion AI, Vercel v0, Cursor,
   Warp, Perplexity, Arc/Dia) — layout, keyboard model, slash/@ affordances.
4. **Discoverability, empty-state, streaming/latency feedback, accessibility** conventions for
   always-on chat-first bars. (Relevant unverified reading gathered: W3C ARIA APG combobox pattern;
   Superhuman "how to build a remarkable command palette"; Maggie Appleton "command bar"; progressive
   disclosure — LogRocket / UXmatters.)

---

## 4 variants for LoopLab (design proposal)

LoopLab already has the pieces: a header `cmdbar` (draft), a Genesis `seed` hook, an in-run `Dock`
chat, and a full `AssistantChat` page + `SharedAssistant`.

| | Variant | Bar location | → Full assistant | Pros | Cons |
|---|---|---|---|---|---|
| **A** ⭐ | Header omnibar + expand-down | topbar (current draft) | focus → dropdown of suggestions; "expand" → `#/assistant` carrying text | max discoverability, little canvas theft, partly built | header space |
| **B** | Bottom-docked "console" bar | pinned bottom, all views | thread grows up as a drawer; in-run it merges with the existing Dock | strongest "always there"; unifies in-run Dock + global assistant into one metaphor | bottom real estate; more refactor |
| **C** | ⌘K centered overlay + mini trigger | small "⌘K to ask/run…" pill | overlay grows to full page | familiar keyboard model, min canvas theft | bar is a *trigger*, not an always-ready input (weaker on the "always displayed" ask) |
| **D** | Right-rail assistant + header quick-entry | thin input in header | "open in rail" beside the DAG, thread preserved | see graph and conversation at once (ideal for steering a run) | more layout complexity |

### Recommendation: hybrid A + B
1. **One input component everywhere** — header omnibar in the list (A); the same component becomes the
   Dock composer inside a run (feel of B). One input surface, everywhere.
2. **Intent routing:** explicit `/ @ #` (Copilot model, verified) + a cheap pre-router
   (heuristic/embedding, per findings 2–3) for bare natural language. On ambiguity, **don't guess** —
   show a disambiguation chip: "▶ Plan a run" / "✦ Ask the assistant."
3. **Escalation = expand-in-place**, carrying the typed text and the thread. LoopLab already does a
   form of this (`seed` + sessionStorage draft + carrying `chat` into the launched run) — that is the
   conversation-state the research (finding 4) says is essential; reuse and extend it.

---

## Sources
- docs.github.com/en/copilot/reference/chat-cheat-sheet
- code.visualstudio.com/docs/copilot/chat
- github.com/aurelio-labs/semantic-router
- arxiv.org/abs/2411.15399 · arxiv.org/abs/2502.00409 · arxiv.org/abs/2605.24660 · arxiv.org/abs/2602.07338
- gist.github.com/mkbctrl/a35764e99fe0c8e8c00b2358f55cd7fa · arize.com/blog/best-practices-for-building-an-ai-agent-router
- (unverified/background) maggieappleton.com/command-bar · superhuman command-palette · W3C ARIA APG combobox · Raycast AI · Mobbin/uxpatterns.dev command-palette · progressive disclosure (LogRocket, UXmatters)

_Stats: 5 angles · 22 sources fetched · 10 claims · 9 confirmed / 1 refuted · 59 agent calls._
