"""Endpoint reachability preflight for a live-backend run.

The sibling of `core/llm.py::validate_bound_profiles`, and deliberately its neighbour at the same
gate (`cli/__init__.py::_engine`): that one fails before transport when a role's CREDENTIAL is
unusable, this one fails before the loop when the role's endpoint is simply not THERE.

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
        return                                  # backend != "llm": no role talks to a provider
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
    if failures:
        raise LLMError(
            "LLM endpoint preflight failed: " + "; ".join(dict.fromkeys(failures))
            + ". The run needs a reachable model for these roles — start the endpoint, or point "
              "LOOPLAB_LLM_BASE_URL / --model at one. Run offline with `--backend toy` "
              "(or -s backend=toy). Refusing to start: the roles would degrade to empty fallback "
              "proposals and the run would report success on a flat, meaningless result.")
