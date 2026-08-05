"""The two live inline-repair runaways, and the verbatim evidence both left behind.

This file is the EVIDENCE: the real symbols, the real stderr tails, the real triage rationales, and
the two defects that produced them. What now stops the loop lives in `test_repair_stop_decision.py`,
which imports these fixtures rather than re-inventing them.

INCIDENT 1 — `runs/rubert-dr-0804`: 2345 `node_repaired` events on ONE node over 3.5 h, two
independent causes.

  Cause 1a — a varying identifier defeated the anti-stuck signature. `transformers` had been
  auto-installed in a broken state, so every eval failed inside its lazy-import registry naming a
  DIFFERENT symbol each time (`'ColPaliProcessor'`, `'MarkupLMProcessor'`, `'SplitModulelist'`, …).
  `_normalize_error_sig` normalized numbers and paths but not quoted identifiers, so 2345 failures
  minted 369 distinct signatures whose longest identical RUN was 2 — under a threshold of 4 the
  counter reset on nearly every attempt and the guard never fired.

  Cause 1b — a failed repair CALL was treated as a repair. The Developer's repair request died with
  `402 out of credits`, so it returned the in-band "(developer error: …)" sentinel; that string was
  committed as the node's code, re-materialized, and re-evaluated. 2343 of the 2345 "repairs" were
  this. A provider/transport failure is not a code defect and must not drive the code-repair loop.

Cause 1a was first fixed by teaching the normalizer to absorb quoted identifiers. That fix was
verified and then found to be ASCII-only — on this Russian-language repo the identical failure with
a CYRILLIC symbol ran 1741 repairs with no terminal — which is why the signature approach was
dropped entirely rather than widened. `_cyrillic_src` below is that reproduction, kept as a fixture
because it is the headline case the current design has to pass.

INCIDENT 2 — `runs/rubert-dr-0805` node 0, `inline_repair_attempts: 6`, deepseek-v4-flash:

    attempt  what it fixed                                          triage rationale said
    1        pytorch_lightning.utilities.cloud_io gone in PL 2.x     "mechanical import error"
    2        NameError: init_empty_weights (accelerate absent)       "mechanical … version mismatch"
    3        NameError: find_tied_parameters (same cause)            "library version/import bug"
    4        TensorBoardLogger, tensorboard absent                   "mechanical missing-library crash"
    5        replace_sampler_ddp removed in PL 2.x                   "mechanical API break"
    6        validation_epoch_end removed in PL 2.0                  "mechanical … v2 API incompatibility"
    —        DDP find_unused_parameters — a MODELLING question       node_failed, budget exhausted

Every attempt reconciled a year-stale repo with today's site-packages, and the first real research
question arrived with nothing left. This is the chain the budget exists to PROTECT: any design that
stops it early has failed in the other direction.
"""
from __future__ import annotations

from looplab.core.models import DEVELOPER_ERROR_PREFIX

# The 402 the run actually got back, verbatim in shape (OpenRouter, credits exhausted).
_402 = (f"{DEVELOPER_ERROR_PREFIX} LLM request to https://openrouter.ai/api/v1 failed: "
        "Error code: 402 - {'error': {'message': 'This request requires more credits, or fewer "
        "max_tokens. You requested up to 64000 tokens, but can only afford 7918.', 'code': 402}})")

# Distinct symbols the real registry walk reported, in first-seen order (374 distinct over 2330
# occurrences). Alphabetic on purpose: the pre-fix normalizer's digit rule did NOT merge them,
# which is exactly why every attempt minted a fresh signature.
_REAL_SYMBOLS = [
    "XLMRobertaModel", "NerPipeline", "ImageTextToTextPipeline", "AriaConfig", "TvpProcessor",
    "PeAudioVideoProcessor", "LayoutXLMProcessor", "PipedPipelineDataFormat", "MergeModulelist",
    "PaddleOCRVLProcessor", "GraniteSpeechProcessor", "ColPaliProcessor", "MarkupLMProcessor",
    "SplitModulelist", "KyutaiSpeechToTextProcessor", "AlignProcessor", "BrosProcessor",
]

