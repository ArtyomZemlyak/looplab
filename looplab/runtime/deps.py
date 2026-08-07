"""Environment self-prep: auto-install a missing library before rejecting the idea.

When an LLM-generated solution crashes *purely* because a library isn't installed
(``ModuleNotFoundError: No module named 'torch'``), the right move on the operator's own
box is not to throw the idea away — it's to install the library and re-run. Before this,
the crash-triage agent (which can only edit code, not the environment) would judge such a
crash an `idea_rejected`, so on a fresh box every torch/XGBoost/CatBoost experiment — e.g.
a GRU model — died without ever running.

This module is the pure, testable core: parse the missing module(s) from a traceback, map
import name -> pip package, and run ``python -m pip install`` against the *eval* interpreter.
The orchestrator calls it from the inline-repair loop (trusted_local tier only — the
untrusted/hostile Docker tiers run ``--network none`` and must not mutate a shared image).

Scope guard (``is_installable``): only KNOWN data-science packages are auto-installed. A
typo'd import or a forgotten local helper module is a real code bug — it must flow to the
Developer's repair path, NOT get pip-installed. Keeping a curated allowlist means
auto-install is fast and predictable and can never chase a name that isn't a real package.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Optional

from looplab.runtime.sandbox import is_secret_env

# "No module named 'X'" / 'X.Y' — the canonical ModuleNotFoundError / ImportError text. Captures
# the dotted path; callers reduce it to the TOP-LEVEL package (the unit pip installs).
_MISSING_RE = re.compile(r"No module named ['\"]([\w][\w\.]*)['\"]")

# Import name -> pip package, for the data-science stack. Entries where the names match are listed
# too so the dict doubles as the install ALLOWLIST (`is_installable` == "key present here"). Add a
# library here to let the engine self-install it. Names that differ (sklearn->scikit-learn) are the
# whole point — `pip install sklearn` is wrong/deprecated.
_PIP_NAME: dict[str, str] = {
    # name mismatches (import != pip)
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "skimage": "scikit-image",
    "bs4": "beautifulsoup4",
    "yaml": "PyYAML",
    "imblearn": "imbalanced-learn",
    "category_encoders": "category-encoders",
    "umap": "umap-learn",
    "pytorch_lightning": "pytorch-lightning",
    "tensorflow_addons": "tensorflow-addons",
    "Levenshtein": "python-Levenshtein",
    "dotenv": "python-dotenv",
    "google": "protobuf",
    # gradient boosting / classic ML (the run's actual failures: xgboost, catboost)
    "xgboost": "xgboost",
    "lightgbm": "lightgbm",
    "catboost": "catboost",
    "scipy": "scipy",
    "statsmodels": "statsmodels",
    "patsy": "patsy",
    "optuna": "optuna",
    "hyperopt": "hyperopt",
    "bayes_opt": "bayesian-optimization",
    "shap": "shap",
    "numpy": "numpy",
    "pandas": "pandas",
    "polars": "polars",
    "pyarrow": "pyarrow",
    "networkx": "networkx",
    # deep learning (the run's GRU experiment: torch)
    "torch": "torch",
    "torchvision": "torchvision",
    "torchaudio": "torchaudio",
    "lightning": "lightning",
    "timm": "timm",
    "einops": "einops",
    "transformers": "transformers",
    "tokenizers": "tokenizers",
    # Retrieval/embedding stack. Added 2026-08-04 after a dense-retrieval repo task hit
    # `No module named 'sentence_transformers'` and the engine treated a mainstream package as a
    # code bug rather than an install — the agent then had no way forward, because the import sat
    # in a `protect`ed file it could not edit. `faiss` maps to the CPU wheel on purpose: it is the
    # one that installs everywhere, and a GPU build is a deliberate environment choice, not
    # something to guess from a traceback.
    "sentence_transformers": "sentence-transformers",
    "faiss": "faiss-cpu",
    "evaluate": "evaluate",
    "datasets": "datasets",
    "accelerate": "accelerate",
    # Experiment loggers. Added 2026-08-05 from a live dense-retrieval run: the repo's Lightning
    # trainer builds a `TensorBoardLogger`, tensorboard was absent, and the node spent a whole repair
    # attempt on it — the ONE resource repairs are budgeted in. A logger is pure instrumentation, so
    # installing it is strictly safer than letting the agent rewrite the trainer to drop logging (its
    # other way out), which silently costs the run its training curves and with them the live ASHA
    # and train-monitor signals. `tensorboardX` is the same contract under the older import name.
    "tensorboard": "tensorboard",
    "tensorboardX": "tensorboardX",
    "tensorflow": "tensorflow",
    "keras": "keras",
    "jax": "jax",
    "flax": "flax",
    "fastai": "fastai",
    # nlp / text
    "nltk": "nltk",
    "spacy": "spacy",
    "gensim": "gensim",
    "sentencepiece": "sentencepiece",
    "textblob": "textblob",
    # clustering / misc DS
    "hdbscan": "hdbscan",
    "tslearn": "tslearn",
    "prophet": "prophet",
    "mlxtend": "mlxtend",
    "tqdm": "tqdm",
    "joblib": "joblib",
    "numba": "numba",
}


def missing_modules(stderr: str) -> list[str]:
    """Top-level package names a traceback reports as missing, de-duplicated, first-seen order.
    ``No module named 'torch.nn'`` -> ``['torch']`` (pip installs the top-level package)."""
    seen: dict[str, None] = {}
    for m in _MISSING_RE.findall(stderr or ""):
        top = m.split(".", 1)[0]
        if top:
            seen.setdefault(top, None)
    return list(seen)


def submodule_only_modules(stderr: str) -> set[str]:
    """Top-level names this traceback reports missing ONLY through a DOTTED path
    (``No module named 'pytorch_lightning.utilities.cloud_io'`` -> ``{'pytorch_lightning'}``) — i.e.
    exactly the names for which `missing_modules` above is REDUCING rather than reading.

    That reduction is right when the distribution is genuinely absent: `torch.nn` is unimportable on
    a box with no torch, and `torch` is the unit pip installs. It is WRONG when the distribution IS
    installed and the code asked it for a submodule that VERSION does not have — the top-level name
    resolves, so installing it changes nothing, and the failure is a version/API mismatch that only a
    code repair (or a different pin) can fix. The two are indistinguishable in the traceback text,
    which is why this is only half the answer: the caller pairs it with `is_present` (see
    `engine/crash_repair.py::_prepare_env`, which carries the measurement).

    A name the traceback ALSO reports BARE is excluded — that is direct evidence of absence and
    outranks the reduction, so a log carrying both shapes still installs."""
    bare: set[str] = set()
    dotted: set[str] = set()
    for m in _MISSING_RE.findall(stderr or ""):
        top = m.split(".", 1)[0]
        if not top:
            continue
        (dotted if "." in m else bare).add(top)
    return dotted - bare


# --- A missing distribution that never reaches `missing_modules` -------------------------------
# A library may DEGRADE an absent optional dependency into something that is not an import error at
# all, and then the module name appears nowhere in the exception. Live 2026-08-05
# (`runs/rubert-dr-0805` node 0): `transformers` guards `init_empty_weights` /
# `find_tied_parameters` behind `is_accelerate_available()`, so an absent `accelerate` surfaced as
#     NameError: name 'init_empty_weights' is not defined
# with the word "accelerate" nowhere in the traceback. `accelerate` was already on the allowlist
# above and would have been installed automatically had the failure named it; instead the node spent
# TWO of its six repair attempts hand-patching the symbol. The crash-triage agent DIAGNOSED it
# correctly both times ("mechanical transformers/accelerate version mismatch … ensure accelerate
# import") and had no way to act, because the engine only ever installs what the TRACEBACK names.
#
# `triage_install_candidates` is that missing path: the triage's own naming of a distribution is
# admitted as evidence, but only in conjunction with the traceback. It is FAIL-CLOSED by
# construction — see its docstring for the conditions, none of which authorizes an install on its
# own, plus the caller-side last one (the distribution must actually be ABSENT).

# The traceback shapes an unresolved name takes. Each captures the name itself, so the same scan
# both PROVES the failure is unresolved-name shaped and yields the identifiers to join on.
_UNRESOLVED_RES = (
    _MISSING_RE,                                                     # ModuleNotFoundError
    re.compile(r"cannot import name ['\"]([\w][\w\.]*)['\"]"),       # renamed/moved API
    re.compile(r"name ['\"]([\w][\w\.]*)['\"] is not defined"),      # NameError — the degraded guard
    re.compile(r"has no attribute ['\"]([\w][\w\.]*)['\"]"),         # AttributeError
)
# A library that re-raises its own "you need X" error, without the canonical wording: the exception
# CLASS is still the tell (`ModuleNotFoundError: Neither `tensorboard` nor `tensorboardX` is
# available.`), as is a pip hint it prints alongside.
_IMPORT_SHAPE = ("ModuleNotFoundError", "ImportError", "pip install")
# Identifiers a traceback points at, beyond the unresolved name: the installed packages on its
# frames (`/site-packages/transformers/modeling_utils.py`) and the names its message quotes
# (backticks included — Lightning quotes that way). These make the JOIN below meaningful without
# admitting arbitrary prose.
_FRAME_RE = re.compile(r"[/\\](?:site|dist)-packages[/\\]([\w][\w\.]*)(?:[/\\]([\w]+))?")
_QUOTED_RE = re.compile(r"[`'\"]([A-Za-z_][\w\.]*)[`'\"]")
# Identifier-ish tokens in a triage rationale / structured field. Distribution spellings (`-`) and
# dotted module paths both count; the allowlist lookup normalizes them.
_TOKEN_RE = re.compile(r"[A-Za-z_][\w\.-]{1,60}")


def _normal(name: str) -> str:
    """Fold the spellings of one distribution together: import name (`sentence_transformers`), pip
    name (`sentence-transformers`) and case are the same thing to the allowlist."""
    return str(name or "").strip().strip(".").replace("-", "_").lower()


def _alias_map() -> dict[str, str]:
    """{normalized import name | normalized pip name -> import name}. Derived from `_PIP_NAME` on
    every call, NOT cached at import: the allowlist above is documented as the one place to add a
    library, and tests extend it at runtime — a cached map would silently keep answering "not on the
    list" for the new entry. It is ~70 entries; the scan runs at most once per repair."""
    out: dict[str, str] = {}
    for imp, pip in _PIP_NAME.items():
        out.setdefault(_normal(imp), imp)
        out.setdefault(_normal(pip), imp)
    return out


