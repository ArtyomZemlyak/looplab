# Quickstart

## Assistant in the web UI

This is the usual path: describe a task in chat, check the experiment before it starts,
then read the measured result in the same workspace. It uses a live model for the chat
and LoopLab's internal research roles. For setup from a fresh checkout, use
[Installation](installation.md#source-install-for-the-web-ui).

1. Start the UI from the checkout with `looplab ui`, then open `http://127.0.0.1:8765`.
   On JupyterHub, use the LoopLab Launcher tile instead; see
   [JupyterHub onboarding](jupyterhub-onboarding.md).
2. In **LoopLab → Settings → Essential → Model**, check the model and endpoint, then **Save**. **Test active LLM**
   makes one provider request and may be billed. The local defaults (`qwen3:8b` at
   `http://localhost:11434/v1`) require a running Ollama server with that model pulled;
   saved values alone do not prove the model is reachable. The first-run Assistant shows
   the saved model and the last explicit test result on this page. **Check connection…**
   opens the same explicit **Test active LLM** control beside your draft; opening it only
   reads settings. **How do I connect a model?** explains Model, Base URL, keys and
   the server meaning of `localhost`; see [model setup](llm-and-agents.md#connect-assistant-in-the-ui).
   The check panel follows the Assistant language choice (English or Russian).
   If a prior request has no verified outcome, **Check previous result**
   recovers its receipt without starting another provider call. Return to
   Runs after saving. **Experiments & resources** and **Time & model budgets** set defaults
   for new runs. Blank time limits and zero model budgets mean no cap. Cost budgets
   use estimates and require provider prices; Assistant chat is outside those run budgets.
3. Select **Start a new run** in Assistant. For example:

   Choose **I have code** or **I have data** to fill an editable example in the
   composer. With **Language / Язык → Русский**, these are **Есть код** and
   **Есть данные**. Replace the text in `[brackets]` with your goal and paths,
   then **Send**. Choosing an example makes no model request and starts no run.
   Examples stay disabled while the composer contains a draft; clear it to choose
   another. If you do not know the evaluation command or metric yet, ask Assistant
   to help choose them before launching.

   > Improve accuracy on my dataset. The code is at `/path/on/the/LoopLab/server/repo`
   > and the data is at `/path/on/the/LoopLab/server/data`. Start with at most three
   > experiments. Show me the evaluation command and editable files before launch.

   Give the goal, accessible paths, and constraints. The Assistant can inspect a repo
   and ask for missing information. The paths must exist on the **LoopLab server**,
   which may be a different machine from your browser.
   Keep **Permissions · Plan** to discuss and prepare the proposal. Expand that control
   when you want to change how file edits and run controls are approved; each option
   explains what happens automatically. You can review and start the launch card in Plan.
4. Review **What this run will do** on the launch card: goal, score direction, paths,
   edit rules where applicable, and limits. Missing facts are marked explicitly.
   Choose **Validate**, inspect the effective preview and technical settings, then
   **Start run**. Changing the proposal invalidates its validation, so validate again.
   With **Русский**, these actions are **Проверить — бесплатно** and **Начать запуск**;
   **Изменить параметры плана** opens the fields. The labels and cost warning also
   follow the language; task JSON, paths and server diagnostics keep their original text.
   **Run LLM budget (USD)** can be edited under **Edit proposal details**. The review
   shows its effective amount only after validation: the smaller positive value of
   `llm_budget_usd` and `llm_cost_limit` applies; zero disables that field's limit.
   Before validation, inherited limits remain unresolved. Chat, external-client models
   and experiment/upstream compute are outside this limit. Missing prices leave spend
   unknown, and calls can exceed the limit before settling.
   The card's **Next:** guidance (Russian: **Дальше:**) explains the current step in
   the Assistant language. **Detailed status / Подробный статус** retains the precise
   technical message. If the startup reply is lost, follow **Check startup / Проверить запуск** to read
   the already-submitted launch; an unknown reply does not prove that it failed.
   Chatting about a plan does not start a run. A live model or evaluator may incur cost;
   check the effective limits in the preview. Experiment and time limits alone do not cap
   model spending; set the run model budget on the card or defaults in Settings if needed.
5. When the run finishes, Assistant shows a free **Run result** summary: the first eligible
   experiment, the engine's selected result, metric direction and confirmation caveats.
   The first eligible experiment is not necessarily a task baseline. **Read Report** opens
   the recorded evidence; **Open selected code** and **Find artifacts** locate the solution.
   **Ask about this result** prepares a message for review; only **Send** contacts the model.
   In an experiment's **Overview**, **Experiment result** separates its evaluation score
   from repeat checks and explains whether parent evaluation conditions match.
   An unconfirmed score remains exploratory; a confirmation mean alone does not prove reliability.
   In chat, **Earlier / Раньше**, **Newer / Новее** and **Latest results / К последним итогам**
   navigate free completion briefs and external-agent interpretations in 50-item pages.
   These reflect current evidence; changed attempts or measurements withdraw old interpretations.
   Run cards, the selected experiment in Report, and the Assistant summary label an
   **evaluation score** separately from a **confirmation mean**. A recorded mean with
   no valid repeat count does not establish multiple successful checks. Check the spread,
   matching evaluation conditions and Trust evidence before relying on the result.
   **Report → Comparisons** shows how many evaluations could be compared with their
   recorded parent and how many had a **Better score**. **Not compared** includes first
   experiments and missing or changed comparison evidence; it does not mean failure.
   Comparisons use evaluation scores. The separately labelled numeric frontier may
   contain confirmation means and is not evidence of a comparable improvement.

## Reuse a useful code change

A fix discovered in one experiment can become shared starting code for later experiments.
Start in Assistant with **Permissions · Plan**:

> Help reuse useful code changes in future experiments. Explain what can be shared,
> which checks are needed and their cost. Prepare a plan for review before changing the base.

For a run with a recorded code base, **Overview** or **Report → Code for future experiments → Choose a change
with Assistant** prepares this discussion. With **Language / Язык → Русский**, the panel
and its prepared question use Russian. The button fills the composer; **Send** contacts
the model and may incur provider charges. An existing draft is kept.

If reuse is disabled for that run, **Prepare with Assistant** asks for a **new** launch
proposal. This feature needs a recorded code archive, protected evaluation, and declared
tests and regression checks. Assistant can explain missing prerequisites; enabling it
requires reviewing the new task before launch. See
[task setup](tasks.md#promote-reusable-code-into-a-verified-base) for those requirements.

Reusing code does not copy a score or rewrite previous results. Checks measure the
proposed change and the original behavior; a base update needs explicit approval while
the engine is stopped. Resuming is a separate action. For Codex or Claude Code, the
[external-agent guide](external-harness.md) describes the same guarded workflow.

## Offline CLI walkthrough

For an offline proof with no model or network, follow the [CLI walkthrough](cli-walkthrough.md).
It also covers result inspection, a regression example, live LLM setup, and crash recovery.
The web Assistant needs a configured model. To drive LoopLab directly from Codex or Claude Code,
follow the [external-agent quickstart](external-harness.md#first-external-run).
