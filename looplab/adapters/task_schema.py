"""The COMPOSABLE task schema: every spelling a task file may be written in, folded into one.

`normalize_task` is the single place old and new task spellings converge, and it is a different
domain from the seam it used to sit inside. `adapters/tasks.py` answers "what IS a task adapter"
(the `TaskAdapter` Protocol, `TASK_OPTIONAL_HOOKS`, the `_KINDS` registry) and "give me one"
(`validate_task` / `load_task` / `submit_warnings`); this module answers the question BEFORE those:
"what did the operator write, and which canonical dict does it mean". It reads a dict and returns a
dict — no adapter class, no pydantic model, no I/O — which is why it moves out whole rather than in
pieces, and why either half can now be read without the other.

WHY IT MOVED (doc 25 RA-01's cap, spent). `adapters/tasks.py` came out of the agent/factory split
at 399 lines against a cap of 400, and two parallel changes then spent it: the `shift_inputs` hook
row (`trust/drift.py`'s advisory distribution-shift record) and `submit_warnings` (the CLI's
hand-copied submit warnings, single-sourced). The guard
`tests/test_agent_factory_split.py::test_neither_module_is_a_god_module_again` says in its own
docstring that when the cap is spent again the answer is an EXTRACTION and not a raise — and this
was the extraction already visible: 203 lines, nearly half the file, one coherent unit, reachable
through its existing re-export.

`adapters/tasks.py` re-exports `normalize_task`, so `from looplab.adapters.tasks import
normalize_task` — the spelling every caller uses (`serve/artifacts.py`, `serve/launch.py`,
`serve/routers/runs.py`, `tests/test_composable_schema.py`) — keeps naming the SAME object, and a
monkeypatch of either path patches both.
"""
from __future__ import annotations


