"""Agent Skills (I18, ADR-9): a directory of SKILL.md (or *.md) files, each with
frontmatter `name`/`description` + a body of instructions. Progressive disclosure —
the agent first sees only name+description (cheap), and pulls the full body on demand
via the `use_skill` tool. Drops into the agentic toolset like KnowledgeTools.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FM = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    body: str


def _parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8-sig", errors="replace")  # utf-8-sig drops a BOM; won't crash load
    name, desc, body = (path.parent.name if path.name == "SKILL.md" else path.stem), "", text.strip()
    m = _FM.match(text)
    if m:
        fm, body = m.group(1), m.group(2).strip()
        for line in fm.splitlines():
            key, _, val = line.partition(":")
            key, val = key.strip().lower(), val.strip()
            if key == "name" and val:
                name = val
            elif key == "description":
                desc = val
    return Skill(name=name, description=desc, body=body)


class SkillLibrary:
    def __init__(self, skills_dir: str):
        self.dir = Path(skills_dir)
        self.skills: dict[str, Skill] = {}
        paths = list(self.dir.glob("**/SKILL.md")) + list(self.dir.glob("*.md"))
        for p in sorted(set(paths)):
            s = _parse_skill(p)
            self.skills[s.name] = s


class SkillTools:
    """Tool provider for the agentic Researcher: list_skills / use_skill."""

    def __init__(self, skills_dir: str):
        self.lib = SkillLibrary(skills_dir)

    def specs(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": "list_skills",
                "description": "List available skills (name + one-line description).",
                "parameters": {"type": "object", "properties": {}, "required": []}}},
            {"type": "function", "function": {
                "name": "use_skill",
                "description": "Load the full instructions of a skill by name.",
                "parameters": {"type": "object",
                               "properties": {"name": {"type": "string"}},
                               "required": ["name"]}}},
        ]

    def execute(self, name: str, args: dict) -> str:
        if name == "list_skills":
            return "\n".join(f"{s.name}: {s.description}"
                             for s in self.lib.skills.values()) or "(no skills)"
        if name == "use_skill":
            s = self.lib.skills.get(args.get("name", ""))
            return s.body if s else f"(no such skill: {args.get('name')})"
        return f"(unknown tool: {name})"
