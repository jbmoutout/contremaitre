"""Docker CLI seam — one module owns every `docker` subprocess call.

Every module that previously ran `subprocess.run(["docker", …])` now calls
through `DockerClient`. Tests use `FakeDockerClient` (the second adapter
that makes the seam real).

Exception contract: every method returns `DockerResult(returncode, stdout, stderr)`.
Never raises for docker errors. Callers decide what's an error.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO


@dataclass(frozen=True)
class RunSpec:
    """Structured parameters for `docker run`."""

    image: str
    cmd: tuple[str, ...]
    volumes: tuple[tuple[str, str, str], ...] = ()
    env: dict[str, str] | None = None
    labels: tuple[tuple[str, str], ...] = ()
    network: str | None = None
    user: str | None = None
    workdir: str = "/app"


@dataclass(frozen=True)
class DockerResult:
    returncode: int
    stdout: str
    stderr: str


class ContainerHandle:
    """Handle to a detached container started via ``DockerClient.run(detach=True)``."""

    def __init__(self, cid: str, docker: DockerClient):
        self._cid = cid
        self._docker = docker

    @property
    def cid(self) -> str:
        return self._cid

    def start_logs(self, stdout_fd: IO[bytes]) -> subprocess.Popen[bytes]:
        """Start ``docker logs -f`` streaming into *stdout_fd*.

        Returns the ``Popen`` handle so the caller can ``wait()`` / ``kill()``.
        """
        return subprocess.Popen(
            ["docker", "logs", "-f", self._cid],
            stdout=stdout_fd,
            stderr=subprocess.PIPE,
        )

    def wait(self, *, timeout: int | None = None) -> int:
        """``docker wait`` — returns exit code."""
        result = self._docker._run(
            ["docker", "wait", self._cid], timeout=timeout or 600
        )
        try:
            return int(result.stdout.strip() or "1")
        except (ValueError, TypeError):
            return 1

    def stop(self, *, timeout: int = 5) -> DockerResult:
        """``docker stop -t <timeout>``."""
        return self._docker._run(
            ["docker", "stop", "-t", str(timeout), self._cid], timeout=15
        )

    def remove(self, *, force: bool = False) -> DockerResult:
        """``docker rm [-f]``."""
        cmd = ["docker", "rm"]
        if force:
            cmd.append("-f")
        cmd.append(self._cid)
        return self._docker._run(cmd, timeout=15)


class DockerClient:
    """One module that owns every docker CLI subprocess call.

    Usage::

        dc = DockerClient()
        result = dc.run(RunSpec(image="python:3", cmd=("python", "-c", "print(1)")))
        if result.returncode != 0:
            ...
    """

    def __init__(self, log_path: Path | None = None):
        self._log_path = log_path

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    def _run(
        self,
        cmd: list[str],
        *,
        timeout: int = 30,
        input: bytes | None = None,
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> DockerResult:
        """Run an arbitrary docker CLI command.

        *check* is passed through to ``subprocess.run`` — use it only when
        the caller genuinely wants ``CalledProcessError`` on non-zero exit.
        *env* overrides specific env vars for the subprocess; the default
        is ``os.environ.copy()``.
        """
        proc_env = os.environ.copy() if env is None else {**os.environ, **env}
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input,
                check=check,
                env=proc_env,
            )
        except FileNotFoundError:
            return DockerResult(returncode=127, stdout="", stderr="docker binary not found")
        except subprocess.TimeoutExpired as exc:
            return DockerResult(
                returncode=-1,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "timeout",
            )
        return DockerResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    # ------------------------------------------------------------------
    # docker run
    # ------------------------------------------------------------------

    def run(
        self, spec: RunSpec, *, detach: bool = False,
        subprocess_env: dict[str, str] | None = None,
    ) -> DockerResult | ContainerHandle:
        """``docker run`` — returns ``DockerResult`` or ``ContainerHandle`` (detach).

        *subprocess_env* is passed as the environment for the ``docker`` CLI
        subprocess. Use it when the docker CLI needs specific env vars
        (e.g. proxy config, API keys as pass-through).
        """
        cmd = ["docker", "run"]
        if detach:
            cmd.append("-d")
        else:
            cmd.append("--rm")
        for key, value in spec.labels:
            cmd.extend(["--label", f"{key}={value}"])
        if spec.user:
            cmd.extend(["--user", spec.user])
        if spec.network:
            cmd.extend(["--network", spec.network])
        for source, target, mode in spec.volumes:
            cmd.extend(["-v", f"{source}:{target}:{mode}"])
        if spec.env:
            for key, value in spec.env.items():
                cmd.extend(["-e", f"{key}={value}"])
        cmd.extend(["-w", spec.workdir, spec.image])
        cmd.extend(spec.cmd)

        result = self._run(cmd, timeout=900, check=False, env=subprocess_env)
        if result.returncode != 0:
            return result
        if detach:
            cid = result.stdout.strip()
            if not cid:
                return DockerResult(returncode=1, stdout="", stderr="no container id returned")
            return ContainerHandle(cid, self)
        return result

    # ------------------------------------------------------------------
    # docker build
    # ------------------------------------------------------------------

    def build(
        self,
        tag: str,
        dockerfile: Path,
        *,
        no_cache: bool = False,
        labels: dict[str, str] | None = None,
    ) -> DockerResult:
        """``docker build`` from a Dockerfile."""
        dockerfile = dockerfile.resolve()
        if not dockerfile.exists():
            return DockerResult(
                returncode=1,
                stdout="",
                stderr=f"Dockerfile not found: {dockerfile}",
            )
        contents = dockerfile.read_bytes()
        digest = hashlib.sha256(contents).hexdigest()
        cmd = [
            "docker",
            "build",
            "-t",
            tag,
            "--label",
            f"contremaitre.dockerfile-sha256={digest}",
        ]
        if no_cache:
            cmd.append("--no-cache")
        if labels:
            for key, value in labels.items():
                cmd.extend(["--label", f"{key}={value}"])
        cmd.append("-")
        return self._run(cmd, timeout=600, input=contents)

    # ------------------------------------------------------------------
    # docker volume
    # ------------------------------------------------------------------

    def volume_create(
        self, name: str, *, labels: tuple[tuple[str, str], ...] = ()
    ) -> DockerResult:
        """``docker volume create``."""
        cmd = ["docker", "volume", "create"]
        for key, value in labels:
            cmd.extend(["--label", f"{key}={value}"])
        cmd.append(name)
        return self._run(cmd, timeout=10)

    def volume_rm(self, name: str, *, force: bool = False) -> DockerResult:
        """``docker volume rm [-f]``."""
        cmd = ["docker", "volume", "rm"]
        if force:
            cmd.append("-f")
        cmd.append(name)
        return self._run(cmd, timeout=10)

    def volume_ls_q(self, *, filter: str = "") -> list[str]:
        """``docker volume ls -q`` — returns a list of volume names."""
        cmd = ["docker", "volume", "ls", "-q"]
        if filter:
            cmd.extend(["--filter", filter])
        result = self._run(cmd, timeout=10)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def volume_inspect(self, name: str) -> DockerResult:
        """``docker volume inspect``."""
        return self._run(["docker", "volume", "inspect", name], timeout=10)

    # ------------------------------------------------------------------
    # docker container (inspect / stop / rm)
    # ------------------------------------------------------------------

    def container_inspect(self, cid: str, *, fmt: str = "") -> DockerResult:
        """``docker inspect`` for a container."""
        cmd = ["docker", "inspect", cid]
        if fmt:
            cmd.extend(["--format", fmt])
        return self._run(cmd, timeout=10)

    def container_stop(self, cid: str, *, timeout: int = 5) -> DockerResult:
        """``docker stop -t <timeout>``."""
        return self._run(
            ["docker", "stop", "-t", str(timeout), cid], timeout=15
        )

    def container_rm(self, cid: str, *, force: bool = False) -> DockerResult:
        """``docker rm [-f]``."""
        cmd = ["docker", "rm"]
        if force:
            cmd.append("-f")
        cmd.append(cid)
        return self._run(cmd, timeout=15)

    # ------------------------------------------------------------------
    # docker ps
    # ------------------------------------------------------------------

    def ps(self, *, filter: str = "", format: str = "",
           no_trunc: bool = False, all_containers: bool = False) -> DockerResult:
        """``docker ps`` with optional filter, format, --no-trunc, -a."""
        cmd = ["docker", "ps"]
        if all_containers:
            cmd.append("-a")
        if no_trunc:
            cmd.append("--no-trunc")
        if filter:
            cmd.extend(["--filter", filter])
        if format:
            cmd.extend(["--format", format])
        return self._run(cmd, timeout=10)

    # ------------------------------------------------------------------
    # docker image
    # ------------------------------------------------------------------

    def image_inspect(self, name: str, *, fmt: str = "") -> DockerResult:
        """``docker image inspect``."""
        cmd = ["docker", "image", "inspect", name]
        if fmt:
            cmd.extend(["--format", fmt])
        return self._run(cmd, timeout=10)

    def image_prune(self) -> DockerResult:
        """``docker image prune -f``."""
        return self._run(["docker", "image", "prune", "-f"], timeout=30)

    def images_q(self, *, filter: str = "") -> list[str]:
        """``docker images -q`` — returns a list of image IDs."""
        cmd = ["docker", "images", "-q"]
        if filter:
            cmd.extend(["--filter", filter])
        result = self._run(cmd, timeout=10)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    # ------------------------------------------------------------------
    # docker version
    # ------------------------------------------------------------------

    def version(self) -> DockerResult:
        """``docker version``."""
        return self._run(
            ["docker", "version", "--format", "{{.Server.Version}}"], timeout=10
        )


class FakeDockerClient:
    """Deterministic docker stand-in for tests.

    Records every call so tests can assert on what was invoked.
    Returns canned ``DockerResult`` values configured by the test.

    Usage::

        dc = FakeDockerClient()
        dc.queue("run", DockerResult(returncode=0, stdout="abc123", stderr=""))
        result = dc.run(...)

    Or use ``default_result`` for calls that aren't explicitly queued::

        dc = FakeDockerClient(default_result=DockerResult(returncode=0, stdout="", stderr=""))
    """

    def __init__(self, default_result: DockerResult | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._queued: dict[str, list[DockerResult]] = {}
        self._default = default_result or DockerResult(0, "", "")

    def queue(self, method: str, result: DockerResult) -> None:
        self._queued.setdefault(method, []).append(result)

    def _record(self, method: str, args: tuple = (), kwargs: dict | None = None) -> DockerResult:
        self.calls.append((method, args, kwargs or {}))
        queue = self._queued.get(method)
        if queue:
            return queue.pop(0)
        return self._default

    def run(self, spec: RunSpec, *, detach: bool = False,
            subprocess_env: dict[str, str] | None = None) -> DockerResult | ContainerHandle:
        kwargs = {"spec": spec, "detach": detach}
        if subprocess_env is not None:
            kwargs["subprocess_env"] = subprocess_env
        result = self._record("run", kwargs=kwargs)
        if detach and result.returncode == 0 and result.stdout.strip():
            return ContainerHandle(result.stdout.strip(), self)
        return result

    @property
    def cid(self) -> str:
        return "<fake-cid>"

    def build(self, tag: str, dockerfile: Path, *, no_cache: bool = False,
              labels: dict[str, str] | None = None) -> DockerResult:
        return self._record("build", kwargs={"tag": tag, "dockerfile": dockerfile, "no_cache": no_cache, "labels": labels})

    def volume_create(self, name: str, *, labels: tuple[tuple[str, str], ...] = ()) -> DockerResult:
        return self._record("volume_create", kwargs={"name": name, "labels": labels})

    def volume_rm(self, name: str, *, force: bool = False) -> DockerResult:
        return self._record("volume_rm", kwargs={"name": name, "force": force})

    def volume_ls_q(self, *, filter: str = "") -> list[str]:
        self._record("volume_ls_q", kwargs={"filter": filter})
        return []

    def volume_inspect(self, name: str) -> DockerResult:
        return self._record("volume_inspect", kwargs={"name": name})

    def container_inspect(self, cid: str, *, fmt: str = "") -> DockerResult:
        return self._record("container_inspect", kwargs={"cid": cid, "fmt": fmt})

    def container_stop(self, cid: str, *, timeout: int = 5) -> DockerResult:
        return self._record("container_stop", kwargs={"cid": cid, "timeout": timeout})

    def container_rm(self, cid: str, *, force: bool = False) -> DockerResult:
        return self._record("container_rm", kwargs={"cid": cid, "force": force})

    def ps(self, *, filter: str = "", format: str = "",
           no_trunc: bool = False, all_containers: bool = False) -> DockerResult:
        return self._record("ps", kwargs={"filter": filter, "format": format,
                                           "no_trunc": no_trunc, "all_containers": all_containers})

    def image_inspect(self, name: str, *, fmt: str = "") -> DockerResult:
        return self._record("image_inspect", kwargs={"name": name, "fmt": fmt})

    def image_prune(self) -> DockerResult:
        return self._record("image_prune")

    def images_q(self, *, filter: str = "") -> list[str]:
        self._record("images_q", kwargs={"filter": filter})
        return []

    def version(self) -> DockerResult:
        return self._record("version")

    def _run(self, cmd: list[str], *,
             timeout: int = 30, input: bytes | None = None,
             check: bool = False, env: dict[str, str] | None = None) -> DockerResult:
        kwargs: dict = {"timeout": timeout}
        if input is not None:
            kwargs["input"] = input
        if check:
            kwargs["check"] = check
        if env is not None:
            kwargs["env"] = env
        self.calls.append(("_run", (cmd,), kwargs))
        return self._default
