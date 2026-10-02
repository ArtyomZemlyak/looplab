"""Dedicated base authoring role, shared by Assistant and external harness agents.

The caller supplies its already-accounted model/author function. This role owns
generalization instructions and validation; it never edits the owner checkout,
executes an opaque training command, certifies a score or advances a base.
"""
from looplab.core.errors import UpstreamRefusal


class Maintainer:
    instruction = (
        "Act as LoopLab Maintainer: generalize a measured node's reusable capability in the "
        "run-owned base worktree. Keep scientific recipe knobs in recipe_files. New behavior "
        "must have an explicit flag with the prior default; remove node-specific paths/model "
        "hardcodes. Reuse existing runners rather than duplicate them. Document the flag and "
        "provide tests. Never change protected evaluator files, labels/split rules or the metric "
        "reader. A critic review and real old/new equivalence plus regression/repair-trigger "
        "probes are required before operator-approved CAS advancement. Your prose is not evidence."
    )

    def propose(self, author, context):
        """Executable authoring seam: any already-accounted model or external author."""
        return self.validate(author(self.instruction, context))

    def validate(self, body):
        required = {"files", "deleted", "recipe_files", "summary", "flag", "documentation_path", "critic"}
        if not isinstance(body, dict) or not required <= set(body):
            raise UpstreamRefusal("upstream_maintainer_invalid", "Maintainer requires patch, recipe, summary, flag, documentation and critic")
        flag, critic = body["flag"], body["critic"]
        if not isinstance(flag, dict) or set(flag) != {"name", "default", "enabled"} or not isinstance(flag["name"], str) or not flag["name"].isidentifier():
            raise UpstreamRefusal("upstream_maintainer_invalid", "Declare a named flag with old default and enabled value")
        if any(not isinstance(flag[k], str) or not flag[k] or len(flag[k]) > 128 for k in flag):
            raise UpstreamRefusal("upstream_maintainer_invalid", "Flag values must be short nonempty strings")
        if flag["default"] == flag["enabled"]:
            raise UpstreamRefusal("upstream_maintainer_invalid", "Flag must distinguish default and enabled behavior")
        if not isinstance(critic, dict) or critic.get("verdict") != "pass" or any(not isinstance(critic.get(k), str) or not critic[k].strip() for k in ("reason", "reviewer")):
            raise UpstreamRefusal("upstream_critic_required", "Record a named critic's reasoned pass; measurements are still required")
        text = body["files"].get(body["documentation_path"]) if isinstance(body["files"], dict) else None
        if not isinstance(text, str) or flag["name"] not in text or not isinstance(body["summary"], str) or not body["summary"].strip():
            raise UpstreamRefusal("upstream_maintainer_invalid", "Patch must document the flag in an editable file and explain generalization")
        return body
