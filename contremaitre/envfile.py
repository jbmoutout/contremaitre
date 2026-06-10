"""Minimal `.env` loading for local Contremaitre operator secrets.

The CLI needs one narrow feature from python-dotenv: populate missing
environment variables before preflight and Docker env whitelisting run. Keeping
this parser local avoids a dependency for a small, predictable file format.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv_defaults(cwd: Path | None = None) -> list[Path]:
    """Load supported `.env` files without overriding process environment.

    Lookup order intentionally preserves operator intent:

    1. explicit environment variables already present in the shell;
    2. `.env` in the current working directory;
    3. `.env` in the source checkout that contains this package.

    The returned paths are the files that existed and were parsed.
    """

    root = Path(__file__).resolve().parents[1]
    candidates = _unique_paths([(cwd or Path.cwd()) / ".env", root / ".env"])
    loaded: list[Path] = []
    for path in candidates:
        if path.exists():
            load_env_file(path)
            loaded.append(path)
    return loaded


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE assignments from `path`.

    Supported syntax is deliberately small: blank lines, `#` comments,
    optional `export`, unquoted values, and single/double quoted values.
    Inline comments are supported only for unquoted values after whitespace.
    Existing variables are never overwritten.
    """

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)


def upsert_env_var(path: Path, key: str, value: str) -> None:
    """Set ``KEY=value`` in the ``.env`` at ``path``, in place.

    Rewrites an existing assignment for ``key`` (matched the same way the loader
    parses lines, so ``export KEY=`` and quoted forms are recognised) or appends
    a new line. Creates the file if absent. Only the target key's line changes;
    comments, blank lines, and other assignments are preserved verbatim. The
    value is single-quoted so ``#``/whitespace survive the loader round-trip.
    """

    line = f"{key}={_quote_value(value)}"
    if not path.exists():
        path.write_text(line + "\n", encoding="utf-8")
        return

    out: list[str] = []
    replaced = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(raw)
        if parsed is not None and parsed[0] == key:
            out.append(line)
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _quote_value(value: str) -> str:
    # Single-quote; the loader's _strip_value only peels matching outer quotes,
    # so a value containing a single quote can't round-trip — refuse it loudly
    # rather than write a file the loader would mis-parse.
    if "'" in value:
        raise ValueError("cannot persist a value containing a single quote to .env")
    return f"'{value}'"


def _parse_line(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    if "=" not in line:
        return None

    key, value = line.split("=", 1)
    key = key.strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        return None

    return key, _strip_value(value.strip())


def _strip_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique
