"""Command-line interface for Contremaitre."""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .envfile import load_dotenv_defaults
from .fixture import init_fixture
from .models import ActorMode, Caps, PublishMode, RunConfig
from .orchestrator import run
from .paths import slugify
from .preflight import run_preflight
from .runtime_image import DepsInstallError, ensure_deps_volume, list_deps_volumes
from .viewer import VIEWER_FILENAME, build_viewer


_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_DOCKERFILE = _PACKAGE_DIR / "Dockerfile"
_DEFAULT_IMAGE = "contremaitre-agent:latest"


def _synthesize_opencode_config(*, agent_model: str, openrouter_env_var: str) -> Path:
    """Write a minimal opencode.json derived from CLI args, return its path.

    No static file shipped with the package — the only knobs are the model
    string and the env-var holding the OpenRouter key, both already on the
    CLI. The tempfile lives for the OS's tempdir lifetime (tiny JSON; not
    worth atexit-cleaning).
    """

    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": agent_model,
        "provider": {
            "openrouter": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "OpenRouter",
                "options": {
                    "baseURL": "https://openrouter.ai/api/v1",
                    "apiKey": "{env:" + openrouter_env_var + "}",
                },
            },
        },
    }
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="contremaitre-opencode-", delete=False, encoding="utf-8"
    )
    json.dump(config, tmp, indent=2)
    tmp.close()
    return Path(tmp.name)


