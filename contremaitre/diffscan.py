"""Deterministic pre-publication diff scanner.

Catches paths that are toxic to publish regardless of SIM verdict —
real secrets, private key material. NOT a substitute for SIM review,
just a belt-and-suspenders deterministic floor.

Policy intent:

  - Block paths whose presence in a diff almost certainly means a leak
    (.env, .envrc, *.pem, *.key) — false positive risk is near-zero
    for a refactor skill and the failure mode (publishing a secret)
    is catastrophic.
  - Allow paths the design might legitimately touch: schema migrations
    (when SETTLED designs for them — the SIM is the gate on whether
    the migration matches the design), example/sample env templates
    (public by convention).

History: `prisma/migrations/*` used to be in the forbidden list. That
turned out to block legitimate runs where SETTLED.md called for a
schema change + migration (the SIM approves the migration as
faithful, the deterministic scanner kills it anyway). Removed —
migrations are the SIM's call now, not a hard gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from .git_utils import GitRepo

# fnmatch's `*` matches `/` too, so e.g. `*.pem` catches subdir paths
# automatically. `.env`-shape patterns don't have that property
# (the leading dot anchors them to a filename) — we have to enumerate
# the `*/`-prefixed variant explicitly to catch nested `.env` files.
FORBIDDEN_PATTERNS = (
    # Environment / dotenv variants — anywhere in the tree.
    ".env",
    "*/.env",
    ".env.*",
    "*/.env.*",
    ".envrc",
    "*/.envrc",
    ".envrc.*",
    "*/.envrc.*",
    # PEM-encoded key + private-key material.
    "*.pem",
    "*.key",
)

# Paths that match FORBIDDEN_PATTERNS but are conventionally public
# (env-shape documentation checked into the repo on purpose). When a
# refactor renames an env variable across the codebase, the agent
# SHOULD update these — blocking them would force either silent skip
# or unnecessary failure.
FORBIDDEN_EXCEPTIONS = (
    ".env.example",
    "*/.env.example",
    ".env.sample",
    "*/.env.sample",
    ".env.template",
    "*/.env.template",
    ".env.defaults",
    "*/.env.defaults",
)


@dataclass(frozen=True)
class DiffScanResult:
    passed: bool
    changed_files: list[str]
    forbidden_files: list[str]
    patterns: tuple[str, ...] = FORBIDDEN_PATTERNS


def scan_diff(
    repo: GitRepo, base: str, patterns: tuple[str, ...] = FORBIDDEN_PATTERNS
) -> DiffScanResult:
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
    if not any(fnmatch(path, pattern) for pattern in patterns):
        return False
    return not any(fnmatch(path, exc) for exc in FORBIDDEN_EXCEPTIONS)
