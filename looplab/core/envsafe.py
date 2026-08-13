"""The DECLARED ENVIRONMENT rules, and the two "is this a secret?" screens they rest on.

THIS MODULE EXISTS BECAUSE OF THE LAYERING RULE. `core` imports nothing above itself, and the three
places that must agree about a declared environment sit on both sides of that line:
`core/config.py::Settings.eval_env` (the run level), `adapters/repo_task.py::EvalSpec.env` (the task
level) and `runtime/command_eval.py::validate_stages` (the stage level). Three levels that get MERGED
must accept exactly the same thing, or an operator's variable changes meaning depending on which line
they wrote it on — so the rule lives in `core`, where every one of them may reach it, rather than
being copied.

`SECRET_ENV`/`is_secret_env` moved here for the same reason and no other: the declared-env rule needs
the screen, and `core/config.py` may not import `runtime/sandbox.py`. `runtime/sandbox.py` re-exports
both names, so every existing `from looplab.runtime.sandbox import is_secret_env` still resolves to
the SAME object — this is a move, not a second copy, which is the whole point (see
`tests/test_secret_env_pattern.py`, the joint that holds this screen against `config._SECRET_ENV_NAME`,
the stricter sibling that stayed where it is because it answers a different question).
"""
from __future__ import annotations

import re
from typing import Optional

# Env-var NAMES that look like a secret — redacted from the child process environment so generated
# code can't read (and persist into the event log) the operator's keys/tokens. Name-based, so it
# never touches PATH/SYSTEMROOT/TEMP etc. that a process legitimately needs.
# `core/config.py::_SECRET_ENV_NAME` is the stricter sibling that validates a profile's `api_key_env`;
# `tests/test_secret_env_pattern.py` pins the two, and this one must stay the LOOSER of the pair.
SECRET_ENV = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|API_KEY|AUTH|COOKIE|WEBHOOK|DSN)",
    re.IGNORECASE)
# ...and the class of secret NO name pattern can catch: a connection string carrying inline
# credentials (`postgres://user:pw@host/db`) under a perfectly innocent name — DATABASE_URL,
# MONGO_URI, *_DSN. Matching those names would also strip LOOPLAB_LLM_BASE_URL and every other
# legitimate endpoint, so the VALUE is what is screened: userinfo in the authority is what makes it a
# credential. A plain `https://host/v1` has none and is untouched.
_CREDENTIAL_URL_VALUE = re.compile(r"\A[A-Za-z][A-Za-z0-9+.\-]*://[^/\s@]*:[^/\s@]*@")


def is_secret_env(name: str, value: str = "") -> bool:
    """Whether a variable must be withheld from a child process (candidate code, agent shell, pip).

    Two independent screens: a NAME that declares itself a secret, and a VALUE that is a URL with
    inline credentials. A `print(os.environ)` or a stack trace in generated code would otherwise
    exfiltrate either into the durable stdout tail, on every tier that spawns a child."""
    return bool(SECRET_ENV.search(str(name or ""))) or bool(
        _CREDENTIAL_URL_VALUE.match(str(value or "")))


