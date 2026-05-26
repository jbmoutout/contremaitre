from __future__ import annotations

from pathlib import Path

from .models import ActorMode, DepsVolume, RunConfig, RunPaths
from .runtime_image import DepsInstallError, clone_deps_volume_for_run, ensure_deps_volume


class DepsProvisioner:
    def __init__(self, config: RunConfig, paths: RunPaths):
        self.config = config
        self.paths = paths

    def ensure_pristine(self, worktree: Path, project_id: str) -> DepsVolume | None:
        if self.config.actor_mode != ActorMode.OPENCODE:
            return None
        try:
            return ensure_deps_volume(
                repo=worktree,
                base_image=self.config.docker_image,
                runs_root=self.config.runs_root,
                project_id=project_id,
            )
        except DepsInstallError as exc:
            raise RuntimeError(str(exc)) from exc

    def provision_run(self, pristine: DepsVolume | None, run_id: str) -> DepsVolume | None:
        if not pristine:
            return None
        return clone_deps_volume_for_run(
            pristine=pristine,
            run_id=run_id,
            base_image=self.config.docker_image,
        )
