"""Command-line interface for Contremaitre."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

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
    p.add_argument(
        "--base",
        required=True,
        help="Branch that the worktree is sourced from + the PR target. The orchestrator fetches `origin/<base>` fresh before each run; the operator's local checkout state never affects reproducibility.",
    )
    p.add_argument(
        "--repo-cache",
        type=Path,
        default=None,
        help="Override the local clone cache path (default: ~/.cache/contremaitre/<host>-<owner>-<repo>/). Cloned lazily from --upstream (or --fork) on first use; subsequent runs reuse the cache and `git fetch origin <base>` for freshness.",
    )
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
    run_p.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Skip the pre-launch Y/n prompt. Useful for scripts / CI.",
    )
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
    cleanup_p.add_argument(
        "--repos",
        action="store_true",
        help=f"Also remove auto-managed local clone caches under {_CACHE_ROOT}. "
             "Off by default — those clones are the across-run object cache; "
             "removing forces a full re-clone on the next run.",
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
    source_url = args.upstream or args.fork
    if source_url is None:
        print(
            "contremaitre: --fork (or --upstream) is required to derive the local clone cache",
            file=sys.stderr,
        )
        return 1
    cache_path = (args.repo_cache or _default_cache_path(source_url)).resolve()
    try:
        _ensure_local_clone(cache_path=cache_path, source_url=source_url)
    except subprocess.CalledProcessError as exc:
        print(f"contremaitre: git clone failed: {exc.stderr or exc}", file=sys.stderr)
        return 1
    try:
        _resolve_models_interactive(args=args, argv_for_explicit_check=sys.argv)
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130
    if not _confirm_launch(args=args, source_url=source_url, cache_path=cache_path):
        print("aborted", file=sys.stderr)
        return 130
    config = _config_from_args(args, repo=cache_path)
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


_CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "contremaitre"
_URL_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _default_cache_path(source_url: str) -> Path:
    """`~/.cache/contremaitre/<host>-<owner>-<repo>/` derived from a clone URL.

    Handles both SSH (`git@github.com:owner/repo.git`) and HTTPS
    (`https://github.com/owner/repo.git`) URL shapes. Falls back to a
    hash-of-URL slug if parsing fails (degenerate URL — we never want to
    error here just because the parser didn't recognise a shape).
    """

    slug = _slug_from_url(source_url)
    return _CACHE_ROOT / slug


def _slug_from_url(source_url: str) -> str:
    url = source_url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    # SSH form: `git@host:owner/repo` → split on `@` and `:`.
    if url.startswith("git@") and ":" in url:
        host_part, _, path_part = url.partition(":")
        host = host_part.partition("@")[2]
        path = path_part.lstrip("/")
    else:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        host = parts.hostname or ""
        path = parts.path.lstrip("/")
    raw = f"{host}-{path}".strip("-/")
    safe = _URL_SAFE_RE.sub("-", raw).strip("-")
    return safe or "contremaitre-target"


def _ensure_local_clone(*, cache_path: Path, source_url: str) -> None:
    """Clone `source_url` into `cache_path` if not already there.

    Idempotent: if `cache_path/.git/` exists, leave it alone — the
    orchestrator's `git fetch origin <base>` (in `_create_worktree`)
    handles freshness on every run. If `cache_path` exists but is not a
    git repo, raise so the operator can choose the resolution; we never
    silently overwrite an unknown directory.
    """

    if (cache_path / ".git").exists():
        return
    if cache_path.exists():
        raise RuntimeError(
            f"cache path exists but is not a git repo: {cache_path}; "
            "remove it or pass --repo-cache to point somewhere else"
        )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"contremaitre: cloning {source_url} → {cache_path}", file=sys.stderr)
    subprocess.run(
        ["git", "clone", source_url, str(cache_path)],
        check=True, capture_output=True, text=True, timeout=600,
    )


def _confirm_launch(*, args: argparse.Namespace, source_url: str, cache_path: Path) -> bool:
    """Pre-launch summary + Y/n. Auto-Y when --yes or stdin isn't a TTY."""

    if args.yes or not sys.stdin.isatty():
        return True
    publish = args.publish_mode
    pr_target = args.gh_repo or (args.upstream or args.fork)
    cost = getattr(args, "max_cost_usd", None)
    wall = getattr(args, "max_wall_minutes", None)
    print()
    print("contremaitre will run autonomously until a draft PR is opened (or the run terminates).")
    print(f"  base branch : {args.base}")
    print(f"  source      : {source_url}")
    print(f"  cache       : {cache_path}")
    print(f"  publish to  : {pr_target} ({publish})")
    if cost is not None or wall is not None:
        print(f"  caps        : ${cost}, {wall}m wall")
    print()
    try:
        reply = input("Continue? [Y/n] ").strip().lower()
    except EOFError:
        return True
    return reply in ("", "y", "yes")


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
    cache_clones = _list_cache_clones() if args.repos else []

    if not (stale_containers or stale_worktrees or dangling_images or deps_volumes or cache_clones):
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
    for path in cache_clones:
        print(f"  clone     {path}")
    if dangling_images:
        print(f"  {len(dangling_images)} dangling image(s)")

    if dry:
        return 0

    import shutil as _shutil

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

    removed_clones = 0
    for path in cache_clones:
        try:
            _shutil.rmtree(path, ignore_errors=True)
            removed_clones += 1
        except OSError:
            pass

    if dangling_images:
        _prune_dangling_images()

    parts = [f"{removed_containers} container(s)", f"{removed_wts} worktree(s)"]
    if args.deps:
        parts.append(f"{removed_vols} deps-volume(s)")
    if args.repos:
        parts.append(f"{removed_clones} clone(s)")
    if dangling_images:
        parts.append(f"{len(dangling_images)} dangling image(s)")
    print("contremaitre cleanup: removed " + ", ".join(parts))
    return 0


def _list_cache_clones() -> list[Path]:
    """Auto-managed local clones under `_CACHE_ROOT`."""

    if not _CACHE_ROOT.is_dir():
        return []
    return sorted(p for p in _CACHE_ROOT.iterdir() if p.is_dir() and (p / ".git").exists())


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
    source_url = args.upstream or args.fork
    if source_url is None:
        print(
            "contremaitre doctor: --fork (or --upstream) is required to derive the local clone cache",
            file=sys.stderr,
        )
        return 1
    cache_path = (args.repo_cache or _default_cache_path(source_url)).resolve()
    if not (cache_path / ".git").exists():
        print(
            f"contremaitre doctor: cache not cloned yet at {cache_path}. "
            f"Run `contremaitre run --fork {args.fork or args.upstream} --base <branch>` once to populate.",
            file=sys.stderr,
        )
        return 1
    config = _config_from_args(args, repo=cache_path)
    report = run_preflight(config)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.passed else 1


def _config_from_args(args: argparse.Namespace, *, repo: Path) -> RunConfig:
    caps = Caps(
        max_turns=getattr(args, "max_turns", 30),
        max_wall_minutes=getattr(args, "max_wall_minutes", 180),
        max_cost_usd=args.max_cost_usd,
        no_progress_turns=getattr(args, "no_progress_turns", 5),
        malformed_verdict_retries=getattr(args, "malformed_verdict_retries", 2),
        max_review_rounds=getattr(args, "max_review_rounds", 3),
    )
    return RunConfig(
        repo=repo,
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


def _has_flag_in(argv: list[str], flag: str) -> bool:
    """True iff `--flag value` or `--flag=value` is present in argv."""

    prefix = f"{flag}="
    return any(item == flag or item.startswith(prefix) for item in argv)


def _fetch_free_models() -> list[dict] | None:
    """Pull OpenRouter's public model catalog, return the $0/$0 entries.

    None on network or parse failure — caller falls through to defaults
    so a picker UI never blocks a run that would otherwise have launched.
    """

    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    free: list[dict] = []
    for m in payload.get("data", []):
        pricing = m.get("pricing") or {}
        if pricing.get("prompt") == "0" and pricing.get("completion") == "0":
            free.append({
                "id": m.get("id") or "?",
                "ctx": int(m.get("context_length") or 0),
            })
    free.sort(key=lambda m: m["id"])
    return free


def _format_ctx(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n//1_000}k"
    return str(n)


def _pick_model(*, role: str, default_id: str, free_models: list[dict]) -> str:
    """Numbered-list picker. Returns the chosen id. Empty input → default."""

    print()
    print(f"contremaitre: pick a free OpenRouter model for {role}")
    print()
    default_idx = next((i for i, m in enumerate(free_models) if m["id"] == default_id), None)
    width = len(str(len(free_models) - 1))
    for i, m in enumerate(free_models):
        marker = "  ← default" if i == default_idx else ""
        print(f"  {i:>{width}}) {m['id']:<55} ctx {_format_ctx(m['ctx']):>5}{marker}")
    if default_idx is None:
        print(f"  (CLI default `{default_id}` not in free list; Enter keeps it anyway)")
    print()
    while True:
        try:
            reply = input(f"Pick [0-{len(free_models)-1}, Enter for default, q to abort]: ").strip().lower()
        except EOFError:
            return default_id
        if reply == "":
            return default_id
        if reply == "q":
            raise KeyboardInterrupt
        if reply.isdigit() and 0 <= int(reply) < len(free_models):
            return free_models[int(reply)]["id"]
        print(f"  not a valid index — try a number 0–{len(free_models)-1}, Enter, or q")


def _resolve_models_interactive(
    *,
    args: argparse.Namespace,
    argv_for_explicit_check: list[str],
    forwarded_to_subprocess: list[str] | None = None,
) -> None:
    """Mutate `args` (and optionally extend `forwarded_to_subprocess`) with picker choices.

    Skip when stdin isn't a TTY, `--yes` is set, or both model flags
    were explicitly passed. Net-fail on the model catalog → warn + skip.
    Two prompts: agent first, then SIM (SIM default = chosen agent so
    Enter twice is the common "same model for both" path).
    """

    if not sys.stdin.isatty() or getattr(args, "yes", False):
        return
    agent_explicit = _has_flag_in(argv_for_explicit_check, "--agent-model")
    sim_explicit = _has_flag_in(argv_for_explicit_check, "--sim-model")
    if agent_explicit and sim_explicit:
        return
    free = _fetch_free_models()
    if not free:
        print("contremaitre: skipping model picker (couldn't fetch OpenRouter catalog)", file=sys.stderr)
        return
    chosen_agent = args.agent_model
    if not agent_explicit:
        chosen_agent = _pick_model(role="agent", default_id=args.agent_model, free_models=free)
        args.agent_model = chosen_agent
        if forwarded_to_subprocess is not None:
            forwarded_to_subprocess.extend(["--agent-model", chosen_agent])
    if not sim_explicit:
        chosen_sim = _pick_model(role="SIM", default_id=chosen_agent, free_models=free)
        args.sim_model = chosen_sim
        if forwarded_to_subprocess is not None:
            forwarded_to_subprocess.extend(["--sim-model", chosen_sim])


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
    # Confirmation has to happen BEFORE the subprocess spawn, because once
    # Textual attaches, stdin is owned by the TUI and an `input()` in the
    # subprocess would block invisibly. Pass --yes downstream so the
    # subprocess doesn't re-prompt.
    fork = _extract_flag_value(forwarded, "--fork", "")
    upstream = _extract_flag_value(forwarded, "--upstream", "")
    base = _extract_flag_value(forwarded, "--base", "")
    source_url = upstream or fork
    if not source_url:
        print("contremaitre tui run: --fork (or --upstream) is required", file=sys.stderr)
        return 1
    if not base:
        print("contremaitre tui run: --base is required", file=sys.stderr)
        return 1
    repo_cache_raw = _extract_flag_value(forwarded, "--repo-cache", "")
    cache_path = Path(repo_cache_raw).resolve() if repo_cache_raw else _default_cache_path(source_url)
    try:
        _ensure_local_clone(cache_path=cache_path, source_url=source_url)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"contremaitre: {exc}", file=sys.stderr)
        return 1
    confirm_args = argparse.Namespace(
        yes=("--yes" in forwarded or "-y" in forwarded),
        base=base, fork=fork or None, upstream=upstream or None,
        gh_repo=_extract_flag_value(forwarded, "--gh-repo", "") or None,
        publish_mode=_extract_flag_value(forwarded, "--publish-mode", PublishMode.STUB.value),
        max_cost_usd=_extract_flag_value(forwarded, "--max-cost-usd", "?"),
        max_wall_minutes=_extract_flag_value(forwarded, "--max-wall-minutes", "?"),
        agent_model=agent_model,
        sim_model=sim_model,
    )
    try:
        _resolve_models_interactive(
            args=confirm_args,
            argv_for_explicit_check=forwarded,
            forwarded_to_subprocess=forwarded,
        )
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130
    # Picker may have appended --agent-model / --sim-model to `forwarded`;
    # refresh the locals so the TUI header displays the chosen models.
    agent_model = _extract_flag_value(forwarded, "--agent-model", agent_model)
    sim_model = _extract_flag_value(forwarded, "--sim-model", sim_model)
    if not _confirm_launch(args=confirm_args, source_url=source_url, cache_path=cache_path):
        print("aborted", file=sys.stderr)
        return 130
    if "--yes" not in forwarded and "-y" not in forwarded:
        forwarded.append("--yes")
    if "--repo-cache" not in " ".join(forwarded):
        forwarded.extend(["--repo-cache", str(cache_path)])
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
