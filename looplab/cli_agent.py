"""External CLI coding agents as a Developer backend (ADR-7), tool-agnostic.

A `CliAgentDeveloper` runs any terminal coding agent head-less as a subprocess that
edits a single `solution.py` in a throwaway dir, pointed at our local OpenAI-compatible
endpoint (Ollama), then reads the file back as the solution code. The specific tool is
just a `CliAgentSpec` preset (argv template + env + whether it needs a git repo), so
switching agents (OpenCode / aider / goose / continue) is config, not code.

Placeholders in the argv template: `{message}`, `{model}`, `{file}`.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .models import Idea
from .validate import AgentRun

_SEED = 'import json\n\n# TODO: implement the solution.\nprint(json.dumps({"metric": 0.0}))\n'


def opencode_config(host: str, model: str) -> str:
    """Self-contained OpenCode project config declaring a local OpenAI-compatible
    provider (Ollama) — dropped into the agent's workdir so `opencode run` never reaches
    for the external model registry (the fetch that hangs behind a TLS-intercepting
    proxy). `model` is the agent's "provider/model" id, e.g. "ollama/qwen3:8b"."""
    provider, _, name = model.partition("/")
    if not name:                     # bare 'qwen3:8b' or malformed 'ollama/' -> ollama provider
        provider, name = "ollama", (model.strip("/") or model)
    base = host.rstrip("/").removesuffix("/v1") + "/v1"
    return json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            provider: {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": base},
                "models": {name: {"name": name}},
            }
        },
    }, indent=2)


@dataclass
class CliAgentSpec:
    name: str
    argv: list[str]                                   # tokens with {message}/{model}/{file}
    needs_git: bool = False
    env: Callable[[str], dict] = field(default=lambda host: {})  # host -> extra env


def _host(base_url: str) -> str:
    return base_url.rstrip("/").removesuffix("/v1")


def _resolve_launcher(name: str) -> list[str]:
    """Resolve a bare command name to something `subprocess` (no shell) can execute.

    On Windows a globally-installed npm CLI like `opencode` is a `.cmd`/`.ps1` shim, not
    an `.exe`; `subprocess.run(["opencode", ...])` then fails with WinError 2 (the bug
    that made every pipeline node silently fall back). We resolve via PATH and, for npm
    shims, prefer the real bundled `.exe` so the agent's multi-line prompt isn't
    re-parsed by cmd.exe. Returns the original name unchanged if PATH lookup fails (the
    launch then raises a clear OSError the validator reports as `agent_launched=False`).
    """
    import shutil

    p = shutil.which(name)
    if not p:
        return [name]
    if os.name == "nt" and p.lower().endswith((".cmd", ".bat", ".ps1")):
        # Prefer the real bundled binary: exact package dirs first (deterministic), then
        # any package, so two packages shipping `bin/<name>.exe` don't pick at random.
        parent = Path(p).parent
        for pat in (f"node_modules/{name}/bin/{name}.exe",
                    f"node_modules/@*/{name}/bin/{name}.exe",
                    f"node_modules/*/bin/{name}.exe"):
            exes = sorted(parent.glob(pat))
            if exes:
                return [str(exes[0])]  # real binary -> no cmd.exe arg mangling
        if p.lower().endswith((".cmd", ".bat")):
            return [p]             # subprocess can run a .cmd/.bat shim directly
        # .ps1 with no bundled .exe: cmd.exe can't run it -> invoke PowerShell.
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", p]
    return [p]


# --- presets ------------------------------------------------------------------
# OpenCode: `opencode run "<prompt>" --model <provider>/<model>`; reads the Ollama
# provider from an opencode.json the caller drops in the workdir (see make_roles).
PRESETS: dict[str, CliAgentSpec] = {
    "opencode": CliAgentSpec(
        name="opencode",
        argv=["opencode", "run", "{message}", "--model", "{model}"],
        needs_git=False,
        env=lambda host: {"OLLAMA_API_BASE": host, "OPENAI_BASE_URL": host + "/v1"},
    ),
    "aider": CliAgentSpec(
        name="aider",
        argv=["aider", "--model", "{model}", "--yes-always", "--no-stream",
              "--no-check-update", "--no-analytics", "--no-show-model-warnings",
              "--message", "{message}", "{file}"],
        needs_git=True,
        env=lambda host: {"OLLAMA_API_BASE": host},
    ),
    "goose": CliAgentSpec(
        name="goose",
        argv=["goose", "run", "--text", "{message}"],
        needs_git=False,
        env=lambda host: {"OLLAMA_HOST": host},
    ),
    "continue": CliAgentSpec(
        name="continue",
        argv=["cn", "-p", "{message}"],
        needs_git=False,
        env=lambda host: {"OLLAMA_API_BASE": host},
    ),
}


