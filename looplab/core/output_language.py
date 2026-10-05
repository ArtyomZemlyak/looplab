"""One prose-language contract for every model role, including streamed and structured output.

No translation call and no rewriting of measured evidence. The provider receives a trusted
instruction for newly authored human prose; schema keys, enum values, code and quotes stay raw.
An explicit Assistant turn overrides the run/default language for its own context only.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_override = ContextVar("looplab_output_language", default=None)


def validate_language(value):
    if value not in ("auto", "en", "ru") or not isinstance(value, str):
        raise ValueError("output_language must be auto, en or ru")
    return value


def output_directive(language):
    language = validate_language(language)
    if language == "auto":
        return ""
    name = "Russian" if language == "ru" else "English"
    return (f"\n[LoopLab output language: {name} ({language})]\n"
            f"Author ALL human-readable prose in {name}: titles, explanations, plans, hypotheses, "
            "research memos, reviews, lessons, skill descriptions, reports, progress messages and "
            "final answers, including human-readable string values in structured JSON. "
            "This also applies when the task or evidence is in another language. "
            "Preserve schema keys, enum values, tool names/arguments that identify actions, "
            "concept IDs/slugs, code, commands, paths, URLs, metric names/numbers, and verbatim "
            "quoted evidence. Never translate protected scoring or rewrite observations.\n")


def language_messages(messages, language):
    directive = output_directive(_override.get() or language)
    if not directive:
        return messages
    rows = list(messages)
    if rows and rows[0].get("role") == "system" and isinstance(rows[0].get("content"), str):
        rows[0] = {**rows[0], "content": rows[0]["content"] + directive}
    else:
        rows.insert(0, {"role": "system", "content": directive})
    return rows


@contextmanager
def language_scope(language):
    value = validate_language(language)
    token = _override.set(_override.get() if value == "auto" else value)
    try:
        yield
    finally:
        _override.reset(token)


def assistant_language_scope(fn):
    @wraps(fn)
    def scoped(*args, **kwargs):
        with language_scope(kwargs.get("response_language", "auto")):
            return fn(*args, **kwargs)
    return scoped


class LanguageClient:
    """Transparent role-client facade; accounting, cancellation, routing and cache stay with it."""

    def __init__(self, client, language):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_language", language)

    def __getattr__(self, key):
        return getattr(self._client, key)

    def __setattr__(self, key, value):
        setattr(self._client, key, value)

    def _messages(self, messages):
        language = self._language() if callable(self._language) else self._language
        return language_messages(messages, language)

    def chat(self, messages, *args, **kwargs):
        return self._client.chat(self._messages(messages), *args, **kwargs)

    def complete_text(self, messages, *args, **kwargs):
        return self._client.complete_text(self._messages(messages), *args, **kwargs)

    def complete_tool(self, messages, *args, **kwargs):
        return self._client.complete_tool(self._messages(messages), *args, **kwargs)

    def complete_text_stream(self, messages, *args, **kwargs):
        # Bind the invoking context now, even if a caller consumes the stream later.
        return self._client.complete_text_stream(self._messages(messages), *args, **kwargs)


def language_client(client, settings):
    language = validate_language(getattr(settings, "output_language", "auto"))
    if client is None or (language == "auto" and _override.get() is None) or isinstance(client, LanguageClient):
        return client
    return LanguageClient(client, lambda: getattr(settings, "output_language", "auto"))


def current_output_language(client=None, fallback="auto"):
    if _override.get() is not None:
        return _override.get()
    if isinstance(client, LanguageClient):
        value = client._language() if callable(client._language) else client._language
        return validate_language(value)
    return validate_language(fallback)
