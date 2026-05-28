"""Provenance manifest for a run.

Records *what version of the system* a run was executed against — git SHAs,
prompt-file hashes, the skills-lock hash, the docker image digest, and tool
versions. Model IDs and image *name* alone are insufficient to attribute
drift between two runs: a prompt edit, a skill-lock bump, or an image rebuild
under the same tag all change behavior invisibly.

Written once, at the start of every run, to `<run_dir>/run_config.json`. The
canary reads it to decide whether two runs are comparable, and to refuse to
promote a baseline captured against a dirty working tree.

All host-side reads tolerate missing tools (no `git`, no `docker`) and
missing files (uninstalled package, missing skills-lock) — every helper
returns `None` on failure rather than raising. The manifest is best-effort
forensics, not a hard gate.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from .models import RunConfig


DOCKERFILE_HASH_LABEL = "contremaitre.dockerfile-sha256"

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parent
_PROMPTS_DIR = _PACKAGE_DIR / "prompts"
_SKILLS_LOCK = _PROJECT_ROOT / "skills-lock.json"


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git_sha(repo: Path, ref: str = "HEAD") -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", ref],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _git_dirty(repo: Path) -> bool | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def _docker_image_id(image: str) -> str | None:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _docker_image_label(image: str, label: str) -> str | None:
    fmt = '{{ index .Config.Labels "' + label + '" }}'
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", fmt, image],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    if not value or value == "<no value>":
        return None
    return value


def _prompt_hashes() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_PROMPTS_DIR.glob("*.md"))
    }


def _contremaitre_version() -> str | None:
    try:
        from importlib import metadata

        return metadata.version("contremaitre")
    except Exception:
        return None


def _base_sha(target_repo: Path, base_ref: str) -> str | None:
    """Resolve `base_ref` to a SHA on the target repo.

    Try the local ref first (`main`), then `origin/main` — covers both
    fixture repos (no remote) and live targets where the local branch may
    be behind origin. Returns None if neither resolves.
    """

    sha = _git_sha(target_repo, base_ref)
    if sha is not None:
        return sha
    return _git_sha(target_repo, f"origin/{base_ref}")


def build_manifest(config: RunConfig) -> dict[str, Any]:
    """Build the run-config dict written at run start.

    Keeps the original model/image/base fields stable so existing readers
    (`tui.py`, `viewer/index.py`) keep working — the new provenance fields
    are additive.
    """

    return {
        # Models / image / base — existing fields, kept stable.
        "agent_model": config.agent_model,
        "sim_model": config.sim_model,
        "extra_reviewer_model": config.extra_reviewer_model,
        "cli_reviewer": config.cli_reviewer,
        "docker_image": config.docker_image,
        "target_url": config.upstream or config.fork or str(config.repo),
        "base": config.base,
        # Provenance — for downstream evaluators / canary baselines.
        "contremaitre_git_sha": _git_sha(_PROJECT_ROOT),
        "contremaitre_git_dirty": _git_dirty(_PROJECT_ROOT),
        "base_sha": _base_sha(config.repo, config.base),
        "docker_image_digest": _docker_image_id(config.docker_image),
        "dockerfile_sha256": _docker_image_label(config.docker_image, DOCKERFILE_HASH_LABEL),
        "skills_lock_sha256": _sha256_file(_SKILLS_LOCK),
        "prompt_hashes": _prompt_hashes(),
        "python_version": sys.version.split()[0],
        "contremaitre_version": _contremaitre_version(),
    }


def manifest_digest(manifest: dict[str, Any]) -> str:
    """Hash of the fields that define "the system under test".

    Used by the canary to decide whether two runs are comparable: same
    digest → same code/prompts/skills/image → results can be aggregated
    into one cell. Different digest → different system → fresh baseline.

    Deliberately excludes `agent_model` / `sim_model` (those are *parameters*
    of the eval, not the system under test) and target-side fields like
    `base_sha` / `target_url` (those change per case, not per system).
    """

    parts = [
        manifest.get("contremaitre_git_sha") or "",
        manifest.get("contremaitre_git_dirty"),
        manifest.get("dockerfile_sha256") or "",
        manifest.get("skills_lock_sha256") or "",
    ]
    for name, digest in sorted((manifest.get("prompt_hashes") or {}).items()):
        parts.append(f"{name}:{digest}")
    blob = "|".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
