"""Endpoint reachability preflight for a live-backend run.

The sibling of `core/llm.py::validate_bound_profiles`, and deliberately its neighbour at the same
gate (`cli/__init__.py::_engine`): that one fails before transport when a role's CREDENTIAL is
unusable, this one fails before the loop when the role's endpoint is simply not THERE.

ONE probe, TWO policies, because the same dead endpoint means different things at different entry
points: `preflight_role_endpoints` REFUSES a run that is about to propose, and
`wrap_up_endpoint_warning` WARNS an entry point that can only complete a run that is already over.
Adding a third caller means choosing between them — and the test for which one is not "does this
command touch the model" but "can this command still start new work".

Its own module rather than a function in `factory.py` for two reasons: it is a gate, not a
composition root (the factory's `test_agent_factory_split.py` line cap is a deliberate reminder of
what that file is FOR), and a preflight that must run BEFORE any role is built has no business
living inside the module that builds them.

LAYERING: same rule as `factory.py` — imports of `agents`/`search`/`tools` stay function-local.
"""
from __future__ import annotations

from looplab.core.errors import LLMError
from looplab.core.llm import llm_credential_consumers, make_llm_client_for, resolve_llm_target

# One bounded, four-token completion — the SAME probe shape, and the same probe-only client controls
# (no stream, no cache, no reasoning, a whole-call wall guard), that the UI's `/api/llm/health` route
# already issues through `OpenAICompatibleClient.probe`. Reusing it is deliberate: a run and the
# health card must agree on what "this endpoint is reachable" means, or the operator gets a green
# card and a dead run. `max_retries=2` keeps one transient 429/5xx blip forgiving while bounding how
# long a preflight can take; a refused connection / bad DNS / 401 is not retried by the client at all,
# so the common failure is instant.
_PREFLIGHT_MESSAGES = [{"role": "user", "content": "Reply with one word: ready"}]
_PREFLIGHT_MAX_TOKENS = 4
_PREFLIGHT_MAX_RETRIES = 2
PREFLIGHT_TIMEOUT_S = 60.0


