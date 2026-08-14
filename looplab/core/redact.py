"""B3 · Secret-leak redaction (ADR-13 trust). A `print(api_key)` or a traceback that echoes an env
secret would otherwise be persisted verbatim into the event log / spans / UI via the stdout/stderr
tail. Run a redaction pass — known credential patterns (always) plus conservative high-entropy token
masking — over every output tail before it is written.

Pure + deterministic. Known-pattern redaction is always safe; the entropy pass is conservative
(long tokens only) to avoid masking legitimate data hashes.

**WHAT `redact_output` GATES, AND WHAT IT NO LONGER GATES (2026-08-14, backlog C2).** Until this
change the WHOLE pass was config-gated and the flag defaults to `False`, so on the DEFAULT path a
`print(os.environ["X"])` or a traceback landed verbatim in the durable `node_evaluated.stdout_tail`
— and a tail travels further than any other captured byte: into `events.jsonl`, the span trace, the
UI node detail, an exported report and a bug report. Every OTHER durable diagnostic boundary in the
tree already sanitized unconditionally (`redact_persisted_text`, ~30 call sites), so the tail was
the ONE channel that skipped the screen the rest of the codebase applies. It no longer is:

* **always, at every tail** — the known credential SHAPES in `_PATTERNS` (negligible false-positive
  risk, which is why they were already documented "always redacted") and the operator's own secret
  ENV VALUES (`redact_env_values`, below).
* **`redact_output=True` only** — the ENTROPY pass, which is the half with a real false-positive
  cost: a 24-char base64/hex run of a legitimate data hash, model checksum or embedding dump reads
  exactly like a credential to it. That cost is the reason the flag was off by default, and it is a
  reason to gate the entropy pass, never a reason to gate the pattern pass.

`redact_output_tail` is the ONE spelling of that split; `engine/audit.py::Engine._redact` is its
only production caller and funnels all six persisted output tails through it.
"""
from __future__ import annotations

import hashlib
import math
import os
from itertools import islice as _islice
import re
import unicodedata

from looplab.core.envsafe import is_secret_env

# Field names that merely CONTAIN a credential substring but are benign diagnostic output
# (tokenizer / max_tokens / *_tokens) — never mask these so operators keep model/token diagnostics.
_BENIGN_KEYS = {"tokenizer", "max_tokens", "num_tokens", "n_tokens",
                "total_tokens", "prompt_tokens", "completion_tokens", "tokens"}