def named_installable(text: str) -> list[str]:
    """Allowlisted IMPORT names that `text` names, by either spelling (`sentence-transformers` and
    `sentence_transformers` both resolve to `sentence_transformers`). De-duplicated, first-seen
    order. Pure text: the caller decides whether naming a package is enough to install it."""
    aliases = _alias_map()
    out: dict[str, None] = {}
    for tok in _TOKEN_RE.findall(text or ""):
        # A dotted path names its top-level distribution too (`transformers.utils` -> transformers).
        for cand in (tok, tok.split(".", 1)[0]):
            imp = aliases.get(_normal(cand))
            if imp:
                out.setdefault(imp, None)
    return list(out)


def traceback_names(traceback: str) -> set[str]:
    """The identifiers a traceback POINTS AT: the names it reports as unresolved, the installed
    packages on its frames, and the identifiers its message quotes. Normalized (see `_normal`), so
    a triage that quotes any of them can be matched case- and spelling-insensitively."""
    names: set[str] = set()
    for rx in _UNRESOLVED_RES:
        for m in rx.findall(traceback or ""):
            names.add(_normal(m))
            names.add(_normal(m.split(".", 1)[0]))
    for pkg, mod in _FRAME_RE.findall(traceback or ""):
        names.add(_normal(pkg))
        if mod:
            names.add(_normal(mod))
    for q in _QUOTED_RE.findall(traceback or ""):
        names.add(_normal(q))
        names.add(_normal(q.split(".", 1)[0]))
    names.discard("")
    return names


