"""Command-line interface for Contremaitre."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import defaults as _defaults
from .envfile import load_dotenv_defaults
from .fixture import init_fixture
from .models import ActorMode, Caps, PublishMode, RunConfig
from .orchestrator import run
from .paths import slugify
from .preflight import run_preflight
from .runtime_image import list_deps_volumes
from .viewer import VIEWER_FILENAME, build_viewer
from .viewer.index import INDEX_FILENAME, build_index


_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_DOCKERFILE = _PACKAGE_DIR / "Dockerfile"
_DEFAULT_IMAGE = "contremaitre-agent:latest"
_RUST_IMAGE = "contremaitre-agent-rust:latest"
_GO_IMAGE = "contremaitre-agent-go:latest"
_VARIANT_DOCKERFILES: dict[str, Path] = {
    "base": _PACKAGE_DIR / "Dockerfile",
    "rust": _PACKAGE_DIR / "Dockerfile.rust",
    "go": _PACKAGE_DIR / "Dockerfile.go",
}
_DOCKERFILE_HASH_LABEL = "contremaitre.dockerfile-sha256"


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
    p.add_argument(
        "--http-proxy",
        default=None,
        help="Optional HTTP_PROXY value passed by env name to containers",
    )
    p.add_argument(
        "--https-proxy",
        default=None,
        help="Optional HTTPS_PROXY value passed by env name to containers",
    )
    p.add_argument(
        "--no-proxy", default=None, help="Optional NO_PROXY value passed by env name to containers"
    )
    p.add_argument(
        "--allow-open-egress",
        action="store_true",
        help="Allow opencode containers without explicit network/proxy policy",
    )
    p.add_argument(
        "--skip-openrouter-key-check",
        action="store_true",
        help="Do not query OpenRouter key metadata",
    )
    p.add_argument(
        "--allow-unlimited-openrouter-key",
        action="store_true",
        help="Allow OpenRouter keys with no provider-side credit limit",
    )
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
    run_p.add_argument(
        "--fork",
        default=None,
        help="Push remote for the run branch. Required for --publish-mode gh.",
    )
    run_p.add_argument(
        "--upstream", default=None, help="Canonical (read-only) remote, mounted as `upstream`."
    )
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
    run_p.add_argument(
        "--extra-reviewer-model",
        default=None,
        help=(
            "Optional second SIM model run alongside the primary every review "
            "round. Pick a different model family from --sim-model to get a "
            "cheap cross-family verdict. Both must APPROVE for the PR to "
            "publish; if either bounces, the agent gets a merged list of "
            "required changes and loops within max-review-rounds. Omit for "
            "single-SIM (back-compat)."
        ),
    )
    run_p.add_argument(
        "--cli-reviewer",
        choices=["auto", "codex", "claude", "both", "none"],
        default="auto",
        help=(
            "Optional local CLI reviewer run AFTER the Draft PR is published. "
            "Uses the operator's interactive subscription (claude/codex), not API. "
            "`auto` (default) detects what's installed and prompts when stdin is a TTY; "
            "`both` runs codex and claude sequentially (two PR comments); "
            "`none` skips. The review is posted as a single comment on the PR."
        ),
    )
    run_p.add_argument(
        "--actor", choices=[mode.value for mode in ActorMode], default=ActorMode.FAKE.value
    )
    run_p.add_argument(
        "--cli-tool",
        choices=["codex", "claude"],
        default="codex",
        help="Frontier CLI to drive as agent/SIM when --actor cli (claude pending OAuth token)",
    )
    run_p.add_argument(
        "--sim-actor",
        choices=[mode.value for mode in ActorMode],
        default=None,
        help="Override the SIM's runtime (default: same as --actor); lets a codex "
        "agent pair with an opencode SIM, or the reverse",
    )
    run_p.add_argument("--run-slug", default="run")
    run_p.add_argument(
        "--check-cmd", action="append", default=[], help="Executable check command; repeatable"
    )
    run_p.add_argument(
        "--sim-scenario",
        choices=[
            "approved",
            "changes_requested",
            "needs_human",
            "malformed",
            "malformed_then_approved",
        ],
        default="approved",
        help="Fake SIM behavior (ignored in --actor opencode)",
    )
    run_p.add_argument(
        "--extra-reviewer-scenario",
        choices=[
            "approved",
            "changes_requested",
            "needs_human",
            "malformed",
            "malformed_then_approved",
        ],
        default="approved",
        help=(
            "Fake extra-reviewer behavior (ignored in --actor opencode and "
            "when --extra-reviewer-model is unset). Lets fixture tests "
            "exercise asymmetric SIM/extra outcomes."
        ),
    )
    run_p.add_argument(
        "--agent-scenario",
        choices=["normal", "forbidden_path", "no_impl_complete"],
        default="normal",
        help="Fake agent behavior (ignored in --actor opencode)",
    )
    run_p.add_argument(
        "--publish-mode",
        choices=[mode.value for mode in PublishMode],
        default=PublishMode.STUB.value,
    )
    run_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the pre-launch Y/n prompt. Useful for scripts / CI.",
    )
    run_p.add_argument(
        "--no-prompt",
        action="store_true",
        help=(
            "Skip the model + cli-reviewer pickers entirely and use the "
            "saved defaults from `contremaitre init` (or hardcoded fallbacks "
            "when no defaults file exists). Implies --yes. The pre-flight "
            "ping and recap still run."
        ),
    )
    run_p.add_argument("--keep-worktree", action="store_true")
    run_p.add_argument("--simulate-drift-after-approval", action="store_true")
    run_p.add_argument(
        "--container-user",
        default=None,
        help="Optional docker --user value, e.g. $(id -u):$(id -g)",
    )
    run_p.add_argument(
        "--skip-preflight", action="store_true", help="Bypass operational preflight checks"
    )
    run_p.add_argument("--agent-timeout-seconds", type=int, default=1800)
    run_p.add_argument("--sim-timeout-seconds", type=int, default=1500)
    run_p.add_argument(
        "--opencode-stdout-stall-seconds",
        type=int,
        default=300,
        help="Kill opencode if its stdout has not grown for this many seconds. 0 to disable.",
    )
    run_p.add_argument(
        "--opencode-transient-retry-max",
        type=int,
        default=1,
        help="Retry an opencode turn this many times on transient provider errors. 0 disables.",
    )
    run_p.add_argument(
        "--opencode-transient-retry-backoff-seconds",
        type=int,
        default=30,
        help="Sleep this many seconds before retrying after a transient provider error.",
    )
    run_p.add_argument(
        "--gh-repo", default=None, help="Optional owner/repo for gh pr create --repo"
    )
    run_p.add_argument("--pr-title", default=None)
    run_p.add_argument("--pr-body", default=None)
    run_p.add_argument("--max-turns", type=int, default=30)
    run_p.add_argument("--max-wall-minutes", type=int, default=180)
    run_p.add_argument("--no-progress-turns", type=int, default=5)
    run_p.add_argument("--malformed-verdict-retries", type=int, default=2)
    run_p.add_argument("--max-review-rounds", type=int, default=3)
    run_p.set_defaults(func=_run_cmd)

    doctor_p = sub.add_parser(
        "doctor", parents=[shared], help="Validate live-run operational prerequisites"
    )
    doctor_p.add_argument(
        "--actor", choices=[mode.value for mode in ActorMode], default=ActorMode.OPENCODE.value
    )
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
    image_build = image_sub.add_parser(
        "build", help="Build the runtime docker image from the package's Dockerfile"
    )
    image_build.add_argument(
        "--variant",
        choices=list(_VARIANT_DOCKERFILES),
        default="base",
        help=(
            "Image variant to build. "
            "`base` (default) builds contremaitre-agent:latest. "
            "`rust` builds contremaitre-agent-rust:latest (extends base, adds Rust toolchain). "
            "`go` builds contremaitre-agent-go:latest (extends base, adds Go toolchain)."
        ),
    )
    image_build.add_argument(
        "--image-name",
        default=None,
        help="Override the output tag (default: derived from --variant)",
    )
    image_build.add_argument(
        "--dockerfile",
        type=Path,
        default=None,
        help="Override Dockerfile path (default: derived from --variant)",
    )
    image_build.add_argument("--no-cache", action="store_true")
    image_build.set_defaults(func=_image_build_cmd)

    cleanup_p = sub.add_parser(
        "cleanup", help="Prune stale containers + worktrees + dangling images"
    )
    cleanup_p.add_argument("--runs-root", type=Path, default=Path(".contremaitre/runs"))
    cleanup_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be removed without touching anything",
    )
    cleanup_p.add_argument(
        "--skip-images",
        action="store_true",
        help="Skip docker image prune (containers + worktrees only)",
    )
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
    tui_run = tui_sub.add_parser(
        "run", help="Spawn `contremaitre run` and attach the TUI to its run dir"
    )
    tui_run.add_argument(
        "run_args",
        nargs=argparse.REMAINDER,
        help="Flags forwarded to `contremaitre run` (e.g. --actor opencode --repo /path …)",
    )
    tui_run.add_argument("--refresh-hz", type=float, default=5.0)
    tui_run.add_argument(
        "--discover-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the spawned run to create its dir",
    )
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

    index_p = sub.add_parser(
        "index",
        help=f"Build {INDEX_FILENAME} listing every run under a runs root",
    )
    index_p.add_argument(
        "runs_root",
        nargs="?",
        type=Path,
        default=Path(".contremaitre/runs"),
        help="Runs root containing per-run directories (default: .contremaitre/runs)",
    )
    index_p.add_argument(
        "--open",
        action="store_true",
        help="Open the index in the default browser",
    )
    index_p.set_defaults(func=_index_cmd)

    eval_p = sub.add_parser("eval", help="v0 regression canary (see golden_cases/)")
    eval_sub = eval_p.add_subparsers(dest="eval_command", required=True)

    eval_runs_root_kwargs = dict(
        type=Path,
        default=Path(".contremaitre/runs"),
        help="Runs root (default: .contremaitre/runs)",
    )

    eval_config_kwargs = dict(
        default="default",
        help="Config name under golden_cases/<case_id>/configs/ (default: default)",
    )

    eval_run = eval_sub.add_parser("run", help="Run one case n times with the named config")
    eval_run.add_argument("case_id")
    eval_run.add_argument("--config", **eval_config_kwargs)
    eval_run.add_argument("--n", type=int, default=3)
    eval_run.add_argument("--runs-root", **eval_runs_root_kwargs)
    eval_run.set_defaults(func=_eval_run_cmd)

    eval_check = eval_sub.add_parser(
        "check", help="Validate one run dir against its (case, config)"
    )
    eval_check.add_argument("run_dir", type=Path)
    eval_check.set_defaults(func=_eval_check_cmd)

    eval_compare = eval_sub.add_parser(
        "compare", help="Aggregate latest n runs and compare to (case, config) baseline"
    )
    eval_compare.add_argument("case_id")
    eval_compare.add_argument("--config", **eval_config_kwargs)
    eval_compare.add_argument("--n", type=int, default=3)
    eval_compare.add_argument("--runs-root", **eval_runs_root_kwargs)
    eval_compare.add_argument(
        "--json", action="store_true", help="Raw JSON output (default: pretty scorecard)"
    )
    eval_compare.set_defaults(func=_eval_compare_cmd)

    eval_promote = eval_sub.add_parser(
        "promote", help="Snapshot the latest n-run cell as the (case, config) baseline"
    )
    eval_promote.add_argument("case_id")
    eval_promote.add_argument("--config", **eval_config_kwargs)
    eval_promote.add_argument("--n", type=int, default=3)
    eval_promote.add_argument("--runs-root", **eval_runs_root_kwargs)
    eval_promote.set_defaults(func=_eval_promote_cmd)

    eval_all = eval_sub.add_parser(
        "all", help="Run every case × the named config and compare to baselines"
    )
    eval_all.add_argument("--config", **eval_config_kwargs)
    eval_all.add_argument("--n", type=int, default=3)
    eval_all.add_argument("--runs-root", **eval_runs_root_kwargs)
    eval_all.set_defaults(func=_eval_all_cmd)

    eval_show = eval_sub.add_parser(
        "show", help="Pretty-print the scorecard for a (case, config) (no side effects)"
    )
    eval_show.add_argument("case_id")
    eval_show.add_argument("--config", **eval_config_kwargs)
    eval_show.add_argument("--n", type=int, default=3)
    eval_show.add_argument("--runs-root", **eval_runs_root_kwargs)
    eval_show.set_defaults(func=_eval_show_cmd)

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
        _ensure_local_clone(cache_path=cache_path, source_url=source_url, base=args.base)
    except subprocess.CalledProcessError as exc:
        print(f"contremaitre: git clone failed: {exc.stderr or exc}", file=sys.stderr)
        return 1
    # Apply saved defaults (~/.config/contremaitre/defaults.toml) — only
    # for fields the operator did not pass explicitly. Per-run CLI flags
    # always win; the file is a convenience layer for picker prefills.
    _apply_saved_defaults(args, argv=sys.argv)
    try:
        if not _launch_screen(args=args, source_url=source_url, argv_for_explicit_check=sys.argv):
            print("aborted", file=sys.stderr)
            return 130
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130
    _maybe_provision_cli_egress(args)
    config = _config_from_args(args, repo=cache_path)
    rc = _ensure_default_image_built(config)
    if rc != 0:
        return rc
    # Deps volume is now provisioned inside the orchestrator, AFTER the
    # per-run worktree is checked out from `origin/<base>`, so the
    # lockfile hash reflects the exact state the agent will see (not
    # whatever the cache clone happened to have at first-clone time).
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


def _ensure_local_clone(*, cache_path: Path, source_url: str, base: str | None = None) -> None:
    """Clone `source_url` into `cache_path` if not already there, then refresh.

    Fresh clone path: `git clone source_url cache_path`. Cache-exists path:
    `git fetch origin <base> --prune` so refs created on the remote since
    the cache was cloned are visible to preflight (which runs *before*
    `_create_worktree`'s own fetch, so the cache must be up-to-date by
    then). A separate `git fetch origin <base>` still happens inside
    `_create_worktree` as defense-in-depth against local-ref tampering.

    The refresh is best-effort: if the user is offline or the remote is
    momentarily unreachable, the stale cache keeps working. The orchestrator
    will fail later if the base ref genuinely doesn't exist.

    If `cache_path` exists but is not a git repo, raise so the operator can
    choose the resolution; we never silently overwrite an unknown directory.
    """

    if (cache_path / ".git").exists():
        if base:
            try:
                subprocess.run(
                    ["git", "-C", str(cache_path), "fetch", "--prune", "origin", base],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.CalledProcessError as exc:
                print(
                    f"contremaitre: cache refresh of origin/{base} failed "
                    f"(rc={exc.returncode}); continuing with stale cache. "
                    f"{exc.stderr.strip()}",
                    file=sys.stderr,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(
                    f"contremaitre: cache refresh of origin/{base} failed: {exc}; "
                    "continuing with stale cache",
                    file=sys.stderr,
                )
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
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )


_RULE = "─" * 52


def _b(s: str) -> str:
    return f"\033[1m{s}\033[0m" if sys.stdout.isatty() else s


def _d(s: str) -> str:
    return f"\033[2m{s}\033[0m" if sys.stdout.isatty() else s


def _y(s: str) -> str:
    return f"\033[33m{s}\033[0m" if sys.stdout.isatty() else s


def _openrouter_key_state(env_var: str, key_url: str, *, timeout: float = 5.0) -> dict:
    """Best-effort probe of the OpenRouter key for the pre-launch banner.

    Always returns a dict — never raises — so a network blip can't block
    the launch screen. Caller renders presence, limit, and error fields
    into either a banner (TTY) or a one-line info string (non-TTY).
    """

    import urllib.error
    import urllib.request

    key = os.environ.get(env_var)
    if not key:
        return {"present": False, "limit": None, "remaining": None, "error": None}
    try:
        req = urllib.request.Request(key_url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return {"present": True, "limit": None, "remaining": None, "error": str(exc)[:80]}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"present": True, "limit": None, "remaining": None, "error": "malformed response"}
    return {
        "present": True,
        "limit": data.get("limit"),
        "remaining": data.get("limit_remaining"),
        "error": None,
    }


def _print_key_banner(env_var: str, state: dict) -> None:
    """Decision-driving banner shown BEFORE the model picker."""

    print()
    print(_RULE)
    if not state["present"]:
        print(f"  {_b(f'{env_var} not found in .env')}")
        print()
        print(f"  {_d('Only free OpenCode Zen models available below.')}")
        print(f"  {_d('Add a key to .env to unlock paid OpenRouter models')}")
        print(f"  {_d('(set a credit limit in your OpenRouter dashboard first).')}")
    elif state["error"]:
        print(f"  {_b(f'{env_var} detected')}  {_d('(key info unavailable)')}")
    elif state["limit"] is None:
        print(f"  {_b(f'{env_var} detected')}  {_y('limit: unlimited ⚠')}")
        print()
        print(f"  {_d('Set a credit limit in your OpenRouter dashboard for safety.')}")
    else:
        try:
            limit = float(state["limit"])
            remaining = float(state["remaining"] or 0)
            details = f"limit ${limit:.2f}  ·  remaining ${remaining:.2f}"
        except (TypeError, ValueError):
            details = "limited"
        print(f"  {_b(f'{env_var} detected')}  {_d(details)}")
    print(_RULE)


def _key_state_info_line(env_var: str, state: dict) -> str:
    """Single-line equivalent of `_print_key_banner` for non-TTY logs."""

    if not state["present"]:
        return f"[info] {env_var} not set — free OpenCode Zen models only."
    if state["error"]:
        return f"[info] {env_var} detected (limit info unavailable)."
    if state["limit"] is None:
        return f"[info] {env_var} detected with no credit limit set."
    try:
        limit = float(state["limit"])
        remaining = float(state["remaining"] or 0)
        return f"[info] {env_var} detected — limit ${limit:.2f}, remaining ${remaining:.2f}."
    except (TypeError, ValueError):
        return f"[info] {env_var} detected."


def _normalize_openrouter_slug(pasted: str) -> str:
    """Prepend `openrouter/` to a pasted OpenRouter model id if missing.

    Bound to the picker's `c` option, which advertises itself as taking
    an OpenRouter slug — operators copy `<vendor>/<model>` straight from
    openrouter.ai/models. The opencode binary needs the `openrouter/`
    provider prefix to dispatch; without it the run dies on turn one
    with `Model not found: <slug>`. Already-prefixed inputs pass
    through so a returning operator who pastes the full id doesn't get
    double-prefixed. Other providers, if added later, get their own
    normalizer + picker branch.
    """

    slug = pasted.strip()
    if not slug:
        return slug
    if slug.startswith("openrouter/") or slug.startswith("opencode/"):
        return slug
    return f"openrouter/{slug}"


def _print_no_cli_reviewer_banner() -> None:
    print()
    print(_RULE)
    print(f"  {_b('No claude/codex CLI on PATH')}")
    print()
    print(f"  {_d('Install either to enable a free post-publish code review on')}")
    print(f"  {_d('your subscription (no API spend). Continuing without it.')}")
    print(_RULE)


def _pick_models_interactive(
    *,
    current_agent: str,
    current_sim: str,
    current_extra: str | None,
    pick_agent: bool,
    pick_sim: bool,
    pick_extra: bool,
    allow_custom: bool,
    extra_default_skip: bool = False,
) -> tuple[str, str, str | None, list[tuple[str, str]]]:
    """Run the agent / sim / extra-reviewer picker; return chosen values.

    Returns `(agent, sim, extra, picker_args)`. `picker_args` is a list of
    `(flag, value)` tuples for callers that pass-through flags to a
    spawned subprocess (the `tui run` path); the run-screen caller
    instead reassigns directly onto its `argparse.Namespace`. Either
    way, the result tuple is authoritative for what was picked.

    `pick_*` flags toggle each role's prompt — `False` means the value
    was explicitly set on the command line (or `init` left it unset)
    and the picker should leave that field alone.

    When the free-model catalog is unreachable, returns inputs unchanged
    and prints a soft warning — never blocks a run that would otherwise
    launch with hardcoded defaults.
    """

    if not (pick_agent or pick_sim or pick_extra):
        return current_agent, current_sim, current_extra, []

    free = _fetch_free_models()
    picker_args: list[tuple[str, str]] = []
    if free is None:
        print()
        print(_d("  (model catalog unavailable — using CLI defaults)"))
        return current_agent, current_sim, current_extra, picker_args

    print()
    print(f"  {_b('Pick models')}  {_d('(free OpenCode Zen — no key needed)')}")
    print()

    def _index_of(model: str | None) -> int | None:
        if not model:
            return None
        bare = model.rsplit("/", 1)[-1]
        candidates = {bare, f"{bare}-free"}
        for i, m in enumerate(free):
            if m["id"] in candidates:
                return i
        return None

    # Per-role default indices read from the saved values. None when the
    # saved slug isn't in the free catalog (custom OpenRouter paste, or
    # no saved value); the picker falls back to a sensible neighbor in
    # that case.
    agent_default = _index_of(current_agent)
    sim_default = _index_of(current_sim)
    extra_default = _index_of(current_extra)
    width = len(str(len(free) - 1))
    for i, m in enumerate(free):
        roles = []
        if agent_default == i:
            roles.append("agent")
        if sim_default == i:
            roles.append("sim")
        if extra_default == i:
            roles.append("extra")
        marker = f"  {_d('← ' + ', '.join(roles))}" if roles else ""
        print(f"    {i:>{width}}  {m['id']}{marker}")
    if allow_custom:
        print(f"    {'c':>{width}}  {_d('paste any OpenRouter model name (openrouter.ai/models)')}")
    print()

    def _pick_inline(role: str, current_idx: int) -> tuple[str, int]:
        default_id = free[current_idx]["id"]
        opts = f"0–{len(free) - 1}" + (", c" if allow_custom else "")
        prompt = f"  {role:<6}[{current_idx} - {default_id}] (Enter=accept, {opts}, q): "
        while True:
            try:
                reply = input(prompt).strip()
            except EOFError:
                return f"opencode/{free[current_idx]['id']}", current_idx
            low = reply.lower()
            if low == "":
                return f"opencode/{free[current_idx]['id']}", current_idx
            if low == "q":
                raise KeyboardInterrupt
            if low.isdigit() and 0 <= int(low) < len(free):
                idx = int(low)
                return f"opencode/{free[idx]['id']}", idx
            if allow_custom and low == "c":
                try:
                    slug = input(f"  paste OpenRouter model for {role}: ").strip()
                except EOFError:
                    continue
                if slug:
                    return _normalize_openrouter_slug(slug), current_idx
                continue
            suffix = ", c" if allow_custom else ""
            print(f"  enter a number 0–{len(free) - 1}{suffix}, Enter, or q")

    agent = current_agent
    agent_idx = agent_default if agent_default is not None else 0
    if pick_agent:
        agent, agent_idx = _pick_inline("agent", agent_idx)
        picker_args.append(("--agent-model", agent))

    sim = current_sim
    # Sim defaults to its OWN saved value, only falling back to agent's
    # index when sim has no saved value. Before this fix, sim always
    # mirrored whatever the operator just picked for agent, which made
    # saved sim_model in defaults.toml a no-op.
    sim_idx = sim_default if sim_default is not None else agent_idx
    if pick_sim:
        sim, sim_idx = _pick_inline("sim", sim_idx)
        picker_args.append(("--sim-model", sim))

    extra = current_extra
    if pick_extra:
        from .model_family import model_family

        # Saved extra_reviewer_model wins the suggested-default slot if
        # it's in the catalog; only fall back to cross-family heuristic
        # when the operator hasn't saved a preference.
        suggested_idx: int | None = extra_default
        if suggested_idx is None:
            sim_fam = model_family(sim)
            if sim_fam != "unknown":
                for i, m in enumerate(free):
                    if model_family(f"opencode/{m['id']}") not in (sim_fam, "unknown"):
                        suggested_idx = i
                        break
        if suggested_idx is None:
            for i in range(len(free)):
                if i != sim_idx:
                    suggested_idx = i
                    break
        opts = f"0–{len(free) - 1}" + (", c" if allow_custom else "")
        # `extra_default_skip` flips the Enter behavior to "skip" — the
        # suggested model is still numerically pickable, but the prompt
        # makes Enter the no-extra-reviewer path. Set by
        # `extra_reviewer_model = "skip"` in defaults.toml.
        enter_skips = extra_default_skip or suggested_idx is None
        if suggested_idx is not None:
            suggested_id = free[suggested_idx]["id"]
            if enter_skips:
                extra_prompt = (
                    f"  extra [{suggested_idx} - {suggested_id}] (Enter=skip, {opts}, q): "
                )
            else:
                extra_prompt = f"  extra [{suggested_idx} - {suggested_id}] (Enter=accept, s=skip, {opts}, q): "
        else:
            extra_prompt = f"  extra  (Enter=skip, {opts}, q): "
        while True:
            try:
                reply = input(extra_prompt).strip()
            except EOFError:
                break
            low = reply.lower()
            if low == "":
                if not enter_skips and suggested_idx is not None:
                    extra = f"opencode/{free[suggested_idx]['id']}"
                    picker_args.append(("--extra-reviewer-model", extra))
                break
            if low in ("s", "skip"):
                break
            if low == "q":
                raise KeyboardInterrupt
            if low.isdigit() and 0 <= int(low) < len(free):
                idx = int(low)
                extra = f"opencode/{free[idx]['id']}"
                picker_args.append(("--extra-reviewer-model", extra))
                break
            if allow_custom and low == "c":
                try:
                    slug = input("  paste OpenRouter model for extra: ").strip()
                except EOFError:
                    continue
                if slug:
                    extra = _normalize_openrouter_slug(slug)
                    picker_args.append(("--extra-reviewer-model", extra))
                    break
                continue
            suffix = ", c" if allow_custom else ""
            print(f"  enter a number 0–{len(free) - 1}{suffix}, Enter, s to skip, or q")

    return agent, sim, extra, picker_args


def _cli_egress_is_auto(args: argparse.Namespace) -> bool:
    """True when CLI mode should auto-provision egress (no explicit egress config)."""

    if getattr(args, "actor", None) != ActorMode.CLI.value:
        return False
    if getattr(args, "allow_open_egress", False):
        return False
    return not (getattr(args, "docker_network", None) or getattr(args, "https_proxy", None))


def _maybe_provision_cli_egress(args: argparse.Namespace) -> None:
    """Stand up the shared allowlist egress proxy and point the run at it.

    Only for `--actor cli` with no explicit `--docker-network`/`--https-proxy`
    and without `--allow-open-egress`. Mutates `args` so the resolved config
    (and the runner) see a locked egress. Failure is non-fatal here — preflight
    and the runner still enforce the egress requirement, so a provision failure
    surfaces as a clean "egress not configured" refusal rather than a crash.
    """

    if not _cli_egress_is_auto(args):
        return
    from .cli_egress import ensure_egress_proxy

    try:
        network, proxy = ensure_egress_proxy()
    except Exception as exc:  # noqa: BLE001 - degrade to the enforced refusal
        print(f"[warn] CLI egress auto-provision failed: {exc}", file=sys.stderr)
        return
    args.docker_network = network
    args.https_proxy = proxy
    print(f"[info] CLI egress: {network} + allowlist proxy ({proxy})")


def _codex_status_lines(args: argparse.Namespace) -> tuple[str, str]:
    """(token_line, egress_line) describing the codex CLI run's posture."""

    import time as _time

    from .cli_actor import _access_token_exp

    auth = Path.home() / ".codex" / "auth.json"
    exp = _access_token_exp(auth) if auth.exists() else None
    if exp:
        token_line = f"codex token: valid (~{(exp - int(_time.time())) // 3600}h left)"
    elif auth.exists():
        token_line = "codex token: present (opaque expiry)"
    else:
        token_line = "codex token: MISSING — run codex once to log in"

    if getattr(args, "allow_open_egress", False):
        egress_line = "egress: OPEN (--allow-open-egress) — token is exfiltratable"
    elif getattr(args, "docker_network", None) or getattr(args, "https_proxy", None):
        net = getattr(args, "docker_network", None) or "(none)"
        proxy = "yes" if getattr(args, "https_proxy", None) else "no"
        egress_line = f"egress: locked (network={net}, proxy={proxy})"
    else:
        egress_line = "egress: auto-provision allowlist proxy on launch (OpenAI-only)"
    return token_line, egress_line