_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|secret|access[_-]?key|token|password|passwd|credential)",
    re.IGNORECASE,
)
# The assignment operator accepts a fat-arrow (`secret => value`, Ruby/JS/Perl hashrocket) as well as
# `:`/`=`. Without the `=>` alternative, `[:=]` consumed only the `=`, the value class then captured the
# lone `>` (a space stops it), and the real credential after the arrow leaked verbatim — a fail-OPEN in
# a security redactor that carries free-form subprocess logs. `=>` is tried first so the operator spans
# the whole arrow and the value class starts at the credential. Over-redaction is not a risk: both
# callbacks gate masking on `is_secret_key_name(group1)`, so a non-secret arrow (`x => y`) is untouched,
# and `a >= b` never matches (the char after the operator slot is `>`, not `:`/`=`, and `=>` needs `=`
# then `>`).
_ASSIGN_PREFIX = r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]+)(['\"]?\s*(?:=>|[:=])\s*)"


def is_secret_key_name(value) -> bool:
    """Whether a structured diagnostic key names credential material."""
    try:
        key = unicodedata.normalize("NFKC", str(value or "")).lower()
    except Exception:  # noqa: BLE001 - an opaque diagnostic key is safer masked than inspected twice
        return True
    return key not in _BENIGN_KEYS and _SECRET_KEY_RE.search(key) is not None


def _keyval_repl(m: re.Match) -> str:
    if not is_secret_key_name(m.group(1)):
        return m.group(0)                      # benign field name -> leave the value intact
    return f"{m.group(1)}{m.group(2)}***"


def _quoted_keyval_repl(m: re.Match) -> str:
    if not is_secret_key_name(m.group(1)):
        return m.group(0)
    value = m.group(3)
    quote = value[:1]
    closing = quote if len(value) > 1 and value.endswith(quote) else ""
    return f"{m.group(1)}{m.group(2)}{quote}***{closing}"


def _authorization_repl(m: re.Match) -> str:
    return f"{m.group(1)}{m.group(2)} ***"


# Known credential shapes — always redacted (negligible false-positive risk).
_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "sk-***"),                     # OpenAI-style (incl. sk-proj-)
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***"),                         # AWS access key id
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),      # GitHub fine-grained PAT
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "gh***"),                 # GitHub token (classic)
    (re.compile(r"hf_[A-Za-z0-9]{20,}"), "hf_***"),                       # HuggingFace token
    (re.compile(r"xox[baprse]-[A-Za-z0-9-]{10,}"), "xox***"),             # Slack token
    # RFC token68 credentials. Match the whole token (including + / _ ~ and = padding) and do not
    # impose a minimum length: short development credentials are still secrets when persisted.
    (re.compile(r"(?i)(\bauthorization\s*[:=]\s*)(bearer|basic)\s+"
                r"[A-Za-z0-9._~+/=-]+"), _authorization_repl),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "bearer ***"),
    # RFC-3986 userinfo occasionally appears in a consulted URL.  It is credential material even
    # without an explicit ``password=`` label, and source URLs are copied into durable memos.
    (re.compile(r"(?i)(?<=://)([^:/\s]+):([^@/\s]+)@"), "***:***@"),
    # run this before the generic assignment scanner. Without a URL-specific pass,
    # that scanner consumes ``https://...?token=value`` as one benign ``https:`` assignment and
    # never revisits the credential-bearing query parameter inside the match.
    (re.compile(
        r"(?i)([?&#;](?:api[_-]?key|secret|access[_-]?key|access[_-]?token|token|password|"
        r"passwd|credential)=)([^&#;\s]+)"
    ), lambda m: f"{m.group(1)}***"),
    # Match a generic identifier once, then classify it in the callback. This avoids quadratic
    # backtracking on large untrusted fields. Quoted values are consumed through their quote or line
    # boundary so spaces cannot leak; the unquoted form accepts any non-empty value.
    (re.compile(
        _ASSIGN_PREFIX
        + r"(\"(?:\\.|[^\"\\\r\n])*(?:\"|(?=\r?\n|\Z))"
          r"|'(?:\\.|[^'\\\r\n])*(?:'|(?=\r?\n|\Z)))",
        re.IGNORECASE,
    ), _quoted_keyval_repl),
    (re.compile(_ASSIGN_PREFIX + r"([^\s'\",;#&}\]]+)", re.IGNORECASE), _keyval_repl),
]


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def redact_secrets(text: str, *, entropy: bool = True,
                   entropy_cutoff: float = 4.2, min_len: int = 24) -> str:
    """Mask credentials in `text`. Known patterns are always redacted; if `entropy`, also mask long
    high-entropy tokens (likely base64/hex secrets) — conservative to avoid hashing false-positives."""
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    if entropy:
        def _mask(m: re.Match) -> str:
            tok = m.group(0)
            return "***REDACTED***" if _entropy(tok) >= entropy_cutoff else tok
        text = re.sub(rf"[A-Za-z0-9+/=_\-]{{{min_len},}}", _mask, text)
    return text


# The shortest env VALUE worth treating as a credential. A screened name with a 1-4 character value
# ("1", "true", "off", a port) is a switch, not a secret, and masking it would scrub those characters
# out of every unrelated word in the tail. Real credentials are longer than this by construction —
# the shortest thing any provider in `_PATTERNS` issues is well past it — so the floor costs nothing
# and the failure mode it removes (a tail rewritten into `***` soup) is one an operator cannot debug.
_MIN_SECRET_ENV_VALUE = 8


