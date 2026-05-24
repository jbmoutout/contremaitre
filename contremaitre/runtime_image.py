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

from .models import DepsVolume


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
    """One ecosystem's deps-cache recipe.

    `cache_mount_path` is the relative-to-repo path that the install tool
    writes its cached deps into. We mount the named docker volume there
    so install output lands in the cache, not in an ephemeral container
    layer. The same path is mounted at `/app/{cache_mount_path}` in the
    agent/sim/check containers downstream so the runtime tool finds the
    cache where it expects it.

    `runtime_env` is the (key, value) env vars the downstream containers
    need to point the ecosystem at the cache. Values use `/app/` paths
    (the install one-shot rewrites them to `/install/` automatically).
    Empty tuple for Node — npm/yarn/pnpm find `node_modules/` by
    convention without env hints.
    """

    name: str
    install_cmd: str
    cache_mount_path: str
    runtime_env: tuple[tuple[str, str], ...] = ()


_PY_VENV_ENV: tuple[tuple[str, str], ...] = (
    ("VIRTUAL_ENV", "/app/.venv"),
    ("PATH", "/app/.venv/bin:/root/.local/bin:/root/.opencode/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
)


_LOCKFILES: tuple[_Lockfile, ...] = (
    _Lockfile("package-lock.json", "npm ci --no-audit --no-fund", "node_modules"),
    _Lockfile("pnpm-lock.yaml", "corepack pnpm install --frozen-lockfile", "node_modules"),
    _Lockfile("yarn.lock", "yarn install --frozen-lockfile --non-interactive", "node_modules"),
    _Lockfile(
        "poetry.lock",
        "POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --no-root",
        ".venv",
        _PY_VENV_ENV,
    ),
    _Lockfile(
        "uv.lock",
        "uv sync --frozen --no-install-project",
        ".venv",
        _PY_VENV_ENV,
    ),
    # rye / pip-tools. The lockfile is an exhaustive pip-installable
    # requirements file (each line is `name==version` with all transitive
    # deps resolved), so `--no-deps` is safe and matches the
    # frozen-lock semantics of uv.lock / poetry.lock above. `uv venv` on
    # an empty mount-point dir creates a venv in place; `uv pip install`
    # then populates it from the lockfile. Lower priority than uv.lock —
    # projects mid-migration that have both will use uv.lock.
    _Lockfile(
        "requirements.lock",
        "uv venv .venv && uv pip install --no-deps -r requirements.lock",
        ".venv",
        _PY_VENV_ENV,
    ),
    _Lockfile(
        "Cargo.lock",
        "cargo fetch",
        ".cargo-cache",
        (("CARGO_HOME", "/app/.cargo-cache"),),
    ),
    _Lockfile(
        "go.sum",
        "go mod download",
        ".go-mod-cache",
        (("GOPATH", "/app/.go-mod-cache"),),
    ),
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


def ensure_deps_volume(
    *,
    repo: Path,
    base_image: str,
    runs_root: Path,
    project_id: str,
) -> DepsVolume | None:
    """Ensure a populated lockhash-keyed deps volume exists, return its handle.

    Returns None if the repo has no recognized lockfile — publication
    then continues without a deps mount, and any L1 check that needs
    installed deps will fail clearly inside the sidecar.

    Raises DepsInstallError if a lockfile *was* detected but the install
    one-shot container exited non-zero. We deliberately do NOT silently
    fall back to "no deps" in that case: the failure mode of running
    `npx tsc` against an empty node_modules is npm-helpfully installing
    the `tsc@2.0.4` placeholder package, which prints a deceptive
    "this is not the tsc command you are looking for" message and
    returns rc=1. That looks like a real TypeScript error in the check
    report but is actually our infra silently degraded. Surface the
    real install error and stop.

    The install container mounts the host repo RW at /app and the
    deps volume RW at /app/{lock.cache_mount_path} — that's
    `node_modules/` for Node, `.venv/` for Python (uv/poetry),
    `.cargo-cache/` for Rust, `.go-mod-cache/` for Go.

    Why RW source: docker needs to create the cache mountpoint
    directory inside the source if it doesn't already exist (`mkdirat
    /app/.venv: read-only file system`). The worktree from `git
    worktree add` has no untracked dirs (no `node_modules/`, no
    `.venv/`), so a RO mount fails at container-create time on every
    fresh repo. RW is safe here: (a) the source is the per-run
    worktree, removed in `finally`; (b) HUSKY=0/CI=1 disables the
    lifecycle hooks that historically wrote to source files; (c) the
    install commands themselves don't write to source.

    Crucially the path matches the runtime mount (also /app) so that
    tools embedding the venv path into their output (uv writes
    shebangs like `#!/app/.venv/bin/python` into installed scripts)
    produce paths that resolve at runtime. An /install vs /app skew
    here silently breaks every Python script in the cache.

    Volume naming includes `project_id` (typically the cache-clone slug,
    e.g. `github.com-jbmoutout-contremaitre`) so two projects with the
    same lockfile kind don't collide in `_prune_stale_deps_volumes`:
    without the scope, running project A then project B would evict
    A's cache because both have e.g. `package-lock.json` and the prune
    looks at lockfile kind alone. Cross-project deduplication is
    forfeit (same content in two repos → two copies cached) but that's
    rare and the eviction was a concrete pain.

    Side effects: docker volume create, docker run, and a per-lockhash
    install log at `<runs_root>/_deps_install_<lockhash>.log`.
    """

    detected = _detect(repo)
    if detected is None:
        return None
    lockfile, lock_path = detected
    digest = _digest(lock_path)
    project_slug = _safe_name(project_id)
    volume = f"contremaitre-deps-{project_slug}-{_safe_name(lockfile.name)}-{digest}"
    handle = DepsVolume(
        name=volume,
        mount_path=lockfile.cache_mount_path,
        runtime_env=lockfile.runtime_env,
    )

    if _volume_exists(volume):
        # Self-heal even on cache hit: an older hash may have lingered
        # from before the operator's last lockfile bump and there's no
        # other moment we'd prune it.
        _prune_stale_deps_volumes(
            project_slug=project_slug,
            lockfile_name=lockfile.name,
            current_volume=volume,
        )
        return handle

    runs_root.mkdir(parents=True, exist_ok=True)
    log_path = runs_root / f"_deps_install_{digest}.log"

    print(f"contremaitre: populating deps volume {volume} (log: {log_path})", file=sys.stderr)
    try:
        subprocess.run(
            ["docker", "volume", "create",
             "--label", "contremaitre.purpose=deps-cache",
             "--label", f"contremaitre.project={project_id}",
             volume],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        log_path.write_text(f"docker volume create failed:\n{exc.stderr}", encoding="utf-8")
        raise DepsInstallError(lockfile=lockfile.name, log_path=log_path, returncode=exc.returncode)

    docker_cmd = [
        "docker", "run", "--rm",
        "--label", "contremaitre.role=deps-install",
        # Prevent lifecycle hooks that try to write to the source repo
        # (which is mounted RO). Husky's `prepare` script calls `husky
        # install` → writes to `.git/hooks/` → EACCES on the RO mount.
        # HUSKY=0 is the canonical opt-out; CI=1 is the broader signal
        # for "don't run interactive setup hooks".
        "-e", "HUSKY=0",
        "-e", "CI=1",
    ]
    # Runtime env vars (VIRTUAL_ENV / CARGO_HOME / GOPATH) pass through
    # unchanged — install and runtime both see /app, so paths embedded
    # at install time (uv shebangs, cargo registry index) resolve later.
    for key, value in lockfile.runtime_env:
        docker_cmd.extend(["-e", f"{key}={value}"])
    docker_cmd.extend([
        "-v", f"{repo.resolve()}:/app:rw",
        "-v", f"{volume}:/app/{lockfile.cache_mount_path}",
        "-w", "/app",
        base_image,
        "sh", "-lc", lockfile.install_cmd,
    ])
    proc = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=900)
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
    _prune_stale_deps_volumes(
        project_slug=project_slug,
        lockfile_name=lockfile.name,
        current_volume=volume,
    )
    return handle


def _prune_stale_deps_volumes(
    *,
    project_slug: str,
    lockfile_name: str,
    current_volume: str,
) -> None:
    """Remove same-project + same-lockfile-kind deps volumes whose hash isn't current.

    Lockfile-hash bumps (e.g. `npm install` adds a dep, lockfile digest
    changes) create a fresh volume; the previous one becomes garbage —
    no future run against this target will reuse it. Sweep them here so
    `docker volume ls` doesn't grow linearly with lockfile churn.

    Scoped to the SAME project AND SAME lockfile kind so:
    - A target that has multiple ecosystems keeps each cache.
    - Running project A then project B doesn't evict A's `package-lock.json`
      cache when B's `package-lock.json` has a different digest.

    Best-effort: a volume in use by another container won't delete, and
    we swallow that — never the auto-prune's job to break parallel runs.
    """

    prefix = f"contremaitre-deps-{project_slug}-{_safe_name(lockfile_name)}-"
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


def clone_deps_volume_for_run(*, pristine: DepsVolume, run_id: str, base_image: str) -> DepsVolume:
    """Clone the pristine deps cache into a per-run volume, return its handle.

    Why a clone per run instead of mounting pristine RW: mounts are
    shared across container runs against the same lockhash. If run N's
    agent does `npm install vitest`, vitest persists into the cache and
    run N+1 sees it even though its lockfile doesn't list it. That's
    silent state-leak between runs. Per-run clone keeps the cache
    pristine (no mutation) and gives each run a fresh RW workspace.

    The clone is a one-shot `cp -a` of one docker volume into another,
    both running inside the contremaitre runtime image so the copy
    happens over the docker storage filesystem (fast — ~5-15s for a
    typical Node project, not the 60-90s of a fresh `npm ci`).

    Per-run volume is labeled `contremaitre.run-id=<id>` so the
    orchestrator's label-based cleanup removes it in `finally`.

    `mount_path` and `runtime_env` carry through from the pristine
    handle unchanged — the clone is just the same bytes under a
    different volume name.
    """

    per_run = f"contremaitre-run-{run_id}-deps"
    subprocess.run(
        ["docker", "volume", "create",
         "--label", "contremaitre.purpose=deps-run",
         "--label", f"contremaitre.run-id={run_id}",
         per_run],
        check=True, capture_output=True, text=True, timeout=10,
    )
    subprocess.run(
        [
            "docker", "run", "--rm",
            "--label", f"contremaitre.run-id={run_id}",
            "--label", "contremaitre.role=deps-clone",
            "-v", f"{pristine.name}:/src:ro",
            "-v", f"{per_run}:/dst",
            base_image,
            "sh", "-lc", "cp -a /src/. /dst/",
        ],
        check=True, capture_output=True, text=True, timeout=300,
    )
    return DepsVolume(
        name=per_run,
        mount_path=pristine.mount_path,
        runtime_env=pristine.runtime_env,
    )


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