def _pick_runtimes_interactive(args: argparse.Namespace) -> None:
    """Prompt for the agent and SIM runtimes; set args.actor / args.sim_actor.

    Runtime is per role: `opencode` (OpenRouter models) or `cli` (the codex
    subscription CLI). They can differ — a codex agent can pair with an
    opencode SIM, or the reverse. Enter keeps the current flag value. Skipped
    for a `fake` default (fixture runs) so the picker only shows for real runs.
    """

    current_agent = getattr(args, "actor", ActorMode.FAKE.value)
    if current_agent == ActorMode.FAKE.value:
        return  # fixture/smoke run — don't surface the runtime picker

    # (value, label). claude is pending a headless OAuth token, so only codex
    # is offered under the `cli` runtime today.
    choices = [
        (ActorMode.OPENCODE.value, "opencode  (OpenRouter models)"),
        (ActorMode.CLI.value, "codex     (subscription CLI)"),
    ]

    def _pick(role: str, current: str) -> str:
        print()
        print(f"  {role} runtime:")
        for i, (val, label) in enumerate(choices, start=1):
            marker = "  ← current" if val == current else ""
            print(f"    {i}  {label}{marker}")
        try:
            reply = input(f"  pick (Enter={current}): ").strip().lower()
        except EOFError:
            return current
        if reply == "":
            return current
        if reply.isdigit():
            idx = int(reply) - 1
            if 0 <= idx < len(choices):
                return choices[idx][0]
        return current

    agent = _pick("agent", current_agent)
    sim = _pick("SIM", getattr(args, "sim_actor", None) or agent)
    args.actor = agent
    # Only record a SIM override when it actually differs from the agent.
    args.sim_actor = None if sim == agent else sim


