"""A tool's DECLARED risk agrees with the risk the permission gate ENFORCES.

Review 2026-09-22, TAT-11 (doc 50 TO-10). There are two permission vocabularies: `ToolCapability`
(`tools/_base.py`, declared by a provider, recorded on the tool span) and
`tools/perm_modes.py::_ACTION_RISK` (the real gate, keyed on `(tool_kind, tool)`). They had already
disagreed: `ShellTools` declared `kill_background` `risk="medium"` while the gate made it HIGH (it ends
a process the operator started). The span said one thing and the gate did another.

The two scales differ, so the agreement is stated as a mapping: a gated READ is a declared `low`, a
gated HIGH a declared `high`, and REVERSIBLE/CONSEQUENTIAL a declared `medium`. Every provider that
declares capabilities is checked against every gate row naming one of its tools. MUTATION: put
`kill_background` back to `medium` -> red, naming the tool.
"""
from __future__ import annotations

from looplab.tools import perm_modes
from looplab.tools.shell_tools import ShellTools

_DECLARED_FOR = {
    perm_modes.RISK_READ: {"low"},
    perm_modes.RISK_REVERSIBLE: {"medium"},
    perm_modes.RISK_CONSEQUENTIAL: {"medium"},
    perm_modes.RISK_HIGH: {"high"},
}


def _declaring_providers(tmp_path):
    # The providers that declare capabilities AND act through the central gate. Other declaring
    # providers (dev commands, the probe, env_inspect, reposcout, MCP) have no `_ACTION_RISK` rows.
    return {"shell": ShellTools([tmp_path], mode="auto")}


def test_every_declared_risk_agrees_with_the_gated_risk(tmp_path):
    disagreements, checked = [], 0
    for kind, provider in _declaring_providers(tmp_path).items():
        declared = {cap.name: cap.risk for cap in provider.capabilities()}
        for (row_kind, tool), gated in perm_modes._ACTION_RISK.items():
            if row_kind != kind or tool not in declared:
                continue
            checked += 1
            if declared[tool] not in _DECLARED_FOR[gated]:
                disagreements.append(f"{kind}/{tool}: declared {declared[tool]!r}, gated {gated}")
    assert checked >= 5, "precondition: the shell provider's five gated tools were compared"
    assert not disagreements, "a declared tool risk contradicts the enforced one:\n  " + (
        "\n  ".join(disagreements))


def test_every_gated_shell_tool_is_declared(tmp_path):
    """The other direction: a gate row naming a shell tool the provider does not declare is a
    renamed or removed verb, which the gate would then answer as UNKNOWN (asks even in Auto)."""
    declared = {cap.name for cap in ShellTools([tmp_path], mode="auto").capabilities()}
    gated = {tool for (kind, tool) in perm_modes._ACTION_RISK if kind == "shell"}
    assert gated <= declared, f"gated shell tools with no declared capability: {sorted(gated - declared)}"
