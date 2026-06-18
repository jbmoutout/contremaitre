"""Lockfile-keyed dependency caching for the agent runtime.

A bind-mounted worktree shadows any `/app/node_modules` baked into an
image layer, so the SWE-bench / OpenHands "deps in image tag" pattern
doesn't work directly. Instead we cache deps in a **named docker volume**
keyed on the lockfile's SHA: `contremaitre-deps-<lockfile>-<digest>`.
First run against a given lockfile populates the volume by running the
install command in a one-shot container; subsequent runs reuse it
verbatim. Different lockfile → different digest → fresh volume.

The pristine cache is cloned into a per-run volume (so one run's mid-run
`npm install` can't leak into the next). The clone is mounted role-aware
(`deps_mount_mode`): the agent gets it RW (to self-verify / install), the
SIM RO, and review roles not at all — deps follow execution. If the agent
genuinely needs a new dep, it edits the manifest + lockfile; the next run
sees a new digest and populates a fresh volume.

Supported ecosystems are the ones with a deterministic lockfile +
non-interactive install command. Unsupported targets get `None` —
publication continues without a deps volume (and without container
checks that depend on installed deps).

The warm step runs with open egress (it must fetch); the agent runs
under locked egress. `assert_deps_offline` closes the gap between them:
after the per-run clone, it runs the operator's check command (or an
ecosystem canary) on the SAME network the agent will face, so a missing
build backend surfaces before the agent — not as a mid-run wall that
reads like agent damage.
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
        super().__init__(f"deps install for {lockfile} failed (rc={returncode}); see {log_path}")
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

    `canary_cmd` is a minimal "the project actually runs offline" smoke
    the host fires after warming the volume, on the SAME network the
    agent will face (see `assert_deps_offline`). It exists to catch the
    warm/run parity gap: deps cached at warm time (open egress) but a
    build backend the runtime needs is missing and can't be fetched under
    the locked egress. Only set where it exercises that path for real;
    empty (`""`) means "no canary — rely on the operator's check command."
    """

    name: str
    install_cmd: str
    cache_mount_path: str
    runtime_env: tuple[tuple[str, str], ...] = ()
    canary_cmd: str = ""


