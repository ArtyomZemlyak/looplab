"""Read-only environment-introspection tools for the repo Developer.

The #1 cause of failed repo experiments was the Developer GUESSING the installed API/version and
being wrong — `precision='16-mixed'` on a Lightning that only accepts `'16'`, a `--gradient_clip_val`
flag the training script doesn't define, an import that moved between versions. The Developer had NO
way to check the real environment (the prompt forbids throwaway `cat`/inspect scripts). These tools
close that: it can look up an installed package's VERSION, read the SOURCE of any installed module,
inspect a class/function SIGNATURE (and an Enum's valid members — exactly the `precision` case), and
GREP installed source for a symbol. All read-only; nothing is written or executed beyond importing an
already-installed package to introspect it (safe in the trusted-local dev tier, sandboxed otherwise).
"""
from __future__ import annotations

import importlib
import importlib.metadata as _md
import importlib.util
import inspect
import io
from contextlib import redirect_stderr, redirect_stdout

from looplab.tools._base import fn_spec

# Per-result char cap. The agent loop hard-caps every tool result at 4000 chars (agents/agent.py
# drive_tool_loop) and drops the TAIL past it — so a bigger provider-side cap isn't generous, it
# silently loses the end of the result (mega-review P3). Stay under the loop cap so OUR truncation
# (which the descriptions state honestly) is the one that decides.
_CAP = 3800


def _top(name: str) -> str:
    """The top-level distribution/import name from a dotted path (lightning.pytorch -> lightning)."""
    return (name or "").split(".", 1)[0].strip()


_INSTALLED_NAMES = None


def _installed_names() -> list:
    """All installed distribution names + top-level importable module names (cached). The pool a
    not-found lookup is fuzzy-matched against."""
    global _INSTALLED_NAMES
    if _INSTALLED_NAMES is None:
        names: set = set()
        try:
            for d in _md.distributions():
                nm = (d.metadata.get("Name") or "").strip()
                if nm:
                    names.add(nm)
                    names.add(nm.replace("-", "_"))
        except Exception:  # noqa: BLE001
            pass
        try:
            import pkgutil
            for m in pkgutil.iter_modules():
                names.add(m.name)
        except Exception:  # noqa: BLE001
            pass
        _INSTALLED_NAMES = sorted(names)
    return _INSTALLED_NAMES


def _suggest(name: str) -> str:
    """A ' — did you mean X, Y?' hint from the closest installed dist/module names, so a not-found
    lookup dead-ends usefully instead of blankly: 'lightning' -> 'pytorch_lightning, lightning_fabric'.
    Combines difflib closest-matches with substring hits (the pytorch_lightning case)."""
    import difflib
    top = _top(name)
    if not top:
        return ""
    pool = _installed_names()
    close = difflib.get_close_matches(top, pool, n=3, cutoff=0.6)
    subs = [n for n in pool if top.lower() in n.lower() and n.lower() != top.lower()][:3]
    hits = list(dict.fromkeys(close + subs))[:4]
    return f" — did you mean: {', '.join(hits)}?" if hits else ""


