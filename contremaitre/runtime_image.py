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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class DepsInstallError(RuntimeError):
    """Lockfile was detected but the install one-shot container exited non-zero.

    Carries the path to the captured install log so the operator can
    inspect the real failure (often a postinstall script — `prisma
    generate`, husky, etc. — that needs source files present in the
    install context).
    """

    def __init__(self, *, lockfile: str, log_path: Path, returncode: int):
        super().__init__(
            f"deps install for {lockfile} failed (rc={returncode}); see {log_path}"
        )
        self.lockfile = lockfile
        self.log_path = log_path
        self.returncode = returncode


@dataclass(frozen=True)
class _Lockfile:
    name: str
    install_cmd: str


_LOCKFILES: tuple[_Lockfile, ...] = (
    _Lockfile("package-lock.json", "npm ci --no-audit --no-fund"),
    _Lockfile("pnpm-lock.yaml", "corepack pnpm install --frozen-lockfile"),
    _Lockfile("yarn.lock", "yarn install --frozen-lockfile --non-interactive"),
    _Lockfile("poetry.lock", "pip install --quiet poetry && poetry install --no-root"),
    _Lockfile("uv.lock", "pip install --quiet uv && uv sync --frozen --no-install-project"),
    _Lockfile("Cargo.lock", "cargo fetch"),
    _Lockfile("go.sum", "go mod download"),
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


def ensure_deps_volume(*, repo: Path, base_image: str, runs_root: Path) -> str | None:
    """Ensure a populated lockhash-keyed deps volume exists, return its name.

    Returns None if the repo has no recognized lockfile — publication
    then continues without a /app/node_modules mount, and any L1 check
    that needs installed deps will fail clearly inside the sidecar.

    Raises DepsInstallError if a lockfile *was* detected but the install
    one-shot container exited non-zero. We deliberately do NOT silently
    fall back to "no deps" in that case: the failure mode of running
    `npx tsc` against an empty node_modules is npm-helpfully installing
    the `tsc@2.0.4` placeholder package, which prints a deceptive
    "this is not the tsc command you are looking for" message and
    returns rc=1. That looks like a real TypeScript error in the check
    report but is actually our infra silently degraded. Surface the
    real install error and stop.

    The install container mounts the host repo RO at /install and the
    deps volume RW at /install/node_modules. This is the simplest
    install context that lets postinstall scripts read project source
    files (e.g. `prisma generate` reads `prisma/schema.prisma`,
    `husky install` reads `.git/`) without our needing to enumerate
    every framework's companion-file list. RO is fine for the common
    case; postinstall scripts that need to write to source files fail
    loud, which is the correct signal.

    Side effects: docker volume create, docker run, and a per-lockhash
    install log at `<runs_root>/_deps_install_<lockhash>.log`.
    """

    detected = _detect(repo)
    if detected is None:
        return None
    lockfile, lock_path = detected
    digest = _digest(lock_path)
    volume = f"contremaitre-deps-{_safe_name(lockfile.name)}-{digest}"

    if _volume_exists(volume):
        # Self-heal even on cache hit: an older hash may have lingered
        # from before the operator's last lockfile bump and there's no
        # other moment we'd prune it.
        _prune_stale_deps_volumes(lockfile_name=lockfile.name, current_volume=volume)
        return volume

    runs_root.mkdir(parents=True, exist_ok=True)
    log_path = runs_root / f"_deps_install_{digest}.log"

    print(f"contremaitre: populating deps volume {volume} (log: {log_path})", file=sys.stderr)
    try:
        subprocess.run(
            ["docker", "volume", "create",
             "--label", "contremaitre.purpose=deps-cache",
             volume],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        log_path.write_text(f"docker volume create failed:\n{exc.stderr}", encoding="utf-8")
        raise DepsInstallError(lockfile=lockfile.name, log_path=log_path, returncode=exc.returncode)

    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            "--label", "contremaitre.role=deps-install",
            "-v", f"{repo.resolve()}:/install:ro",
            "-v", f"{volume}:/install/node_modules",
            "-w", "/install",
            base_image,
            "sh", "-lc", lockfile.install_cmd,
        ],
        capture_output=True, text=True, timeout=900,
    )
    log_path.write_text(
        f"$ {lockfile.install_cmd}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
        encoding="utf-8",
    )
    if proc.returncode != 0:
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume],
            capture_output=True, timeout=10,
        )
        raise DepsInstallError(lockfile=lockfile.name, log_path=log_path, returncode=proc.returncode)
    _prune_stale_deps_volumes(lockfile_name=lockfile.name, current_volume=volume)
    return volume


def _prune_stale_deps_volumes(*, lockfile_name: str, current_volume: str) -> None:
    """Remove same-lockfile-kind deps volumes whose hash isn't current.

    Lockfile-hash bumps (e.g. `npm install` adds a dep, lockfile digest
    changes) create a fresh volume; the previous one becomes garbage —
    no future run against this target will reuse it. Sweep them here so
    `docker volume ls` doesn't grow linearly with lockfile churn.

    Scoped to the SAME lockfile kind (`package-lock-json-*` doesn't
    touch `yarn-lock-*`) so a target that has multiple ecosystems keeps
    each cache. Best-effort: a volume in use by another container won't
    delete, and we swallow that — never the auto-prune's job to break
    parallel runs.
    """

    prefix = f"contremaitre-deps-{_safe_name(lockfile_name)}-"
    try:
        proc = subprocess.run(
            ["docker", "volume", "ls", "-q", "--filter", f"name={prefix}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if proc.returncode != 0:
        return
    for name in proc.stdout.splitlines():
        name = name.strip()
        if not name or name == current_volume or not name.startswith(prefix):
            continue
        rm = subprocess.run(
            ["docker", "volume", "rm", name],
            capture_output=True, text=True, timeout=10,
        )
        if rm.returncode == 0:
            print(f"contremaitre: pruned stale deps volume {name}", file=sys.stderr)


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
