"""Git tool provider for the assistant. A thin layer over `ShellTools.exec_argv`, so cwd-confinement,
the docker wrap and the permission MODE are shared with the shell provider. Read-only verbs
(status/diff/log/branch-list) always run inline (safe in every mode incl. plan); mutating verbs
(add/commit/checkout/new-branch) follow the ask/auto rule. `push` is intentionally omitted (a remote,
irreversible action) — the user can run it explicitly via run_command in auto mode if they want.
"""
from __future__ import annotations


from looplab.tools._base import fn_spec


class GitTools:
    def __init__(self, shell, cwd=None):
        self.shell = shell          # a ShellTools — reuse its gated exec_argv
        self.cwd = str(cwd) if cwd else None

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("git_status", "Show `git status --short` for the repo.", {}, []),
            fn_spec("git_diff", "Show `git diff` (optionally for one path).",
                     {"path": {"type": "string"}}, []),
            fn_spec("git_log", "Show recent commits (`git log --oneline -n N`).",
                     {"n": {"type": "integer"}}, []),
            fn_spec("git_add", "Stage files (`git add`).",
                     {"paths": {"type": "array", "items": {"type": "string"}}}, ["paths"]),
            fn_spec("git_commit", "Commit staged changes with a message (`git commit -m`).",
                     {"message": {"type": "string"}}, ["message"]),
            fn_spec("git_branch", "List branches, or create one when `name` is given.",
                     {"name": {"type": "string"}}, []),
            fn_spec("git_checkout", "Switch to a branch/ref (`git checkout`).",
                     {"ref": {"type": "string"}}, ["ref"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "git_status":
                return self._ro(["git", "status", "--short"], "git status")
            if name == "git_diff":
                path = args.get("path")
                # `--` so a model-supplied path starting with '-' can't become a git option (e.g.
                # `--output=<file>` would make this "read-only" verb write a file).
                return self._ro(["git", "diff"] + (["--", str(path)] if path else []), "git diff")
            if name == "git_log":
                n = int(args.get("n") or 10)
                return self._ro(["git", "log", "--oneline", "-n", str(n)], "git log")
            if name == "git_branch":
                nm = str(args.get("name") or "").strip()
                if nm:
                    if nm.startswith("-"):   # a leading dash would be parsed as a git OPTION, not a name
                        return f"(refused: {nm!r} is not a valid branch name)"
                    return self._mut(["git", "branch", nm], f"git branch {nm}", "git_branch")
                return self._ro(["git", "branch", "--list"], "git branch")
            if name == "git_add":
                paths = [str(p) for p in (args.get("paths") or []) if p]
                if not paths:
                    return "(git_add needs a non-empty list of paths)"
                # `--` stops git from parsing a path that starts with '-' as an option (git option
                # injection from a model-supplied path).
                return self._mut(
                    ["git", "add", "--", *paths], f"git add {' '.join(paths)[:60]}", "git_add")
            if name == "git_commit":
                msg = str(args.get("message") or "").strip()
                if not msg:
                    return "(git_commit needs a message)"
                # Inject a fallback committer identity so an auto-commit succeeds on a fresh box where
                # git user.name/user.email aren't configured (otherwise git aborts with "Author identity
                # unknown / Please tell me who you are"). A real global/local git config still wins —
                # `-c` only sets a default for this one invocation.
                return self._mut(["git", "-c", "user.name=LoopLab",
                                  "-c", "user.email=looplab@localhost", "commit", "-m", msg],
                                 f"git commit -m {msg[:50]}", "git_commit")
            if name == "git_checkout":
                ref = str(args.get("ref") or "").strip()
                if not ref:
                    return "(git_checkout needs a ref)"
                # A leading dash WOULD be parsed as a git option even with the trailing `--` (which
                # only disambiguates ref-vs-path): `git checkout -f --` silently discards all
                # uncommitted changes. Refuse dash-refs outright; no real ref starts with '-'.
                if ref.startswith("-"):
                    return f"(refused: {ref!r} is not a valid ref)"
                return self._mut(
                    ["git", "checkout", ref, "--"], f"git checkout {ref}", "git_checkout")
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 - never crash the loop
            return f"(error: {e})"

    def _ro(self, argv, label) -> str:
        return self.shell.exec_argv(argv, self.cwd, "git_ro", label)

    def _mut(self, argv, label, action_id) -> str:
        return self.shell.exec_argv(
            argv, self.cwd, "git_mut", label, action_id=action_id)