def unresolved_name_failure(traceback: str) -> bool:
    """True iff the traceback shows a failure of the shape an ABSENT distribution produces: an
    unresolved module/symbol/attribute, or a library re-raising its own missing-dependency error.
    A shape mismatch, a CUDA OOM, a metric disagreement — anything else — is False, so no triage
    rationale can turn it into an install."""
    tb = traceback or ""
    return (any(rx.search(tb) for rx in _UNRESOLVED_RES)
            or any(marker in tb for marker in _IMPORT_SHAPE))


def _tokens(text: str) -> set[str]:
    """Normalized identifier-ish tokens of a triage text, dotted paths also reduced to their
    top-level name (`transformers.utils` -> {transformers_utils…, transformers})."""
    out: set[str] = set()
    for tok in _TOKEN_RE.findall(text or ""):
        out.add(_normal(tok))
        out.add(_normal(tok.split(".", 1)[0]))
    out.discard("")
    return out


def triage_install_candidates(named: str, rationale: str, traceback: str) -> list[str]:
    """Import names to offer for install when a missing distribution degraded into a failure that
    is NOT an import error (see the block comment above). `named` is the triage's STRUCTURED
    missing-dependency field, `rationale` its free text. FAIL CLOSED — a candidate needs the
    traceback and the triage to point at it JOINTLY, which they can do in exactly two ways:

      A. the triage NAMES it in its structured field, its rationale demonstrably describes this
         traceback (it mentions at least one identifier the traceback points at — the unresolved
         symbol, a package on its frames, a name its message quotes), AND that same rationale names
         the distribution itself, which must be the ONLY one the field names. This is the
         `accelerate` case: the distribution appears nowhere in the traceback, so only the agent can
         supply the name, and the rationale is what proves the agent was reading THIS failure.
      B. the traceback AND the rationale both name it. This is the `tensorboard` case: the library
         re-raised its own missing-dependency error naming the package in prose
         (``Neither `tensorboard` nor `tensorboardX` is available``), which the canonical
         `missing_modules` scan cannot parse, while the triage independently named the same package.

    Over both paths: the failure must be unresolved-name shaped (`unresolved_name_failure`) — a
    shape mismatch, an OOM or a metric disagreement never installs anything, whatever the triage
    says — and the name must be on the curated allowlist, so an off-list name stays a code bug.
    Free rationale text alone can NEVER mint a candidate: a rationale mentioning "the installed
    Lightning version" while the traceback names `pytorch_lightning` must not install `lightning`.

    WHY PATH A NEEDS ITS LAST TWO CONDITIONS. The join used to be one-sided: the rationale had only
    to ECHO one identifier the traceback points at, after which the structured field was trusted
    whole. `transformers` appears on every frame of the real accelerate traceback, so an honest
    rationale satisfied that on its own — and verified through the engine seam, the verbatim
    ``NameError: name 'init_empty_weights' is not defined`` traceback with that honest rationale and
    ``missing_dependency="tensorflow, jax, prophet, fastai"`` had the engine attempt exactly those
    four heavyweight installs into the SHARED eval interpreter, spending nothing on either ledger.
    Bounded (by `_dep_attempted`, `_MAX_DEP_ROUNDS` and the `trusted_local` gate) but not harmless:
    pip can downgrade numpy/protobuf under every other node in the run. The two added conditions are
    what the degraded-dependency SHAPE actually claims — "library L guards symbol S behind
    `is_X_available()`", one distribution X, diagnosed in the same sentences that read the traceback
    — so a field the agent's own prose does not stand behind, or a LIST of them (a guess, not a
    diagnosis), authorizes nothing. Path B is unaffected and can still return several, because there
    the traceback itself names each one.

    The CALLER adds the last and decisive condition: the distribution must actually be ABSENT from
    the eval interpreter (`is_present`), so a diagnosis that merely mentions an installed package
    installs nothing."""
    if not unresolved_name_failure(traceback):
        return []
    pointed = traceback_names(traceback)
    out: dict[str, None] = {}
    # A: the triage is about THIS failure, and stands behind exactly one name for the cause.
    structured = named_installable(named)
    if len(structured) == 1 and pointed & _tokens(f"{named or ''}\n{rationale or ''}"):
        if _normal(structured[0]) in _tokens(rationale):
            out.setdefault(structured[0], None)
    for m in named_installable(rationale):                         # B: named by BOTH, independently
        if _normal(m) in pointed:
            out.setdefault(m, None)
    return list(out)


