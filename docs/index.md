---
hide:
  - navigation
---

<div class="ll-hero" markdown>

# LoopLab

**An autonomous ML/DS research engine.** Give it a goal; it **invents → implements → tests → improves**
candidate solutions in a loop and returns the best *verified* result. Domain decisions are lines in an
append-only event log that is authoritative for replayable `RunState`, so the search is reproducible and
crash-resumable by replay. Task/config, tracing, chat, command and cross-run sidecars keep their own
explicit contracts.

<div class="ll-verbs">
  <span>● Invent →</span><span>● Implement →</span><span>● Test →</span><span>● Improve ↺</span><span>● Champion</span>
</div>

</div>

[Explore the architecture infographic :material-arrow-right:](guide/architecture.md){ .md-button .md-button--primary }
[Quickstart :material-rocket-launch:](guide/quickstart.md){ .md-button }

## The loop in one picture

A candidate solution is a **node**. Each turn expands one node into a child; the color-coded roles
hand work around the wheel, and every arrow also appends one event to `events.jsonl`.

<div class="ll-loopsvg">
<svg viewBox="0 0 960 330" role="img" aria-label="The LoopLab loop: Researcher to Developer to Sandbox to Evaluator, the policy picks the next node and repeats, with repair and merge edges, ending in a confirmed champion." style="width:100%;height:auto;font-family:var(--md-code-font-family)">
  <defs>
    <marker id="a" markerWidth="9" markerHeight="9" refX="6.5" refY="4.5" orient="auto"><path d="M1 1 L7 4.5 L1 8" fill="none" style="stroke:var(--md-default-fg-color--light)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></marker>
    <marker id="ar" markerWidth="9" markerHeight="9" refX="6.5" refY="4.5" orient="auto"><path d="M1 1 L7 4.5 L1 8" fill="none" style="stroke:#c0554f" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></marker>
    <marker id="ag" markerWidth="9" markerHeight="9" refX="6.5" refY="4.5" orient="auto"><path d="M1 1 L7 4.5 L1 8" fill="none" style="stroke:#b5842a" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></marker>
  </defs>

  <!-- return arc: Evaluator -> Researcher (policy picks next) -->
  <path d="M760 120 C 760 46 470 40 250 40 C 140 40 95 66 95 118" fill="none" style="stroke:var(--md-default-fg-color--light)" stroke-width="1.7" stroke-dasharray="1 0" marker-end="url(#a)"/>
  <text x="430" y="32" text-anchor="middle" font-size="12" style="fill:var(--md-default-fg-color--light)">policy picks the next node · repeat ↺</text>

  <!-- forward arrows -->
  <path d="M170 150 L212 150" fill="none" style="stroke:var(--md-default-fg-color--light)" stroke-width="1.8" marker-end="url(#a)"/>
  <path d="M400 150 L442 150" fill="none" style="stroke:var(--md-default-fg-color--light)" stroke-width="1.8" marker-end="url(#a)"/>
  <path d="M630 150 L672 150" fill="none" style="stroke:var(--md-default-fg-color--light)" stroke-width="1.8" marker-end="url(#a)"/>
  <text x="191" y="142" text-anchor="middle" font-size="10.5" style="fill:var(--md-default-fg-color--lighter)">idea</text>
  <text x="421" y="142" text-anchor="middle" font-size="10.5" style="fill:var(--md-default-fg-color--lighter)">code</text>
  <text x="651" y="142" text-anchor="middle" font-size="10.5" style="fill:var(--md-default-fg-color--lighter)">run</text>

  <!-- repair back-edge: Sandbox -> Developer -->
  <path d="M520 178 C 500 226 340 226 320 180" fill="none" style="stroke:#c0554f" stroke-width="1.6" stroke-dasharray="5 5" marker-end="url(#ar)"/>
  <text x="420" y="238" text-anchor="middle" font-size="11" style="fill:#c0554f">repair ↺  (crash / timeout → stderr fed back)</text>

  <!-- confirm -> champion -->
  <path d="M760 180 L760 250" fill="none" style="stroke:#b5842a" stroke-width="1.8" marker-end="url(#ag)"/>
  <text x="775" y="222" text-anchor="start" font-size="10.5" style="fill:#b5842a">budget spent → confirm</text>

  <!-- role pills -->
  <g><rect x="20" y="122" width="150" height="56" rx="12" style="fill:color-mix(in srgb,#6b5fd6 16%,transparent);stroke:#6b5fd6" stroke-width="1.6"/><text x="95" y="147" text-anchor="middle" font-size="13.5" font-weight="700" style="fill:var(--md-default-fg-color)">Researcher</text><text x="95" y="164" text-anchor="middle" font-size="10" style="fill:var(--md-default-fg-color--light)">propose idea</text></g>
  <g><rect x="250" y="122" width="150" height="56" rx="12" style="fill:color-mix(in srgb,#2f7fd6 16%,transparent);stroke:#2f7fd6" stroke-width="1.6"/><text x="325" y="147" text-anchor="middle" font-size="13.5" font-weight="700" style="fill:var(--md-default-fg-color)">Developer</text><text x="325" y="164" text-anchor="middle" font-size="10" style="fill:var(--md-default-fg-color--light)">write code</text></g>
  <g><rect x="480" y="122" width="150" height="56" rx="12" style="fill:color-mix(in srgb,#159b93 16%,transparent);stroke:#159b93" stroke-width="1.6"/><text x="555" y="147" text-anchor="middle" font-size="13.5" font-weight="700" style="fill:var(--md-default-fg-color)">Sandbox</text><text x="555" y="164" text-anchor="middle" font-size="10" style="fill:var(--md-default-fg-color--light)">run · isolated</text></g>
  <g><rect x="710" y="122" width="150" height="56" rx="12" style="fill:color-mix(in srgb,#3f9d55 16%,transparent);stroke:#3f9d55" stroke-width="1.6"/><text x="785" y="147" text-anchor="middle" font-size="13.5" font-weight="700" style="fill:var(--md-default-fg-color)">Evaluator</text><text x="785" y="164" text-anchor="middle" font-size="10" style="fill:var(--md-default-fg-color--light)">CV + gate</text></g>

  <!-- champion -->
  <g><rect x="690" y="250" width="190" height="48" rx="12" style="fill:color-mix(in srgb,#b5842a 18%,transparent);stroke:#b5842a" stroke-width="1.8"/><text x="785" y="279" text-anchor="middle" font-size="13.5" font-weight="700" style="fill:var(--md-default-fg-color)">🏆 Champion</text></g>
