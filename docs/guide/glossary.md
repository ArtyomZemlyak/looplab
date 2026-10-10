# Glossary

**Start here** if a word on the Report, in the UI or in `looplab inspect` stopped you. One line per
term, and a link to the page that explains it in full. The words are grouped the way you meet them.

## A run and its experiments

| Term | Meaning |
|---|---|
| **run** | One search on one task: a directory under `runs/` holding its event log, settings and experiments. |
| **experiment** | One candidate solution the run built and evaluated. Internally, in the event log, the API and the code, an experiment is a **node** — the CLI's `nodes=6` counts them. [Concepts](concepts.md) |
| **attempt** / **generation** | An experiment can be evaluated again (a repair, a reset): each try is an attempt; a reset starts a new generation of the same node. |
| **lifecycle** | One generation of a node from its build to its result. |
| **artifact node** | A preparation step other experiments use (a cleaned dataset, a trained encoder); it is never ranked itself. |
| **node budget** (`max_nodes`) | How many experiments the run may build. `finished — node budget spent (6/6 experiments)` means it used them all; `looplab resume RUN --max-nodes N` raises it. |
| **finished** / **paused** / **stopped** | Finished: the run wrote its end. Paused (stopped): resumable — `looplab resume`. A run with no end in its log was killed from outside. [CLI reference](cli-reference.md#the-stop-account) |
| **finalization** | The end-of-run wrap-up: report, lessons, cost roll-up. `looplab stop` skips it, `looplab finalize` runs it. |
| **event log** | `events.jsonl`, the append-only record every screen is rebuilt from. **replay** rebuilds a run's state from it alone. |
| **stop evidence** | The `inspect` line that says what the log's last rows show the run was doing — printed for a run that did not finish cleanly. |

## Results and how far to trust them

| Term | Meaning |
|---|---|
| **metric** / **direction** | The number each experiment is scored by, and whether lower or higher is better. |
| **selected result** / **champion** | The experiment the engine picked as the run's answer (`BEST experiment #N`). |
| **eligible** | An experiment whose result may count: evaluated, within its constraints, not excluded by a trust gate. |
| **first eligible experiment** | The Report's baseline: the earliest eligible experiment, which a "better by" is measured against. |
| **comparable** / **comparability key** | Two scores may be ranked only when they were measured on the same thing; the key says what. [Tasks](tasks.md) |
| **comparison contract** | The task's own declaration of what its scores are comparable with (metric, data, evaluator, …). |
| **deterministic** | A contract field: one evaluation is the score, so repeating it measures nothing — the Report then asks for no repeat runs. |
| **confirmed** / **confirmation mean** | The selected result re-evaluated with several seeds, and the mean of those repeats. **repeat checks** are those repeats. |
| **unconfirmed** | A single evaluation with no repeats — exploratory on a task whose score varies between runs. |
| **caveat** | Something the record says about a result that limits how far to trust it (a salvaged metric, a trust flag, different conditions). |
| **trust scan** | The detectors run over each result (reward hacking, data leakage, a critic); `inspect` says how many were scanned. [Concepts](concepts.md) |
| **reward hacking** / **leakage** | A score earned by gaming the evaluation, or by the solution seeing the answers. |
| **offline baseline** | With `backend=toy` and nothing to tune, the score of the task's fixed baseline — a pipeline check, not a result. |
| **base** / **seed** | The code an experiment started from; "Base unknown" means the record does not say which. |

## How the search decides

| Term | Meaning |
|---|---|
| **backend** | Who proposes and writes experiments: `llm` (a model) or `toy` (a fixed offline optimizer, no model). |
| **preflight** | The check before a run starts that the model endpoint answers; it refuses the run if it does not. |
| **Genesis** | A model writes the task from your `--goal`. Off with `--backend toy`. |
| **Researcher** / **Developer** | The two model roles: one proposes the next idea, the other writes its code. |
| **Strategist** | The role that picks the search policy and its settings as the run goes. |
| **policy** | The rule that picks the next experiment to build on: greedy, evolutionary, MCTS, ASHA, BOHB. [Concepts](concepts.md) |
| **ASHA** / **rung** | Racing: candidates run cheaply first (a rung), only the best get the full evaluation. |
| **endgame** / **reserve** | The last part of the node budget, kept for combining the best results (an **ensemble**) and a **sweep** of the champion's settings. |
| **operator** | The kind of step an experiment takes from its parent: draft, improve, merge, ablate, … |
| **Card** | A unit of work on the board — a hypothesis with its experiments; its **lane** is its work status (Proposed … Evaluated). [Web UI](ui.md) |
| **verdict** | A card's research verdict, separate from its lane: supported (an experiment improved), tested (evaluated, no improvement), abandoned. |

## Memory and research

| Term | Meaning |
|---|---|
| **cross-run memory** | Lessons and cases kept in `~/.looplab/memory` from finished runs and offered to later ones. |
| **lesson** | One short learning a finished run wrote down for the next. |
| **claim** | A statement about what works, with the runs that support or oppose it. |
| **concept** | A tag for what an experiment tried (a model family, a feature); experiments are grouped by concept. |
| **Deep Research** / **memo** | A research pass that reads sources and proposes directions; its memo is ideas, not evidence that they work. |

## The interface

| Term | Meaning |
|---|---|
| **Assistant** | The chat in the web UI that starts runs, explains results and steers a live run. In settings its role is named **boss**. |
| **Report** / **Lineage** / **Trace** | A run's result page; the tree of experiments and their parents; the record of each step's model calls and tools. |
| **Energy** | A visual-effects switch (animation of the lineage), nothing about the run. |
| **external harness** | A mode where an outside coding agent (Codex, Claude Code) writes the experiments and LoopLab evaluates them. [External agents](external-harness.md) |
