"""A Researcher that may PROPOSE artifact nodes (doc 73 §1.4, stage 3), under
`Settings.researcher_artifacts`.

WHY (2026-10-08). Artifact nodes — a node whose pipeline PRODUCES what later experiments read, such
as a prepared dataset — could only be created by an operator inject or the Assistant. The role that
plans the experiments, and that sees ten nodes in a row re-tokenize the same corpus, had no way to
say "prepare this once".

WHAT CHANGES WHEN ON, and only then:

* the emit schema is `core/models.py::ArtifactIdeaEmission` — `IdeaEmission` with `node_kind` and
  `uses` VISIBLE (they exist on `Idea` hidden from the schema and omitted from every dump when empty,
  so OFF is the historical schema and the historical payload byte for byte);
* the user turn carries `artifact_cue` — the PRODUCED ARTIFACTS this run holds, the ids `uses` may
  name.

OFF, an emit that carries either key anyway (a model guessing at a field it was never shown) has it
stripped before validation (`strip_artifact_fields`), so a flag-off run can never mint an artifact.
The fold keeps only `uses` ids that name artifact nodes (`events/replay.py::_idea_uses`), and the
consumer fence (`engine/artifact_fence.py`) decides at evaluation whether they are produced.

The flag is RUN-PINNED (`LEGACY_CONFIG_SNAPSHOT_DEFAULTS` holds it False), because it changes a paid
prompt: a resumed pre-field run keeps its historical request.
"""
from __future__ import annotations

ARTIFACT_IDEA_FIELDS = ("node_kind", "uses")

_CUE_MAX_ARTIFACTS = 12
_CUE_RATIONALE_CHARS = 160


def researcher_artifacts_enabled(settings) -> bool:
    """THE ONE READER of `Settings.researcher_artifacts`. A duck-typed or absent settings object reads
    OFF — the historical schema and prompt."""
    return getattr(settings, "researcher_artifacts", False) is True


def mark_artifact_researcher(researcher, settings) -> bool:
    """`make_roles`' one call: read the flag, set it on a plain Researcher that has the attribute
    (`roles.py::LLMResearcher`; the tool-using one takes it at its constructor), return it."""
    enabled = researcher_artifacts_enabled(settings)
    if enabled and hasattr(researcher, "artifact_ideas"):
        researcher.artifact_ideas = True
    return enabled


def emission_model(enabled: bool):
    """The emit schema class: `ArtifactIdeaEmission` when on, the historical `IdeaEmission` off."""
    from looplab.core.models import ArtifactIdeaEmission, IdeaEmission
    return ArtifactIdeaEmission if enabled else IdeaEmission


def strip_artifact_fields(args, enabled: bool):
    """`args` less the two artifact keys when the flag is off; untouched when on (or not a dict)."""
    if enabled or not isinstance(args, dict):
        return args
    return {k: v for k, v in args.items() if k not in ARTIFACT_IDEA_FIELDS}


def drop_artifact_fields(idea, enabled: bool):
    """The parsed Idea less the two artifact fields when the flag is off — the structured-output
    parsers validate against `IdeaEmission`, which accepts the hidden fields, so a flag-off run
    clears what a model volunteered after the parse rather than before it."""
    if enabled or (getattr(idea, "node_kind", None) is None and not getattr(idea, "uses", None)):
        return idea
    return idea.model_copy(update={"node_kind": None, "uses": []})


def artifact_turn(enabled: bool, state):
    """One proposal's artifact half, for the plain Researcher's single structured call
    (`roles.py::LLMResearcher.propose`): the cue its user turn appends ("" off), the emit model it
    parses with, and the drop applied to what it parsed."""
    from types import SimpleNamespace
    return SimpleNamespace(cue=artifact_cue(state) if enabled else "", model=emission_model(enabled),
                           drop=lambda idea: drop_artifact_fields(idea, enabled))


def artifact_cue(state) -> str:
    """The user-turn block naming the run's PRODUCED artifact nodes (evaluated in their current
    lifecycle) and the two fields — every line derived from the fold, nothing from model text but the
    bounded rationale the node was proposed with."""
    produced = []
    for node in sorted((getattr(state, "nodes", None) or {}).values(), key=lambda n: n.id):
        status = getattr(getattr(node, "status", None), "value", getattr(node, "status", ""))
        if (getattr(node, "kind", None) == "artifact" and status == "evaluated"
                and not getattr(node, "tombstoned", False)):
            produced.append(node)
    lines = ["\n\nPRODUCED ARTIFACTS (preparation nodes later experiments can read):"]
    if not produced:
        lines.append("- none yet")
    for node in produced[-_CUE_MAX_ARTIFACTS:]:
        rationale = " ".join(str(getattr(node.idea, "rationale", "") or "").split())
        lines.append(f"- #{node.id}: {rationale[:_CUE_RATIONALE_CHARS] or '(no rationale)'}")
    lines.append(
        'To read one, list its id in `uses`. To have an expensive preparation step done ONCE for '
        'several later experiments, propose a node with `node_kind: "artifact"` — it produces files '
        "and is never ranked — and have the experiments after it name it in `uses`. An artifact's "
        "rationale must say WHAT files it writes and WHERE under its own working directory: that is "
        "where the experiments after it read them.")
    return "\n".join(lines)
