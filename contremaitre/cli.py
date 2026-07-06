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
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .envfile import load_dotenv_defaults
from .fixture import init_fixture
from .models import ActorMode, Caps, ModelSpec, PublishMode, RunConfig
from .orchestrator import run
from .paths import slugify
from .preflight import _image_exists, freshness_row, run_preflight
from .runtime_image import list_deps_volumes
from .viewer import VIEWER_FILENAME, build_viewer
from .viewer.index import INDEX_FILENAME, build_index


_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_DOCKERFILE = _PACKAGE_DIR / "Dockerfile"
_DEFAULT_IMAGE = "contremaitre-agent:latest"
_RUST_IMAGE = "contremaitre-agent-rust:latest"
_GO_IMAGE = "contremaitre-agent-go:latest"
_DEFAULT_ZEN_MODEL = "opencode/deepseek-v4-flash-free"
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
        help="Disable the egress lock — containers get open network. Unsafe: the in-container token is long-lived and exfiltratable. Off by default.",
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


def _adr_relpath(value: str) -> str:
    """argparse type for `--adr`: a repo-relative path that can't escape the worktree.

    Existence is checked later at INIT, against the `origin/<base>` checkout —
    the ADR must be committed on the base branch, so probing the operator's
    local tree here would validate the wrong ref.
    """

    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise argparse.ArgumentTypeError(
            f"--adr must be a repo-relative path without '..' (got {value!r})"
        )
    return value


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
        "--adr",
        type=_adr_relpath,
        default=None,
        metavar="RELPATH",
        help=(
            "Repo-relative path to an ADR committed on --base (e.g. "
            "docs/adr/0003-foo.md). Seeds the run from that ADR: the agent "
            "skips the skill's exploration/candidate phases, fact-checks the "
            "ADR against the tree (correcting factual drift in place), then "
            "enters the grilling loop with the ADR as the plan under grill."
        ),
    )
    run_p.add_argument(
        "--agent-model",
        default="",
        help="OpenRouter/OpenCode model slug for an opencode agent. Omit to pick on TTY.",
    )
    run_p.add_argument(
        "--sim-model",
        default="",
        help="OpenRouter/OpenCode model slug for an opencode SIM. Omit to pick on TTY.",
    )
    run_p.add_argument(
        "--cli-reviewer",
        choices=["auto", "codex", "claude", "none"],
        default="auto",
        help=(
            "Post-PR revision loop driver. After the draft PR is published, the "
            "CLI reviewer (operator's claude/codex subscription) reads the PR, "
            "posts a review comment, and if MUST_FIX re-enters the agent (fresh "
            "Docker session) until LOOKS_GOOD or --max-cli-review-rounds exhausted. "
            "`auto` detects what's installed; `none` skips the loop entirely."
        ),
    )
    run_p.add_argument(
        "--max-cli-review-rounds",
        type=int,
        default=3,
        help="Maximum post-PR CLI review + agent revision rounds (default 3).",
    )
    run_p.add_argument(
        "--agent",
        choices=["fake", "opencode", "claude", "codex"],
        default="fake",
        help="Runtime for the agent role: opencode (OpenRouter/Zen model), "
        "claude (subscription CLI), codex (subscription CLI), or fake (smoke tests)",
    )
    run_p.add_argument(
        "--sim",
        choices=["opencode", "claude", "codex"],
        default=None,
        help="Runtime for the SIM role (default: same as --agent). "
        "Allows mixed runs, e.g. --agent claude --sim opencode.",
    )
    run_p.add_argument(
        "--codex-model",
        default="gpt-5.5",
        help="codex-native model for a codex role",
    )
    run_p.add_argument(
        "--codex-effort",
        choices=["minimal", "low", "medium", "high", "xhigh"],
        default="high",
        help="codex reasoning effort (-c model_reasoning_effort) for a codex role",
    )
    run_p.add_argument(
        "--claude-model",
        default="",
        help="claude model for a claude role (e.g. opus / claude-opus-4-8); "
        "empty uses the ~/.claude account default",
    )
    run_p.add_argument(
        "--claude-effort",
        choices=["low", "medium", "high", "max"],
        default="high",
        help="claude effort (--effort) for a claude role",
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
        help="Fake SIM behavior (ignored when --agent opencode)",
    )
    run_p.add_argument(
        "--agent-scenario",
        choices=["normal", "forbidden_path", "no_impl_complete"],
        default="normal",
        help="Fake agent behavior (ignored when --agent opencode)",
    )
    run_p.add_argument(
        "--publish-mode",
        choices=[mode.value for mode in PublishMode],
        default=PublishMode.STUB.value,
    )
    run_p.add_argument("--keep-worktree", action="store_true")
    run_p.add_argument("--simulate-drift-after-approval", action="store_true")
    run_p.add_argument(
        "--container-user",
        default=None,
        help="Optional docker --user value, e.g. $(id -u):$(id -g)",
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
        "--agent",
        choices=["fake", "opencode", "claude", "codex"],
        default="opencode",
        help="Runtime for the agent role (same choices as `run --agent`)",
    )
    doctor_p.add_argument(
        "--sim",
        choices=["opencode", "claude", "codex"],
        default=None,
        help="Runtime for the SIM role (default: same as --agent)",
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

    models_p = sub.add_parser(
        "models",
        help="List available OpenCode Zen free models with live quota status",
    )
    models_p.set_defaults(func=_models_cmd)

    tui_p = sub.add_parser("tui", help="Live Textual TUI (requires `textual`)")
    tui_sub = tui_p.add_subparsers(dest="tui_command", required=True)
    tui_run = tui_sub.add_parser(
        "run", help="Spawn `contremaitre run` and attach the TUI to its run dir"
    )
    tui_run.add_argument(
        "run_args",
        nargs=argparse.REMAINDER,
        help="Flags forwarded to `contremaitre run` (e.g. --agent opencode --repo /path …)",
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

    eval_ab = eval_sub.add_parser(
        "ab",
        help=(
            "Head-to-head A/B: run two configs on one pinned case (interleaved) "
            "and build an HTML comparison report"
        ),
    )
    eval_ab.add_argument("case_id")
    eval_ab.add_argument(
        "--config-a",
        required=True,
        help="Arm A config name under golden_cases/<case_id>/configs/",
    )
    eval_ab.add_argument(
        "--config-b",
        required=True,
        help="Arm B config name under golden_cases/<case_id>/configs/",
    )
    eval_ab.add_argument("--n", type=int, default=3, help="Runs per arm (default: 3)")
    eval_ab.add_argument("--runs-root", **eval_runs_root_kwargs)
    eval_ab.add_argument(
        "--report-only",
        action="store_true",
        help="Skip launching; rebuild the report from the latest n runs per config on disk",
    )
    eval_ab.add_argument(
        "--open",
        action="store_true",
        help="Open the report in the default browser",
    )
    eval_ab.set_defaults(func=_eval_ab_cmd)

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

    agent_name = getattr(args, "agent", "fake")
    sim_name = getattr(args, "sim", None)

    # Zen picker: only when opencode + no model + TTY
    if agent_name == "opencode" and not getattr(args, "agent_model", ""):
        if not sys.stdin.isatty():
            print(
                "contremaitre: --agent-model required in non-interactive mode "
                "(set AGENT_MODEL in Makefile)",
                file=sys.stderr,
            )
            return 1
        try:
            picked = _pick_zen_model_interactive("agent")
        except KeyboardInterrupt:
            print("aborted", file=sys.stderr)
            return 130
        args.agent_model = picked

    sim_is_opencode = (sim_name or agent_name) == "opencode"
    if sim_is_opencode and not getattr(args, "sim_model", ""):
        if agent_name == "opencode" and getattr(args, "agent_model", "") and not sys.stdin.isatty():
            args.sim_model = args.agent_model
        else:
            if not sys.stdin.isatty():
                print(
                    "contremaitre: --sim-model required in non-interactive mode "
                    "(set SIM_MODEL in Makefile)",
                    file=sys.stderr,
                )
                return 1
            try:
                picked_sim = _pick_zen_model_interactive(
                    "sim", default=getattr(args, "agent_model", None)
                )
            except KeyboardInterrupt:
                print("aborted", file=sys.stderr)
                return 130
            args.sim_model = picked_sim

    # Pre-flight presence check (before Y/n; full auth validation runs inside run())
    rc = _preflight_presence_check(args)
    if rc != 0:
        return rc

    # Recap + Y/n
    if sys.stdin.isatty():
        if not _recap_and_confirm(args, source_url=source_url):
            print("aborted", file=sys.stderr)
            return 130

    _maybe_provision_cli_egress(args)
    config = _config_from_args(args, repo=cache_path)
    rc = _ensure_default_image_built(config)
    if rc != 0:
        return rc
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


def _b(s: str) -> str:
    return f"\033[1m{s}\033[0m" if sys.stdout.isatty() else s


def _d(s: str) -> str:
    return f"\033[2m{s}\033[0m" if sys.stdout.isatty() else s


def _active_codex_roles(args: argparse.Namespace) -> bool:
    """True when codex drives any role (agent / SIM / post-publish CLI reviewer).

    Only codex carries a short-lived access token *inside* its container, so a
    codex role is what triggers the locked egress. claude carries no in-container
    credential — the host auth-inject proxy (`cli_auth_proxy`) adds the bearer —
    so claude roles need no lock and run open egress. `auto` reviewer is treated
    as possibly-codex (it picks the cross-family tool, which is codex when the
    agent is claude); over-provisioning the lock is harmless because claude
    containers ignore the internal network anyway.
    """

    from .egress import CREDENTIAL_BEARING_CLI_TOOLS

    agent_name = getattr(args, "agent", "fake")
    sim_name = getattr(args, "sim", None) or agent_name
    reviewer = getattr(args, "cli_reviewer", "none")
    # A credential-bearing CLI tool (codex) is what pulls in the egress lock; an
    # `auto` reviewer is treated as possibly-codex (it picks the cross-family tool).
    return bool({agent_name, sim_name} & CREDENTIAL_BEARING_CLI_TOOLS) or reviewer in (
        CREDENTIAL_BEARING_CLI_TOOLS | {"auto"}
    )


def _cli_egress_is_auto(args: argparse.Namespace) -> bool:
    """True when a codex role should auto-provision its locked egress.

    Only codex carries an exfiltratable in-container token, so only a codex role
    triggers the lock — the secure default. `--allow-open-egress` is the
    explicit, warned override (the operator accepts the codex token risk, e.g. so
    codex can install deps from PyPI/npm the provider-only allowlist would
    block). Explicit `--docker-network`/`--https-proxy` also win.
    """

    if not _active_codex_roles(args):
        return False
    if getattr(args, "allow_open_egress", False):
        return False
    return not (getattr(args, "docker_network", None) or getattr(args, "https_proxy", None))


def _maybe_provision_cli_egress(args: argparse.Namespace) -> None:
    """Stand up the shared allowlist egress proxy for codex roles.

    claude roles need no lock (the host auth-inject proxy holds the credential;
    the container has none), so this fires only when a codex role is active with
    no explicit `--docker-network`/`--https-proxy` and without
    `--allow-open-egress`. Mutates `args` so the resolved config and the runner
    see a locked egress for codex. Failure is non-fatal — preflight and the
    runner still enforce the codex egress requirement, so a provision failure
    surfaces as a clean refusal rather than a crash.
    """

    if getattr(args, "allow_open_egress", False):
        if _active_codex_roles(args):
            print(
                "[warn] codex egress OPEN (--allow-open-egress): codex mounts a "
                "short-lived in-container token, so an injected command could "
                "exfiltrate it. Lock egress unless you know why you need this.",
                file=sys.stderr,
            )
        return
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
    print(
        f"[info] codex egress LOCKED: {network} (no route/DNS) + allowlist proxy "
        f"({proxy}, provider domains only) — the only exit for codex's in-container token."
    )


def _codex_token_line() -> str:
    import time as _time

    from .cli_actor import _access_token_exp

    auth = Path.home() / ".codex" / "auth.json"
    exp = _access_token_exp(auth) if auth.exists() else None
    if exp:
        return f"codex token: valid (~{(exp - int(_time.time())) // 3600}h left)"
    if auth.exists():
        return "codex token: present (opaque expiry)"
    return "codex token: MISSING — run `codex login` on the host"


def _claude_token_line() -> str:
    from .cli_actor import _CLAUDE_OAUTH_ENV
    from .cli_auth_proxy import AuthProxyError, resolve_claude_token

    try:
        resolve_claude_token()
    except AuthProxyError:
        return (
            f"claude token: MISSING — run `claude setup-token` and add {_CLAUDE_OAUTH_ENV} "
            "to .env (or log in with `claude`)"
        )
    src = "env" if os.environ.get(_CLAUDE_OAUTH_ENV) else "keychain / ~/.claude/.credentials.json"
    return f"claude token: present (host-injected, never in container; source: {src})"


def _opencode_key_line(env_var: str) -> str:
    if os.environ.get(env_var):
        return f"opencode key: present ({env_var} set)"
    return f"opencode key: MISSING — add {env_var} to .env (paid/OpenRouter model selected)"


def _onboard_claude_token() -> bool:
    """Guide the operator through `claude setup-token` and persist the result.

    Offers to run the headless OAuth flow (inheriting the terminal so the
    browser handshake works), prompts for the printed token, validates its
    shape, writes ``CLAUDE_CODE_OAUTH_TOKEN`` to ``./.env``, and exports it into
    the current process so the in-flight run can proceed. Returns True only when
    a token was captured and persisted; False if the operator declined, the
    claude CLI is absent, or the pasted value is not a setup-token.
    """
    import shutil
    import subprocess

    from .cli_actor import _CLAUDE_OAUTH_ENV
    from .envfile import upsert_env_var

    print()
    print(f"  {_b('No claude token found.')} {_d(f'({_CLAUDE_OAUTH_ENV} unset)')}")
    if shutil.which("claude") is None:
        print(f"  {_d('claude CLI is not on PATH — install it first, then re-run.')}")
        return False
    try:
        reply = input("  Run `claude setup-token` now? [Y/n] ").strip().lower()
    except EOFError:
        return False
    if reply not in ("", "y", "yes"):
        return False

    print(f"  {_d('launching claude setup-token (browser OAuth) …')}")
    try:
        subprocess.run(["claude", "setup-token"], check=False)
    except OSError as exc:
        print(f"  {_d(f'could not launch claude: {exc}')}")
        return False

    try:
        token = input("  Paste the token shown above: ").strip()
    except EOFError:
        return False
    if not token.startswith("sk-ant-oat"):
        print(f"  {_d('not a setup-token (expected sk-ant-oat…) — skipping')}")
        return False

    env_path = Path.cwd() / ".env"
    try:
        upsert_env_var(env_path, _CLAUDE_OAUTH_ENV, token)
    except ValueError as exc:
        print(f"  {_d(f'could not write .env: {exc}')}")
        return False
    os.environ[_CLAUDE_OAUTH_ENV] = token
    print(f"  {_b('✓')} wrote {_CLAUDE_OAUTH_ENV} to {env_path}")
    return True


def _agent_name_to_runtime(name: str) -> tuple[ActorMode, str | None]:
    """Translate operator-facing agent name to (ActorMode, cli_tool | None)."""
    if name == "claude":
        return ActorMode.CLI, "claude"
    elif name == "codex":
        return ActorMode.CLI, "codex"
    elif name == "opencode":
        return ActorMode.OPENCODE, None
    else:  # "fake"
        return ActorMode.FAKE, None


def _pick_zen_model_interactive(role: str, *, default: str | None = None) -> str:
    """Interactive Zen model picker for one role. Returns the chosen model string."""
    free = _fetch_free_models()
    if free is None:
        fallback = default or _DEFAULT_ZEN_MODEL
        print(f"  {_d('(model catalog unavailable — using')} {fallback}{_d(')')}")
        return fallback
    if not free:
        fallback = default or _DEFAULT_ZEN_MODEL
        print(f"  {_d('(model catalog empty — using')} {fallback}{_d(')')}")
        return fallback

    print()
    print(f"  {_b('Pick model')} {_d(f'for {role}')}")
    print()
    width = len(str(len(free) - 1))
    default_idx = 0
    if default:
        bare = default.rsplit("/", 1)[-1]
        for i, m in enumerate(free):
            if m["id"] == bare or m["id"] == f"{bare}-free":
                default_idx = i
                break
    for i, m in enumerate(free):
        marker = f"  {_d('← default')}" if i == default_idx else ""
        print(f"    {i:>{width}}  {m['id']}{marker}")
    print()
    opts = f"0–{len(free) - 1}, paste OpenRouter slug"
    default_id = free[default_idx]["id"]
    prompt = f"  {role:<6}[{default_idx} - {default_id}] (Enter=accept, {opts}, q): "
    free_ids = {m["id"] for m in free}
    catalog_unfetched = object()
    openrouter_catalog: set[str] | None | object = catalog_unfetched
    while True:
        try:
            reply = input(prompt).strip()
        except EOFError:
            return f"opencode/{default_id}"
        if reply == "":
            return f"opencode/{default_id}"
        if reply.lower() == "q":
            raise KeyboardInterrupt
        if reply.isdigit() and 0 <= int(reply) < len(free):
            return f"opencode/{free[int(reply)]['id']}"
        if reply in free_ids:
            return f"opencode/{reply}"
        slug = _normalize_openrouter_slug(reply)
        if slug.startswith("opencode/"):
            model_id = slug.removeprefix("opencode/")
            if model_id in free_ids:
                return slug
            print(f"  unknown OpenCode Zen model in current catalog: {model_id}")
            continue
        if slug.startswith("openrouter/"):
            model_id = slug.removeprefix("openrouter/")
            if openrouter_catalog is catalog_unfetched:
                openrouter_catalog = _fetch_openrouter_catalog()
            if openrouter_catalog is None or model_id in openrouter_catalog:
                return slug
            print(f"  OpenRouter model not found in live catalog: {model_id}")
            continue
        print(f"  enter a number {opts}, Enter, or q")


def _preflight_presence_check(args: argparse.Namespace) -> int:
    """Quick pre-Y/n credential presence check. Returns 0 on pass, 1 on failure.

    For a missing claude token on a TTY, offers guided `claude setup-token`
    onboarding that persists ``CLAUDE_CODE_OAUTH_TOKEN`` to ``.env`` before it
    decides pass/fail, so first-run operators don't bounce off a bare message.
    """
    from .models import is_zen_model

    agent_name = getattr(args, "agent", "fake")
    sim_name = getattr(args, "sim", None) or agent_name
    reviewer = getattr(args, "cli_reviewer", "none")
    env_var = getattr(args, "openrouter_env_var", "OPENROUTER_API_KEY")

    # CLI roles (claude/codex), grouped by tool, role labels preserved.
    roles_by_name: dict[str, list[str]] = {}
    for role_name, name in (
        ("agent", agent_name),
        ("sim", sim_name),
        (f"reviewer ({reviewer})", reviewer if reviewer in ("claude", "codex") else None),
    ):
        if name in ("claude", "codex"):
            roles_by_name.setdefault(name, []).append(role_name)

    # opencode roles on a paid (non-Zen) model need OPENROUTER_API_KEY; Zen
    # models reach the provider through the opencode binary's built-in access.
    opencode_paid_roles: list[str] = []
    for role_name, name, model in (
        ("agent", agent_name, getattr(args, "agent_model", "")),
        ("sim", sim_name, getattr(args, "sim_model", "")),
    ):
        if name == "opencode" and model and not is_zen_model(model):
            opencode_paid_roles.append(role_name)

    if not roles_by_name and not opencode_paid_roles:
        return 0  # opencode/fake on free models only — nothing to check here

    print()
    print(f"  {_d('pre-flight …')}")
    failed = False

    for tool_name, roles in roles_by_name.items():
        role_label = " + ".join(roles)
        line = _claude_token_line() if tool_name == "claude" else _codex_token_line()
        ok = "MISSING" not in line
        if not ok and tool_name == "claude" and sys.stdin.isatty():
            if _onboard_claude_token():
                line = _claude_token_line()
                ok = "MISSING" not in line
        mark = "✓" if ok else "✗"
        print(f"    {role_label:<16}  {tool_name:<8}  {mark}  {_d(line)}")
        if not ok:
            failed = True

    # CLI freshness: WARN if the in-image claude/codex lags npm. Advisory only —
    # never sets `failed` (a stale CLI usually still works). Skipped when the
    # image isn't built yet: this screen runs BEFORE the build, so a cold first
    # run would otherwise cry "couldn't read version" about an image that simply
    # doesn't exist yet (it's built right after the Y/n).
    image = getattr(args, "docker_image", _DEFAULT_IMAGE)
    if roles_by_name and _image_exists(image):
        for tool_name in roles_by_name:
            status, message = freshness_row(image, tool_name)
            mark = "⚠" if status == "WARN" else "✓"
            print(f"    {'freshness':<16}  {tool_name:<8}  {mark}  {_d(message)}")

    if opencode_paid_roles:
        role_label = " + ".join(opencode_paid_roles)
        line = _opencode_key_line(env_var)
        ok = "MISSING" not in line
        mark = "✓" if ok else "✗"
        print(f"    {role_label:<16}  {'opencode':<8}  {mark}  {_d(line)}")
        if not ok:
            failed = True

    if failed:
        print()
        return 1
    return 0


def _recap_and_confirm(args: argparse.Namespace, *, source_url: str) -> bool:
    """Print one-line recap and prompt Y/n. Returns True to proceed."""
    agent_name = getattr(args, "agent", "fake")
    sim_name = getattr(args, "sim", None) or agent_name
    reviewer = getattr(args, "cli_reviewer", "none")
    base = getattr(args, "base", "?")
    publish_mode = getattr(args, "publish_mode", "stub")

    # Short repo slug
    repo_slug = source_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    parts = [f"{agent_name} (agent)", f"{sim_name} (sim)", f"{reviewer} (reviewer)"]
    combo = " + ".join(parts)

    print()
    print(f"  contremaitre: {_b(combo)} → {_d(repo_slug)}  [branch: {base}]")
    if publish_mode == "gh":
        action = "run autonomously and open a draft PR"
    else:
        action = "run autonomously (dry run — no PR will be published)"
    print(f"  Will {action}. Continue? [Y/n] ", end="", flush=True)
    try:
        reply = input().strip().lower()
    except EOFError:
        print()
        return True
    print()
    return reply in ("", "y", "yes")


def _models_cmd(_args) -> int:
    """List available OpenCode Zen free models with live quota status."""
    free = _fetch_free_models()
    if free is None:
        print("contremaitre models: catalog unavailable (network error)", file=sys.stderr)
        return 1
    if not free:
        print("contremaitre models: no free models in catalog")
        return 0

    print()
    print(f"  {_b('OpenCode Zen free models')}")
    print()
    width = len(str(len(free) - 1))
    for i, m in enumerate(free):
        model_id = m["id"]
        full = f"opencode/{model_id}"
        status, _ = _probe_zen_model(full)
        mark = "✓" if status == "ok" else ("✗ quota" if status == "quota_exhausted" else "?")
        print(f"  {i:>{width}}  {model_id:<40}  {mark}")
    print()
    print(f"  {_d('Use: --agent-model opencode/<id>  or set AGENT_MODEL in Makefile')}")
    print()
    return 0


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
    """Auto-build a known contremaitre image before any real (non-fake) run."""
    return _ensure_image_for(
        docker_image=config.docker_image,
        actor_mode=config.actor_mode,
        sim_actor_mode=config.sim_actor_mode,
    )


def _ensure_image_for(
    *,
    docker_image: str,
    actor_mode: ActorMode,
    sim_actor_mode: ActorMode | None,
) -> int:
    """Build/rebuild the image if it's missing or stale.

    Extracted so `_tui_run_cmd` can trigger the build BEFORE spawning the
    subprocess — otherwise the build blocks the subprocess for minutes while
    the TUI's discover timeout (30 s) fires waiting for the run dir.

    Fires whenever any active role needs a container; a pure-fake run skips.
    Rebuilds when the image is missing OR its dockerfile-sha256 label drifts.
    Custom images are the operator's responsibility.
    """
    modes = {actor_mode, sim_actor_mode or actor_mode}
    if modes <= {ActorMode.FAKE}:
        return 0
    auto_build_map = {
        _DEFAULT_IMAGE: _VARIANT_DOCKERFILES["base"],
        _RUST_IMAGE: _VARIANT_DOCKERFILES["rust"],
        _GO_IMAGE: _VARIANT_DOCKERFILES["go"],
    }
    dockerfile = auto_build_map.get(docker_image)
    if dockerfile is None:
        return 0
    expected_hash = _dockerfile_hash(dockerfile)
    if expected_hash is None:
        # Dockerfile missing — fall through to build which surfaces the same error.
        return _build_image_inline(image_name=docker_image, dockerfile=dockerfile, no_cache=False)
    try:
        inspect = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                docker_image,
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
            f"contremaitre: image {docker_image} not found — building inline",
            file=sys.stderr,
        )
        return _build_image_inline(image_name=docker_image, dockerfile=dockerfile, no_cache=False)
    actual_hash = inspect.stdout.strip()
    if actual_hash == expected_hash:
        return 0
    print(
        f"contremaitre: image {docker_image} stale "
        f"(label={actual_hash or '<missing>'}, dockerfile={expected_hash}) — rebuilding",
        file=sys.stderr,
    )
    return _build_image_inline(image_name=docker_image, dockerfile=dockerfile, no_cache=False)


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


def _resolve_actor_fields(args: argparse.Namespace) -> dict:
    """Translate --agent / --sim into RunConfig actor_mode / cli_tool fields."""
    agent_name = getattr(args, "agent", "fake")
    sim_name = getattr(args, "sim", None) or agent_name
    actor_mode, cli_tool = _agent_name_to_runtime(agent_name)
    sim_actor_mode_val, sim_cli_tool_val = _agent_name_to_runtime(sim_name)
    # Only record a SIM override when it differs from the agent.
    resolved_sim = sim_actor_mode_val if sim_actor_mode_val != actor_mode else None
    resolved_sim_cli = sim_cli_tool_val if sim_cli_tool_val != cli_tool else None
    return {
        "actor_mode": actor_mode,
        "cli_tool": cli_tool or "codex",
        "sim_actor_mode": resolved_sim,
        "sim_cli_tool": resolved_sim_cli,
        "codex_model": getattr(args, "codex_model", "gpt-5.5"),
        "codex_effort": getattr(args, "codex_effort", "high"),
        "claude_model": getattr(args, "claude_model", ""),
        "claude_effort": getattr(args, "claude_effort", "high"),
    }


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
        adr_path=getattr(args, "adr", None),
        agent_model=getattr(args, "agent_model", ""),
        sim_model=getattr(args, "sim_model", ""),
        cli_reviewer=getattr(args, "cli_reviewer", "none"),
        max_cli_review_rounds=getattr(args, "max_cli_review_rounds", 3),
        **_resolve_actor_fields(args),
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
                agent_model=getattr(args, "agent_model", ""),
                openrouter_env_var=args.openrouter_env_var,
            )
        ),
        openrouter_env_var=args.openrouter_env_var,
        container_user=getattr(args, "container_user", None),
        docker_network=args.docker_network,
        http_proxy=args.http_proxy,
        https_proxy=args.https_proxy,
        no_proxy=args.no_proxy,
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