def is_present(module: str, *, python: Optional[str] = None, timeout: float = 30.0) -> bool:
    """True iff the EVAL interpreter can resolve `module` — i.e. installing it would be a no-op.

    FAIL CLOSED: any doubt (a launch failure, a timeout, a name that resolves to nothing useful)
    answers True, so the caller installs only what is provably absent. `find_spec` locates the
    module without importing it, so a heavyweight or broken package is not executed just to ask.
    """
    name = str(module or "").split(".", 1)[0]
    if not name.isidentifier():
        return True
    probe = ("import importlib.util as u, sys\n"
             "try:\n"
             "    sys.exit(0 if u.find_spec(sys.argv[1]) is not None else 1)\n"
             "except Exception:\n"
             "    sys.exit(0)\n")            # unreadable/broken install -> "present", never install over it
    try:
        # Same secret scrub as `install()` below — site initialization (.pth files) runs arbitrary
        # code in any interpreter spawn, so this child gets no more environment than that one does.
        proc = subprocess.run([python or sys.executable, "-c", probe, name],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env={k: v for k, v in os.environ.items()
                                                    if k.upper().startswith("PIP_")
                                                    or not is_secret_env(k, v)})
    except (OSError, subprocess.SubprocessError):
        return True
    return proc.returncode == 0