_GOOD = "import json; print(json.dumps({'metric': 0.1}))\n"


def _lazy_import_src(symbol: str) -> str:
    """A solution whose crash reproduces the broken-registry shape: same exception, same message
    template, same frame — only the quoted symbol differs."""
    return (f"raise ModuleNotFoundError(\"Could not import module '{symbol}'. "
            "Are this object's requirements defined correctly?\")\n")


# The operator's real workload is Russian-language dense retrieval, and the live reproduction was a
# Russian schema whose vendor renames a column on every export: 49 `node_repaired`, 47 distinct
# signatures, max recurrence 2 against a threshold of 4, zero terminals. Same message template, same
# frame, a never-before-seen Cyrillic column name each time.
_CYRILLIC_ALPHABET = "абвгдежзиклмнопрстуфхцчшщэюя"


def _cyrillic_column(i: int) -> str:
    name, n = "", i + 1
    while n:
        name += _CYRILLIC_ALPHABET[n % len(_CYRILLIC_ALPHABET)]
        n //= len(_CYRILLIC_ALPHABET)
    return "поле_" + name


def _cyrillic_src(i: int) -> str:
    """`_lazy_import_src`'s twin with a Cyrillic operand — byte-for-byte the same control flow, and
    the reason the error-signature approach was abandoned instead of widened."""
    return _emits(
        '  File "/w/nodes/node_0/prep.py", line 41, in column\n'
        "    return table[name]\n           ~~~~~^^^^^^\n"
        f"KeyError: {_cyrillic_column(i)!r}\n")


def _emits(tail: str, installed_flag=None) -> str:
    """A solution whose failure IS the given stderr tail: it writes the tail verbatim and exits
    non-zero, so the engine's `_failure_reason` sees exactly the bytes the live run gave it.

    With `installed_flag` it also behaves like the real thing when a MISSING LIBRARY arrives: the
    injected installer touches that path, and the very same UNCHANGED source then runs clean — which
    is the property the install path has to have (no repair, no new code, just a re-run)."""
    guard = (f"import os\nif os.path.exists({str(installed_flag)!r}):\n"
             "    import json; print(json.dumps({'metric': 0.1})); raise SystemExit(0)\n"
             if installed_flag is not None else "")
    return f"{guard}import sys\nsys.stderr.write({tail!r})\nsys.exit(1)\n"


# --- VERBATIM from `runs/rubert-dr-0805` -------------------------------------------------------
# `_T<n>` is the `error_in` of that run's `node_repaired` #n (the engine's own 500-char stderr
# tail); `_R<n>` is the triage rationale it recorded alongside. `_DDP` is the `node_failed`
# error — the research question the node reached with nothing left to spend.
# attempt 1: pytorch_lightning.utilities.cloud_io gone in PL 2.x
_T1 = ('05/nodes/node_0/train.py", line 23, in <module>\n    from model import BertDSSMExtractCL'
       'S\n  File "/home/jovyan/data/looplab/runs/rubert-dr-0805/nodes/node_0/model.py", line 20'
       ', in <module>\n    from utils import load_by_all_means, load_state_dict_by_all_means\n  '
       'File "/home/jovyan/data/looplab/runs/rubert-dr-0805/nodes/node_0/utils.py", line 6, in <'
       'module>\n    from pytorch_lightning.utilities.cloud_io import get_filesystem\nModuleNotF'
       'oundError: No module named \'pytorch_lightning.utilities.cloud_io\'\n')
_R1 = ('The crash is a mechanical import error: `pytorch_lightning.utilities.cloud_io` no longer'
       ' exists in the installed Lightning version; the fix is to import `get_filesystem` from `'
       'lightning_fabric.utilities.cloud_io`, leaving the fine-tuning idea intact.')
