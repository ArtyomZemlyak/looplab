"""Pure execution-plan helpers for LLM consumers hidden behind role backends.

Settings alone describe configured role targets, but they do not say which task-owned consumers are
reachable. Startup credential validation and endpoint preflight must agree: a script-writing
validation fallback and Repo onboarding can consume an in-process Developer target even while the
node Developer starts as an external CLI. A merely potential live Strategy swap is checked lazily
by the Developer factory if it is ever requested; it is not a startup consumer.

This module deliberately builds no roles and no clients.  It is safe to call before either startup
gate, preserving the invariant that a failed preflight has not constructed role machinery yet.
"""
from __future__ import annotations

from dataclasses import dataclass

from looplab.core.llm import LLM_ROLE_KEYS, llm_credential_consumers


def role_has_client_leaf(obj) -> bool:
    """Whether rebinding can reach a writable client slot without orphaning an inner client."""

    from inspect import getattr_static
    missing, pending, seen = object(), [obj], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        children = tuple(getattr(current, attr, None) for attr in ("inner", "fallback"))
        pending.extend(children)
        try:
            getattr_static(current, "client")
            descriptor = getattr_static(type(current), "client", missing)
            if isinstance(descriptor, property):
                if descriptor.fset is not None and all(child is None for child in children):
                    return True
            elif (descriptor is missing or hasattr(descriptor, "__set__")
                  or hasattr(current, "__dict__")):
                return True
        except AttributeError:
            pass
    return False


@dataclass(frozen=True)
class LlmConsumerPlan:
    """The strict in-process provider consumers for one task/configuration."""

    shared_active: bool
    roles: frozenset[str | None]
    # External Developer stage roles whose credential belongs to the in-process validation
    # fallback.  The nested coding-agent process still authenticates from its own store.
    external_fallback_roles: frozenset[str]
    # Task-owned run-start, pre-search agents whose clients are constructed outside make_roles.
    # Repo onboarding always resolves role="developer", including in unified mode.
    onboarder_roles: frozenset[str] = frozenset()

    @property
    def trusted_in_process_roles(self) -> frozenset[str]:
        """External-backend role names that are nevertheless consumed inside LoopLab."""

        return self.external_fallback_roles | self.onboarder_roles


def external_developer_stage_roles(settings) -> frozenset[str]:
    """Developer role keys selected by split versus unified composition."""

    return (frozenset({"implement", "repair"})
            if getattr(settings, "unified_agent", False)
            else frozenset({"developer"}))


def external_developer_fallback_uses_llm(task, settings) -> bool:
    """Whether validation can reach a LoopLab-managed LLM Developer for this task.

    Shipped adapters declare the capability explicitly.  A legacy custom repo-like adapter keeps
    the historical no-credential behaviour by default; an unknown non-repo adapter is treated
    conservatively as LLM-backed so startup cannot report a false-green fallback target.
    """

    if (getattr(settings, "backend", "toy") != "llm"
            or getattr(settings, "developer_backend", "default") == "default"
            or not getattr(settings, "validate_agent", True)):
        return False
    declared = getattr(task, "external_fallback_uses_llm", None)
    if declared is not None:
        return bool(declared() if callable(declared) else declared)
    return not callable(getattr(task, "repo_spec", None))


def external_developer_llm_roles(task, settings) -> frozenset[str]:
    """External Developer stage keys that remain reachable through validation fallback."""

    if not external_developer_fallback_uses_llm(task, settings):
        return frozenset()
    return external_developer_stage_roles(settings)


def task_onboarder_llm_roles(task, settings) -> frozenset[str]:
    """Strict LLM roles consumed by an active task-owned onboarder, without building it."""

    if getattr(settings, "backend", "toy") != "llm":
        return frozenset()
    declared = getattr(task, "onboarder_llm_roles", None)
    if not callable(declared):
        return frozenset()
    roles = frozenset(declared(settings) or ())
    invalid = sorted(repr(role) for role in roles
                     if not isinstance(role, str) or role not in LLM_ROLE_KEYS)
    if invalid:
        raise ValueError(
            "onboarder_llm_roles returned unknown/non-string role(s): " + ", ".join(invalid))
    return roles


def llm_consumer_plan(task, settings) -> LlmConsumerPlan:
    """Return the exact strict in-process LLM consumers without constructing roles."""

    shared_active, configured = llm_credential_consumers(settings)
    roles = set(configured)
    fallback_roles: frozenset[str] = frozenset()
    if getattr(settings, "developer_backend", "default") != "default":
        stages = external_developer_stage_roles(settings)
        roles.difference_update(stages)
        fallback_roles = external_developer_llm_roles(task, settings)
        roles.update(fallback_roles)
    # Deliberately AFTER external-stage subtraction: RepoTask.make_onboarder is a trusted in-process
    # Developer consumer even when the node Developer is an isolated external CLI. In split mode it
    # uses the same role spelling that was just subtracted; in unified mode it is still `developer`,
    # not `implement`/`repair`.
    onboarder_roles = task_onboarder_llm_roles(task, settings)
    roles.update(onboarder_roles)
    return LlmConsumerPlan(shared_active, frozenset(roles), fallback_roles, onboarder_roles)


def llm_consumer_roles(task, settings) -> frozenset[str | None]:
    """Compatibility-sized view for callers that need only the role-key set."""

    return llm_consumer_plan(task, settings).roles