_PY_VENV_ENV: tuple[tuple[str, str], ...] = (
    ("VIRTUAL_ENV", "/app/.venv"),
    (
        "PATH",
        "/app/.venv/bin:/root/.local/bin:/root/.opencode/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    ),
)

# uv's runtime env adds UV_NO_SYNC so `uv run` uses the warmed venv AS-IS
# instead of re-syncing (which, for a packaged project, rebuilds it and
# refetches the build backend `setuptools>=68` — blocked under locked
# egress, the wall a deepseek run hit at turn 5). Deps are frozen at warm
# time; a mid-run dep addition needs a fresh run (new lockhash → new
# volume), which is already the module's model.
_UV_RUNTIME_ENV: tuple[tuple[str, str], ...] = _PY_VENV_ENV + (("UV_NO_SYNC", "1"),)

# The canary that exercises the proven-broken path: `uv run` resolving the
# project against the warmed venv with no network. Passes on the fixed
# recipe; fires only if warm/run parity regresses.
_UV_CANARY = "uv run python -c ''"


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
    # Full `uv sync --frozen` (NOT `--no-install-project`): install the
    # project into the venv at warm time, where open egress can still
    # fetch the build backend. `--no-install-project` deferred that build
    # to the first runtime `uv run`, under locked egress, where it failed.
    # Surfacing a broken build here (warm, networked, clear log) beats a
    # turn-5 escalation that reads as agent damage.
    _Lockfile(
        "uv.lock",
        "uv sync --frozen",
        ".venv",
        _UV_RUNTIME_ENV,
        _UV_CANARY,
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
        _UV_RUNTIME_ENV,
        _UV_CANARY,
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


def _recipe_tag(lockfile: _Lockfile) -> str:
    """Short hash of the install command, folded into the volume name so a
    recipe change forces a rebuild.

    The lockfile-digest key alone is recipe-blind: editing `install_cmd`
    (e.g. dropping `--no-install-project`) leaves the lockfile digest
    unchanged, so the stale volume from the OLD recipe is reused and the
    fix silently no-ops — exactly what masked the uv parity fix on first
    test. Keying on the install command too means a recipe edit lands a new
    volume name → cache miss → fresh install, and the prefix-scoped prune
    sweeps the superseded volume on that same run (no manual
    `cleanup --deps` needed).

    Only `install_cmd` is hashed: it alone determines volume CONTENTS.
    `runtime_env` / `canary_cmd` are consumed at mount/probe time, not
    baked in, so editing them shouldn't trigger a needless 60-90s rebuild.
    """

    return hashlib.sha256(lockfile.install_cmd.encode()).hexdigest()[:8]


def _safe_name(lockfile_name: str) -> str:
    return lockfile_name.replace(".", "-")


# Roles that read/reason over the diff but never execute the project's code.
# Publication gating stays a deterministic gate (the agent's self-verify + the L1
# `check` sidecar): the pre-publish `review` (SIM) role never executes, so LLM
# judgement can't blur into it. `cli_review` runs POST-publish (revision advice on
# an already-drafted PR), so it may run tests to ground its findings — it gets deps.
_NON_EXECUTING_ROLES = frozenset({"review"})


def deps_mount_mode(role: str, worktree_mount_mode: str) -> str | None:
    """Deps-volume mount mode for an actor role, or None to skip the mount.

    Deps follow execution: the **agent** runs the project's tests (writable venv);
    the **cli_review** role (post-publish revision advice) may run them too, against
    a throwaway worktree copy. The **sim** reasons over the diff with a read-only
    venv. The pre-publish **review** role never touches deps — keeping it deps-free
    preserves "hard gates are deterministic, LLM judgement never gates publication."

    For executing/reasoning roles the deps mount mirrors the worktree mode
    (agent → rw, sim → ro), so it never grants more write access than the
    worktree it shadows. Single home for both `actors.py` (opencode) and
    `cli_actor.py` (codex/claude) so the policy can't drift between them.
    """

    if role in _NON_EXECUTING_ROLES:
        return None
    return worktree_mount_mode


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

    Volume naming is `contremaitre-deps-{project}-{lockfile}-{digest}-{recipe}`:
    - `project_id` (typically the cache-clone slug, e.g.
      `github.com-owner-repo`) so two projects with the same lockfile kind
      don't collide in `_prune_stale_deps_volumes` — without the scope,
      running project A then project B would evict A's cache because both
      have e.g. `package-lock.json` and the prune looks at lockfile kind
      alone. Cross-project dedup is forfeit (same content in two repos →
      two copies cached) but that's rare and the eviction was a concrete
      pain.
    - `digest` keys on lockfile content (a dep bump → fresh volume).
    - `recipe` (`_recipe_tag`) keys on the install command, so editing a
      recipe forces a rebuild instead of silently reusing a volume built
      by the old command.

    Side effects: docker volume create, docker run, and a per-lockhash
    install log at `<runs_root>/_deps_install_<lockhash>.log`.
    """

    detected = _detect(repo)
    if detected is None:
        return None
    lockfile, lock_path = detected
    digest = _digest(lock_path)
    project_slug = _safe_name(project_id)
    recipe = _recipe_tag(lockfile)
    volume = f"contremaitre-deps-{project_slug}-{_safe_name(lockfile.name)}-{digest}-{recipe}"
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
            [
                "docker",
                "volume",
                "create",
                "--label",
                "contremaitre.purpose=deps-cache",
                "--label",
                f"contremaitre.project={project_id}",
                volume,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        log_path.write_text(f"docker volume create failed:\n{exc.stderr}", encoding="utf-8")
        raise DepsInstallError(lockfile=lockfile.name, log_path=log_path, returncode=exc.returncode)

    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--label",
        "contremaitre.role=deps-install",
        # Prevent lifecycle hooks that try to write to the source repo
        # (which is mounted RO). Husky's `prepare` script calls `husky
        # install` → writes to `.git/hooks/` → EACCES on the RO mount.
        # HUSKY=0 is the canonical opt-out; CI=1 is the broader signal
        # for "don't run interactive setup hooks".
        "-e",
        "HUSKY=0",
        "-e",
        "CI=1",
    ]
    # Runtime env vars (VIRTUAL_ENV / CARGO_HOME / GOPATH) pass through
    # unchanged — install and runtime both see /app, so paths embedded
    # at install time (uv shebangs, cargo registry index) resolve later.
    for key, value in lockfile.runtime_env:
        docker_cmd.extend(["-e", f"{key}={value}"])
    docker_cmd.extend(
        [
            "-v",
            f"{repo.resolve()}:/app:rw",
            "-v",
            f"{volume}:/app/{lockfile.cache_mount_path}",
            "-w",
            "/app",
            base_image,
            "sh",
            "-lc",
            lockfile.install_cmd,
        ]
    )
    proc = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=900)
    log_path.write_text(
        f"$ {lockfile.install_cmd}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
        encoding="utf-8",
    )
    if proc.returncode != 0:
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume],
            capture_output=True,
            timeout=10,
        )
        raise DepsInstallError(
            lockfile=lockfile.name, log_path=log_path, returncode=proc.returncode
        )
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
            capture_output=True,
            text=True,
            timeout=10,
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
            capture_output=True,
            text=True,
            timeout=10,
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
        [
            "docker",
            "volume",
            "create",
            "--label",
            "contremaitre.purpose=deps-run",
            "--label",
            f"contremaitre.run-id={run_id}",
            per_run,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--label",
            f"contremaitre.run-id={run_id}",
            "--label",
            "contremaitre.role=deps-clone",
            "-v",
            f"{pristine.name}:/src:ro",
            "-v",
            f"{per_run}:/dst",
            base_image,
            "sh",
            "-lc",
            "cp -a /src/. /dst/",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return DepsVolume(
        name=per_run,
        mount_path=pristine.mount_path,
        runtime_env=pristine.runtime_env,
    )


@dataclass(frozen=True)
class OfflineAssertResult:
    """Outcome of `assert_deps_offline`.

    `source` is "check_cmd" (the operator's L1 gate, the commands joined
    with `&&`) or "canary" (an ecosystem smoke we authored). `network` is
    the docker network the probe ran on — None means open egress, where a
    failure is advisory only (the agent could still fetch what's missing).
    """

    ok: bool
    cmd: str
    source: str
    returncode: int
    output: str
    network: str | None


def _offline_assert_cmd(*, worktree: Path, check_cmds: tuple[str, ...]) -> tuple[str, str] | None:
    """Pick `(command, source)` to prove offline, or None if nothing to assert.

    The operator's check command wins — it's the real contract the L1
    sidecar runs later, so proving it offline now is the strongest signal.
    With no check command, fall back to the detected ecosystem's canary;
    ecosystems without one (Node/Rust/Go today) return None and the assert
    is skipped rather than faking confidence we can't back.
    """

    if check_cmds:
        return (" && ".join(check_cmds), "check_cmd")
    detected = _detect(worktree)
    if detected and detected[0].canary_cmd:
        return (detected[0].canary_cmd, "canary")
    return None


# Captured probe output is tailed to this many chars in the result — enough
# for the failing build/import traceback, bounded so a chatty check doesn't
# bloat the guardrail log.
_OFFLINE_ASSERT_OUTPUT_TAIL = 2000


def assert_deps_offline(
    *,
    worktree: Path,
    base_image: str,
    docker_network: str | None,
    container_user: str | None,
    deps_volume: DepsVolume | None,
    check_cmds: tuple[str, ...],
    runner=subprocess.run,
) -> OfflineAssertResult | None:
    """Run the check command (or ecosystem canary) on the agent's network.

    Mirrors the L1 sidecar mount shape (`checks._run_sidecar`): worktree
    at /app, deps volume RW at the cache path, runtime env, and crucially
    the SAME `docker_network` the agent uses — locked when the agent is
    locked, open when open. That fidelity is the point: a pass here means
    the agent's environment satisfies the command without egress, so the
    warm/run parity gap (a build backend missing under the lock) can't
    ambush the agent mid-run.

    Returns None when there's nothing to assert (no check command and the
    ecosystem has no canary). The caller owns severity — this function
    only runs the probe and reports.
    """

    selected = _offline_assert_cmd(worktree=worktree, check_cmds=check_cmds)
    if selected is None:
        return None
    cmd, source = selected

    docker_cmd = ["docker", "run", "--rm", "--label", "contremaitre.role=deps-assert"]
    if container_user:
        docker_cmd.extend(["--user", container_user])
    if docker_network:
        docker_cmd.extend(["--network", docker_network])
    docker_cmd.extend(["-v", f"{worktree}:/app:rw"])
    if deps_volume:
        # RW mirrors the L1 sidecar: a check that touches the venv (uv
        # metadata, a pytest cache dir) must not hit EACCES and read as a
        # failure when the real issue is a read-only mount.
        docker_cmd.extend(["-v", f"{deps_volume.name}:/app/{deps_volume.mount_path}:rw"])
        for key, value in deps_volume.runtime_env:
            docker_cmd.extend(["-e", f"{key}={value}"])
    # `sh -c`, not `-lc`: a login shell sources /etc/profile and resets
    # PATH, dropping the venv passed via -e. Same reasoning as
    # checks._run_sidecar.
    docker_cmd.extend(["-w", "/app", base_image, "sh", "-c", cmd])

    try:
        proc = runner(docker_cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return OfflineAssertResult(
            ok=False,
            cmd=cmd,
            source=source,
            returncode=-1,
            output=f"assert container did not complete: {exc}",
            network=docker_network,
        )
    combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return OfflineAssertResult(
        ok=proc.returncode == 0,
        cmd=cmd,
        source=source,
        returncode=proc.returncode,
        output=combined[-_OFFLINE_ASSERT_OUTPUT_TAIL:],
        network=docker_network,
    )


def _volume_exists(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "volume", "inspect", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def list_deps_volumes() -> list[str]:
    """All `contremaitre-deps-*` volumes on the host. Used by `cleanup --deps`."""

    try:
        proc = subprocess.run(
            ["docker", "volume", "ls", "-q", "--filter", "name=contremaitre-deps-"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