class CliAgentDeveloper:
    def __init__(self, model: str, base_url: str = "http://localhost:11434/v1",
                 brief: str = "", spec: Optional[CliAgentSpec] = None,
                 cmd_override: Optional[list] = None, timeout: float = 600.0,
                 workdir_files: Optional[dict] = None,
                 patch_gate: bool = False, surface: Optional[list] = None,
                 seed_dir: Optional[str] = None, seed_dirs: Optional[list] = None,
                 protect: Optional[list] = None, editable_prefixes: Optional[list] = None):
        # seed_dir(s): seed the agent's worktree from existing repo tree(s) (RepoTask) instead
        # of a single solution.py — the agent edits real repo files; the patch gate diffs
        # against that worktree and returns the accepted in-surface edits as `last_files`.
        # Phase 4: `seed_dirs` is a list of {name, path}; each repo mounts at its subdir
        # (name="." -> worktree root). `seed_dir` is the single-repo-at-root shorthand.
        if seed_dirs is None and seed_dir is not None:
            seed_dirs = [{"name": ".", "path": seed_dir}]
        self.seed_dirs = seed_dirs
        self.seed_dir = seed_dir
        self.model = model                       # provider/model, e.g. "ollama/qwen3:8b"
        self.host = _host(base_url)
        self.brief = brief
        self.spec = spec or PRESETS["opencode"]
        self.cmd_override = cmd_override          # replace argv[0..] launcher (e.g. binary path or stub)
        self.timeout = timeout
        self.workdir_files = workdir_files or {}  # extra files to drop in (e.g. opencode.json)
        # Patch-gated multi-file mode (ADR-7 Rule 3): run the agent in a git worktree,
        # diff its changes, and accept only edits within the `surface` allow-list (globs).
        self.patch_gate = patch_gate
        self.surface = surface or ["*.py"]
        # Protected paths (eval/grader/metric/adapter): rejected by the gate even when in
        # surface, so the agent can never edit the files that author/feed the score.
        self.protect = protect or []
        # Named multi-editable repo subdirs — scopes each repo's surface to its own subdir.
        self.editable_prefixes = editable_prefixes or []
        # Per-invocation audit signal, read by the ValidatingDeveloper (ADR-7):
        self.last_run: Optional[AgentRun] = None  # process-level result of the last run
        self.last_seed: str = ""                  # file content handed to the last run
        self.last_files: dict[str, str] = {}      # accepted in-surface files (multi-file)
        self.last_deleted: list[str] = []         # accepted in-surface DELETIONS (applied at eval)
        self.last_patch: Optional[dict] = None    # surface-gate verdict {ok,paths,rejected}

    def _argv(self, message: str, file: str) -> list[str]:
        subst = {"{message}": message, "{model}": self.model, "{file}": file}
        base = list(self.spec.argv)
        if self.cmd_override:                      # explicit launcher path: use as-is
            base = list(self.cmd_override) + base[1:]
        else:                                      # bare preset name -> resolve on PATH
            base = _resolve_launcher(base[0]) + base[1:]
        return [subst.get(tok, tok) for tok in base]

    def _run(self, message: str, seed_code: str) -> str:
        self.last_seed = seed_code
        self.last_run = AgentRun()
        self.last_files = {}
        self.last_deleted = []
        self.last_patch = None
        with tempfile.TemporaryDirectory(prefix="LOOPLAB_cliagent_") as d:
            wd = Path(d)
            if self.seed_dirs:                   # RepoTask: seed the worktree from the repo(s)
                import shutil
                ig = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv",
                                            "node_modules")
                for s in self.seed_dirs:         # each editable repo at its subdir
                    dst = wd if s["name"] in (".", "") else wd / s["name"]
                    shutil.copytree(s["path"], dst, dirs_exist_ok=True, ignore=ig)
            else:
                (wd / "solution.py").write_text(seed_code, encoding="utf-8")
            for name, content in self.workdir_files.items():
                (wd / name).write_text(content, encoding="utf-8")
            # A committed git seed is needed for the patch gate, repo seeding, and aider.
            seed_sha = (_git_seed(wd)
                        if (self.patch_gate or self.seed_dirs or self.spec.needs_git)
                        else None)
            env = {**os.environ, **self.spec.env(self.host)}
            argv = self._argv((self.brief + "\n\n" + message).strip(), "solution.py")
            try:
                # encoding/errors explicit: agents print UTF-8 glyphs (·, →) that the
                # Windows locale codec (cp1252) can't decode — the default text=True
                # crashes the stdout reader thread mid-run and loses the captured output.
                p = subprocess.run(argv, cwd=str(wd), env=env, timeout=self.timeout,
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
                self.last_run = AgentRun(launched=True, exit_code=p.returncode,
                                         stdout_tail=(p.stdout or "")[-2000:],
                                         stderr_tail=(p.stderr or "")[-2000:])
            except subprocess.TimeoutExpired as e:
                self.last_run = AgentRun(launched=True, timed_out=True,
                                         stderr_tail=str(e)[-2000:])
            except OSError as e:
                # binary missing / not executable -> leave the seed; the validator flags
                # `agent_launched=False` and the loop's eval/debug copes.
                self.last_run = AgentRun(launched=False, stderr_tail=str(e)[-2000:])
            if (self.patch_gate or self.seed_dirs) and seed_sha:
                return self._collect_gated(wd, seed_sha)
            return _read_if(wd / "solution.py")

    def _collect_gated(self, wd: Path, seed_sha: str) -> str:
        """Diff the agent's changes (vs the seed commit, robust to agents that make their
        own commits) and accept them only if every touched path is within the edit-surface
        allow-list (ADR-7 Rule 3, reject-not-strip). On rejection the whole change set is
        reverted to the seed (the validator then flags the no-op)."""
        from . import patch as _patch
        if _git(wd, "add", "-A") != 0:               # stale index -> can't trust the diff
            self.last_patch = {"ok": False, "paths": [], "rejected": [], "error": "git add failed"}
            return self.last_seed
        diff = _git_out(wd, "diff", "--cached", seed_sha)
        g = _patch.gate(diff, self.surface, self.protect, self.editable_prefixes)
        self.last_patch = {"ok": g["ok"], "paths": g["paths"], "rejected": g["rejected"]}
        if not g["ok"]:
            # Reject the entire patch (reject-not-strip). Return the known seed string
            # directly rather than reading the worktree back — a failed revert (e.g. a
            # Windows file lock) must never let rejected code leak through.
            _git(wd, "reset", "--hard", "-q", seed_sha)
            _git(wd, "clean", "-fdq")
            return self.last_seed
        files: dict[str, str] = {}
        deleted: list[str] = []
        for rel in g["paths"]:
            fp = wd / rel
            if fp.is_file():
                files[rel] = fp.read_text(encoding="utf-8", errors="replace")
            else:
                deleted.append(rel.replace("\\", "/"))   # accepted in-surface deletion
        self.last_files = files
        self.last_deleted = deleted
        # `code` is the solution.py entrypoint when present; for a RepoTask (no single
        # entrypoint) it may be absent — the eval runs the command over `last_files`, not code.
        return files.get("solution.py", _read_if(wd / "solution.py"))

    def implement(self, idea: Idea) -> str:
        if self.seed_dirs:                       # RepoTask: edit the existing repo(s)
            msg = (f"Edit the repository files (within the allowed paths) to implement: "
                   f"{idea.rationale} Parameters: {idea.params}.").strip()
            return self._run(msg, "")
        # "Write … completely, overwriting" biases the agent toward its write tool. Small
        # models are unreliable with edit/diff tools (oldString-match failures leave the
        # seed unchanged or produce truncated code); a full rewrite is far more robust.
        msg = (f"Write solution.py completely, overwriting the existing file, to "
               f"implement the solution with parameters {idea.params}. {idea.rationale}"
               ).strip()
        return self._run(msg, _SEED)

    def repair(self, idea: Idea, code: str, error: str) -> str:
        # Fold in the idea rationale — the ValidatingDeveloper appends the validator's rejection
        # feedback to it per retry; without it a validation-retry re-sends an identical prompt.
        extra = ("\n" + idea.rationale) if (idea is not None and getattr(idea, "rationale", "")) else ""
        if self.seed_dirs:                       # RepoTask: fix the repo edits in place
            return self._run(
                f"The eval failed with:\n{error}\nEdit the repository files to fix it.{extra}", "")
        return self._run(
            f"Rewrite solution.py completely (overwrite the whole file) to fix this "
            f"error:\n{error}\nReturn a corrected, complete script.{extra}", code)


def _read_if(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def _git(cwd: Path, *args: str) -> int:
    """Run a git command in `cwd`; return its exit code (-1 if git is unavailable).
    Explicit UTF-8/replace: git emits UTF-8, and the default cp1252 decode on Windows
    would raise UnicodeDecodeError (not OSError) and escape the guard."""
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=60).returncode
    except (OSError, subprocess.TimeoutExpired):   # missing git OR a hung git -> treat as failure
        return -1


def _git_out(cwd: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=60,
                           text=True, encoding="utf-8", errors="replace")
        return p.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _git_seed(wd: Path) -> Optional[str]:
    """Init a repo and commit the seed worktree. Returns the seed commit SHA, or None
    (degrade to whole-file readback) if git isn't available — never raises."""
    if _git(wd, "init", "-q") != 0:
        return None
    _git(wd, "add", "-A")
    if _git(wd, "-c", "user.email=a@LoopLab", "-c", "user.name=LoopLab",
            "commit", "-q", "-m", "seed") != 0:
        return None
    return _git_out(wd, "rev-parse", "HEAD").strip() or None