def _cli_launch_screen(*, args: argparse.Namespace) -> bool:
    """Launch screen for `--actor cli` (codex/claude on the operator's plan).

    Subscription-driven, so there's no OpenRouter key probe or model picker —
    instead a concise status: which tool, whether the codex token is valid, and
    whether egress is locked (the CLI actor refuses to run on open egress unless
    --allow-open-egress). yes/non-TTY mode collapses to one info line.
    """

    tool = getattr(args, "cli_tool", "codex")
    yes_mode = (
        getattr(args, "yes", False)
        or getattr(args, "no_prompt", False)
        or not sys.stdin.isatty()
    )
    token_line, egress_line = _codex_status_lines(args)

    if yes_mode:
        print(f"[info] actor=cli tool={tool}; {token_line}; {egress_line}")
        return True

    print()
    print(_RULE)
    print(f"  {_b('contremaitre')} · CLI actor ({tool})")
    print(f"  {token_line}")
    print(f"  {egress_line}")
    print(_RULE)
    try:
        reply = input("  launch? [Y/n] ").strip().lower()
    except EOFError:
        return True
    return reply in ("", "y", "yes")


def _launch_screen(
    *,
    args: argparse.Namespace,
    source_url: str,
    argv_for_explicit_check: list[str],
    forwarded_to_subprocess: list[str] | None = None,
) -> bool:
    """Pre-launch screen: status banners → pickers → recap → Y/n.

    Order matters. Decision-driving info (key status, cli-reviewer
    availability) lives in banners BEFORE the picker it affects, so the
    operator sees the constraints at the moment they pick. The recap at
    the bottom is purely a confirmation of the resolved config — no
    warnings, no decisions left to make.

    In --yes / non-TTY mode the banners collapse to one-line `[info]`
    log lines (so CI output explains what was auto-assumed) and the
    function returns True without prompting.
    """

    from . import cli_reviewer

    env_var = getattr(args, "openrouter_env_var", "OPENROUTER_API_KEY")
    key_url = getattr(args, "openrouter_key_url", "https://openrouter.ai/api/v1/key")
    cli_reviewer_explicit = _has_flag_in(argv_for_explicit_check, "--cli-reviewer")
    available_clis = cli_reviewer.detect_available()

    # `--no-prompt` forces non-interactive even on a TTY. The recap still
    # prints but the pickers are skipped — saved defaults (from
    # `contremaitre init`) or hardcoded values flow through unchanged.
    no_prompt = getattr(args, "no_prompt", False)
    yes_mode = getattr(args, "yes", False) or no_prompt or not sys.stdin.isatty()

    # Per-role runtime selection (interactive only): agent and SIM can each run
    # opencode or codex. Sets args.actor / args.sim_actor; flags are the
    # non-interactive defaults.
    if not yes_mode:
        _pick_runtimes_interactive(args)
    agent_mode = getattr(args, "actor", ActorMode.FAKE.value)
    sim_mode = getattr(args, "sim_actor", None) or agent_mode

    # No opencode role → a pure CLI run (codex/claude): the smaller CLI status
    # screen, no OpenRouter probe or model picker.
    if ActorMode.OPENCODE.value not in (agent_mode, sim_mode):
        return _cli_launch_screen(args=args)
    any_cli = ActorMode.CLI.value in (agent_mode, sim_mode)

    if yes_mode:
        # Skip the network probe in non-TTY mode — we only need presence
        # in the log line, and probing on every CI invocation is wasted
        # latency.
        key_state = {
            "present": bool(os.environ.get(env_var)),
            "limit": None,
            "remaining": None,
            "error": None,
        }
        print(_key_state_info_line(env_var, key_state))
        if not available_clis and not cli_reviewer_explicit:
            print("[info] No claude/codex CLI on PATH — post-publish code review disabled.")
        return True

    # ----- interactive path -----
    key_state = _openrouter_key_state(env_var, key_url)
    allow_custom = key_state["present"]

    print()
    print(_RULE)
    print(f"  {_b('contremaitre')}")

    # Banner before the picker: tells the operator whether paid models
    # are reachable at all, so the `c` paste option makes sense in
    # context.
    _print_key_banner(env_var, key_state)

    # When one role is codex (mixed run), surface its token + egress posture
    # alongside the OpenRouter banner so the operator sees both constraints.
    if any_cli:
        token_line, egress_line = _codex_status_lines(args)
        cli_role = "agent" if agent_mode == ActorMode.CLI.value else "SIM"
        print(f"  codex {cli_role}: {token_line}; {egress_line}")

    agent_explicit = _has_flag_in(argv_for_explicit_check, "--agent-model")
    sim_explicit = _has_flag_in(argv_for_explicit_check, "--sim-model")
    extra_explicit = _has_flag_in(argv_for_explicit_check, "--extra-reviewer-model")

    # ----- model picker (single list, agent then SIM, then extra) -----
    agent, sim, extra_model, picker_args = _pick_models_interactive(
        current_agent=getattr(args, "agent_model", ""),
        current_sim=getattr(args, "sim_model", ""),
        current_extra=getattr(args, "extra_reviewer_model", None),
        # Only pick an OpenRouter model for a role that actually runs opencode;
        # a codex role ignores it.
        pick_agent=(agent_mode == ActorMode.OPENCODE.value) and not agent_explicit,
        pick_sim=(sim_mode == ActorMode.OPENCODE.value) and not sim_explicit,
        pick_extra=not extra_explicit,
        # `extra_reviewer_model = "skip"` in defaults.toml flips the
        # extra-reviewer Enter behavior from "accept the suggestion" to
        # "skip" — the suggestion is still numerically pickable.
        extra_default_skip=getattr(args, "_defaults_skip_extra", False),
        allow_custom=allow_custom,
    )
    args.agent_model = agent
    args.sim_model = sim
    args.extra_reviewer_model = extra_model
    if forwarded_to_subprocess is not None:
        for flag, value in picker_args:
            forwarded_to_subprocess.extend([flag, value])

    # ----- CLI reviewer (post-publish, subscription-bound) -----
    # Banner-then-picker mirrors the OpenRouter-key flow above: when no
    # CLI is on PATH, the hint banner explains the missing capability;
    # otherwise we fall through to the normal picker.
    if not cli_reviewer_explicit:
        if available_clis:
            print()
            # When the value came from defaults.toml (not an explicit CLI
            # flag), force the picker to run with the saved value as the
            # Enter prefill. Otherwise `flag_value="both"` would
            # short-circuit and the operator would never see the prompt.
            prefill = getattr(args, "_defaults_cli_reviewer_prefill", None)
            picker_flag_value = "auto" if prefill else getattr(args, "cli_reviewer", "auto")
            chosen = cli_reviewer.resolve_choice(
                flag_value=picker_flag_value,
                available=available_clis,
                tty=True,
                saved_default=prefill,
            )
        else:
            _print_no_cli_reviewer_banner()
            chosen = "none"
        args.cli_reviewer = chosen
        if forwarded_to_subprocess is not None and chosen != "auto":
            forwarded_to_subprocess.extend(["--cli-reviewer", chosen])

    # ----- pre-flight ping (per-provider liveness probe) -----
    # Zen: free-tier models occasionally land in the catalog while the
    # operator's per-model quota is already spent. One tiny chat
    # completion catches it now and lets the operator pick another
    # model rather than burn the full 1800s timeout on turn one.
    # OpenRouter: pasted slugs are easy to typo or to copy without the
    # `openrouter/` provider prefix; cross-referencing OR's public
    # catalog catches `Model not found` before launch.
    probe_targets: list[tuple[str, str, str]] = []  # (role, model, kind)
    seen_models: set[str] = set()
    for role, model in (
        ("agent", args.agent_model),
        ("sim", args.sim_model),
        ("extra", extra_model),
    ):
        if not model:
            continue
        if model.startswith("opencode/"):
            kind = "zen"
        elif model.startswith("openrouter/"):
            kind = "openrouter"
        else:
            continue
        if model in seen_models:
            continue
        seen_models.add(model)
        probe_targets.append((role, model, kind))
    quota_blockers: list[tuple[str, str]] = []
    unknown_or_models: list[tuple[str, str]] = []
    or_catalog: set[str] | None = None
    or_catalog_fetched = False
    if probe_targets:
        print()
        print(f"  {_d('pre-flight ping …')}")
        for role, model, kind in probe_targets:
            if kind == "zen":
                short = model.rsplit("/", 1)[-1]
                status, detail = _probe_zen_model(model)
                if status == "ok":
                    print(f"    {role:<6}  {_d(short)}  ✓")
                elif status == "quota_exhausted":
                    print(f"    {role:<6}  {_b(short)}  ✗  free-tier quota exhausted")
                    quota_blockers.append((role, model))
                else:
                    print(f"    {role:<6}  {_d(short)}  ?  {_d(detail or 'probe failed')}")
            else:  # openrouter
                short = model[len("openrouter/") :]
                if not or_catalog_fetched:
                    or_catalog = _fetch_openrouter_catalog()
                    or_catalog_fetched = True
                if or_catalog is None:
                    print(f"    {role:<6}  {_d(short)}  ?  {_d('OpenRouter catalog unavailable')}")
                elif short in or_catalog:
                    print(f"    {role:<6}  {_d(short)}  ✓")
                else:
                    print(f"    {role:<6}  {_b(short)}  ✗  not found on OpenRouter")
                    unknown_or_models.append((role, model))

    # ----- recap (decision-free) -----
    publish = getattr(args, "publish_mode", "stub")
    cost = getattr(args, "max_cost_usd", None)
    wall = getattr(args, "max_wall_minutes", None)
    base = getattr(args, "base", "?")
    cli_reviewer_choice = getattr(args, "cli_reviewer", "none")

    print()
    print(_RULE)
    print(f"  {_b('Run summary')}")
    print()
    print(f"  target          {source_url}")
    print(f"  branch          {base}  {_d(f'({publish})')}")
    print()
    print(f"  agent           {_b(args.agent_model)}")
    print(f"  sim             {_b(args.sim_model)}")
    if extra_model:
        print(f"  extra           {_b(extra_model)}")
    if cli_reviewer_choice in ("codex", "claude"):
        print(f"  code-review     {_b(cli_reviewer_choice)}  {_d('(post-publish, subscription)')}")
    elif cli_reviewer_choice == "both":
        print(f"  code-review     {_b('codex + claude')}  {_d('(post-publish, two PR comments)')}")
    elif cli_reviewer_choice == "none":
        print(f"  code-review     {_d('skipped')}")
    caps_parts: list[str] = []
    if cost is not None:
        caps_parts.append(f"${cost}")
    if wall is not None:
        caps_parts.append(f"{wall}m wall")
    if caps_parts:
        print(f"  caps            {_d('  ·  '.join(caps_parts))}")

    # Network posture mirrors preflight._check_network_policy: an explicit
    # proxy or docker-network wins over the --allow-open-egress fallback.
    http_proxy = getattr(args, "http_proxy", None)
    https_proxy = getattr(args, "https_proxy", None)
    docker_network = getattr(args, "docker_network", None)
    allow_open_egress = getattr(args, "allow_open_egress", False)
    if http_proxy or https_proxy:
        network_label = "proxy"
    elif docker_network:
        network_label = f"network: {docker_network}"
    elif allow_open_egress:
        network_label = "open egress"
    else:
        network_label = "not configured"
    print(f"  network         {_d(network_label)}")
    print(_RULE)
    print()

    if quota_blockers or unknown_or_models:
        if quota_blockers:
            print(f"  {_b('free-tier quota exhausted for:')}")
            for role, model in quota_blockers:
                print(f"    {role}  {model}")
            print(
                f"  {_d('try again later, or pick a different model with --' + 'agent-model/--sim-model.')}"
            )
            print()
        if unknown_or_models:
            print(f"  {_b('unknown OpenRouter model:')}")
            for role, model in unknown_or_models:
                print(f"    {role}  {model}")
            print(f"  {_d('check the slug at openrouter.ai/models, or pick a different model.')}")
            print()
        try:
            reply = input("  proceed anyway? [y/N] ").strip().lower()
        except EOFError:
            return False
        print()
        return reply in ("y", "yes")

    print(f"  {_d('Contremaitre will run autonomously and create a Draft PR on')} {source_url}")
    print(f"  {_d('Ctrl-C to abort')}")
    print()
    try:
        reply = input("  proceed? [Y/n] ").strip().lower()
    except EOFError:
        return True
    print()
    return reply in ("", "y", "yes")