def _shared_run_doctor_parser() -> argparse.ArgumentParser:
    """Flags common to `run` and `doctor`. Single source of truth; attach via parents=[…]."""

    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--repo", required=True, type=Path, help="Local source checkout used for git worktree add")
    p.add_argument("--base", required=True, help="Base branch for worktree and diff. The orchestrator fetches `origin/<base>` from the source repo and branches the worktree from it, ignoring local refs.")
    p.add_argument("--runs-root", type=Path, default=Path(".contremaitre/runs"))
    p.add_argument("--docker-image", default=_DEFAULT_IMAGE)
    p.add_argument(
        "--opencode-config",
        type=Path,
        default=None,
        help="Path to opencode.json. If omitted, a minimal config is "
             "synthesized from --agent-model and --openrouter-env-var.",
    )
    p.add_argument("--openrouter-env-var", default="OPENROUTER_API_KEY")
    p.add_argument("--docker-network", default=None, help="Optional docker --network value")
    p.add_argument("--http-proxy", default=None, help="Optional HTTP_PROXY value passed by env name to containers")
    p.add_argument("--https-proxy", default=None, help="Optional HTTPS_PROXY value passed by env name to containers")
    p.add_argument("--no-proxy", default=None, help="Optional NO_PROXY value passed by env name to containers")
    p.add_argument("--allow-open-egress", action="store_true", help="Allow opencode containers without explicit network/proxy policy")
    p.add_argument("--skip-openrouter-key-check", action="store_true", help="Do not query OpenRouter key metadata")
    p.add_argument("--allow-unlimited-openrouter-key", action="store_true", help="Allow OpenRouter keys with no provider-side credit limit")
    p.add_argument("--openrouter-key-url", default="https://openrouter.ai/api/v1/key")
    p.add_argument("--max-cost-usd", type=float, default=30.0)
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contremaitre",
        description="Deterministic control plane for architecture-agent PR runs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    shared = _shared_run_doctor_parser()

    run_p = sub.add_parser("run", parents=[shared], help="Run the WORK + REVIEW loop")
    run_p.add_argument("--fork", default=None, help="Push remote for the run branch. Required for --publish-mode gh.")
    run_p.add_argument("--upstream", default=None, help="Canonical (read-only) remote, mounted as `upstream`.")
    run_p.add_argument("--branch-prefix", default="refactor")
    run_p.add_argument(
        "--agent-model",
        default="openrouter/deepseek/deepseek-v4-flash",
        help="OpenRouter model string for the agent (ignored in --actor fake)",
    )
    run_p.add_argument(
        "--sim-model",
        default="openrouter/deepseek/deepseek-v4-flash",
        help="OpenRouter model string for the SIM (ignored in --actor fake)",
    )
    run_p.add_argument("--actor", choices=[mode.value for mode in ActorMode], default=ActorMode.FAKE.value)
    run_p.add_argument("--run-slug", default="run")
    run_p.add_argument("--check-cmd", action="append", default=[], help="Executable check command; repeatable")
    run_p.add_argument(
        "--sim-scenario",
        choices=["approved", "changes_requested", "needs_human", "malformed", "malformed_then_approved"],
        default="approved",
        help="Fake SIM behavior (ignored in --actor opencode)",
    )
    run_p.add_argument(
        "--agent-scenario",
        choices=["normal", "forbidden_path", "no_impl_complete"],
        default="normal",
        help="Fake agent behavior (ignored in --actor opencode)",
    )
    run_p.add_argument("--publish-mode", choices=[mode.value for mode in PublishMode], default=PublishMode.STUB.value)
    run_p.add_argument("--keep-worktree", action="store_true")
    run_p.add_argument("--simulate-drift-after-approval", action="store_true")
    run_p.add_argument("--container-user", default=None, help="Optional docker --user value, e.g. $(id -u):$(id -g)")
    run_p.add_argument("--skip-preflight", action="store_true", help="Bypass operational preflight checks")
    run_p.add_argument("--agent-timeout-seconds", type=int, default=1800)
    run_p.add_argument("--sim-timeout-seconds", type=int, default=900)
    run_p.add_argument("--gh-repo", default=None, help="Optional owner/repo for gh pr create --repo")
    run_p.add_argument("--pr-title", default=None)
    run_p.add_argument("--pr-body", default=None)
    run_p.add_argument("--max-turns", type=int, default=30)
    run_p.add_argument("--max-wall-minutes", type=int, default=180)
    run_p.add_argument("--no-progress-turns", type=int, default=5)
    run_p.add_argument("--malformed-verdict-retries", type=int, default=2)
    run_p.add_argument("--max-review-rounds", type=int, default=3)
    run_p.set_defaults(func=_run_cmd)

    doctor_p = sub.add_parser("doctor", parents=[shared], help="Validate live-run operational prerequisites")
    doctor_p.add_argument("--actor", choices=[mode.value for mode in ActorMode], default=ActorMode.OPENCODE.value)
    doctor_p.add_argument("--run-slug", default="doctor")
    doctor_p.set_defaults(func=_doctor_cmd)

    fixture_p = sub.add_parser("fixture", help="Fixture helpers for fake-mode smoke runs")
    fixture_sub = fixture_p.add_subparsers(dest="fixture_command", required=True)
    fixture_init = fixture_sub.add_parser("init", help="Create a tiny local git repo")
    fixture_init.add_argument("path", type=Path)
    fixture_init.add_argument("--overwrite", action="store_true")
    fixture_init.set_defaults(func=_fixture_init_cmd)

    image_p = sub.add_parser("image", help="Manage the opencode runtime image")
    image_sub = image_p.add_subparsers(dest="image_command", required=True)
    image_build = image_sub.add_parser("build", help="Build the runtime docker image from the package's Dockerfile")
    image_build.add_argument("--image-name", default=_DEFAULT_IMAGE, help="Tag for the built image")
    image_build.add_argument(
        "--dockerfile",
        type=Path,
        default=None,
        help=f"Override Dockerfile path (default: {_DEFAULT_DOCKERFILE})",
    )
    image_build.add_argument("--no-cache", action="store_true")
    image_build.set_defaults(func=_image_build_cmd)

    cleanup_p = sub.add_parser("cleanup", help="Prune stale containers + worktrees + dangling images")
    cleanup_p.add_argument("--runs-root", type=Path, default=Path(".contremaitre/runs"))
    cleanup_p.add_argument("--dry-run", action="store_true", help="Report what would be removed without touching anything")
    cleanup_p.add_argument("--skip-images", action="store_true", help="Skip docker image prune (containers + worktrees only)")
    cleanup_p.add_argument(
        "--deps",
        action="store_true",
        help="Also remove cached lockhash-keyed deps volumes (contremaitre-deps-*). "
             "Off by default — those volumes are the across-run dependency cache.",
    )
    cleanup_p.set_defaults(func=_cleanup_cmd)

    tui_p = sub.add_parser("tui", help="Live Textual TUI (requires `textual`)")
    tui_sub = tui_p.add_subparsers(dest="tui_command", required=True)
    tui_run = tui_sub.add_parser("run", help="Spawn `contremaitre run` and attach the TUI to its run dir")
    tui_run.add_argument(
        "run_args",
        nargs=argparse.REMAINDER,
        help="Flags forwarded to `contremaitre run` (e.g. --actor opencode --repo /path …)",
    )
    tui_run.add_argument("--refresh-hz", type=float, default=5.0)
    tui_run.add_argument("--discover-timeout", type=float, default=30.0,
                         help="Seconds to wait for the spawned run to create its dir")
    tui_run.set_defaults(func=_tui_run_cmd)
    tui_attach = tui_sub.add_parser("attach", help="Read-only attach to an existing run directory")
    tui_attach.add_argument("run_dir", type=Path)
    tui_attach.add_argument("--refresh-hz", type=float, default=5.0)
    tui_attach.set_defaults(func=_tui_attach_cmd)

    viewer_p = sub.add_parser(
        "viewer",
        help=f"Rebuild {VIEWER_FILENAME} for an existing run directory",
    )
    viewer_p.add_argument(
        "run_dir",
        type=Path,
        help="Path to a run directory under .contremaitre/runs/",
    )
    viewer_p.add_argument(
        "--open",
        action="store_true",
        help="Open the rebuilt viewer in the default browser",
    )
    viewer_p.set_defaults(func=_viewer_cmd)

    return parser