# attempt 2: accelerate absent -> transformers' guarded symbol is undefined
_T2 = ('mers/modeling_utils.py", line 4333, in from_pretrained\n    model_init_context = cls.get'
       '_init_context(is_quantized, _is_ds_init_called)\n                         ^^^^^^^^^^^^^^'
       '^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File "/opt/conda/lib/python3.11/site-package'
       's/transformers/modeling_utils.py", line 3736, in get_init_context\n    init_contexts = ['
       'no_init_weights(), init_empty_weights()]\n                                        ^^^^^^'
       '^^^^^^^^^^^^\nNameError: name \'init_empty_weights\' is not defined\n')
_R2 = ('This is a mechanical transformers/accelerate version mismatch (NameError: init_empty_wei'
       'ghts) in the from_pretrained model-loading path, not a flaw in the NV-Retriever false-ne'
       'gative-masking idea — fix the loading call (remove low_cpu_mem_usage/device_map or ensur'
       'e accelerate import) and re-run.')
# attempt 3: the SAME absent accelerate, one symbol later
_T3 = ('n3.11/site-packages/transformers/modeling_utils.py", line 4659, in _load_pretrained_mode'
       'l\n    missing_keys, unexpected_keys = _find_missing_and_unexpected_keys(\n             '
       '                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File "/opt/conda/lib/python'
       '3.11/site-packages/transformers/modeling_utils.py", line 1352, in _find_missing_and_unex'
       'pected_keys\n    tied_params = find_tied_parameters(model)\n                  ^^^^^^^^^^'
       '^^^^^^^^^^\nNameError: name \'find_tied_parameters\' is not defined\n')
_R3 = ('NameError for find_tied_parameters inside transformers.modeling_utils is a library versi'
       'on/import bug, not a flaw in the loss idea — patch the missing symbol (e.g. `import tran'
       'sformers.modeling_utils as mu; from transformers.utils import find_tied_parameters; mu.f'
       'ind_tied_parameters = find_tied_para')
# attempt 4: tensorboard absent, named only in prose by the library's own error
_T4 = ('orch_lightning/loggers/tensorboard.py", line 96, in __init__\n    super().__init__(\n  F'
       'ile "/opt/conda/lib/python3.11/site-packages/lightning_fabric/loggers/tensorboard.py", l'
       'ine 93, in __init__\n    raise ModuleNotFoundError(\nModuleNotFoundError: Neither `tenso'
       'rboard` nor `tensorboardX` is available. Try `pip install`ing either.\nRequirement \'ten'
       'sorboardX\' not met. HINT: Try running `pip install -U \'tensorboardX\'`\nRequirement \''
       'tensorboard\' not met. HINT: Try running `pip install -U \'tensorboard\'`\n')
_R4 = ("Mechanical missing-library crash: the Lightning trainer uses a TensorBoardLogger but ten"
       "sorboard/tensorboardX aren't installable offline. Fix by switching to CSVLogger (or disa"
       "bling the logger) so training runs without the unavailable dependency — the dense-retrie"
       "val fine-tuning idea itself is sound")
# attempt 5: replace_sampler_ddp removed in PL 2.x
_T5 = ('b/runs/rubert-dr-0805/nodes/node_0/train.py", line 461, in <module>\n    main(get_args()'
       ')\n  File "/home/jovyan/data/looplab/runs/rubert-dr-0805/nodes/node_0/train.py", line 43'
       '6, in main\n    trainer = Trainer(\n              ^^^^^^^^\n  File "/opt/conda/lib/pytho'
       'n3.11/site-packages/pytorch_lightning/utilities/argparse.py", line 70, in insert_env_def'
       'aults\n    return fn(self, **kwargs)\n           ^^^^^^^^^^^^^^^^^^\nTypeError: Trainer.'
       '__init__() got an unexpected keyword argument \'replace_sampler_ddp\'\n')
_R5 = ("Mechanical API break: `replace_sampler_ddp` was removed in the installed PyTorch Lightni"
       "ng version; dropping the arg (single-GPU training doesn't need it) fixes the crash witho"
       "ut changing the approach.")
