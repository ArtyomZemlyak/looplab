# Reference goal — dense retrieval (ESCI v2, SentenceTransformer backbone)

THE CANONICAL GOAL for this task family. **Change it only when the TASK changes** — a new eval, a new
backbone, a new data selector, a moved path. Never to give a run a result: every sentence added here
is a hypothesis some future run no longer gets to form, and the point of the framework is that it
forms them.

It is 1100 characters. The version it replaces was 6,409, of which 70% was findings the shipped
tools produce on their own and 24% duplicated machinery that injects the same fact — see
`docs/guide/tasks.md`, "Writing a `goal`", for the line-by-line accounting and the four questions
every candidate sentence has to survive.

## The text

```
Maximize test recall@100 on the ESCI v2 dense-retrieval benchmark by fine-tuning the SentenceTransformer backbone at /home/jovyan/data/embedder/d0rj/e5-small-en-ru.

The repo is /home/jovyan/data/vectorizer-unified — read it before changing anything. You realise an idea by EDITING THE REPO; nothing here is applied automatically.

CLAIM[<slug>] the backbone named above is the model on disk at that path decided:`present:XLMRobertaModel@/home/jovyan/data/embedder/d0rj/e5-small-en-ru/config.json`

DATA: use dataset_version '2'. The local data root is already exported into every stage's environment — do not set it in code.

SCALE CAVEAT, the one thing you cannot derive from your own tools: the operator's manual benchmark table was measured against a ~37k-item catalogue and this eval indexes ~641k, so its absolute numbers are NOT on this scale and a lower number here is not a gap to close. Use that table for the RANKING of backbones and recipes; take every target value and every comparison from runs on THIS eval, which `list_sibling_runs` and `diff_nodes` will give you.
```

> **Why the pin is written `CLAIM[<slug>]` here and not with its real slug.** `claimpin` scans the
> whole repo tree for `CLAIM[…]` markers and does not distinguish a pin from a QUOTED EXAMPLE of one.
> Pasting the goal verbatim into this file therefore created two real defects — a repo pin citing an
> absolute path outside the checkout, and a second mention with no `decided:` clause in range. The
> slug is elided so the example stays readable without becoming a pin. **Restore the real slug when
> you copy the text into a task file, never here.**

## Why each part is here

| part | why it cannot be found |
|---|---|
| objective + metric name | it IS the task |
| repo path, backbone path | the agent cannot guess where, and a moved path is silent breakage |
| "you realise an idea by EDITING THE REPO" | a mode fact about this task kind, not a finding |
| `dataset_version '2'` | an operator selection; nothing in the repo implies it |
| the scale caveat (~37k vs ~641k) | the ONE derivation the tools cannot do — they will happily compare two numbers that were never on one scale, and the error is expensive |
| the `CLAIM[<slug>]` pin | guards the only world-fact left, so the goal cannot rot unnoticed the way its predecessor did |

## What was deliberately removed, and who answers it now

| removed | who answers it |
|---|---|
| "the repo config still names rubert-tiny-lite" | Developer — `read_file` on `config.yaml` |
| e5 `query:`/`passage:` prefixes, and whether this repo applies them | Developer — the repo read, then an EXPERIMENT, which is what it always was |
| memory arithmetic: MiB/example, batch 2048 peak, 3072 OOM, the `gradient_checkpointing` lever | Developer — `dev_probe` against the real card |
| prior-run numbers (0.683 vs 0.738, best 0.762048) | Researcher — `list_sibling_runs`, cross-run priors |
| "params in the record are proposals", the 9.0% divergence rate | `diff_nodes` plus the rendered `applied_params` |
| "this box has TWO GPUs, the footprint is yours" | the engine's own GPU BUDGET cue, computed from the launched width |
| "the scorer prints RECALL@100" | `eval.metric.pattern`, where it already lives |
| "that mistake was already made and cost hours" | LESSONS — they reach the repair prompt whole |

## Expected cost of the trim, stated up front

A run on this goal will be **slower** and will re-walk its own OOM ladder rather than inheriting the
answer. That is the test. It is also real GPU hours, and whoever launches should know they are buying
a measurement of the framework, not the fastest path to a metric.
