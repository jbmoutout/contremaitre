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