# --- The DECLARED ENVIRONMENT (`env`) ------------------------------------------------------------
# THE THIRD THING A STAGE DECLARES, and the last one that had no home but CODE. `expect` states what
# a stage writes, `needs` what it reads, and neither could say what it needs SET.
#
# Measured cost (backlog F1d): `rubertlite-dr-unified-v6` node 0 crashed on its first attempt with
# `botocore InvalidAccessKeyId … ListObjects` because the data loader reached S3 with
# `VS_LOCAL_DATA_ROOT` unset. The repair was correct and took three minutes — an
# `os.environ.setdefault(...)` at import in the repo's config. Then node 1 hit the IDENTICAL error,
# because a node is seeded from the SOURCE repo and never from a sibling node, so every node
# rediscovers the same fact and spends one repair attempt on it. At `max_nodes: 14` that is up to
# fourteen repair attempts spent on something the operator knew before the run started. The Developer
# did the best available thing: with no `env` on the stage contract, CODE was the only surface — which
# bakes the environment into the repo under repair, the wrong place for it, and pollutes the diff.
#
# WHO MAY DECLARE IT: the OPERATOR only (`allow_env`, default False — fail closed). The Developer
# authors the scripts, not the environment they run in. An agent that could set arbitrary environment
# for its own protected `score` stage would have another route around the trust boundary the scorer
# freeze exists to hold (`PYTHONPATH` alone re-points every import the scorer makes), so
# `declare_stages` and a hand-written `looplab_stages.json` are refused with a message that names the
# operator surface instead of silently dropping the key.
#
# THREE LEVELS, one merge, most specific wins: `Settings.eval_env` (every stage of every node in the
# run) < `eval.env` (every stage of this task's eval) < `stages[].env` (this stage). The engine
# composes the first two into the process env it already hands the sandbox; only the third is
# per-stage, and `_run_stages` is where it lands.
MAX_STAGE_ENV_VARS = 32
MAX_ENV_VALUE_CHARS = 4096
# POSIX-portable variable names, and deliberately not more: a name outside this set is unreachable
# from `os.environ[...]` in a portable shell/interpreter anyway, and `docker run -e NAME=value` parses
# on `=`, so an embedded `=` in a NAME would silently redefine a different variable in the container.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
# Names the ENGINE owns, refused to every declarer including the operator. Not a style rule — each is
# a value some part of LoopLab computes per eval and then relies on:
#   • CUDA_VISIBLE_DEVICES is the GPU PIN (`engine/resources.py::_resource_eval_env`), reconciled with
#     the Docker `--gpus` args and with `cap_gpu_flags`. A declared one would hand a node its
#     siblings' devices while the host pool lease still says otherwise.
#   • LOOPLAB_* is the engine's own settings namespace (`LOOPLAB_<FIELD>`), so a declared one
#     reconfigures any LoopLab the eval happens to invoke — and `LOOPLAB_READ_FENCE_DIR` in
#     particular is the marker `run_argv` turns into the read fence's PYTHONPATH entry, i.e. the one
#     variable that can switch the fence off from inside the thing being fenced.
ENGINE_OWNED_ENV = ("CUDA_VISIBLE_DEVICES",)
# NOTE the read-fence marker is `runtime/read_fence.py::FENCE_DIR_ENV = "LOOPLAB_READ_FENCE_DIR"`,
# i.e. it is covered by the LOOPLAB_ prefix rule below rather than by its own entry. `core` may not
# import `runtime` to assert that, so `tests/test_stage_environment.py` holds the two together.


def validate_env_map(where: str, values, *,
                     cap: int = MAX_STAGE_ENV_VARS) -> tuple[Optional[dict], Optional[str]]:
    """Validate a declared environment block into its canonical `{NAME: "value"}` form:
    `(clean, None)` or `(None, reason)`. THE single definition, shared by `stages[].env`,
    `EvalSpec.env` and `Settings.eval_env`, so the three levels that get MERGED cannot disagree about
    what a legal declaration is — a name one level accepts and another refuses would be an
    environment that changes meaning depending on where the operator wrote it.

    `where` names the declaring site in every message (`stage 'train' env`, `eval.env`, `eval_env`),
    because all three refusals are read by an operator who has to find the line they wrote.

    SECRETS ARE REFUSED HERE, and that is the whole policy — see `_secret_refusal`.
    """
    if not isinstance(values, dict):
        return None, (f"{where} must be an object of NAME -> value strings, e.g. "
                      "{\"VS_LOCAL_DATA_ROOT\": \"/data/local\"}.")
    if len(values) > cap:
        return None, f"{where} may declare at most {cap} variables."
    clean: dict = {}
    for name, raw in values.items():
        nm = str(name or "").strip()
        if not _ENV_NAME_RE.match(nm):
            return None, (f"{where} name {name!r} is not a portable environment variable name "
                          "(letters, digits and '_', not starting with a digit).")
        if nm in ENGINE_OWNED_ENV or nm.startswith("LOOPLAB_"):
            return None, (f"{where} may not declare {nm!r}: LoopLab computes it per eval "
                          "(the GPU pin, the read-fence marker, the engine's own settings), and a "
                          "declared value would silently override the run's own treatment.")
        # A bool is an int in Python and `str(True)` is "True", which is not what anyone writing
        # `DEBUG: true` in YAML means to the shell. Numbers ARE meaningful (`OMP_NUM_THREADS: 4`) and
        # are stringified, because the environment has no other type.
        if isinstance(raw, bool) or raw is None or isinstance(raw, (list, dict)):
            return None, (f"{where} value for {nm!r} must be a string or a number — the environment "
                          "has no other type, and a YAML boolean would reach the process as 'True'.")
        val = str(raw)
        if "\x00" in val:
            return None, f"{where} value for {nm!r} contains a NUL byte."
        if len(val) > MAX_ENV_VALUE_CHARS:
            return None, f"{where} value for {nm!r} is longer than {MAX_ENV_VALUE_CHARS} characters."
        if is_secret_env(nm, val):
            return None, _secret_refusal(where, nm)
        clean[nm] = val
    return clean, None


# WHAT HAPPENS TO A VALUE THAT LOOKS LIKE A SECRET: it is REFUSED, at declaration time, and the
# message says where credentials go instead. This is not caution about a hypothetical — a declared
# environment is DURABLE by construction, which is the half that makes it useful and the half that
# makes it the wrong container for a credential. `stages[].env`/`eval.env` are written verbatim into
# `task.snapshot.json`; `Settings.eval_env` into `config.snapshot.json` AND the `run_started` event,
# i.e. into `events.jsonl`, which is the file the exporter ships, the UI renders and the operator
# pastes into a bug report. There is no redaction that could make that safe without also making the
# value unreadable to the resume that has to reproduce it.
#
# It is also the ONE route around a boundary that already exists: `is_secret_env` — the function
# directly above — strips secret-named host variables from EVERY child process precisely so generated
# code cannot print them into the durable stdout tail. That the two live in one module is the point:
# they are the same question asked twice, and a declared env that answered it differently would
# undo the strip from the other side. A declared env is applied AFTER that strip, on purpose (that is what
# makes it reach the eval) — so accepting a secret here would re-add exactly what the strip removed,
# by the operator's own hand, in the one place it is also written to disk.
#
# We deliberately do NOT invent a secret store. The honest answer is that the credentials LoopLab
# itself needs already have a boundary (`LOOPLAB_LLM_API_KEY` and a profile's `api_key_env` — runtime
# fields, environment-only, refused by `--set` so they never enter shell history), and that a
# credential for the CANDIDATE's own code has no boundary because the eval sandbox is where untrusted
# agent-written code runs. Exporting it in the launching shell is still possible and is the operator's
# informed choice; what this refuses to do is write it down.
#
# The screen is this module's `is_secret_env` — the LOOSER of the two patterns
# (`core/config._SECRET_ENV_NAME` is the stricter sibling), name AND credential-URL value. It
# over-matches on purpose: `HF_TOKEN` is refused, and renaming the variable is not a workaround an
# operator can reach by accident.
def _secret_refusal(where: str, name: str) -> str:
    return (f"{where} may not declare {name!r}: its name or value looks like a credential, and a "
            "declared environment is written verbatim into the run's durable record "
            "(task.snapshot.json / config.snapshot.json / the run_started event in events.jsonl), "
            "which is exported, rendered in the UI and pasted into bug reports. LoopLab has no "
            "secret store and is not adding one here. LoopLab's OWN credentials go through the "
            "runtime-only environment boundary (LOOPLAB_LLM_API_KEY, a profile's api_key_env) which "
            "is never snapshotted; a credential your EVAL needs has no such boundary — export it in "
            "the launching shell if you accept that, and keep it out of the run record.")


def merge_env(*layers) -> dict:
    """Compose the declared environment layers, most specific LAST (run < eval < stage).

    One function so the composition order is stated once and read the same way by the engine (which
    folds run- and task-level into the process env it hands the sandbox) and by `_run_stages` (which
    overlays the per-stage block). A `None`/empty layer contributes nothing, so an eval that declares
    nothing composes to `{}` and every existing caller keeps its byte-identical child environment.
    """
    out: dict = {}
    for layer in layers:
        if layer:
            out.update({str(k): str(v) for k, v in layer.items()})
    return out
