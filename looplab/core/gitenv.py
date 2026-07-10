"""Credential-scrubbed git environment for child processes (moved verbatim from
tools/shell_tools.py — a pure os.environ reader belongs in core, so runtime/bg_tasks can import
it DOWNWARD instead of the tools layer reaching up via a load-bearing lazy import).

Consumers: tools/shell_tools (its own git subprocesses; re-exports these names for its tests)
and runtime/bg_tasks (background git commands)."""
from __future__ import annotations

# Only these host GIT_* vars are passed through to a `git` child (see shell_tools.exec_argv): the
# multi-var config (which `_run_argv` would partially scrub because GIT_CONFIG_KEY_* contains "KEY")
# + commit identity. Deliberately EXCLUDES credential-bearing vars (GIT_ASKPASS, GIT_SSH_COMMAND,
# GIT_HTTP_EXTRAHEADER, GIT_TOKEN, …) so a token can't reach a git subprocess whose stdout is
# returned to a remote model.
_GIT_IDENTITY = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                 "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}

# Substrings in a git-config KEY NAME that mark the pair as credential-bearing. `GIT_CONFIG_KEY_n` /
# `GIT_CONFIG_VALUE_n` pairs (the multi-var config protocol) can smuggle the SAME secret the exclusion
# above blocks — e.g. KEY_0=http.extraheader VALUE_0="Authorization: Bearer <token>" (a common CI
# injection). Passing those through would let `git config --list` print the token into a tool result
# returned to (and traced for) a possibly-remote model. Matched on the config KEY name (not a broad
# "credential", which would wrongly drop the SAFE `credential.interactive=false` prompt-disabler).
_GIT_CRED_KEY_MARKERS = ("extraheader", "askpass", "sshcommand", "proxyauthorization")


def git_config_env() -> dict:
    import os as _os
    env = _os.environ
    out: dict = {}
    # Pass through commit identity + any non-COUNT-protocol GIT_CONFIG_* (GLOBAL/SYSTEM/NOSYSTEM…).
    for k, v in env.items():
        if k in _GIT_IDENTITY:
            out[k] = v
        elif (k.startswith("GIT_CONFIG_") and k != "GIT_CONFIG_COUNT"
              and not k.startswith("GIT_CONFIG_KEY_") and not k.startswith("GIT_CONFIG_VALUE_")):
            out[k] = v
    # Rebuild the KEY_i/VALUE_i sequence CONTIGUOUSLY, dropping credential-bearing pairs. git requires
    # indices 0..COUNT-1 with no gaps, so a pair can't just be deleted (git would 'missing config key'
    # and abort) — survivors are renumbered and COUNT is reset to the kept length.
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except (TypeError, ValueError):
        count = 0
    kept: list = []
    for i in range(max(0, count)):
        key = env.get(f"GIT_CONFIG_KEY_{i}")
        if key is None:
            continue
        val = env.get(f"GIT_CONFIG_VALUE_{i}")
        kname, vval = key.lower(), (val or "").lower()
        credentialish = (any(m in kname for m in _GIT_CRED_KEY_MARKERS)
                         or "authorization:" in vval or "bearer " in vval)
        if credentialish:
            continue
        kept.append((key, val))
    for j, (key, val) in enumerate(kept):
        out[f"GIT_CONFIG_KEY_{j}"] = key
        # ALWAYS emit VALUE_j (empty when the original had none): the child inherits the host env, in
        # which sandbox `_run_argv` scrubs GIT_CONFIG_KEY_* (name contains "KEY") but NOT VALUE_* — so a
        # renumbered survivor with no value would otherwise leave a STALE GIT_CONFIG_VALUE_j (a dropped
        # credential's value) visible at that index. Emitting our own value shadows it.
        out[f"GIT_CONFIG_VALUE_{j}"] = val if val is not None else ""
    if "GIT_CONFIG_COUNT" in env:
        out["GIT_CONFIG_COUNT"] = str(len(kept))
    return out
