"""Lockfile-keyed dependency caching for the agent runtime.

A bind-mounted worktree shadows any `/app/node_modules` baked into an
image layer, so the SWE-bench / OpenHands "deps in image tag" pattern
doesn't work directly. Instead we cache deps in a **named docker volume**
keyed on the lockfile's SHA: `contremaitre-deps-<lockfile>-<digest>`.
First run against a given lockfile populates the volume by running the
install command in a one-shot container; subsequent runs reuse it
verbatim. Different lockfile → different digest → fresh volume.

The volume is mounted read-only in agent + SIM + check containers so
runs against the same lockhash don't poison each other. If the agent
genuinely needs a new dep, it edits package.json + lockfile; the next
run sees a new digest and populates a fresh volume.

Supported ecosystems are the ones with a deterministic lockfile +
non-interactive install command. Unsupported targets get `None` —
publication continues without a deps volume (and without container
checks that depend on installed deps).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _Lockfile:
    name: str
    install_cmd: str
    # Files in addition to the lockfile that the install command needs in
    # the working directory (e.g. npm needs package.json present).
    companion_files: tuple[str, ...] = ()


_LOCKFILES: tuple[_Lockfile, ...] = (
    _Lockfile("package-lock.json", "npm ci --no-audit --no-fund", ("package.json",)),
    _Lockfile("pnpm-lock.yaml", "corepack pnpm install --frozen-lockfile", ("package.json",)),
    _Lockfile("yarn.lock", "yarn install --frozen-lockfile --non-interactive", ("package.json",)),
    _Lockfile("poetry.lock", "pip install --quiet poetry && poetry install --no-root", ("pyproject.toml",)),
    _Lockfile("uv.lock", "pip install --quiet uv && uv sync --frozen --no-install-project", ("pyproject.toml",)),
    _Lockfile("Cargo.lock", "cargo fetch", ("Cargo.toml",)),
    _Lockfile("go.sum", "go mod download", ("go.mod",)),
)


def _detect(repo: Path) -> tuple[_Lockfile, Path] | None:
    for lock in _LOCKFILES:
        path = repo / lock.name
        if path.exists():
            return lock, path
    return None


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _safe_name(lockfile_name: str) -> str:
    return lockfile_name.replace(".", "-")


def ensure_deps_volume(*, repo: Path, base_image: str) -> str | None:
    """Ensure a populated lockhash-keyed deps volume exists, return its name.

    Returns None if the repo has no recognized lockfile (publication then
    continues without a /app/node_modules mount; checks that need installed
    deps will fail loudly inside the sidecar, which is the correct signal).

    Side effects: may call `docker volume create` and `docker run` to
    populate the volume. Both are idempotent across re-invocations.
    """

    detected = _detect(repo)
    if detected is None:
        return None
    lockfile, lock_path = detected
    volume = f"contremaitre-deps-{_safe_name(lockfile.name)}-{_digest(lock_path)}"

    if _volume_exists(volume):
        return volume

    print(f"contremaitre: populating deps volume {volume}", file=sys.stderr)
    try:
        subprocess.run(
            ["docker", "volume", "create", volume],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        print(f"contremaitre: docker volume create failed: {exc.stderr}", file=sys.stderr)
        return None

    with tempfile.TemporaryDirectory() as ctx_str:
        ctx = Path(ctx_str)
        shutil.copy2(lock_path, ctx / lockfile.name)
        for companion in lockfile.companion_files:
            src = repo / companion
            if src.exists():
                shutil.copy2(src, ctx / companion)
        # `node_modules` is the path the agent and checks read from; the
        # install command writes there relative to /work.
        proc = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{ctx}:/work:ro",
                "-v", f"{volume}:/install/node_modules",
                "-w", "/install",
                base_image,
                "sh", "-lc",
                # Copy ctx contents into /install so the install command
                # operates on a writable tree, then run the install which
                # populates /install/node_modules → the named volume.
                f"cp -r /work/. /install/ && {lockfile.install_cmd}",
            ],
            capture_output=True, text=True, timeout=900,
        )
        if proc.returncode != 0:
            print(
                f"contremaitre: deps install failed (rc={proc.returncode})\n"
                f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}",
                file=sys.stderr,
            )
            subprocess.run(
                ["docker", "volume", "rm", "-f", volume],
                capture_output=True, timeout=10,
            )
            return None
    return volume


def _volume_exists(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "volume", "inspect", name],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def list_deps_volumes() -> list[str]:
    """All `contremaitre-deps-*` volumes on the host. Used by `cleanup --deps`."""

    try:
        proc = subprocess.run(
            ["docker", "volume", "ls", "-q", "--filter", "name=contremaitre-deps-"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