# attempt 6: validation_epoch_end removed in PL 2.0
_T6 = ('lib/python3.11/site-packages/pytorch_lightning/trainer/configuration_validator.py", line'
       ' 84, in __verify_train_val_loop_configuration\n    raise NotImplementedError(\nNotImplem'
       'entedError: Support for `validation_epoch_end` has been removed in v2.0.0. `LitModel` im'
       'plements this method. You can use the `on_validation_epoch_end` hook instead. To access '
       'outputs, save them in-memory as instance attributes. You can find migration examples in '
       'https://github.com/Lightning-AI/pytorch-lightning/pull/16520.\n')
_R6 = ("The crash is a mechanical PyTorch Lightning v2 API incompatibility (validation_epoch_end"
       " removed in v2.0.0, replaced by on_validation_epoch_end); the underlying fine-tuning ide"
       "a is sound, so I'll migrate the hook and re-run in place.")
# …and the first genuine research question: a MODELLING decision about which parameters
# participate in the loss, raised inside torch's DDP.
_DDP = ("er._rebuild_buckets():\n[rank0]:                                    ^^^^^^^^^^^^^^^^^^^^"
        "^^^^^^^^^^^\n[rank0]: RuntimeError: It looks like your LightningModule has parameters th"
        "at were not used in producing the loss returned by training_step. If this is intentional"
        ", you must enable the detection of unused parameters in DDP, either by setting the strin"
        "g value `strategy='ddp_find_unused_parameters_true'` or by setting the flag in the strat"
        "egy with `strategy=DDPStrategy(find_unused_parameters=True)`.\n")

# The six migrations, in the order the run hit them: (marker, stderr tail, triage rationale).
# `marker` is a substring unique to that failure — a scripted judge keys on it exactly as a model
# keys on the traceback it is reading.
_SEQUENCE = [
    ("cloud_io", _T1, _R1),
    ("init_empty_weights", _T2, _R2),
    ("find_tied_parameters", _T3, _R3),
    ("tensorboardX", _T4, _R4),
    ("replace_sampler_ddp", _T5, _R5),
    ("validation_epoch_end", _T6, _R6),
]
# The research question the node never got to ask, and a second one behind it.
_DDP_RATIONALE = ("DDP reports unused parameters — decide whether the pooler/head should "
                  "participate in the loss, or enable find_unused_parameters.")
_SECOND_QUESTION = ("[rank0]: RuntimeError: Expected to have finished reduction in the prior "
                    "iteration before starting a new one.\n")


def test_the_incident_fixtures_are_the_shapes_they_claim_to_be():
    """A file of verbatim evidence still has to be self-checking: every fixture below is imported
    by `test_repair_stop_decision.py` and a silent edit there would weaken every case at once."""
    # Cause 1a: same exception, same template, same frame — only the quoted operand moves.
    bodies = {_lazy_import_src(s).replace(s, "<SYM>") for s in _REAL_SYMBOLS}
    assert len(bodies) == 1 and len(_REAL_SYMBOLS) == len(set(_REAL_SYMBOLS)) == 17
    # …and its Cyrillic twin really is Cyrillic, really varies, and really is the same template.
    names = {_cyrillic_column(i) for i in range(40)}
    assert len(names) == 40 and all(any("Ѐ" <= ch <= "ӿ" for ch in n) for n in names)
    assert len({_cyrillic_src(i).replace(_cyrillic_column(i), "<COL>") for i in range(40)}) == 1
    # Cause 1b: the sentinel the Developer returns when its OWN session died.
    assert _402.startswith(DEVELOPER_ERROR_PREFIX) and "402" in _402
    # Incident 2: six distinct migrations, each rationale calling itself mechanical, then a
    # research question that does not.
    assert len(_SEQUENCE) == 6
    for marker, tail, rationale in _SEQUENCE:
        assert marker in tail and rationale
    assert "find_unused_parameters" in _DDP and "mechanical" not in _DDP_RATIONALE