def _extract_flag_value(args: list[str], flag: str, default: str) -> str:
    """Find a `--flag value` or `--flag=value` pair in a passthrough arg list."""

    for i, item in enumerate(args):
        if item == flag and i + 1 < len(args):
            return args[i + 1]
        prefix = f"{flag}="
        if item.startswith(prefix):
            return item[len(prefix) :]
    return default


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


def _normalize_openrouter_slug(raw: str) -> str:
    """Normalize a user-typed model slug for opencode dispatch.

    Copy-paste from openrouter.ai/models gives `<vendor>/<model>` — prepend
    `openrouter/` so opencode can dispatch. Already-prefixed slugs
    (`openrouter/…`, `opencode/…`) are passed through unchanged.
    """
    slug = raw.strip()
    if not slug:
        return ""
    if "/" in slug and not slug.startswith(("openrouter/", "opencode/")):
        return f"openrouter/{slug}"
    return slug


def _fetch_openrouter_catalog() -> set[str] | None:
    """Return the set of model IDs from the OpenRouter catalog.

    Used to validate a user-typed slug against the live catalog before
    committing it as the run model. Returns None on any network or parse
    error — callers treat that as "catalog unavailable, skip validation".
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "contremaitre"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    ids: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        if isinstance(mid, str) and mid:
            ids.add(mid)
    return ids


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

    # TUI defaults agent to opencode for real runs (not fake)
    agent_name = _extract_flag_value(forwarded, "--agent", "opencode")
    sim_name = _extract_flag_value(forwarded, "--sim", "") or None

    repo_cache_raw = _extract_flag_value(forwarded, "--repo-cache", "")
    cache_path = (
        Path(repo_cache_raw).resolve() if repo_cache_raw else _default_cache_path(source_url)
    )
    try:
        _ensure_local_clone(cache_path=cache_path, source_url=source_url, base=base)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"contremaitre: {exc}", file=sys.stderr)
        return 1

    # Confirmation BEFORE subprocess spawn: once Textual attaches, stdin is owned
    # by the TUI and interactive prompts in the subprocess would block invisibly.
    agent_model = _extract_flag_value(forwarded, "--agent-model", "")
    sim_model = _extract_flag_value(forwarded, "--sim-model", "")

    if agent_name == "opencode" and not agent_model:
        try:
            agent_model = _pick_zen_model_interactive("agent")
        except KeyboardInterrupt:
            print("aborted", file=sys.stderr)
            return 130
        _set_flag_value(forwarded, "--agent-model", agent_model)

    sim_is_opencode = (sim_name or agent_name) == "opencode"
    if sim_is_opencode and not sim_model:
        try:
            sim_model = _pick_zen_model_interactive("sim", default=agent_model or None)
        except KeyboardInterrupt:
            print("aborted", file=sys.stderr)
            return 130
        _set_flag_value(forwarded, "--sim-model", sim_model)

    confirm_args = argparse.Namespace(
        agent=agent_name,
        sim=sim_name,
        agent_model=agent_model,
        sim_model=sim_model,
        openrouter_env_var=_extract_flag_value(
            forwarded, "--openrouter-env-var", "OPENROUTER_API_KEY"
        ),
        cli_reviewer=_extract_flag_value(forwarded, "--cli-reviewer", "auto"),
        base=base,
        publish_mode=_extract_flag_value(forwarded, "--publish-mode", PublishMode.STUB.value),
        allow_open_egress=("--allow-open-egress" in forwarded),
        docker_network=_extract_flag_value(forwarded, "--docker-network", "") or None,
        https_proxy=_extract_flag_value(forwarded, "--https-proxy", "") or None,
    )
    rc = _preflight_presence_check(confirm_args)
    if rc != 0:
        return rc

    try:
        if not _recap_and_confirm(confirm_args, source_url=source_url):
            print("aborted", file=sys.stderr)
            return 130
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130

    _maybe_provision_cli_egress(confirm_args)
    if confirm_args.docker_network:
        _set_flag_value(forwarded, "--docker-network", confirm_args.docker_network)
    if confirm_args.https_proxy:
        _set_flag_value(forwarded, "--https-proxy", confirm_args.https_proxy)

    if "--repo-cache" not in " ".join(forwarded):
        forwarded.extend(["--repo-cache", str(cache_path)])

    # Ensure --agent is explicit in forwarded so the subprocess sees it
    _set_flag_value(forwarded, "--agent", agent_name)

    run_cmd = [sys.executable, "-m", "contremaitre", "run", *forwarded]

    # TUI header labels + image check inputs
    agent_model = _extract_flag_value(forwarded, "--agent-model", "")
    sim_model = _extract_flag_value(forwarded, "--sim-model", "")
    cli_reviewer_choice = _extract_flag_value(forwarded, "--cli-reviewer", "auto")
    docker_image = _extract_flag_value(forwarded, "--docker-image", _DEFAULT_IMAGE)
    codex_model = _extract_flag_value(forwarded, "--codex-model", "gpt-5.5")
    codex_effort = _extract_flag_value(forwarded, "--codex-effort", "high")
    claude_model = _extract_flag_value(forwarded, "--claude-model", "")
    claude_effort = _extract_flag_value(forwarded, "--claude-effort", "high")
    agent_mode, agent_cli_tool = _agent_name_to_runtime(agent_name)
    sim_mode, sim_cli_tool = _agent_name_to_runtime(sim_name or agent_name)

    # Build the Docker image BEFORE spawning the subprocess. If the image is
    # missing or stale, building it takes ~3 min — the TUI's 30s discover
    # timeout would fire waiting for the run dir while the subprocess builds.
    rc = _ensure_image_for(
        docker_image=docker_image,
        actor_mode=agent_mode,
        sim_actor_mode=sim_mode if sim_mode != agent_mode else None,
    )
    if rc != 0:
        return rc
    _label_kw = dict(
        codex_model=codex_model,
        codex_effort=codex_effort,
        claude_model=claude_model,
        claude_effort=claude_effort,
    )
    return tui.spawn_and_attach(
        runs_root=runs_root,
        run_slug=slugify(run_slug),
        run_cmd=run_cmd,
        refresh_hz=args.refresh_hz,
        discover_timeout_s=args.discover_timeout,
        agent_model=ModelSpec.build(
            mode=agent_mode.value,
            opencode_model=agent_model,
            cli_tool=agent_cli_tool or "codex",
            **_label_kw,
        ),
        sim_model=ModelSpec.build(
            mode=sim_mode.value,
            opencode_model=sim_model,
            cli_tool=sim_cli_tool or "codex",
            **_label_kw,
        ),
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


def _eval_ab_cmd(args: argparse.Namespace) -> int:
    from .eval import cmd_ab

    return cmd_ab(
        project_root=_eval_project_root(),
        case_id=args.case_id,
        config_a=args.config_a,
        config_b=args.config_b,
        n=args.n,
        runs_root=args.runs_root.resolve(),
        report_only=args.report_only,
        open_report=args.open,
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