def secret_env_values(env=None) -> list[str]:
    """The operator's OWN secret env VALUES, longest first — the authenticated half of the screen.

    `_PATTERNS` recognises credential SHAPES, which is everything a regex can do about a secret it
    has never seen. It cannot recognise `hunter2hunter2` as this box's `POSTGRES_PASSWORD`, or a
    provider's bare-uuid key, or an internal token with a bespoke prefix — and those are exactly the
    secrets a `print(os.environ)`, a `subprocess` echo or a library traceback puts in a tail.

    So the second screen is the one place that KNOWS: `envsafe.is_secret_env`, the SAME predicate
    `runtime/sandbox.py` uses to withhold a variable from the child process. What the sandbox refuses
    to hand the candidate, this refuses to persist about it — one rule, two directions. (The value
    screen matters as much as the name one: `DATABASE_URL` names nothing suspicious and carries
    `postgres://user:pw@host/db`.)

    This is deterministic over AUTHENTICATED evidence in doc 36's sense: the authority is the
    operator's own process environment, never text the candidate wrote. Nothing a node prints can add
    a value to this list or take one off it.

    Longest first so a variable whose value is a PREFIX of another's cannot mask the shorter one
    first and leave the longer one's tail exposed as `***<rest>`.
    """
    items = (os.environ if env is None else env)
    try:
        pairs = list(items.items())
    except Exception:  # noqa: BLE001 - a diagnostic redactor must never raise into its caller
        return []
    out: set[str] = set()
    for name, value in pairs:
        try:
            text = str(value or "")
            if len(text.strip()) < _MIN_SECRET_ENV_VALUE:
                continue
            if is_secret_env(str(name or ""), text):
                out.add(text)
        except Exception:  # noqa: BLE001 - one odd variable must not disable the whole screen
            continue
    return sorted(out, key=lambda s: (-len(s), s))


def redact_env_values(text: str, env=None) -> str:
    """Mask every literal occurrence of a secret env VALUE in `text`. Exact substring, never a regex:
    the value is data, not a pattern, and `re.escape`-ing it back into one buys nothing."""
    if not text:
        return text
    for value in secret_env_values(env):
        if value in text:
            text = text.replace(value, "***REDACTED_ENV***")
    return text


def redact_output_tail(text: str, *, entropy: bool, env=None) -> str:
    """The persisted-tail redactor, and the ONE place the `redact_output` split is spelled.

    Known credential shapes and the operator's own env values are masked ALWAYS; `entropy` is the
    caller's `redact_output` and gates only the high-entropy pass (see this module's docstring for
    why those two halves are gated differently). Env values go FIRST — a secret masked by identity
    can no longer be half-eaten by a shape rule and re-emerge as a recognisable fragment.
    """
    if not text:
        return text
    return redact_secrets(redact_env_values(text, env), entropy=entropy)


def _persisted_input(value) -> str:
    """Stringify an untrusted diagnostic without allowing it to perturb its caller."""
    try:
        return "" if value is None else str(value)
    except Exception:  # noqa: BLE001 - diagnostics must not perturb the operation being recorded
        return "<unavailable>"


def _without_controls(text: str, *, keep_newlines: bool) -> str:
    """Replace terminal/browser control and format codepoints with inert spaces."""
    return "".join(
        ch if (ch == "\n" and keep_newlines) or not unicodedata.category(ch).startswith("C")
        else " "
        for ch in text
    )


