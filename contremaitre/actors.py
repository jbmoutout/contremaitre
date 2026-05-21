"""Actor process adapters.

The orchestrator depends on the small `ActorRunner` surface in this module.
`FakeActorRunner` uses deterministic subprocesses for tests and fixture
smoke runs. `OpencodeActorRunner` drives opencode-in-Docker for live runs.
Neither holds git, GitHub, diff-scan, or cap-enforcement responsibility —
those stay host-owned.

Protocol (multi-turn WORK session + single-shot REVIEW pass):

    agent_turn(message)        -> ActorOutput  # agent's reply, persistent session
    sim_turn(message)          -> ActorOutput  # SIM's reply, persistent session
    sim_review(...)            -> ActorOutput  # single-shot JSON verdict, fresh session

The hand-rolled multi-turn loop in the orchestrator drives these by
alternating agent_turn / sim_turn until `.contremaitre/IMPLEMENTATION_COMPLETE`
appears in the worktree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .jsonlog import append_jsonl
from .models import ActorMode, RunConfig, RunPaths


class ActorError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActorOutput:
    stdout: str
    stderr: str
    returncode: int
    raw_export_written: bool = False


class ActorRunner(Protocol):
    def agent_turn(self, message: str) -> ActorOutput: ...

    def sim_turn(self, message: str) -> ActorOutput: ...

    def sim_review(
        self,
        *,
        diff_file: Path,
        settled_file: Path,
        scenario: str,
        attempt: int,
    ) -> ActorOutput: ...


# ------------------------------- Fake --------------------------------


class FakeActorRunner:
    """Deterministic subprocess actor for fixture smoke runs.

    The fake agent writes `.contremaitre/SETTLED_DESIGN.md`, a small
    implementation, and `.contremaitre/IMPLEMENTATION_COMPLETE` on its first
    turn, so the orchestrator's WORK loop terminates immediately. The fake
    SIM emits canned strings or strict JSON verdicts based on scenario.
    """

    def __init__(self, *, worktree: Path, git_log: Path, agent_scenario: str, sim_scenario: str):
        self.worktree = worktree
        self.git_log = git_log
        self.agent_scenario = agent_scenario
        self.sim_scenario = sim_scenario

    def agent_turn(self, message: str) -> ActorOutput:
        return self._fake(
            [
                "agent",
                "--worktree",
                str(self.worktree),
                "--scenario",
                self.agent_scenario,
            ]
        )

    def sim_turn(self, message: str) -> ActorOutput:
        return self._fake(["sim-turn"])

    def sim_review(
        self,
        *,
        diff_file: Path,
        settled_file: Path,
        scenario: str,
        attempt: int,
    ) -> ActorOutput:
        return self._fake(
            [
                "sim-review",
                "--diff-file",
                str(diff_file),
                "--settled-file",
                str(settled_file),
                "--scenario",
                scenario,
                "--attempt",
                str(attempt),
            ]
        )

    def _fake(self, args: list[str]) -> ActorOutput:
        package_root = Path(__file__).resolve().parents[1]
        env = {
            **os.environ,
            "PYTHONPATH": f"{package_root}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        }
        cmd = [sys.executable, "-m", "contremaitre.fake_actor", *args]
        proc = subprocess.run(
            cmd,
            cwd=self.worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise ActorError(f"fake actor failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
        return ActorOutput(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)


# ----------------------------- Opencode ------------------------------


class OpencodeActorRunner:
    """Run agent and SIM turns through opencode inside Docker.

    The agent gets a writable `/app` mount and one persistent session across
    WORK turns. The SIM gets the same worktree as a read-only mount and one
    persistent session across WORK turns. The REVIEW pass uses a fresh SIM
    session with an additional read-only `/review` mount containing the
    settled design and the diff.

    GitHub credentials are never passed into either container.
    """

    def __init__(self, *, config: RunConfig, paths: RunPaths):
        self.config = config
        self.paths = paths
        self.worktree = paths.worktree
        self.agent_state = paths.run_dir / "opencode-agent-state"
        self.sim_state = paths.run_dir / "opencode-sim-state"
        self.review_state = paths.run_dir / "opencode-review-state"
        self.agent_state.mkdir(parents=True, exist_ok=True)
        self.sim_state.mkdir(parents=True, exist_ok=True)
        self.review_state.mkdir(parents=True, exist_ok=True)
        self._agent_session: str | None = None
        self._sim_session: str | None = None

    def agent_turn(self, message: str) -> ActorOutput:
        return self._opencode_turn(
            role="agent",
            prompt=message,
            raw_export=self.paths.raw_export,
            state_dir=self.agent_state,
            mount_mode="rw",
            model=self.config.agent_model,
            timeout_seconds=self.config.agent_timeout_seconds,
            session_attr="_agent_session",
        )

    def sim_turn(self, message: str) -> ActorOutput:
        return self._opencode_turn(
            role="sim",
            prompt=message,
            raw_export=self.paths.sim_raw_export,
            state_dir=self.sim_state,
            mount_mode="ro",
            model=self.config.sim_model,
            timeout_seconds=self.config.sim_timeout_seconds,
            session_attr="_sim_session",
        )

    def sim_review(
        self,
        *,
        diff_file: Path,
        settled_file: Path,
        scenario: str,
        attempt: int,
    ) -> ActorOutput:
        from . import prompts

        review_dir = self.paths.run_dir / "review_input"
        review_dir.mkdir(exist_ok=True)
        (review_dir / "SETTLED_DESIGN.md").write_text(
            settled_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (review_dir / "diff.patch").write_text(diff_file.read_text(encoding="utf-8"), encoding="utf-8")
        # Fresh session every review attempt so the SIM has clean context.
        attempt_state = self.review_state / f"attempt-{attempt}"
        attempt_state.mkdir(parents=True, exist_ok=True)
        return self._opencode_turn(
            role="review",
            prompt=prompts.SIM_REVIEW_PROMPT,
            raw_export=self.paths.sim_raw_export,
            state_dir=attempt_state,
            mount_mode="ro",
            model=self.config.sim_model,
            timeout_seconds=self.config.sim_timeout_seconds,
            session_attr=None,
            extra_mounts=[(review_dir, "/review", "ro")],
        )

    def _opencode_turn(
        self,
        *,
        role: str,
        prompt: str,
        raw_export: Path,
        state_dir: Path,
        mount_mode: str,
        model: str,
        timeout_seconds: int,
        session_attr: str | None,
        extra_mounts: list[tuple[Path, str, str]] | None = None,
    ) -> ActorOutput:
        pre_text_count = _count_text_events(raw_export)
        session_id = getattr(self, session_attr) if session_attr else None
        cmd, env = build_docker_command(
            config=self.config,
            paths=self.paths,
            worktree=self.worktree,
            state_dir=state_dir,
            mount_mode=mount_mode,
            model=model,
            prompt=prompt,
            session_id=session_id,
            extra_mounts=extra_mounts or [],
            cidfile=self._cidfile(role),
        )
        append_jsonl(
            self.paths.guardrail_events,
            {
                "event": "opencode_actor_start",
                "role": role,
                "mount_mode": mount_mode,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "cmd_redacted": redact_command(cmd),
            },
        )
        raw_export.parent.mkdir(parents=True, exist_ok=True)
        with raw_export.open("ab") as stdout_f:
            proc = subprocess.Popen(cmd, stdout=stdout_f, stderr=subprocess.PIPE, env=env)
            try:
                _, stderr_bytes = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _kill_container_from_cidfile(self._latest_cidfile(role))
                proc.kill()
                _, stderr_bytes = proc.communicate()
                raise ActorError(f"{role} opencode timed out after {timeout_seconds}s") from exc
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        if proc.returncode != 0:
            raise ActorError(f"{role} opencode exited {proc.returncode}: {stderr[:500]}")
        new_session_id = _latest_session_id(raw_export)
        if new_session_id and session_attr:
            setattr(self, session_attr, new_session_id)
        post_text_count = _count_text_events(raw_export)
        if post_text_count == pre_text_count:
            error = _latest_error(raw_export)
            if error:
                raise ActorError(f"{role} opencode emitted error without text: {error[:500]}")
            raise ActorError(f"{role} opencode emitted no text")
        latest = _latest_text(raw_export)
        return ActorOutput(stdout=latest, stderr=stderr, returncode=proc.returncode, raw_export_written=True)

    def _cidfile(self, role: str) -> Path:
        cidfile = self.paths.run_dir / f"opencode-{role}-{time.monotonic_ns()}.cid"
        _write_latest_cidfile_pointer(self.paths.run_dir, role, cidfile)
        return cidfile

    def _latest_cidfile(self, role: str) -> Path:
        pointer = self.paths.run_dir / f"opencode-{role}.latest-cidfile"
        if pointer.exists():
            return Path(pointer.read_text(encoding="utf-8").strip())
        return self.paths.run_dir / f"opencode-{role}.cid"


def make_actor_runner(*, config: RunConfig, paths: RunPaths) -> ActorRunner:
    if config.actor_mode == ActorMode.FAKE:
        return FakeActorRunner(
            worktree=paths.worktree,
            git_log=paths.git_log,
            agent_scenario=config.agent_scenario,
            sim_scenario=config.sim_scenario,
        )
    if config.actor_mode == ActorMode.OPENCODE:
        return OpencodeActorRunner(config=config, paths=paths)
    raise ActorError(f"unknown actor mode: {config.actor_mode}")


def build_docker_command(
    *,
    config: RunConfig,
    paths: RunPaths,
    worktree: Path,
    state_dir: Path,
    mount_mode: str,
    model: str,
    prompt: str,
    session_id: str | None,
    extra_mounts: list[tuple[Path, str, str]] | None = None,
    cidfile: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    env_var = config.openrouter_env_var
    if env_var not in env:
        raise ActorError(f"{env_var} is required for opencode actor mode")
    proxy_vars: list[str] = []
    if config.http_proxy:
        env["HTTP_PROXY"] = config.http_proxy
        proxy_vars.append("HTTP_PROXY")
    if config.https_proxy:
        env["HTTPS_PROXY"] = config.https_proxy
        proxy_vars.append("HTTPS_PROXY")
    if config.no_proxy:
        env["NO_PROXY"] = config.no_proxy
        proxy_vars.append("NO_PROXY")

    opencode_cmd = [
        "/root/.opencode/bin/opencode",
        "run",
        "--dangerously-skip-permissions",
        "--format",
        "json",
        "--model",
        model,
    ]
    if session_id:
        opencode_cmd.extend(["--session", session_id])
    opencode_cmd.append(prompt)

    cmd = ["docker", "run", "--rm"]
    if cidfile:
        cidfile.unlink(missing_ok=True)
        cmd.extend(["--cidfile", str(cidfile)])
    if config.container_user:
        cmd.extend(["--user", config.container_user])
    if config.docker_network:
        cmd.extend(["--network", config.docker_network])
    cmd.extend(
        [
            "-v",
            f"{paths.run_dir}:/results",
            "-v",
            f"{state_dir}:/root/.local/share/opencode",
            "-v",
            f"{worktree}:/app:{mount_mode}",
            "-v",
            "/app/node_modules",
        ]
    )
    if config.opencode_config:
        cmd.extend(["-v", f"{config.opencode_config}:/app/opencode.json:ro"])
    for host_path, container_path, mode in extra_mounts or []:
        cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])
    cmd.extend(["-e", env_var])
    for proxy_var in proxy_vars:
        cmd.extend(["-e", proxy_var])
    cmd.extend(["-w", "/app", config.docker_image, *opencode_cmd])
    return cmd, env


def _write_latest_cidfile_pointer(run_dir: Path, role: str, cidfile: Path) -> None:
    (run_dir / f"opencode-{role}.latest-cidfile").write_text(str(cidfile), encoding="utf-8")


def _kill_container_from_cidfile(cidfile: Path) -> None:
    if not cidfile.exists():
        return
    container_id = cidfile.read_text(encoding="utf-8").strip()
    if not container_id:
        return
    try:
        subprocess.run(["docker", "kill", container_id], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return


def redact_command(cmd: list[str]) -> list[str]:
    """Keep logs useful without exposing long prompts."""

    redacted = list(cmd)
    if redacted:
        redacted[-1] = f"<prompt {len(redacted[-1])} chars>"
    return redacted


def _read_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _count_text_events(path: Path) -> int:
    return sum(1 for event in _read_events(path) if event.get("type") == "text")


def _latest_text(path: Path) -> str:
    for event in reversed(_read_events(path)):
        if event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    return text
    return ""


def _latest_session_id(path: Path) -> str | None:
    for event in reversed(_read_events(path)):
        session = event.get("sessionID") or event.get("session_id")
        if isinstance(session, str):
            return session
    return None


def _latest_error(path: Path) -> str | None:
    for event in reversed(_read_events(path)):
        if event.get("type") == "error":
            return json.dumps(event, sort_keys=True)
    return None
