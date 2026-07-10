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

import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

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
    "datasets": "datasets",
    "accelerate": "accelerate",
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


@dataclass
class PrepResult:
    """Outcome of one env-prep pass over a crash: which pip packages installed, which failed."""
    installed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:        # truthy iff something was installed (engine re-runs the eval)
        return bool(self.installed)


# Stop auto-installing after pip TIMES OUT repeatedly: on a no-/restricted-egress JupyterHub pod pip
# hangs to the full timeout on EVERY distinct missing lib (torch, xgboost, …), so without a circuit
# breaker a multi-node run could burn dep_install_timeout × N minutes hanging. We use a CONSECUTIVE
# count (latch only after _EGRESS_TIMEOUT_LATCH timeouts in a row), so a single transient slow-mirror
# timeout — or one genuinely huge wheel that legitimately overran — doesn't disable self-prep for the
# whole run; ANY pip RESPONSE (a success, or a clean "no matching distribution" failure — both prove
# egress works) resets the count. The clean fix for a true no-egress pod is a pre-baked image with
# auto_install_deps off. A connection-REFUSED fails fast and is handled per-package by the caller.
_consecutive_install_timeouts = 0
_EGRESS_TIMEOUT_LATCH = 2


def reset_install_latch() -> None:
    """Clear the consecutive-timeout latch. Called at run start so the breaker is per-RUN, not a
    process-lifetime global: in the long-lived `looplab ui` server a run that latched (egress blip)
    otherwise leaves auto-install disabled for the NEXT run in the same process."""
    global _consecutive_install_timeouts
    _consecutive_install_timeouts = 0


def install(package: str, *, python: Optional[str] = None, timeout: float = 900.0) -> InstallResult:
    """``<python> -m pip install <package>`` against the EVAL interpreter (so the install lands in
    the same env the solution runs in). Generous default timeout — wheels like torch are large.
    Best-effort and self-contained: any launch failure is returned as ``ok=False`` (never raises),
    so a missing-pip / offline box degrades to the normal triage/repair path."""
    global _consecutive_install_timeouts
    if _consecutive_install_timeouts >= _EGRESS_TIMEOUT_LATCH:
        return InstallResult(package=package, ok=False, returncode=-1,
                             output=f"skipped: pip timed out {_consecutive_install_timeouts}× in a row "
                                    "(egress looks blocked); pre-bake deps or set auto_install_deps=false",
                             timed_out=True)
    py = python or sys.executable
    argv = [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", package]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        _consecutive_install_timeouts += 1   # latch only after several in a row (true no-egress signal)
        return InstallResult(package=package, ok=False, returncode=-1,
                             output="pip install timed out", timed_out=True)
    except OSError as e:
        return InstallResult(package=package, ok=False, returncode=-1, output=f"failed to launch pip: {e}")
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    _consecutive_install_timeouts = 0   # pip RESPONDED (success or clean fail) → egress works → reset latch
    return InstallResult(package=package, ok=proc.returncode == 0, returncode=proc.returncode, output=tail)