def _run_cmd(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    rc = _ensure_default_image_built(config)
    if rc != 0:
        return rc
    if config.actor_mode == ActorMode.OPENCODE:
        try:
            volume = ensure_deps_volume(
                repo=config.repo,
                base_image=config.docker_image,
                runs_root=config.runs_root,
            )
        except DepsInstallError as exc:
            # Hard fail: continuing without deps makes L1 checks look like
            # real failures (the `tsc@2.0.4` placeholder trap) when the
            # real issue is a postinstall script. See log for the actual error.
            print(f"contremaitre: {exc}", file=sys.stderr)
            return 1
        if volume:
            config = dataclasses.replace(config, deps_volume=volume)
    result = run(config)
    print(f"{result.verdict.value}: {result.reason}")
    print(f"run_dir={result.run_dir}")
    if result.pr_created:
        return 0
    return 2 if result.verdict.value.startswith("NO_PR") else 1


def _ensure_default_image_built(config: RunConfig) -> int:
    """Auto-build the default image before opencode-mode runs if it's missing.

    Only fires for `--actor opencode` AND `--docker-image contremaitre-agent:latest`
    (the default). Custom images are the operator's responsibility — preflight
    will surface a clean failure with the build hint.
    """

    if config.actor_mode != ActorMode.OPENCODE:
        return 0
    if config.docker_image != _DEFAULT_IMAGE:
        return 0
    try:
        inspect = subprocess.run(
            ["docker", "image", "inspect", config.docker_image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Docker daemon not reachable — let preflight surface a clean message.
        return 0
    if inspect.returncode == 0:
        return 0
    print(
        f"contremaitre: default image {config.docker_image} not found — building inline",
        file=sys.stderr,
    )
    return _build_image_inline(image_name=config.docker_image, dockerfile=_DEFAULT_DOCKERFILE, no_cache=False)


_WORKTREE_NAME_RE = re.compile(r"^contremaitre-(\d{8}-\d{6}-[A-Za-z0-9._-]+)$")


def _cleanup_cmd(args: argparse.Namespace) -> int:
    """Prune stale containers + worktrees + dangling images (+ deps volumes).

    A container/worktree is "stale" when its corresponding run-dir under
    `--runs-root` no longer exists — i.e. the orchestrator's `finally`
    didn't get to clean it up (typically because the parent was
    SIGKILL'd or the host rebooted mid-run).

    Containers are identified by their `contremaitre.run-id=<id>` label,
    set at launch on every contremaitre-managed docker run. Worktrees
    are identified by their `/tmp/contremaitre-<run-id>/` path.

    Lockhash-keyed deps volumes (`contremaitre-deps-*`) are the
    across-run dependency cache and are kept by default. Pass `--deps`
    to nuke them too (forces re-install on the next run).
    """

    runs_root = args.runs_root.resolve()
    dry = args.dry_run

    stale_containers = _scan_stale_containers(runs_root)
    stale_worktrees = _scan_stale_worktrees(runs_root)
    dangling_images = [] if args.skip_images else _scan_dangling_images()
    deps_volumes = list_deps_volumes() if args.deps else []

    if not stale_containers and not stale_worktrees and not dangling_images and not deps_volumes:
        print("contremaitre cleanup: nothing to do")
        return 0

    action = "would remove (dry-run)" if dry else "removing"
    print(f"contremaitre cleanup: {action}")
    for cid, run_id in stale_containers:
        print(f"  container {cid}  (run-id {run_id})")
    for path in stale_worktrees:
        print(f"  worktree  {path}")
    for name in deps_volumes:
        print(f"  deps-vol  {name}")
    if dangling_images:
        print(f"  {len(dangling_images)} dangling image(s)")

    if dry:
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
            import shutil as _shutil
            _shutil.rmtree(path, ignore_errors=True)
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

    if dangling_images:
        _prune_dangling_images()

    parts = [f"{removed_containers} container(s)", f"{removed_wts} worktree(s)"]
    if args.deps:
        parts.append(f"{removed_vols} deps-volume(s)")
    if dangling_images:
        parts.append(f"{len(dangling_images)} dangling image(s)")
    print("contremaitre cleanup: removed " + ", ".join(parts))
    return 0


def _scan_stale_containers(runs_root: Path) -> list[tuple[str, str]]:
    """Containers labeled `contremaitre.run-id=<id>` whose run-dir is gone.

    Returns [(container_id, run_id), …]. Includes stopped/exited containers
    so left-behind `--rm` containers that the daemon didn't auto-remove
    (rare, but happens on daemon crash) get cleaned too.
    """

    try:
        proc = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "label=contremaitre.run-id",
             "--format", "{{.ID}}\t{{.Label \"contremaitre.run-id\"}}"],
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


def _scan_stale_worktrees(runs_root: Path) -> list[Path]:
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


def _scan_dangling_images() -> list[str]:
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


def _build_image_inline(*, image_name: str, dockerfile: Path, no_cache: bool) -> int:
    dockerfile = dockerfile.resolve()
    if not dockerfile.exists():
        print(f"contremaitre: Dockerfile not found: {dockerfile}", file=sys.stderr)
        return 1
    contents = dockerfile.read_text(encoding="utf-8")
    cmd = ["docker", "build", "-t", image_name]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append("-")
    print(f"contremaitre: building {image_name} from {dockerfile}", file=sys.stderr)
    try:
        proc = subprocess.run(cmd, input=contents.encode("utf-8"), check=False)
    except FileNotFoundError:
        print("contremaitre: docker binary not found in PATH", file=sys.stderr)
        return 1
    if proc.returncode == 0:
        _prune_dangling_images()
    return proc.returncode


def _prune_dangling_images() -> None:
    """Remove dangling images. Rebuilds with the same tag orphan the prior
    image as <none>:<none>; we don't want those accumulating across rebuilds.
    """

    try:
        subprocess.run(
            ["docker", "image", "prune", "-f"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _doctor_cmd(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    report = run_preflight(config)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.passed else 1


def _config_from_args(args: argparse.Namespace) -> RunConfig:
    caps = Caps(
        max_turns=getattr(args, "max_turns", 30),
        max_wall_minutes=getattr(args, "max_wall_minutes", 180),
        max_cost_usd=args.max_cost_usd,
        no_progress_turns=getattr(args, "no_progress_turns", 5),
        malformed_verdict_retries=getattr(args, "malformed_verdict_retries", 2),
        max_review_rounds=getattr(args, "max_review_rounds", 3),
    )
    return RunConfig(
        repo=args.repo.resolve(),
        base=getattr(args, "base", "main"),
        runs_root=args.runs_root.resolve(),
        run_slug=slugify(args.run_slug),
        branch_prefix=getattr(args, "branch_prefix", "refactor"),
        fork=getattr(args, "fork", None),
        upstream=getattr(args, "upstream", None),
        agent_model=getattr(args, "agent_model", "openrouter/deepseek/deepseek-v4-flash"),
        sim_model=getattr(args, "sim_model", "openrouter/deepseek/deepseek-v4-flash"),
        actor_mode=ActorMode(args.actor),
        check_cmds=tuple(getattr(args, "check_cmd", [])),
        sim_scenario=getattr(args, "sim_scenario", "approved"),
        agent_scenario=getattr(args, "agent_scenario", "normal"),
        publish_mode=PublishMode(getattr(args, "publish_mode", PublishMode.STUB.value)),
        keep_worktree=getattr(args, "keep_worktree", False),
        simulate_drift_after_approval=getattr(args, "simulate_drift_after_approval", False),
        docker_image=args.docker_image,
        opencode_config=(
            args.opencode_config.resolve()
            if args.opencode_config
            else _synthesize_opencode_config(
                agent_model=getattr(args, "agent_model", "openrouter/deepseek/deepseek-v4-flash"),
                openrouter_env_var=args.openrouter_env_var,
            )
        ),
        openrouter_env_var=args.openrouter_env_var,
        container_user=getattr(args, "container_user", None),
        docker_network=args.docker_network,
        http_proxy=args.http_proxy,
        https_proxy=args.https_proxy,
        no_proxy=args.no_proxy,
        skip_preflight=getattr(args, "skip_preflight", False),
        allow_open_egress=args.allow_open_egress,
        skip_openrouter_key_check=args.skip_openrouter_key_check,
        allow_unlimited_openrouter_key=args.allow_unlimited_openrouter_key,
        openrouter_key_url=args.openrouter_key_url,
        agent_timeout_seconds=getattr(args, "agent_timeout_seconds", 1800),
        sim_timeout_seconds=getattr(args, "sim_timeout_seconds", 900),
        gh_repo=getattr(args, "gh_repo", None),
        pr_title=getattr(args, "pr_title", None),
        pr_body=getattr(args, "pr_body", None),
        caps=caps,
    )


def _fixture_init_cmd(args: argparse.Namespace) -> int:
    path = init_fixture(args.path.resolve(), overwrite=args.overwrite)
    print(path)
    return 0


def _extract_flag_value(args: list[str], flag: str, default: str) -> str:
    """Find a `--flag value` or `--flag=value` pair in a passthrough arg list."""

    for i, item in enumerate(args):
        if item == flag and i + 1 < len(args):
            return args[i + 1]
        prefix = f"{flag}="
        if item.startswith(prefix):
            return item[len(prefix):]
    return default


def _tui_run_cmd(args: argparse.Namespace) -> int:
    from . import tui  # imported lazily so the rest of the CLI works without textual

    forwarded = list(args.run_args or [])
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    run_slug = _extract_flag_value(forwarded, "--run-slug", "run")
    runs_root = Path(_extract_flag_value(forwarded, "--runs-root", ".contremaitre/runs"))
    agent_model = _extract_flag_value(forwarded, "--agent-model", "openrouter/deepseek/deepseek-v4-flash")
    sim_model = _extract_flag_value(forwarded, "--sim-model", "openrouter/deepseek/deepseek-v4-flash")
    docker_image = _extract_flag_value(forwarded, "--docker-image", _DEFAULT_IMAGE)
    run_cmd = [sys.executable, "-m", "contremaitre", "run", *forwarded]
    return tui.spawn_and_attach(
        runs_root=runs_root,
        run_slug=slugify(run_slug),
        run_cmd=run_cmd,
        refresh_hz=args.refresh_hz,
        discover_timeout_s=args.discover_timeout,
        agent_model=agent_model,
        sim_model=sim_model,
        docker_image=docker_image,
    )


def _tui_attach_cmd(args: argparse.Namespace) -> int:
    from . import tui  # imported lazily

    return tui.attach(args.run_dir.resolve(), refresh_hz=args.refresh_hz)


def _viewer_cmd(args: argparse.Namespace) -> int:
    """Rebuild viewer.html for an existing run directory.

    The orchestrator already builds the viewer at run termination; this
    command back-fills runs created before the viewer existed, or
    refreshes a viewer after manually editing artifacts in a run dir.
    """

    run_dir: Path = args.run_dir.resolve()
    if not run_dir.is_dir():
        print(f"contremaitre viewer: not a directory: {run_dir}", file=sys.stderr)
        return 1

    runs_root = run_dir.parent
    from .paths import build_run_paths

    paths = build_run_paths(runs_root, run_dir.name)
    if not paths.stats.exists():
        print(
            f"contremaitre viewer: {paths.stats} is missing — run never reached a terminal state",
            file=sys.stderr,
        )
        return 1

    out = build_viewer(paths)
    print(f"wrote {out}")

    if args.open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, str(out)], check=False)
    return 0


def _image_build_cmd(args: argparse.Namespace) -> int:
    return _build_image_inline(
        image_name=args.image_name,
        dockerfile=args.dockerfile or _DEFAULT_DOCKERFILE,
        no_cache=args.no_cache,
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv_defaults()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"contremaitre: {exc}", file=sys.stderr)
        return 1