</svg>
</div>

Every domain/control arrow above appends one logical event to `events.jsonl`, the replay authority for
`RunState`. A physical JSONL line may contain one event or a versioned atomic batch envelope.
Diagnostic and cross-run boxes also use their documented sidecars.

[See the full interactive infographic — every component and stage :material-open-in-new:](guide/architecture.md)

## Start here

<div class="grid cards" markdown>

-   :material-download: **[Installation](guide/installation.md)**

    ---

    Requirements, install extras, and the optional backends. Core is small and pure-Python.

-   :material-rocket-launch: **[Quickstart](guide/quickstart.md)**

    ---

    Your first run — offline in one command, then driven by a live LLM. Read and verify the result.

-   :material-console: **[CLI reference](guide/cli-reference.md)**

    ---

    Every command: `run`, `resume`, `replay`, `inspect`, `smoke`, `bench`, `ui`, `export-*`.

-   :material-tune-variant: **[Configuration](guide/configuration.md)**

    ---

    Every `LOOPLAB_*` setting, grouped by topic, with its default — and what's on out of the box.

</div>

## How it works

<div class="grid cards" markdown>

-   :material-sitemap: **[Architecture overview](guide/architecture.md)**

    ---

    The visual map of the whole agent — the six-stage lifecycle, one loop iteration, and every subsystem.

-   :material-lightbulb-on: **[Concepts](guide/concepts.md)**

    ---

    Event log & replay, sandbox & trust tiers, operators, gates, confirmation, search policies.

-   :material-brain: **[Memory & knowledge](guide/memory.md)**

    ---

    Cases, lessons, causal meta-notes, skills and the knowledge base — injected *and* agentically retrieved.

-   :material-robot: **[LLM & coding agents](guide/llm-and-agents.md)**

    ---

    Any OpenAI-compatible backend, external coding agents, per-role models, and reasoning depth.

</div>

## Write the code for you

<div class="grid cards" markdown>

-   :material-file-code: **[Generating train & test code](guide/generating-code.md)**

    ---

    Let the agent write the whole solution from a goal + data, or edit your own repo inside an allow-listed surface.

-   :material-clipboard-list: **[Tasks](guide/tasks.md)**

    ---

    All nine task adapters — from a toy objective to real Kaggle competitions — and their JSON fields.

-   :material-monitor-dashboard: **[Web UI](guide/ui.md)**

    ---

    The live React control plane: the full execution trace, the lineage DAG, and steering by chat.

-   :material-server: **[Deployment](guide/deployment.md)**

    ---

    The one-command Docker Compose stack and the untrusted, network-off sandbox tier.

</div>

!!! tip "Two properties everything rests on"

    **Reproducible by replay** — `looplab replay RUN_DIR` folds the event log into state with a pure
    function, identical every time. **Crash-resumable at the durable frontier** — `looplab resume RUN_DIR`
    replays the complete event prefix and does not re-serve work whose durable fulfillment receipt is
    present. External effects and cross-run sidecars have their own narrower recovery contracts; an
    unreceipted side effect does not receive a blanket exactly-once guarantee.