def _probe_role_endpoints(settings, *, timeout_s: float) -> list[str]:
    """Probe every distinct role target once; return one description per unreachable target.

    The shared body of the two policies below. Both need exactly the same measurement — what differs
    is only what a failure MEANS at that entry point, and that decision belongs to the caller.

    Probes each DISTINCT target of the roles this configuration actually requires a live model for —
    `llm_credential_consumers`, the same role set the credential preflight checks — so the ordinary
    single-model run costs exactly one four-token completion, not one per role.
    """
    # THE composition root's binding, resolved at call time: `make_roles` builds its own client
    # through `factory.make_llm_client`, so anything that redirects roles (a test double, an
    # integration that supplies its own transport) redirects the preflight with them instead of
    # having its endpoints probed behind its back. A module-level binding here would be a SECOND
    # seam that silently diverges from the one the run actually uses.
    from looplab.agents.factory import make_llm_client

    _shared_active, roles = llm_credential_consumers(settings)
    if not roles:
        return []                               # backend != "llm": no role talks to a provider
    # Roles served by an EXTERNAL coding-agent process are excluded for the same reason
    # `validate_bound_profiles` special-cases them: that process authenticates from its own
    # credential store and is launched with every secret-looking variable stripped, so a probe from
    # HERE would test a credential the run never uses and could fail an endpoint that works.
    if getattr(settings, "developer_backend", "default") != "default":
        roles -= ({"implement", "repair"} if getattr(settings, "unified_agent", False)
                  else {"developer"})

    def _probe_factory(_settings, **target_kwargs):
        return make_llm_client(_settings, **target_kwargs, stream=False, cache=False,
                               disable_reasoning=True, max_retries=_PREFLIGHT_MAX_RETRIES,
                               wall_timeout=timeout_s)

    seen: set = set()
    failures: list[str] = []
    for role in sorted(roles, key=lambda item: item or ""):
        try:
            target = resolve_llm_target(settings, role=role)
            # Dedupe on everything that decides WHERE the request goes and WITH WHAT — the same key
            # `LlmTarget` is documented to be for the client cache. Two roles that share a target
            # share one probe; two that differ in the credential alone are still both probed.
            if target in seen:
                continue
            seen.add(target)
            client = make_llm_client_for(
                settings, role=role, timeout=timeout_s, factory=_probe_factory)
        except Exception as exc:  # noqa: BLE001 — an unbuildable client is a preflight failure too
            failures.append(f"{role or 'the default target'}: {exc}")
            continue
        # `probe` is a capability of the real transport, not of the LLMClient PROTOCOL
        # (`core/parse.py::LLMClient` declares only complete_tool/complete_text). A client that
        # doesn't have it — a test double, an integration that supplies its own transport through the
        # `make_llm_client` seam — is not evidence of an unreachable endpoint, and treating a missing
        # method as a failed probe would turn every such seam into a run that refuses to start. Same
        # optional-hook rule as `tools/_base.py::bind_state`.
        if not callable(getattr(client, "probe", None)):
            continue
        try:
            client.probe(_PREFLIGHT_MESSAGES, max_tokens=_PREFLIGHT_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001 — every transport/protocol failure is the answer
            failures.append(
                f"{role or 'the default target'} ({target.model} at {target.base_url}): {exc}")
    return list(dict.fromkeys(failures))


def preflight_role_endpoints(settings, *, timeout_s: float = PREFLIGHT_TIMEOUT_S) -> None:
    """Fail before the run starts when a role's provider cannot be reached.

    Why this has to be loud. Every LLM role degrades on purpose, and each of those degradations is
    right for ONE flaky answer and catastrophic for an endpoint that is not there:
    `core/parse.py::parse_structured` catches `LLMError` and reports it as an unparseable answer,
    `roles.py::LLMResearcher.propose` turns two of those into an empty `Idea(operator="draft")`, and
    `agent.py::ToolUsingResearcher.propose` catches Exception and returns `_fallback`. Stacked, a
    `looplab run examples/toy_task.json --max-nodes 3` against a dead endpoint produced three
    IDENTICAL `x=0,y=0` nodes annotated "fallback (agent parse failed)", a metric flat at 10.0, and
    `finished=True` — a completed run with a confident-looking flat result and no error anywhere,
    while `--backend toy` optimized the same task to 8.05. With `Settings.backend` now defaulting to
    "llm", that is what a misconfigured or unreachable endpoint hands a user BY DEFAULT.

    THE SCOPE OF THAT REASONING IS A RUN THAT IS ABOUT TO DO WORK. Every sentence above is about a
    PROPOSAL; a run that is already over makes none, so this refusal is wrong for a wrap-up entry
    point and `wrap_up_endpoint_warning` is what those call instead.
    """
    failures = _probe_role_endpoints(settings, timeout_s=timeout_s)
    if failures:
        raise LLMError(
            "LLM endpoint preflight failed: " + "; ".join(failures)
            + ". The run needs a reachable model for these roles — start the endpoint, or point "
              "LOOPLAB_LLM_BASE_URL / --model at one. Run offline with `--backend toy` "
              "(or -s backend=toy). Refusing to start: the roles would degrade to empty fallback "
              "proposals and the run would report success on a flat, meaningless result. "
              "(Wrapping up a run that is already over is not refused — `looplab finalize` warns "
              "and names the artifacts a missing model degrades.)")


def wrap_up_endpoint_warning(settings, *, timeout_s: float = PREFLIGHT_TIMEOUT_S) -> str | None:
    """The same probe, for an entry point that can only COMPLETE a run's wrap-up. Never refuses.

    `preflight_role_endpoints` exists because roles that silently degrade to fallback proposals let a
    run report success on a meaningless result. Past the terminal boundary there is no proposal to
    degrade: `finalize`, and a `run`/`resume` that lands on an existing wrap-up, may only turn work
    that was ALREADY PAID FOR into the report, the lessons, the cost roll-up and `tree.html`.
    Refusing there costs the operator every deterministic artifact — budget summary, diversity
    archive, case + concept capsule, cost roll-up, `readmodel.sqlite`, `trace.json`, `tree.html`,
    none of which need a model — to protect the two the model can no longer produce anyway. Worse,
    the run then stays `finalization_pending` forever: its spend stays stranded in
    `.llm-usage-outbox` and every later entry point tries the wrap-up again and refuses again.

    So the answer is to proceed and SAY what the missing model costs, rather than to refuse or to
    degrade silently. The paid steps are the ones named in the returned text, and each of them is
    already durably marked complete once attempted (`engine/finalize.py::_mark_finalize_step`), so
    the warning has to arrive BEFORE the wrap-up runs — which is why it is issued at the same gate as
    the refusal, in `cli/__init__.py::_engine`, before any role is built.

    Returns the operator-facing warning, or None when every required endpoint answers.
    """
    failures = _probe_role_endpoints(settings, timeout_s=timeout_s)
    if not failures:
        return None
    return (
        "⚠ LLM endpoint unreachable while wrapping up: " + "; ".join(failures)
        + ".\n  This run is over, so no proposal can degrade — wrapping it up anyway. What the "
          "missing model costs:\n"
          "    · end-of-run report → the placeholder \"(report unavailable)\", not the written "
          "report\n"
          "    · cross-run lessons, skills and curation → nothing the model would have authored; "
          "the\n      reflection note and each curation log still record this run deterministically"
          "\n  Everything else is computed without a model and lands normally: budget summary, "
          "diversity\n  archive, case + concept capsule, LLM cost roll-up, readmodel.sqlite, "
          "trace.json, tree.html.\n"
          "  Each step is marked COMPLETE once attempted, so a later `looplab finalize` will NOT "
          "redo it.\n  If you want the model-written report and lessons, stop now, bring the "
          "endpoint back, and\n  re-run this command.")