def is_installable(module: str) -> bool:
    """True iff `module` is a known data-science package we'll auto-install (allowlist). A name
    that isn't here is treated as a code bug (typo / missing local module), not an install."""
    return module in _PIP_NAME


def pip_package(module: str) -> str:
    """pip package name for an import name (identity when unmapped)."""
    return _PIP_NAME.get(module, module)


@dataclass
class InstallResult:
    package: str
    ok: bool
    returncode: int
    output: str = ""          # combined stdout+stderr tail (audit; never the engine's secrets)
    timed_out: bool = False


# Stop auto-installing after pip TIMES OUT repeatedly: on a no-/restricted-egress JupyterHub pod pip
# hangs to the full timeout on EVERY distinct missing lib (torch, xgboost, …), so without a circuit
# breaker a multi-node run could burn dep_install_timeout × N minutes hanging. We use a CONSECUTIVE
# count (latch only after _EGRESS_TIMEOUT_LATCH timeouts in a row), so a single transient slow-mirror
# timeout — or one genuinely huge wheel that legitimately overran — doesn't disable self-prep for the
# whole run; ANY pip RESPONSE (a success, or a clean "no matching distribution" failure — both prove
# egress works) resets the count. The clean fix for a true no-egress pod is a pre-baked image with
# auto_install_deps off. A connection-REFUSED fails fast and is handled per-package by the caller.
# The latch counter carries its OWN lock rather than relying on a caller-side one. Today every
# install() reaches this through crash_repair._prepare_env, which serializes under the engine's
# `_dep_lock` — but that is an invariant nothing here can enforce, and a future caller that skips it
# would make the latch lossy in exactly the situation it exists for: several eval threads all hanging
# on pip at once, each losing the other's increment, so the breaker never trips.
_latch_lock = threading.Lock()
_consecutive_install_timeouts = 0
_EGRESS_TIMEOUT_LATCH = 2