def _fetch_openrouter_catalog(timeout: float = 8.0) -> set[str] | None:
    """Set of available OpenRouter model ids, or None on failure.

    The `/api/v1/models` endpoint is unauthenticated and returns the
    full public catalog. Used to validate pasted OpenRouter slugs in
    pre-flight — catches typos and the missing-prefix shape (e.g. a
    raw `qwen/qwen3.7-max` paste that opencode would later reject as
    `Model not found`) without burning a real completion. Returns None
    on network or parse failure so the picker degrades to a soft `?`
    rather than blocking a run that would otherwise launch fine.
    """

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "contremaitre"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    ids: set[str] = set()
    for entry in data:
        if isinstance(entry, dict):
            mid = entry.get("id")
            if isinstance(mid, str) and mid:
                ids.add(mid)
    return ids


def _probe_zen_model(model: str, *, timeout: float = 10.0) -> tuple[str, str | None]:
    """One-shot probe of an opencode Zen model. Detects free-tier quota loss.

    Returns one of:
      - ("ok", None)               — model responded normally
      - ("quota_exhausted", body)  — 429 with `FreeUsageLimitError`
      - ("error", description)     — network / unexpected status; caller
                                     should fall through (the in-run
                                     fast-fail catches real quota errors)

    The endpoint is unauthenticated for Zen free models (opencode binary
    uses the same endpoint without an API key), so we can hit it directly
    from the host without spinning up docker. One-token completion keeps
    the cost negligible and the latency under a second when healthy.
    """

    import urllib.error
    import urllib.request

    model_id = model.rsplit("/", 1)[-1]
    body = json.dumps(
        {
            "model": model_id,
            "max_tokens": 4,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://opencode.ai/zen/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "contremaitre"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return ("ok", None)
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        if exc.code == 429 and "FreeUsageLimitError" in err_body:
            return ("quota_exhausted", err_body[:200])
        return ("error", f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return ("error", str(exc)[:120])


def _ensure_default_image_built(config: RunConfig) -> int:
    """Auto-build a known contremaitre image before any real (non-fake) run.

    Fires whenever the agent OR the SIM needs a container — `--actor opencode`,
    `--actor cli` (codex), or a mixed/composite pair — and the image name
    matches a shipped variant. The image is shared by both roles, so a single
    build covers the mix. Rebuilds when:
    - The image doesn't exist, OR
    - Its `contremaitre.dockerfile-sha256` label is missing or mismatches the
      current Dockerfile contents (catches "Dockerfile edited, image not
      rebuilt" — the failure mode that left python3/uv missing, and would
      otherwise leave codex missing from a stale pre-codex image).

    Custom / third-party images are the operator's responsibility — preflight
    will surface a clean failure with the build hint.
    """

    # Build whenever any active role needs a container; only a pure-fake run
    # (no agent and no SIM container) skips. Mirrors preflight's union check
    # in `preflight.run_preflight`.
    modes = {config.actor_mode, config.sim_actor_mode or config.actor_mode}
    if modes <= {ActorMode.FAKE}:
        return 0
    auto_build_map = {
        _DEFAULT_IMAGE: _VARIANT_DOCKERFILES["base"],
        _RUST_IMAGE: _VARIANT_DOCKERFILES["rust"],
        _GO_IMAGE: _VARIANT_DOCKERFILES["go"],
    }
    dockerfile = auto_build_map.get(config.docker_image)
    if dockerfile is None:
        return 0
    expected_hash = _dockerfile_hash(dockerfile)
    if expected_hash is None:
        # Dockerfile missing — fall through to build which surfaces the same error.
        return _build_image_inline(
            image_name=config.docker_image, dockerfile=dockerfile, no_cache=False
        )
    try:
        inspect = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                config.docker_image,
                "--format",
                '{{ index .Config.Labels "' + _DOCKERFILE_HASH_LABEL + '" }}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if inspect.returncode != 0:
        print(
            f"contremaitre: image {config.docker_image} not found — building inline",
            file=sys.stderr,
        )
        return _build_image_inline(
            image_name=config.docker_image, dockerfile=dockerfile, no_cache=False
        )
    actual_hash = inspect.stdout.strip()
    if actual_hash == expected_hash:
        return 0
    print(
        f"contremaitre: image {config.docker_image} stale "
        f"(label={actual_hash or '<missing>'}, dockerfile={expected_hash}) — rebuilding",
        file=sys.stderr,
    )
    return _build_image_inline(
        image_name=config.docker_image, dockerfile=dockerfile, no_cache=False
    )


def _dockerfile_hash(dockerfile: Path) -> str | None:
    """SHA-256 of the Dockerfile contents, or None if the file is missing.

    Used as both the image-build label and the staleness check. Hashes
    the file the operator would actually rebuild from — variant images
    (rust, go) inherit `FROM contremaitre-agent:latest`, so a stale base
    is caught when the base is rebuilt and the variant's `FROM` resolves
    to a different layer ID (next variant run sees its own dockerfile
    hash unchanged but inspect succeeds via separate label match).
    """

    try:
        return hashlib.sha256(dockerfile.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


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
            capture_output=True,
            text=True,
            timeout=15,
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
            capture_output=True,
            text=True,
            timeout=10,
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
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=contremaitre.run-id",
                "--format",
                '{{.ID}}\t{{.Label "contremaitre.run-id"}}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
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
            capture_output=True,
            text=True,
            timeout=10,
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
    contents = dockerfile.read_bytes()
    digest = hashlib.sha256(contents).hexdigest()
    cmd = ["docker", "build", "-t", image_name, "--label", f"{_DOCKERFILE_HASH_LABEL}={digest}"]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append("-")
    print(f"contremaitre: building {image_name} from {dockerfile}", file=sys.stderr)
    try:
        proc = subprocess.run(cmd, input=contents, check=False)
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
        extra_reviewer_model=getattr(args, "extra_reviewer_model", None),
        cli_reviewer=getattr(args, "cli_reviewer", "none"),
        actor_mode=ActorMode(args.actor),
        cli_tool=getattr(args, "cli_tool", "codex"),
        sim_actor_mode=(
            ActorMode(args.sim_actor) if getattr(args, "sim_actor", None) else None
        ),
        check_cmds=tuple(getattr(args, "check_cmd", [])),
        sim_scenario=getattr(args, "sim_scenario", "approved"),
        extra_reviewer_scenario=getattr(args, "extra_reviewer_scenario", "approved"),
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
        sim_timeout_seconds=getattr(args, "sim_timeout_seconds", 1500),
        opencode_stdout_stall_seconds=getattr(args, "opencode_stdout_stall_seconds", 300),
        opencode_transient_retry_max=getattr(args, "opencode_transient_retry_max", 1),
        opencode_transient_retry_backoff_seconds=getattr(
            args, "opencode_transient_retry_backoff_seconds", 30
        ),
        gh_repo=getattr(args, "gh_repo", None),
        pr_title=getattr(args, "pr_title", None),
        pr_body=getattr(args, "pr_body", None),
        caps=caps,
    )


def _fixture_init_cmd(args: argparse.Namespace) -> int:
    path = init_fixture(args.path.resolve(), overwrite=args.overwrite)
    print(path)
    return 0


def _apply_saved_defaults(args: argparse.Namespace, *, argv: list[str]) -> None:
    """Overlay `defaults.toml` onto `args` for any field the operator left blank.

    Loaded BEFORE the launch screen runs, so the picker prefills already
    show the saved values. We never override an explicit CLI flag —
    presence is checked against `argv`, not against `args.X != default`
    (the argparse default is itself a fallback we want to be supersedable
    by the saved file).
    """

    saved = _defaults.load()
    if (
        saved.agent_model
        and not _has_flag_in(argv, "--agent-model")
        and hasattr(args, "agent_model")
    ):
        args.agent_model = saved.agent_model
    if saved.sim_model and not _has_flag_in(argv, "--sim-model") and hasattr(args, "sim_model"):
        args.sim_model = saved.sim_model
    if (
        saved.extra_reviewer_model
        and not _has_flag_in(argv, "--extra-reviewer-model")
        and hasattr(args, "extra_reviewer_model")
    ):
        args.extra_reviewer_model = saved.extra_reviewer_model
    # `extra_reviewer_model = "skip"` in the file flips the picker's
    # Enter default from "accept the suggestion" to "skip". The picker
    # still PROMPTS — the operator can numerically pick a model — but
    # Enter is the no-extra-reviewer path. `_launch_screen` reads
    # `args._defaults_skip_extra` and passes it to the picker.
    if saved.extra_reviewer_skip and not _has_flag_in(argv, "--extra-reviewer-model"):
        args.extra_reviewer_model = None
        args._defaults_skip_extra = True
    if (
        saved.cli_reviewer
        and not _has_flag_in(argv, "--cli-reviewer")
        and hasattr(args, "cli_reviewer")
    ):
        # Two attributes for two paths: `cli_reviewer` carries the
        # value through `--no-prompt` / yes_mode (no picker), while
        # `_defaults_cli_reviewer_prefill` tells the interactive picker
        # to still PROMPT but make Enter accept the saved value
        # instead of skipping. Without the prefill, the interactive
        # branch would short-circuit on "both" / "codex" / "claude"
        # (treated as explicit) and never ask the operator.
        args.cli_reviewer = saved.cli_reviewer
        args._defaults_cli_reviewer_prefill = saved.cli_reviewer


def _extract_flag_value(args: list[str], flag: str, default: str) -> str:
    """Find a `--flag value` or `--flag=value` pair in a passthrough arg list."""

    for i, item in enumerate(args):
        if item == flag and i + 1 < len(args):
            return args[i + 1]
        prefix = f"{flag}="
        if item.startswith(prefix):
            return item[len(prefix) :]
    return default


def _has_flag_in(argv: list[str], flag: str) -> bool:
    """True iff `--flag value` or `--flag=value` is present in argv."""

    prefix = f"{flag}="
    return any(item == flag or item.startswith(prefix) for item in argv)


def _remove_flag(args: list[str], flag: str) -> None:
    """Drop every `--flag value` / `--flag=value` occurrence from a passthrough
    list, in place."""

    prefix = f"{flag}="
    i = 0
    while i < len(args):
        if args[i] == flag:
            del args[i : i + 2]  # the flag and its separate value
        elif args[i].startswith(prefix):
            del args[i]
        else:
            i += 1


def _set_flag_value(args: list[str], flag: str, value: str) -> None:
    """Replace (or append) a `--flag value` pair in a passthrough list, in place.

    Used to fold an interactive choice back into the flags forwarded to the
    `contremaitre run` subprocess without leaving a duplicate the operator may
    have passed explicitly.
    """

    _remove_flag(args, flag)
    args.extend([flag, value])


def _fetch_free_models() -> list[dict] | None:
    """Pull OpenCode's model catalog, return selectable Zen free entries.

    Why OpenCode Zen and not OpenRouter: the OpenRouter `:free` models
    route through third-party providers (Crucible, Lambda, etc.) whose
    daily free quota is shared across all OpenRouter users. We hit
    `"Out of credits"` mid-run when the upstream is exhausted, even
    though the model is free and the operator's account has budget.
    OpenCode Zen's free tier is served by OpenCode itself with no
    OPENCODE_API_KEY required — the opencode binary in our runtime
    image has built-in access.

    OpenCode resolves built-in providers from models.dev and filters
    deprecated models before dispatch. Reading the same source prevents
    the picker from offering Zen models that the opencode binary rejects
    later.

    Filter heuristic: id ends in `-free` (the convention for NVIDIA-trial /
    DeepSeek / Qwen tiers) plus a small allow-list for stealth-named free
    models (e.g. `big-pickle`).

    None on network or parse failure — caller falls through to
    defaults so a picker UI never blocks a run that would otherwise
    have launched.
    """

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://models.dev/api.json",
        headers={"User-Agent": "contremaitre"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    opencode = payload.get("opencode")
    if not isinstance(opencode, dict):
        return None
    models = opencode.get("models")
    if not isinstance(models, dict):
        return None

    free: list[dict] = []
    stealth = {"big-pickle"}
    for model_id, m in models.items():
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or model_id
        if not isinstance(mid, str) or not mid:
            continue
        if str(m.get("status", "")).lower() == "deprecated":
            continue
        if mid.endswith("-free") or mid in stealth:
            free.append({"id": mid})
    free.sort(key=lambda m: m["id"])
    return free


def _tui_run_cmd(args: argparse.Namespace) -> int:
    from . import tui  # imported lazily so the rest of the CLI works without textual

    forwarded = list(args.run_args or [])
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    run_slug = _extract_flag_value(forwarded, "--run-slug", "run")
    runs_root = Path(_extract_flag_value(forwarded, "--runs-root", ".contremaitre/runs"))
    # Saved defaults seed the prefills before any flag scan — explicit
    # forwarded flags (--agent-model, etc.) still win because
    # `_extract_flag_value` finds them first.
    _saved = _defaults.load()
    agent_model = _extract_flag_value(
        forwarded,
        "--agent-model",
        _saved.agent_model or "openrouter/deepseek/deepseek-v4-flash",
    )
    sim_model = _extract_flag_value(
        forwarded,
        "--sim-model",
        _saved.sim_model or "openrouter/deepseek/deepseek-v4-flash",
    )
    extra_reviewer_model = (
        _extract_flag_value(forwarded, "--extra-reviewer-model", _saved.extra_reviewer_model or "")
        or None
    )
    cli_reviewer_choice = _extract_flag_value(
        forwarded, "--cli-reviewer", _saved.cli_reviewer or "auto"
    )
    docker_image = _extract_flag_value(forwarded, "--docker-image", _DEFAULT_IMAGE)
    # The TUI is a real-run surface: default the agent runtime to a real actor
    # (opencode) rather than `contremaitre run`'s `fake` fixture default, so the
    # per-role runtime picker actually appears on a bare `tui run` (the picker
    # early-returns on a `fake` default). An explicit `--actor fake` still
    # forces a fixture run.
    actor = _extract_flag_value(forwarded, "--actor", ActorMode.OPENCODE.value)
    sim_actor = _extract_flag_value(forwarded, "--sim-actor", "") or None
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
    cache_path = (
        Path(repo_cache_raw).resolve() if repo_cache_raw else _default_cache_path(source_url)
    )
    try:
        _ensure_local_clone(cache_path=cache_path, source_url=source_url, base=base)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"contremaitre: {exc}", file=sys.stderr)
        return 1
    confirm_args = argparse.Namespace(
        yes=("--yes" in forwarded or "-y" in forwarded),
        no_prompt=("--no-prompt" in forwarded),
        base=base,
        fork=fork or None,
        upstream=upstream or None,
        gh_repo=_extract_flag_value(forwarded, "--gh-repo", "") or None,
        publish_mode=_extract_flag_value(forwarded, "--publish-mode", PublishMode.STUB.value),
        max_cost_usd=_extract_flag_value(forwarded, "--max-cost-usd", "?"),
        max_wall_minutes=_extract_flag_value(forwarded, "--max-wall-minutes", "?"),
        agent_model=agent_model,
        sim_model=sim_model,
        extra_reviewer_model=extra_reviewer_model,
        cli_reviewer=cli_reviewer_choice,
        actor=actor,
        sim_actor=sim_actor,
        cli_tool=_extract_flag_value(forwarded, "--cli-tool", "codex"),
        allow_open_egress=("--allow-open-egress" in forwarded),
        docker_network=_extract_flag_value(forwarded, "--docker-network", "") or None,
        http_proxy=_extract_flag_value(forwarded, "--http-proxy", "") or None,
        https_proxy=_extract_flag_value(forwarded, "--https-proxy", "") or None,
    )
    # Propagate the `extra_reviewer_model = "skip"` sentinel from
    # defaults.toml so the picker's Enter-skip behavior matches
    # `contremaitre run`. Only honor it when the operator didn't pass
    # --extra-reviewer-model explicitly.
    if _saved.extra_reviewer_skip and not _has_flag_in(forwarded, "--extra-reviewer-model"):
        confirm_args._defaults_skip_extra = True
    # Same story for cli_reviewer: a saved value should prefill the
    # picker (Enter accepts it), not short-circuit it. Without this the
    # TUI path would silently apply `cli_reviewer = "both"` from the
    # file without ever asking.
    if _saved.cli_reviewer and not _has_flag_in(forwarded, "--cli-reviewer"):
        confirm_args._defaults_cli_reviewer_prefill = _saved.cli_reviewer
    try:
        if not _launch_screen(
            args=confirm_args,
            source_url=source_url,
            argv_for_explicit_check=forwarded,
            forwarded_to_subprocess=forwarded,
        ):
            print("aborted", file=sys.stderr)
            return 130
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130
    # Picker may have appended --agent-model / --sim-model /
    # --extra-reviewer-model to `forwarded`; refresh the locals so the TUI
    # header displays the chosen models.
    agent_model = _extract_flag_value(forwarded, "--agent-model", agent_model)
    sim_model = _extract_flag_value(forwarded, "--sim-model", sim_model)
    extra_reviewer_model = _extract_flag_value(forwarded, "--extra-reviewer-model", "") or None
    cli_reviewer_choice = _extract_flag_value(forwarded, "--cli-reviewer", cli_reviewer_choice)
    # Fold the per-role runtime choice (the picker may have changed it) back
    # into the subprocess flags. The picker writes confirm_args.actor /
    # .sim_actor; `contremaitre run` reads --actor / --sim-actor.
    _set_flag_value(forwarded, "--actor", getattr(confirm_args, "actor", actor))
    resolved_sim = getattr(confirm_args, "sim_actor", sim_actor)
    if resolved_sim:
        _set_flag_value(forwarded, "--sim-actor", resolved_sim)
    else:
        _remove_flag(forwarded, "--sim-actor")
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
        extra_reviewer_model=extra_reviewer_model,
        cli_reviewer=cli_reviewer_choice,
        docker_image=docker_image,
        target_url=source_url,
        base=base,
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


def _index_cmd(args: argparse.Namespace) -> int:
    """Build an index.html listing every run under a runs root."""

    runs_root: Path = args.runs_root.resolve()
    if not runs_root.is_dir():
        print(f"contremaitre index: not a directory: {runs_root}", file=sys.stderr)
        return 1

    out = build_index(runs_root)
    print(f"wrote {out}")

    if args.open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, str(out)], check=False)
    return 0


def _eval_project_root() -> Path:
    return _PACKAGE_DIR.parent


def _eval_run_cmd(args: argparse.Namespace) -> int:
    from .eval import cmd_run

    return cmd_run(
        project_root=_eval_project_root(),
        case_id=args.case_id,
        config_name=args.config,
        n=args.n,
        runs_root=args.runs_root.resolve(),
    )


def _eval_check_cmd(args: argparse.Namespace) -> int:
    from .eval import cmd_check

    return cmd_check(
        project_root=_eval_project_root(),
        run_dir=args.run_dir.resolve(),
    )


def _eval_compare_cmd(args: argparse.Namespace) -> int:
    from .eval import cmd_compare

    return cmd_compare(
        project_root=_eval_project_root(),
        case_id=args.case_id,
        config_name=args.config,
        runs_root=args.runs_root.resolve(),
        n=args.n,
        as_json=args.json,
    )


def _eval_promote_cmd(args: argparse.Namespace) -> int:
    from .eval import cmd_promote

    return cmd_promote(
        project_root=_eval_project_root(),
        case_id=args.case_id,
        config_name=args.config,
        runs_root=args.runs_root.resolve(),
        n=args.n,
    )


def _eval_all_cmd(args: argparse.Namespace) -> int:
    from .eval import cmd_all

    return cmd_all(
        project_root=_eval_project_root(),
        config_name=args.config,
        runs_root=args.runs_root.resolve(),
        n=args.n,
    )


def _eval_show_cmd(args: argparse.Namespace) -> int:
    from .eval import cmd_show

    return cmd_show(
        project_root=_eval_project_root(),
        case_id=args.case_id,
        config_name=args.config,
        runs_root=args.runs_root.resolve(),
        n=args.n,
    )


_VARIANT_DEFAULT_TAGS: dict[str, str] = {
    "base": _DEFAULT_IMAGE,
    "rust": _RUST_IMAGE,
    "go": _GO_IMAGE,
}


def _image_build_cmd(args: argparse.Namespace) -> int:
    variant_dockerfile = _VARIANT_DOCKERFILES.get(args.variant, _DEFAULT_DOCKERFILE)
    variant_default_tag = _VARIANT_DEFAULT_TAGS.get(args.variant, _DEFAULT_IMAGE)
    return _build_image_inline(
        image_name=args.image_name or variant_default_tag,
        dockerfile=args.dockerfile or variant_dockerfile,
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
