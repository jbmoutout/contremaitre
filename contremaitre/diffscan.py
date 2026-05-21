"""Deterministic pre-publication diff scanner."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from .git_utils import GitRepo

FORBIDDEN_PATTERNS = (
    "prisma/migrations/*",
    "**/prisma/migrations/*",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
)


@dataclass(frozen=True)
class DiffScanResult:
    passed: bool
    changed_files: list[str]
    forbidden_files: list[str]
    patterns: tuple[str, ...] = FORBIDDEN_PATTERNS


def scan_diff(repo: GitRepo, base: str, patterns: tuple[str, ...] = FORBIDDEN_PATTERNS) -> DiffScanResult:
    raw = repo.output("diff", "--name-only", f"{base}...HEAD")
    changed = [line.strip() for line in raw.splitlines() if line.strip()]
    forbidden = [path for path in changed if _is_forbidden(path, patterns)]
    return DiffScanResult(
        passed=not forbidden,
        changed_files=changed,
        forbidden_files=forbidden,
        patterns=patterns,
    )


def _is_forbidden(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(path, pattern) for pattern in patterns)