def reset_install_latch() -> None:
    """Clear the consecutive-timeout latch. Called at run start so the breaker is per-RUN, not a
    process-lifetime global: in the long-lived `looplab ui` server a run that latched (egress blip)
    otherwise leaves auto-install disabled for the NEXT run in the same process."""
    global _consecutive_install_timeouts
    with _latch_lock:
        _consecutive_install_timeouts = 0


def install(package: str, *, python: Optional[str] = None, timeout: float = 900.0) -> InstallResult:
    """``<python> -m pip install <package>`` against the EVAL interpreter (so the install lands in
    the same env the solution runs in). Generous default timeout — wheels like torch are large.
    Best-effort and self-contained: any launch failure is returned as ``ok=False`` (never raises),
    so a missing-pip / offline box degrades to the normal triage/repair path."""
    global _consecutive_install_timeouts
    with _latch_lock:
        latched = _consecutive_install_timeouts
    if latched >= _EGRESS_TIMEOUT_LATCH:
        return InstallResult(package=package, ok=False, returncode=-1,
                             output=f"skipped: pip timed out {latched}× in a row "
                                    "(egress looks blocked); pre-bake deps or set auto_install_deps=false",
                             timed_out=True)
    py = python or sys.executable
    argv = [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", package]
    # `pip install` of an sdist executes the package's setup.py/build backend — ARBITRARY CODE — so
    # this child gets the same secret scrub as every other spawn (sandbox.run_argv,
    # bg_tasks._child_env); inheriting the full os.environ handed it the operator's LLM_API_KEY and
    # cloud creds. The curated allowlist keeps the risk low, but `install()` itself does not enforce
    # `is_installable` (that guard is caller-side in crash_repair), so a future caller bypassing it
    # would otherwise hand a typosquatted sdist the secrets.
    # NAME-based only, deliberately: pip's own configuration is credential-bearing by design
    # (`PIP_INDEX_URL`/`PIP_EXTRA_INDEX_URL` carry inline tokens for a private index), and stripping
    # it would break exactly the installs an operator configured, so PIP_* is exempted below by
    # name and pip keeps its index while the LLM/cloud keys and inline URL credentials are gone.
    # The PIP_* exemption is exactly that — an exemption, not a reason to use a weaker screen. This
    # used to be a NAME-only filter, diverging from the value-aware `is_secret_env(k, v)` every OTHER
    # child spawn uses (`sandbox.run_argv`, `bg_tasks._child_env`). That sibling also strips
    # INLINE-CREDENTIAL URL VALUES (`scheme://user:pw@host` under DATABASE_URL / MONGO_URI / *_DSN)
    # whose name matches nothing — so a database credential was handed to the sdist's setup.py while
    # being stripped everywhere else. Now the full screen applies, minus the PIP_* index vars the
    # rationale above actually covers: those are credential-bearing BY DESIGN and stripping them
    # breaks precisely the private-index installs an operator configured.
    env = {k: v for k, v in os.environ.items()
           if k.upper().startswith("PIP_") or not is_secret_env(k, v)}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        with _latch_lock:                    # latch only after several in a row (true no-egress signal)
            _consecutive_install_timeouts += 1
        return InstallResult(package=package, ok=False, returncode=-1,
                             output="pip install timed out", timed_out=True)
    except OSError as e:
        return InstallResult(package=package, ok=False, returncode=-1, output=f"failed to launch pip: {e}")
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    with _latch_lock:                   # pip RESPONDED (success or clean fail) → egress works → reset
        _consecutive_install_timeouts = 0
    return InstallResult(package=package, ok=proc.returncode == 0, returncode=proc.returncode, output=tail)