def _bounded_redacted_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Bound already-redacted text without making the original secret an oracle, AND say whether the
    cap actually DROPPED anything.

    The second element is the only honest source for a "this was shortened" receipt, because it is
    decided HERE — against the cap that is about to be applied — rather than reconstructed further
    down by a caller measuring the result.  Neither downstream comparison works, and both shipped:

    * against the RAW input (`bounded_redacted_tree.safe_text` until 2026-08-06):
      `_redact_persisted` NFKC-normalizes and redacts BEFORE bounding, so output length says nothing
      about truncation in either direction.  `"ﬄ" * 300` EXPANDS to 900 characters, gets hashed away
      at a 500-char cap, and still measures 500 > 300 -> the destroyed value was reported as
      untruncated.  Lengths can also simply COINCIDE: `"password=x"` masks to 12 characters and
      hashes back down to 10, exactly its input.
    * against the REDACTED text: that only moves the lie.  A masked `sk-…` credential is shorter
      than its input, so every row carrying a secret would report itself truncated — the exact
      misleading receipt `bounded_redacted_tree` documents it must never emit.

    Returned as a tuple rather than through a second `_bounded_…`-named helper on purpose: two names
    one letter apart, one of which quietly discards the receipt, is how this comes back.
    """
    cap = max(0, int(max_chars))
    if len(text) <= cap:
        return text[:cap], False
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    marker = f"\n[redacted preview: original_chars={len(text)} sha256={digest}]"
    if len(marker) >= cap:
        return (marker[-cap:] if cap else ""), True
    return text[:cap - len(marker)] + marker, True


def _redact_persisted(value, *, max_chars: int, entropy: bool = True,
                      single_line: bool = False) -> tuple[str, bool]:
    """:func:`redact_persisted_text`, plus the bounder's own "the cap shortened this" bit.

    Split out so a caller that owes its operator a truncation receipt (`bounded_redacted_tree`) can
    read the fact from the one place that knows it, instead of inferring it from lengths.  The
    public spelling stays a plain `str`: ~30 call sites persist that return value directly.
    """
    text = _persisted_input(value)
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    # Persisted strings may be rendered by terminals and browsers. Keep an ordinary
    # newline only for multi-line prose; replace every other Unicode control/format character before
    # redaction, hashing, or truncation so ANSI/bidi/NUL payloads cannot survive in any representation.
    text = _without_controls(text, keep_newlines=not single_line)
    text = redact_secrets(text, entropy=entropy)
    if single_line:
        text = " ".join(text.split())
    return _bounded_redacted_text(text, max_chars)


def redact_persisted_text(value, *, max_chars: int, entropy: bool = True,
                          single_line: bool = False) -> str:
    # This always-on sanitizer belongs at durable boundaries, independent of UI settings.
    """Return a deterministic, display-safe string for an always-durable diagnostic field.

    Unlike :func:`redact_secrets`, this helper is not controlled by ``redact_output``: callers use
    it only at explicit persistence boundaries (memos/traces), where credentials and terminal or
    bidi controls must never be retained.  The digest is over the already-redacted canonical text,
    so a truncation marker cannot become an oracle for the original secret.
    """
    return _redact_persisted(value, max_chars=max_chars, entropy=entropy,
                             single_line=single_line)[0]


def redact_persisted_identity(value, *, max_chars: int) -> str:
    """Return one bounded/redacted opaque identity without Unicode compatibility folding.

    Run, task and action ids are equality keys, not display prose: NFKC would make distinct values such
    as ``"Ａ"`` and ``"A"`` alias.  Controls still become spaces and every known credential shape is
    masked.  If compatibility folding itself reveals a credential spelling that the original codepoints
    concealed, fail closed by masking the whole identity rather than retaining a confusable secret.
    """
    text = _without_controls(_persisted_input(value), keep_newlines=False)
    normalized = unicodedata.normalize("NFKC", text)
    if normalized != text and redact_secrets(normalized, entropy=False) != normalized:
        text = "***"
    else:
        text = redact_secrets(text, entropy=False)
    # Raw newlines were replaced above; only the truncation receipt can introduce one.  Replace that
    # delimiter without collapsing otherwise-significant whitespace in the opaque identity.  An
    # identity is an equality key with nowhere to carry a receipt, so the bounder's "was it cut" bit
    # is deliberately dropped here — the marker itself is already visible in the value.
    return _bounded_redacted_text(text, max_chars)[0].replace("\n", " ")


def bounded_redacted_tree(value, budget: list[int], items: list[int], *,
                          max_items: int = 64, max_depth: int = 5,
                          str_cap: int | None = None, key_cap: int = 160, depth: int = 0,
                          truncated: list[bool] | None = None):
    """Bound and redact an untrusted structured value into a small JSON-compatible shape.

    ONE walker for both durable boundaries that need this (doc 25 CO-06): the span/trace records in
    `core/tracing.py` and the advisory payload projections in `core/advisory_payloads.py`. They had
    written it twice, and the copies had already drifted — which is exactly the harm, because what
    this function does is REDACT: a fix landing in one walker silently missed the other.

    `budget` and `items` are shared mutable cells (a total character budget and a total node budget),
    passed in rather than created here so a caller that also spends the same budget on sibling text —
    `advisory_payloads` charges URLs and verdict rows against it — cannot be starved or double-spend.

    Callers differ ONLY in constants: `str_cap` (None = spend the whole remaining budget on one
    string; advisory caps each at 2000 so one oversized early field cannot consume the page) and
    `key_cap`. Everything else is uniform, deliberately, on the STRICTER of the two prior behaviours:

    * **A hostile mapping degrades, it does not raise.** A `dict` subclass whose `items()` throws used
      to take down the advisory projection with a RuntimeError; tracing already answered
      `"<mapping unavailable>"`. A redaction boundary that can raise is a boundary that can drop a
      whole payload, so the guard is now universal.
    * **A key that redacts to empty is DROPPED**, not emitted as `""`. Two such keys collide into one
      JSON member, silently discarding a value.
    * **A credential-named key is MASKED as `"***"`, never dropped.** `genesis` used to emit nothing
      at all for one (and call that truncation) while `misc` masked it, so the same payload got two
      answers depending on which endpoint served it. Masking wins because "this field exists and is
      a secret" is strictly more useful to an operator than a silently absent key — and a key that
      simply vanishes is indistinguishable from one that was never there. The drop behaviour was
      abandoned in ea9ea86f and survived until 2026-08-06 as an unreachable `mask_secret_keys=False`
      escape hatch no call site ever passed; it is gone, and a caller that genuinely needs it must
      re-argue the case above rather than flip a flag.
    * **Depth is cut at `>= max_depth`**, the earlier of the two cuts, and the marker is charged to
      the budget like any other emitted text.
    * **An `int` SUBCLASS stays an int** (`isinstance`, not `type(...) is`), so an `IntEnum` keeps its
      value instead of becoming a string.
    * **The walk stops as soon as the character budget is spent**, at every container level.

    `truncated` is an optional out-cell (`[False]`) set True whenever the projection OMITTED or
    SHORTENED something: a spent budget, a sliced fanout, a depth cut, a key that redacted to empty,
    a string the cap shortened, or a mapping that degraded. It exists because the serve routers show
    the operator a "this was cut" receipt (doc 25 SR-06) and had each grown their own walker to
    compute it. Masking a secret key is deliberately NOT truncation: nothing was dropped for SIZE,
    and telling an operator their config was truncated when a credential was merely hidden is a
    misleading receipt. Neither is NORMALIZATION — NFKC folding, control characters becoming spaces,
    `single_line` collapsing runs of whitespace: the value is all still there. What counts is the
    list above, and every member of it is decided by the code that does the omitting, never
    reconstructed afterwards from a length (`_bounded_redacted_text` records what that cost).
    """
    def cut():
        if truncated is not None:
            truncated[0] = True

    def safe_text(item, *, cap=None, single_line=False):
        allowed = budget[0] if cap is None else min(budget[0], max(0, int(cap)))
        # The BOUNDER reports whether the cap dropped characters; this used to guess it by comparing
        # the output against `len(_persisted_input(item))`, which was wrong in BOTH directions
        # (`_bounded_redacted_text` records the two measurements). Take the signal alone.
        text, shortened = _redact_persisted(item, max_chars=allowed, entropy=True,
                                            single_line=single_line)
        if shortened:
            cut()
        budget[0] = max(0, budget[0] - len(text))
        return text

    def walk(item, level):
        if budget[0] <= 0:
            cut()
            return ""
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            return safe_text(item, cap=str_cap)
        if isinstance(item, int):
            return item if -(1 << 63) <= item <= (1 << 63) - 1 else safe_text(item, cap=128)
        if isinstance(item, float):
            return item if math.isfinite(item) else safe_text(item, cap=32)
        if level >= max_depth:
            cut()
            return safe_text("<depth-limited>", cap=32, single_line=True)
        if isinstance(item, dict):
            out = {}
            try:
                if len(item) > max_items:
                    cut()
                for key, child in _islice(item.items(), max_items):
                    if items[0] <= 0:
                        cut()
                        break
                    items[0] -= 1
                    safe_key = safe_text(key, cap=key_cap, single_line=True)
                    if not safe_key or safe_key in out:
                        cut()          # a key that redacts to empty, or collides, loses its value
                        continue
                    if is_secret_key_name(key):
                        out[safe_key] = "***"
                        budget[0] = max(0, budget[0] - 3)
                    else:
                        out[safe_key] = walk(child, level + 1)
                    if budget[0] <= 0:
                        cut()
                        break
                return out
            except Exception:  # noqa: BLE001 - a redaction boundary must never raise at its caller
                cut()
                return safe_text("<mapping unavailable>", cap=64, single_line=True)
        if isinstance(item, (list, tuple)):
            out = []
            if len(item) > max_items:
                cut()
            for child in _islice(item, max_items):
                if budget[0] <= 0 or items[0] <= 0:
                    cut()
                    break
                items[0] -= 1
                out.append(walk(child, level + 1))
            return out
        return safe_text(item, cap=str_cap)

    return walk(value, depth)