def normalize_task(data: dict) -> dict:
    """Front-end for the COMPOSABLE task schema — the single place old and new spellings converge.

    A task is defined by WHICH capability fields it carries, not a `kind` enum:
      • `repo`     -> an editable codebase (agent edits within it)        [alias: editable_path]
      • `dataset`  -> read-only data mounts (path or {name: path})        [alias: data]
      • `cmd`      -> how to run + score (a command/argv OR a full spec)  [alias: eval]
      • `kaggle`   -> a Kaggle / MLE-bench competition slug               [-> kind=mlebench_real]
      • `benchmark`-> a built-in synthetic task (quadratic/regression/…)  [-> kind=<name>]
    and inside `cmd`/`eval`: `metric.reader` (alias of the old `metric.kind`); reader "auto" folds
    to the onboarding path (the agent writes the metric adapter).

    Returns a canonical dict the registered adapters validate (with an inferred `kind`). Idempotent
    and back-compatible: a legacy `{kind, eval, onboard, editable_path, metric.kind}` dict passes
    through unchanged, so old snapshots / example files / tests keep working. Raises ValueError on
    a task that cannot be a task — a non-argv `cmd`, or no recognizable capability field at all
    (never a silent default to the quadratic toy)."""
    d = dict(data)

    # --- built-in benchmarks: an explicit selector for the internal synthetic tasks ---
    if d.get("benchmark") and not d.get("kind"):
        d["kind"] = d.pop("benchmark")

    # --- kaggle competition -> the mlebench_real adapter (accept the `kaggle` alias or a bare
    #     `competition`; either one, with no explicit kind, IS a competition task) ---
    if d.get("kaggle"):
        # `kaggle` is the composable spelling and WINS over a stale `competition` riding along in the
        # same dict (setdefault kept the old value, so a user editing the Kaggle field in a boss-
        # authored spec launched the DISPLAYED slug's predecessor — the wrong competition).
        d["competition"] = d.pop("kaggle")
    if d.get("competition") and not d.get("kind"):
        d["kind"] = "mlebench_real"

    # --- repo (editable codebase): "do whatever WITHIN it" — default the surface to ALL files ---
    if "repo" in d:
        _repo = d.pop("repo")
        # CONFLICTING aliases are an ERROR, not silent-keep-the-old (arch-review §3 P0-5): {repo: NEW,
        # editable_path: OLD} used to keep OLD because `repo` was only consumed when editable_path was
        # absent — so a user who switched the repo via the composable alias silently ran the old one.
        if d.get("editable_path") and d["editable_path"] != _repo:
            raise ValueError(
                f"conflicting task aliases: repo={_repo!r} and editable_path={d['editable_path']!r} "
                "name different codebases — set exactly one.")
        # Spelling the SAME path under BOTH aliases is not a conflict (the check above passed), and
        # it must mean what `repo` alone means. Keying the composable default off "editable_path was
        # absent" made `{repo: p}` and `{repo: p, editable_path: p}` differ: the second skipped this
        # branch entirely and fell through to RepoTask's much narrower `["**/*.py"]`, so the same
        # task silently got a smaller edit surface for naming its repo twice.
        d["editable_path"] = _repo
        d.setdefault("edit_surface", ["**/*"])   # composable-repo default: full freedom (protect=exceptions)

    # --- dataset: read-only mounts. A bare path -> one mount named "dataset"; a dict -> merged ---
    if "dataset" in d:
        ds = d.pop("dataset")
        existing = d.get("data")
        mounts = dict(existing) if isinstance(existing, dict) else {}
        if isinstance(ds, str) and ds:
            name, i = "dataset", 2               # avoid clobbering an explicit `data` mount of the same name
            while name in mounts:
                name, i = f"dataset{i}", i + 1
            mounts[name] = ds
        elif isinstance(ds, dict):
            # A name spelled in BOTH `data` and `dataset` is a config error: mounts.update would
            # silently shadow one path and every node would evaluate against the wrong source, far
            # from the misconfiguration. (The bare-path branch above can rename because its name is
            # invented; an explicit name collision has no right answer.)
            clash = sorted(set(mounts) & set(ds))
            if clash:
                raise ValueError(
                    f"data mount name(s) declared in BOTH `data` and `dataset`: {', '.join(clash)} — "
                    "the same name would silently shadow one of the paths; rename or drop one side.")
            mounts.update(ds)
        if mounts:
            d["data"] = mounts

    # --- REJECT a stray dotted `cmd.<field>` / `eval.<field>` top-level key with an actionable error.
    #     The docs describe fields in dotted shorthand (`cmd.setup`, `cmd.profiles`, …) meaning "the
    #     field of cmd", and a model — the assistant's propose_run especially — sometimes emits them
    #     LITERALLY as top-level keys instead of nesting. Silently dropping them (the old behavior) lost
    #     the setup/profiles with no signal. Raising a clear message instead lets propose_run bounce it
    #     BACK to the assistant, which re-emits with the field nested — the model self-corrects rather
    #     than shipping a task whose setup never runs. ---
    _stray = sorted(k for k in d if isinstance(k, str)
                    and (k.startswith("cmd.") or k.startswith("eval.")) and len(k) > 4)
    if _stray:
        base, field = _stray[0].split(".", 1)
        raise ValueError(
            f"`{_stray[0]}` is not a valid field — write `{field}` INSIDE the `{base}` object, not as a "
            f"top-level \"{_stray[0]}\" key. e.g. cmd:{{command:[…], metric:{{…}}, {field}:…}}. "
            f"Stray dotted keys: {', '.join(_stray)}.")

    # --- cmd (how to run + score) is the new name for `eval` ---
    # Both present is an authoring conflict: mapping `cmd`->`eval` only "when eval is absent" would
    # SILENTLY drop the composable `cmd` in favor of a stale legacy `eval` (pydantic ignores the
    # leftover unknown `cmd` key) — the exact silent-loss the data/dataset clash check guards against.
    if "cmd" in d and "eval" in d:
        raise ValueError(
            "give EITHER `cmd` (the current name) OR `eval` (the legacy alias) — not both; they are "
            "the same field and specifying both is ambiguous. Keep `cmd` and drop `eval`.")
    if "cmd" in d and "eval" not in d:
        cmd = d.pop("cmd")
        if isinstance(cmd, list):
            d["eval"] = {"command": list(cmd)}
        elif isinstance(cmd, dict):
            d["eval"] = dict(cmd)
        elif cmd:
            # The natural authoring mistake is a shell STRING — dict("python test.py") would raise a
            # cryptic 'dictionary update sequence' ValueError (a 500 on /api/start, a TUI crash).
            # Reject it with an actionable message instead; the engine runs argv with NO shell.
            raise ValueError(
                f"`cmd` must be an argv list ([\"python\",\"test.py\"]) or a spec object "
                f"{{command, metric, timeout}}, got {type(cmd).__name__}: {str(cmd)[:80]!r} — "
                "split a shell string into argv items.")
        # falsy cmd (None/""/{}) -> treated as absent; the repo-task gate below gives the real message

    # --- inside the eval/cmd spec: metric.reader alias + "auto" -> onboarding fold ---
    def _reader_to_kind(spec):
        # A metric-reader dict may spell its reader as `reader` (composable) — map to the engine's
        # `kind`. Applied to EVERY reader (primary metric, multi-objective `metrics`, `constraints`,
        # `cross_check`), so a `reader:`-spelled sub-reader isn't silently read as stdout_json.
        if isinstance(spec, dict) and "reader" in spec and "kind" not in spec:
            spec = dict(spec)
            spec["kind"] = spec.pop("reader")
        # A regex reader needs its regex in `pattern`; an LLM/operator authoring the composable metric
        # naturally puts it in `key` (the field stdout_json/file_json use). Promote key->pattern for regex
        # readers so `{"reader":"stdout_regex","key":"RECALL@100: (...)"}` works instead of crashing the
        # eval with KeyError('pattern').
        if isinstance(spec, dict) and spec.get("kind") in ("stdout_regex", "file_regex") \
                and "pattern" not in spec and spec.get("key"):
            spec = dict(spec)
            spec["pattern"] = spec.pop("key")
        return spec

    ev = d.get("eval")
    if isinstance(ev, dict):
        ev = dict(ev)
        m = ev.get("metric")
        if isinstance(m, dict) and m.get("reader") == "auto":
            # "auto" reader == the agent writes the metric adapter -> the onboarding path. The command
            # becomes the onboard command; `eval` is left None until the onboarder ratifies.
            d.setdefault("onboard", True)
            # A string `command` here would `list(...)` into a per-CHARACTER argv (['p','y','t',…]).
            # The non-auto path is guarded by EvalSpec validation, but this onboard fold sets eval=None
            # and bypasses it — so reject a shell string exactly like the top-level `cmd` branch does.
            # A non-empty string raises; a whitespace-only/empty string is treated as ABSENT (else it
            # would slip past the `.strip()` check yet still be truthy at `if _cmd`, becoming list(' ')).
            _cmd = ev.get("command")
            if isinstance(_cmd, str):
                if _cmd.strip():
                    raise ValueError(
                        "`cmd.command` must be an argv list ([\"python\",\"test.py\"]), not a shell string: "
                        f"{_cmd[:80]!r} — split it into argv items.")
                _cmd = None
            if _cmd and not d.get("onboard_command"):
                d["onboard_command"] = list(_cmd)
            if ev.get("timeout"):
                d.setdefault("onboard_timeout", float(ev["timeout"]))
            d["eval"] = None
            ev = None
        else:
            if isinstance(m, dict):
                ev["metric"] = _reader_to_kind(m)
            if isinstance(ev.get("metrics"), dict):          # multi-objective aux readers
                ev["metrics"] = {k: _reader_to_kind(v) for k, v in ev["metrics"].items()}
            if isinstance(ev.get("constraints"), list):      # constraint readers
                ev["constraints"] = [_reader_to_kind(c) for c in ev["constraints"]]
            if isinstance(ev.get("cross_check"), dict):      # drift cross-check reader
                ev["cross_check"] = _reader_to_kind(ev["cross_check"])
        if ev is not None:
            d["eval"] = ev

    # --- infer the dispatch kind from the fields present (composable -> kind) ---
    if not d.get("kind"):
        if d.get("editable_path") or d.get("editables"):
            d["kind"] = "repo"
        elif d.get("eval") or d.get("onboard"):
            d["kind"] = "repo"            # a bare run+score spec is a (path-less) repo-style task
        elif d.get("data") or d.get("data_path"):
            d["kind"] = "dataset"
        elif d.get("bounds"):
            # the classic kind-less TOY file (examples/toy_task.json predates `kind`): a numeric
            # `bounds` space with no repo/data/cmd IS the toy capability — keep those loading.
            d["kind"] = "quadratic"
        else:
            # NO silent default to the quadratic toy (the guarantee the old /api/start kind-guard
            # gave): a typo'd capability field (repo_path for repo, …) would otherwise validate as a
            # ToyTask and burn the run's nodes/LLM budget optimizing (x-3)^2. An offline toy run says
            # so explicitly (`kind`/`benchmark`: "quadratic").
            raise ValueError(
                "cannot infer the task: no capability field recognized. Give one of `repo` (an "
                "editable codebase), `dataset`/`data` (data mounts), `cmd` (how to run + score), "
                "`kaggle`/`competition` (a competition slug), `benchmark` (a built-in synthetic "
                "task) — or an explicit legacy `kind`.")

    # Per-source permission OBJECTS ({path, mount, edit, …}) are repo-task machinery — mount/edit
    # drive the repo workspace seeding and the write gate. The dataset kind reads data by ABSOLUTE
    # path with no mounts (DatasetTask.data is name -> path), so only the path survives here:
    # flatten the documented object form instead of bouncing it with a pydantic type error.
    if d["kind"] == "dataset" and isinstance(d.get("data"), dict):
        d["data"] = {k: (v.get("path", "") if isinstance(v, dict) else v)
                     for k, v in d["data"].items()}

    return d
