"""`LoopOptions` — the TYPED bundle of `drive_tool_loop` options (doc 25 AG-01).

Every tool-using persona is configured the same way: `loop_opts_from_settings(settings)` collects the
config-driven loop knobs once, and each call site spreads them into `drive_tool_loop`. That bundle
used to be a bare `dict`, and the shape it made possible has already cost a run:

    drive_tool_loop(..., context_budget_chars=self.context_budget_chars, **loop_opts)

`loop_opts_from_settings` also injects `context_budget_chars` (the `Settings` default is non-None, so
it is ALWAYS present), which makes that call `TypeError: got multiple values for keyword argument`.
It did not surface as a crash: `ToolUsingResearcher.propose` wraps its loop in a broad `except
Exception` that degrades to a safe draft Idea, so the agentic Researcher was simply DEAD in the
default config — every node a "fallback (agent parse failed)" no-op, and the only symptom a flat
metric. The fix at the time was a `dict.setdefault` merge duplicated into each ctor.

What makes the shape impossible here is a two-part rule, and both halves are enforced by
`tests/test_loop_options.py`:

  1. An option travels ONLY in the bundle. A `LoopOptions` is a Mapping whose keys are its SET
     fields, so `**options` yields exactly one key per option — a dataclass cannot declare a field
     twice, and a mapping cannot hold a key twice. Merging is `.replace()` (this value WINS) or
     `.with_defaults()` (fill only what is unset); both name the precedence out loud, where the old
     `setdefault` left it to be inferred.
  2. A call site never passes an option BOTH ways. `LOOP_OPTION_FIELDS` and `EXPLICIT_ONLY_LOOP_ARGS`
     partition `drive_tool_loop`'s keyword-only parameters: the first set may only arrive through a
     bundle, the second may only arrive as an explicit keyword. So an explicit keyword next to a
     `**` spread is a violation the guard test finds by AST, and a bundle carrying `finalize` /
     `nudge_prompt` is refused by `coerce` at the boundary instead of colliding at the call.

The split is not arbitrary. `EXPLICIT_ONLY_LOOP_ARGS` are the per-call CALLBACKS (`finalize`,
`fallback`, `validate`, the three observers, the provenance hook) and the two PROMPT CONTRACTS
(`nudge_prompt` / `stuck_prompt`) — prompt strings are contracts (CLAUDE.md), so their wording
belongs at the site that owns it, verbatim, never in a config bundle that a settings file could
reword. `emit_retries` is there for the same reason as the callbacks: nothing derives it from
`Settings`, so keeping it out means a caller can always pass it safely beside a spread.

An unknown key is LOUD (`TypeError` naming the key and the valid set) rather than silently carried:
the bundle used to be a plain dict, so `{"stuck_retries": 3}` — a typo of `stuck_repeat` — travelled
all the way to `drive_tool_loop` before failing. That is not hypothetical. The suite's own
`emit_loop` fixture used exactly that misspelling, asserted on it by name, and stayed green, because
the fake driver it fed took `**kwargs`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace as _dc_replace


class _Unset:
    """The 'this option was never set' sentinel — distinct from `None`, which several options use as
    a real value (`context_budget_chars=None` means 'unset, use the built-in default' while `0`
    means 'compaction OFF'). A plain `None` default would collapse "absent" into "explicitly None"
    and silently break `with_defaults`, which must not overwrite a value a caller really passed."""

    __slots__ = ()

    def __repr__(self) -> str:      # so a refusal message / repr reads as UNSET, not <object at 0x…>
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


@dataclass(frozen=True)
class LoopOptions(Mapping):
    """The config-driven `drive_tool_loop` options, as ONE declaration point per option.

    Frozen and Mapping-shaped on purpose: `**options` keeps working at every existing call site
    (byte-identical to the dict it replaces — only the SET fields are spread), while mutation has to
    go through `replace`/`with_defaults`/`without`, which return a new bundle and reject an unknown
    name. See the module docstring for the rule the two halves enforce together.

    Field semantics are `drive_tool_loop`'s own; its docstring is the reference. Defaults live THERE,
    not here — an UNSET field is simply not passed, so the loop's signature stays the single source
    of truth for what an unconfigured loop does.
    """

    max_turns: int | _Unset = UNSET
    time_budget_s: float | _Unset = UNSET
    context_budget_chars: int | None | _Unset = UNSET
    stuck_detection: bool | _Unset = UNSET
    stuck_repeat: int | _Unset = UNSET
    stuck_alternate: int | _Unset = UNSET
    self_plan: bool | _Unset = UNSET
    plan_reinject_every: int | _Unset = UNSET
    auto_summary: bool | _Unset = UNSET
    summary_client: object | _Unset = UNSET
    emit_after: int | _Unset = UNSET
    emit_force: int | _Unset = UNSET

    # ---------------------------------------------------------------- Mapping (so `**opts` works)
    def __iter__(self):
        """The SET field names, in declaration order — so `**options` carries exactly the options a
        caller actually configured and leaves the rest to `drive_tool_loop`'s own defaults."""
        return (f.name for f in fields(self) if getattr(self, f.name) is not UNSET)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __getitem__(self, key):
        value = getattr(self, key, UNSET) if key in LOOP_OPTION_FIELDS else UNSET
        if value is UNSET:
            raise KeyError(key)
        return value

    # ---------------------------------------------------------------- merging (precedence is named)
    def replace(self, **overrides) -> "LoopOptions":
        """This value WINS over whatever the bundle carries. Use it where the call site owns the
        decision (a hard per-session turn ceiling, the assistant's own wall-clock floor, a stage that
        must never expose `update_plan`). Pass `UNSET` to clear a field — or use `without`."""
        _check_names(overrides, "replace")
        return _dc_replace(self, **overrides) if overrides else self

    def with_defaults(self, **defaults) -> "LoopOptions":
        """Fill only the fields this bundle has NOT set — the `dict.setdefault` merge the two ctors
        each spelled out, now written once. The bundle WINS, which is the precedence those
        `setdefault` calls already had: a value that came from `loop_opts_from_settings` is the
        operator's configured value, and a ctor default (`max_turns=0`) must not silently override
        it."""
        _check_names(defaults, "with_defaults")
        fill = {name: value for name, value in defaults.items() if getattr(self, name) is UNSET}
        return _dc_replace(self, **fill) if fill else self

    def without(self, *names: str) -> "LoopOptions":
        """Drop options so the loop's own defaults apply again (the DeepResearcher's `summary_client`
        divergence: that stage has always compacted with its own client, never the D11 compressor)."""
        _check_names(names, "without")
        return _dc_replace(self, **{name: UNSET for name in names}) if names else self

    # ---------------------------------------------------------------- boundary
    @classmethod
    def coerce(cls, options) -> "LoopOptions":
        """Accept `None`, a plain mapping (the historical `loop_opts=` dict), or a `LoopOptions`.

        This is where a hand-built bundle is CHECKED, and it must run at the boundary — a role's
        `__init__`, an `agentic_*` entry point — rather than at the drive call, so a bad key raises
        where nothing is catching it instead of inside the containment `except` that turns every
        agentic failure into a safe default."""
        if options is None:
            return cls()
        if isinstance(options, cls):
            return options
        try:
            items = dict(options)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"loop options must be a mapping or LoopOptions, "
                            f"got {type(options).__name__}") from exc
        _check_names(items, "coerce")
        return cls(**items)


# The registry half of the seam (CLAUDE.md's registry-guarded-seam convention). Adding a parameter to
# `drive_tool_loop` means declaring which side it is on IN THE SAME CHANGE — a config knob becomes a
# `LoopOptions` field, anything per-call goes in `EXPLICIT_ONLY_LOOP_ARGS`. `tests/test_loop_options.py`
# asserts the two sets partition the loop's keyword-only parameters exactly, in both directions, so a
# new parameter that lands in neither is a red test rather than a knob no bundle can ever carry.
LOOP_OPTION_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(LoopOptions))

EXPLICIT_ONLY_LOOP_ARGS: tuple[str, ...] = (
    "finalize", "fallback", "validate",         # per-call result handling
    "on_step", "on_text", "cancel_check", "on_tool_result",  # per-call observers / provenance hook
    "on_budget",                                # …and the one that fires when the loop RAN OUT
    "nudge_prompt", "stuck_prompt",             # prompt CONTRACTS — kept verbatim at the owning site
    "emit_retries",                             # per-call; nothing derives it from Settings
    # Whether a forced emit on an exit with NO retry turn may skip `validate`. A per-call
    # POLICY that belongs beside the callback it modifies: only the repair session wants it,
    # and a settings bundle that could turn it on for the stages caller would silently
    # disable the operator's wall-budget and manifest-collision checks.
    "terminal_salvage",
    # The evidence FENCE stamped on every tool result (`tool_loop.py::fence_untrusted`). Explicit-only
    # for `nudge_prompt`'s exact reason: the label is the wording of a security statement the system
    # prompt already makes, so it belongs at the site that makes it — `serve/assistant.py`, beside
    # `ASSISTANT_EVIDENCE_GUARD` — and not in a bundle a settings file could reword or switch off.
    "tool_result_label",
)


def _check_names(names, where: str) -> None:
    """Refuse an unknown option name LOUDLY, naming it and the valid set.

    The message names `EXPLICIT_ONLY_LOOP_ARGS` separately when that is what was passed, because
    "`finalize` is not an option" is a different (and more confusing) mistake than a typo: it is a
    real `drive_tool_loop` parameter that deliberately cannot ride a bundle."""
    unknown = [name for name in names if name not in LOOP_OPTION_FIELDS]
    if not unknown:
        return
    explicit = [name for name in unknown if name in EXPLICIT_ONLY_LOOP_ARGS]
    detail = ""
    if explicit:
        detail = (f" ({', '.join(explicit)} is a per-call `drive_tool_loop` argument: pass it "
                  f"explicitly at the call site, it must never ride an options bundle)")
    raise TypeError(f"LoopOptions.{where}: unknown loop option(s) {', '.join(unknown)}{detail}. "
                    f"Valid options: {', '.join(LOOP_OPTION_FIELDS)}")
