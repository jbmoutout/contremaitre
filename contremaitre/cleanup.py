"""Stale-container, worktree, and dangling-image cleanup.

The CLI dispatches to `run_cleanup(...)` with typed parameters; the module
owns scanner orchestration, docker subprocess calls, and the definition of
"stale" (run-dir no longer exists).

Container/worktree lifecycle:
  - Containers are identified by the `contremaitre.run-id=<id>` label set at
    launch on every contremaitre-managed docker run.
  - Worktrees are identified by `/tmp/contremaitre-<run-id>/` paths.
  - Both are "stale" when their corresponding run-dir under `--runs-root`
    no longer exists — i.e. the orchestrator's `finally` didn't get to clean
    them up (parent SIGKILL'd, host rebooted mid-run).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .runtime_image import list_deps_volumes


_WORKTREE_NAME_RE = re.compile(r"^contremaitre-(\d{8}-\d{6}-[A-Za-z0-9._-]+)$")

_CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "contremaitre"


def run_cleanup(
    *,
    runs_root: Path,
    dry_run: bool = False,
    skip_images: bool = False,
    deps: bool = False,
    repos: bool = False,
    cache_root: Path | None = None,
) -> int:
    """Orchestrate cleanup of stale contremaitre resources.

    Each category (containers, worktrees, images, deps volumes, cache clones)
    is scanned independently and removed in sequence. Returns 0 always
    (best-effort cleanup never fails the exit code; partial removals are
    normal).
    """

    stale_containers = scan_stale_containers(runs_root)
    stale_worktrees = scan_stale_worktrees(runs_root)
    dangling_images = [] if skip_images else scan_dangling_images()
    deps_volumes = list_deps_volumes() if deps else []
    cache_clones = _list_cache_clones(cache_root) if repos else []

    if not (stale_containers or stale_worktrees or dangling_images or deps_volumes or cache_clones):
        print("contremaitre cleanup: nothing to do")
        return 0

    action = "would remove (dry-run)" if dry_run else "removing"
    print(f"contremaitre cleanup: {action}")
    for cid, run_id in stale_containers:
        print(f"  container {cid}  (run-id {run_id})")
    for path in stale_worktrees:
        print(f"  worktree  {path}")
    for name in deps_volumes:
        print(f"  deps-vol  {name}")
    for path in cache_clones:
        print(f"  clone     {path}")
    if dangling_images:
        print(f"  {len(dangling_images)} dangling image(s)")

    if dry_run:
        return 0

    removed_containers = 0
    for cid, _ in stale_containers:
        proc = subprocess.run(
            ["docker", "rm", "-f", cid],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            removed_containers += 1

    removed_wts = 0
    for path in stale_worktrees:
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed_wts += 1
        except OSError:
            pass

    removed_vols = 0
    for name in deps_volumes:
        proc = subprocess.run(
            ["docker", "volume", "rm", "-f", name],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            removed_vols += 1

    removed_clones = 0
    for path in cache_clones:
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed_clones += 1
        except OSError:
            pass

    if dangling_images:
        prune_dangling_images()

    parts = [f"{removed_containers} container(s)", f"{removed_wts} worktree(s)"]
    if deps:
        parts.append(f"{removed_vols} deps-volume(s)")
    if repos:
        parts.append(f"{removed_clones} clone(s)")
    if dangling_images:
        parts.append(f"{len(dangling_images)} dangling image(s)")
    print("contremaitre cleanup: removed " + ", ".join(parts))
    return 0


def scan_stale_containers(runs_root: Path) -> list[tuple[str, str]]:
    """Containers labeled `contremaitre.run-id=<id>` whose run-dir is gone.

    Returns [(container_id, run_id), …]. Includes stopped/exited containers
    so left-behind `--rm` containers that the daemon didn't auto-remove
    (rare, but happens on daemon crash) get cleaned too.
    """

    try:
        proc = subprocess.run(
            [
                "docker", "ps", "-aq",
                "--filter", "label=contremaitre.run-id",
                "--format", '{{.ID}}\t{{.Label "contremaitre.run-id"}}',
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    stale: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        cid, run_id = parts[0].strip(), parts[1].strip()
        if not cid or not run_id:
            continue
        if not (runs_root / run_id).exists():
            stale.append((cid, run_id))
    return stale


def scan_stale_worktrees(runs_root: Path) -> list[Path]:
    """Return /tmp/contremaitre-* directories whose run dir is gone."""

    stale: list[Path] = []
    for tmp_root in (Path("/tmp"), Path("/private/tmp")):
        if not tmp_root.exists():
            continue
        for path in tmp_root.glob("contremaitre-*"):
            if not path.is_dir():
                continue
            match = _WORKTREE_NAME_RE.match(path.name)
            if not match:
                continue
            run_id = match.group(1)
            run_dir = runs_root / run_id
            if not run_dir.exists() and path not in stale:
                stale.append(path)
    return stale


def scan_dangling_images() -> list[str]:
    try:
        proc = subprocess.run(
            ["docker", "images", "-q", "--filter", "dangling=true"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def prune_dangling_images() -> None:
    """Remove dangling images. Rebuilds with the same tag orphan the prior
    image as <none>:<none>; we don't want those accumulating across rebuilds.
    """

    try:
        subprocess.run(
            ["docker", "image", "prune", "-f"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _list_cache_clones(cache_root: Path | None = None) -> list[Path]:
    """Auto-managed local clones under ``cache_root`` or the default cache dir."""

    root = cache_root or _CACHE_ROOT
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists())



