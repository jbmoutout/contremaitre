"""Coarse model-family classification.

Used by model-pair diagnostics to label and compare coarse agent/SIM model
families. Heuristic, not authoritative — model strings follow a
`<provider>/<family>/<variant>` or `<provider>/<id>` convention and we
extract the family from the middle component when possible, falling back
to a prefix match on the bare id.

Returns lowercased family slugs (`deepseek`, `qwen`, `glm`, `kimi`, …) or
`"unknown"` when the string doesn't match any known family. Callers should
treat "unknown" as "can't compare families" — i.e. don't claim
cross-family-ness when one side is unknown.
"""

from __future__ import annotations

# Bare-id prefixes that map to a family. Used as a fallback when the middle
# segment doesn't itself name the family (e.g. opencode/qwen-32b/free).
_PREFIX_FAMILIES: tuple[tuple[str, str], ...] = (
    ("deepseek", "deepseek"),
    ("qwen", "qwen"),
    ("glm", "glm"),
    ("kimi", "kimi"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("claude", "anthropic"),
    ("gemini", "google"),
    ("llama", "meta"),
    ("mistral", "mistral"),
    ("mixtral", "mistral"),
    ("big-pickle", "unknown"),
)


def model_family(model_str: str | None) -> str:
    """Return the lowercased family slug for a model string, or `"unknown"`."""

    if not model_str:
        return "unknown"
    parts = model_str.split("/")
    # `<provider>/<family>/<variant>` — middle segment is canonical family.
    if len(parts) >= 3:
        candidate = parts[1].lower()
        for _prefix, family in _PREFIX_FAMILIES:
            if candidate.startswith(_prefix):
                return family
        return candidate
    # `<provider>/<id>` or bare id — fall back to prefix scan on the last
    # segment.
    bare = parts[-1].lower()
    for prefix, family in _PREFIX_FAMILIES:
        if bare.startswith(prefix):
            return family
    return "unknown"
