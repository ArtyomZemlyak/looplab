"""Allow-listed response language; a turn pins it alongside its instruction/mode."""


def response_language(value="auto") -> str:
    if not isinstance(value, str) or value not in ("auto", "en", "ru"):
        raise ValueError("response_language must be auto, en or ru")
    return value


def language_directive(value="auto") -> str:
    value = response_language(value)
    if value == "auto":
        return ""
    name = {"en": "English", "ru": "Russian"}[value]
    return (f"\nThe operator selected response language: {name} ({value}). "
            "Use it for explanations, result summaries and the final answer, even when evidence "
            "is in another language. Preserve code, identifiers, paths and quoted evidence.\n")