class EnvInspectTools:
    """ToolProvider (specs()/execute()) giving the Developer read-only visibility into the ACTUAL
    installed Python environment, so it grounds generated code in the real API instead of guessing."""

    def specs(self) -> list[dict]:
        return [
            fn_spec("pkg_info",
                    "Look up an INSTALLED package's exact version + install location + summary. Use "
                    "this BEFORE using a framework API whose call/args changed across versions (e.g. "
                    "check the pytorch-lightning version before choosing a Trainer arg). Returns "
                    "'(not installed)' if absent.",
                    {"name": {"type": "string", "description": "import or distribution name, e.g. "
                              "'lightning' / 'pytorch_lightning' / 'torch'"}},
                    ["name"]),
            fn_spec("py_api",
                    "Inspect a class/function/method or an Enum in an INSTALLED package: its signature, "
                    "docstring, and — for a class — public members, or — for an Enum — its VALID VALUES "
                    "(exactly what you need to pick a legal `precision`/`strategy`/etc.). Give a dotted "
                    "path to the object, e.g. 'lightning.pytorch.Trainer' or "
                    "'torch.optim.AdamW'.",
                    {"target": {"type": "string", "description": "dotted path to a class/function/enum"}},
                    ["target"]),
            fn_spec("read_installed",
                    "Read the SOURCE CODE of an installed module (so you can see exactly what an API "
                    "does / what args a script's argparse defines). Give a dotted module path "
                    "(e.g. 'lightning.pytorch.trainer.trainer'). Returns ONE page of at most ~3600 "
                    "chars of its .py source; window with start_line + lines. A page with more source "
                    "below it ENDS with '… (more below — continue with start_line=N)' — continue from "
                    "exactly that N; a reply WITHOUT that marker IS the end of the file. Read-only.",
                    {"module": {"type": "string", "description": "dotted module path"},
                     "start_line": {"type": "integer", "description": "1-based first line (optional; "
                                    "use the N from the previous page's 'continue with' marker)"},
                     "lines": {"type": "integer", "description": "how many lines to return (optional "
                               "window; omit for as many as fit in one page)"}},
                    ["module"]),
            fn_spec("grep_installed",
                    "Search the SOURCE of an installed package for a string/symbol — find where an arg "
                    "is parsed, where a value is validated, what the allowed options are. Returns "
                    "matching file:line snippets across the package (default 20 hits; total output is "
                    "clamped to fit one tool result — narrow `package` to a submodule for more depth). "
                    "Read-only.",
                    {"query": {"type": "string", "description": "literal substring to find"},
                     "package": {"type": "string", "description": "package/module to search under, "
                                 "e.g. 'lightning'"},
                     "max_hits": {"type": "integer", "description": "cap on hits (optional, default 20)"}},
                    ["query", "package"]),
            fn_spec("gpu_info",
                    "Report the GPUs available for training: count, names, and per-device memory (via "
                    "torch.cuda). Use this INSTEAD of `nvidia-smi` — you have no shell, so nvidia-smi is "
                    "not callable; this is the equivalent. Returns '(no CUDA / torch)' when unavailable "
                    "(e.g. CPU-only).", {}),
        ]

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "pkg_info":
                return self._pkg_info(str(args.get("name", "")))
            if name == "py_api":
                return self._py_api(str(args.get("target", "")))
            if name == "read_installed":
                # `lines` is the documented window param (consistent with read_file/repo_read);
                # `max_lines` stays accepted — older transcripts/models still pass it.
                return self._read_installed(str(args.get("module", "")),
                                            args.get("start_line"),
                                            args.get("lines", args.get("max_lines")))
            if name == "grep_installed":
                return self._grep_installed(str(args.get("query", "")), str(args.get("package", "")),
                                            args.get("max_hits"))
            if name == "gpu_info":
                return self._gpu_info()
        except Exception as e:  # noqa: BLE001 — a read-only probe must never crash the tool loop
            return f"(inspect error: {type(e).__name__}: {e})"
        return f"(unknown tool: {name})"

    @staticmethod
    def _gpu_info() -> str:
        try:
            import torch
        except Exception:  # noqa: BLE001
            return "(no torch installed — cannot query GPUs)"
        if not torch.cuda.is_available():
            return "(no CUDA GPU available — CPU only)"
        n = torch.cuda.device_count()
        lines = [f"CUDA available: {n} GPU(s)"]
        for i in range(n):
            try:
                p = torch.cuda.get_device_properties(i)
                lines.append(f"  cuda:{i} = {p.name}, {round(p.total_memory / 1024**3, 1)} GiB")
            except Exception as e:  # noqa: BLE001
                lines.append(f"  cuda:{i} = (props unavailable: {e})")
        return "\n".join(lines)

    # ------------------------------------------------------------------ pkg_info
    def _pkg_info(self, name: str) -> str:
        name = name.strip()
        if not name:
            return "(pkg_info: give a package name)"
        # Try the distribution name first, then the import name's top-level distribution.
        for cand in (name, _top(name)):
            try:
                ver = _md.version(cand)
                try:
                    meta = _md.metadata(cand)
                    summary = meta.get("Summary", "") if meta else ""
                except Exception:  # noqa: BLE001
                    summary = ""
                loc = ""
                spec = importlib.util.find_spec(_top(name))
                if spec and spec.origin:
                    loc = spec.origin
                return f"{cand} {ver}\nsummary: {summary}\nlocation: {loc}"
            except _md.PackageNotFoundError:
                continue
        # not a distribution — maybe an importable module with __version__
        try:
            mod = importlib.import_module(_top(name))
            v = getattr(mod, "__version__", "(no __version__)")
            return f"{_top(name)} {v}\nlocation: {getattr(mod, '__file__', '')}"
        except Exception:  # noqa: BLE001
            return f"({name}: not installed{_suggest(name)})"

    # ------------------------------------------------------------------- py_api
    def _py_api(self, target: str) -> str:
        target = target.strip()
        if not target:
            return "(py_api: give a dotted path to a class/function/enum)"
        obj, err = _resolve(target)
        if obj is None:
            return f"(py_api: could not resolve {target}: {err})"
        out: list[str] = [f"{target}: {type(obj).__name__}"]
        # Enum -> its valid members/values (the precision='16-mixed' case)
        try:
            import enum
            if isinstance(obj, type) and issubclass(obj, enum.Enum):
                out.append("VALID VALUES: " + ", ".join(f"{m.name}={m.value!r}" for m in obj))
        except Exception:  # noqa: BLE001
            pass
        try:
            out.append("signature: " + str(inspect.signature(obj)))
        except (TypeError, ValueError):
            pass
        doc = inspect.getdoc(obj)
        if doc:
            out.append("doc:\n" + doc[:2000])
        if inspect.isclass(obj):
            members = [n for n, _ in inspect.getmembers(obj) if not n.startswith("_")]
            if members:
                out.append("public members: " + ", ".join(members[:60]))
        return "\n".join(out)[:_CAP]

    # ------------------------------------------------------------ read_installed
    def _read_installed(self, module: str, start_line, lines) -> str:
        module = module.strip()
        if not module:
            return "(read_installed: give a dotted module path)"
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError, ModuleNotFoundError) as e:
            return f"(read_installed: cannot locate {module}: {e}{_suggest(module)})"
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return f"(read_installed: {module} has no readable .py source at {getattr(spec, 'origin', None)})"
        try:
            with open(spec.origin, encoding="utf-8", errors="replace") as f:
                data = f.read()
        except OSError as e:
            return f"(read_installed: read error: {e})"
        # Paginate exactly like the repo scout's read_file (SHARED window logic => one marker
        # contract across all the source readers): each page fits the agent loop's 4000-char result
        # cap WITH the resume marker, and a reply ending without the marker IS the end of the file.
        # (The old 300-line default page had NO resume pointer at all and its _CAP tail-truncation
        # could eat the end silently — mega-review P3.)
        from looplab.tools.reposcout import RepoScoutTools
        return f"# {spec.origin} " + RepoScoutTools._paginate(data, start_line, lines)

    # ------------------------------------------------------------ grep_installed
    def _grep_installed(self, query: str, package: str, max_hits) -> str:
        query = query.strip()
        if not query or not package.strip():
            return "(grep_installed: give a query and a package)"
        pkg = package.strip()
        try:
            # Resolve the FULL dotted path (not just `_top`), so scoping to a submodule actually
            # narrows the walk — `grep_installed(query, "torch.nn")` searches torch/nn, not all of
            # torch. This is what makes the `_FILE_BUDGET` overflow hint ("narrow `package` to a
            # submodule to search deeper") actionable; `read_installed` already resolves full paths.
            spec = importlib.util.find_spec(pkg)
        except (ImportError, ValueError, ModuleNotFoundError) as e:
            return f"(grep_installed: cannot locate {package}: {e}{_suggest(package)})"
        except Exception:  # noqa: BLE001 — resolving a DOTTED name imports the parent package, which in a
            # broken env can raise anything (an OSError on a missing native lib). A TOP-LEVEL name isn't
            # imported by find_spec, so fall back to grepping the whole top package on disk — grep stays
            # useful (import-free) exactly when introspection matters most, just without the submodule scope.
            try:
                spec = importlib.util.find_spec(_top(pkg))
            except Exception:  # noqa: BLE001
                spec = None
        if spec is None:
            return f"(grep_installed: {package} not found{_suggest(package)})"
        import os
        roots = list(getattr(spec, "submodule_search_locations", None) or [])
        # A single-file top-level module (e.g. `six`) has NO submodule_search_locations and its origin
        # is the .py itself — grep JUST that file. Falling back to its DIRECTORY would be site-packages,
        # so os.walk would scan every OTHER installed package and mis-attribute hits (verified).
        single = spec.origin if (not roots and spec.origin and spec.origin.endswith(".py")) else None
        cap = max(1, min(int(max_hits) if max_hits else 20, 100))   # a model-supplied cap can't blow the budget
        scanned = 0
        _FILE_BUDGET = 4000     # bound the walk so a not-found query on a huge pkg (torch) can't
        #                         crawl thousands of files — report the truncation, don't lie "absent"
        hits: list[str] = []
        walked = ([(os.path.dirname(single), [], [os.path.basename(single)])] if single
                  else ((dp, d, f) for root in roots for dp, d, f in os.walk(root)))
        for dirpath, _dirs, files in walked:
                for fn in files:
                    if not fn.endswith(".py"):
                        continue
                    if scanned >= _FILE_BUDGET:
                        return self._clamp("\n".join(hits) + f"\n(stopped after scanning {scanned} "
                                           "files; narrow `package` to a submodule to search deeper)")
                    scanned += 1
                    fp = os.path.join(dirpath, fn)
                    try:
                        with open(fp, encoding="utf-8", errors="replace") as f:
                            for i, line in enumerate(f, 1):
                                if query in line:
                                    hits.append(f"{fp}:{i}: {line.strip()[:160]}")
                                    if len(hits) >= cap:
                                        return self._clamp("\n".join(hits) + f"\n(capped at {cap} hits)")
                    except OSError:
                        continue
        return self._clamp("\n".join(hits)) if hits \
            else f"(grep_installed: '{query}' not found under {package})"

    @staticmethod
    def _clamp(text: str, budget: int = 3600) -> str:
        """Fit a grep result under the agent loop's 4000-char cap ourselves, with an HONEST marker:
        long site-packages paths make even 20 hits overflow the cap, where the loop would drop the
        tail (and any '(capped at …)' note) silently. Cut at a line boundary so no half-hit shows."""
        if len(text) <= budget:
            return text
        cut = text[:budget]
        cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
        return cut + "\n(output clamped — narrow `package` to a submodule or lower max_hits)"


def _resolve(dotted: str):
    """Resolve a dotted path to a live object: import the longest importable module prefix, then
    getattr the rest. Returns (obj, err). Import runs the package's import code (needed for a
    signature) — safe for installed deps."""
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        mod_name = ".".join(parts[:i])
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001 — try a shorter module prefix
            continue
        obj = mod
        try:
            for attr in parts[i:]:
                obj = getattr(obj, attr)
            return obj, None
        except AttributeError as e:
            return None, str(e)
    return None, "no importable module prefix"
