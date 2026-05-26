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
    p.add_argument("--http-proxy", default=None, help="Optional HTTP_PROXY value passed by env name to containers")
    p.add_argument("--https-proxy", default=None, help="Optional HTTPS_PROXY value passed by env name to containers")
    p.add_argument("--no-proxy", default=None, help="Optional NO_PROXY value passed by env name to containers")
    p.add_argument(
        "--allow-open-egress",
        action="store_true",
        help="Allow opencode containers without explicit network/proxy policy",
    )
    p.add_argument("--skip-openrouter-key-check", action="store_true", help="Do not query OpenRouter key metadata")
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
        choices=["auto", "codex", "claude", "none"],
        default="auto",
        help=(
            "Optional local CLI reviewer run AFTER the Draft PR is published. "
            "Uses the operator's interactive subscription (claude/codex), not API. "
            "`auto` (default) detects what's installed and prompts when stdin is a TTY; "
            "`none` skips. The review is posted as a single comment on the PR."
        ),
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
        "--extra-reviewer-scenario",
        choices=["approved", "changes_requested", "needs_human", "malformed", "malformed_then_approved"],
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
    run_p.add_argument("--publish-mode", choices=[mode.value for mode in PublishMode], default=PublishMode.STUB.value)
    run_p.add_argument(
        "-y",
        "--yes",
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

    cleanup_p = sub.add_parser("cleanup", help="Prune stale containers + worktrees + dangling images")
    cleanup_p.add_argument("--runs-root", type=Path, default=Path(".contremaitre/runs"))
    cleanup_p.add_argument(
        "--dry-run", action="store_true", help="Report what would be removed without touching anything"
    )
    cleanup_p.add_argument(
        "--skip-images", action="store_true", help="Skip docker image prune (containers + worktrees only)"
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
    tui_run = tui_sub.add_parser("run", help="Spawn `contremaitre run` and attach the TUI to its run dir")
    tui_run.add_argument(
        "run_args",
        nargs=argparse.REMAINDER,
        help="Flags forwarded to `contremaitre run` (e.g. --actor opencode --repo /path …)",
    )
    tui_run.add_argument("--refresh-hz", type=float, default=5.0)
    tui_run.add_argument(
        "--discover-timeout", type=float, default=30.0, help="Seconds to wait for the spawned run to create its dir"
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
        if not _launch_screen(args=args, source_url=source_url, argv_for_explicit_check=sys.argv):
            print("aborted", file=sys.stderr)
            return 130
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130
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


def _launch_screen(
    *,
    args: argparse.Namespace,
    source_url: str,
    argv_for_explicit_check: list[str],
    forwarded_to_subprocess: list[str] | None = None,
) -> bool:
    """Unified pre-launch screen: run summary → model picker → Y/n.

    Returns True to proceed, False to abort. Auto-proceeds when --yes or
    stdin is not a TTY. Replaces the old _confirm_launch +
    _resolve_models_interactive pair, which split information across two
    disconnected prompts and rendered the model list twice.
    """

    if getattr(args, "yes", False) or not sys.stdin.isatty():
        return True

    agent_explicit = _has_flag_in(argv_for_explicit_check, "--agent-model")
    sim_explicit = _has_flag_in(argv_for_explicit_check, "--sim-model")
    extra_explicit = _has_flag_in(argv_for_explicit_check, "--extra-reviewer-model")

    publish = getattr(args, "publish_mode", "stub")
    cost = getattr(args, "max_cost_usd", None)
    wall = getattr(args, "max_wall_minutes", None)
    base = getattr(args, "base", "?")

    # ----- run summary -----
    print()
    print(_RULE)
    print(f"  {_b('contremaitre')}")
    print(_RULE)
    print(f"  target   {source_url}")
    print(f"  branch   {base}  {_d(f'({publish})')}")
    caps_parts = []
    if cost is not None:
        caps_parts.append(f"${cost}")
    if wall is not None:
        caps_parts.append(f"{wall}m wall")
    if caps_parts:
        print(f"  caps     {_d('  ·  '.join(caps_parts))}")

    # ----- model picker (single list, agent then SIM) -----
    if not (agent_explicit and sim_explicit and extra_explicit):
        free = _fetch_free_models()
        if free is None:
            print()
            print(_d("  (model catalog unavailable — using CLI defaults)"))
        else:
            print(_RULE)
            print(f"  free models  {_d('(OpenCode Zen — no key needed)')}")
            print()
            bare = (args.agent_model or "").rsplit("/", 1)[-1]
            candidates = {bare, f"{bare}-free"}
            default_idx = next((i for i, m in enumerate(free) if m["id"] in candidates), 0)
            width = len(str(len(free) - 1))
            for i, m in enumerate(free):
                marker = f"  {_d('← default')}" if i == default_idx else ""
                print(f"    {i:>{width}}  {m['id']}{marker}")
            print()

            def _pick_inline(role: str, current_idx: int) -> tuple[str, int]:
                default_id = free[current_idx]["id"]
                prompt = f"  {role:<6}[{current_idx} - {default_id}] " f"(Enter=accept, 0–{len(free) - 1}, q): "
                while True:
                    try:
                        reply = input(prompt).strip().lower()
                    except EOFError:
                        return f"opencode/{free[current_idx]['id']}", current_idx
                    if reply == "":
                        return f"opencode/{free[current_idx]['id']}", current_idx
                    if reply == "q":
                        raise KeyboardInterrupt
                    if reply.isdigit() and 0 <= int(reply) < len(free):
                        idx = int(reply)
                        return f"opencode/{free[idx]['id']}", idx
                    print(f"  enter a number 0–{len(free) - 1}, Enter, or q")

            agent_idx = default_idx
            if not agent_explicit:
                chosen_agent, agent_idx = _pick_inline("agent", default_idx)
                args.agent_model = chosen_agent
                if forwarded_to_subprocess is not None:
                    forwarded_to_subprocess.extend(["--agent-model", chosen_agent])

            sim_idx = agent_idx
            if not sim_explicit:
                chosen_sim, sim_idx = _pick_inline("sim", agent_idx)
                args.sim_model = chosen_sim
                if forwarded_to_subprocess is not None:
                    forwarded_to_subprocess.extend(["--sim-model", chosen_sim])

            # ----- optional extra reviewer (different model family) -----
            # Default suggestion: first model whose family differs from the
            # chosen SIM — Enter accepts it. `s` skips (extra is optional).
            # When family detection can't find a cross-family pick (unknown
            # families, or every listed model shares SIM's family), fall
            # back to the first model that isn't SIM itself — still useful
            # diversity, just not family-level. Skip-only fallback only
            # triggers when there's a single model in the catalog.
            if not extra_explicit:
                from .model_family import model_family

                sim_full = f"opencode/{free[sim_idx]['id']}"
                sim_fam = model_family(sim_full)
                suggested_idx: int | None = None
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
                if suggested_idx is not None:
                    suggested_id = free[suggested_idx]["id"]
                    extra_prompt = (
                        f"  extra [{suggested_idx} - {suggested_id}] " f"(Enter=accept, s=skip, 0–{len(free) - 1}, q): "
                    )
                else:
                    extra_prompt = f"  extra  (Enter=skip, 0–{len(free) - 1}, q): "
                while True:
                    try:
                        reply = input(extra_prompt).strip().lower()
                    except EOFError:
                        break
                    if reply == "":
                        if suggested_idx is not None:
                            chosen_extra = f"opencode/{free[suggested_idx]['id']}"
                            args.extra_reviewer_model = chosen_extra
                            if forwarded_to_subprocess is not None:
                                forwarded_to_subprocess.extend(["--extra-reviewer-model", chosen_extra])
                        break
                    if reply in ("s", "skip"):
                        break
                    if reply == "q":
                        raise KeyboardInterrupt
                    if reply.isdigit() and 0 <= int(reply) < len(free):
                        idx = int(reply)
                        chosen_extra = f"opencode/{free[idx]['id']}"
                        args.extra_reviewer_model = chosen_extra
                        if forwarded_to_subprocess is not None:
                            forwarded_to_subprocess.extend(["--extra-reviewer-model", chosen_extra])
                        break
                    print(f"  enter a number 0–{len(free) - 1}, Enter, s to skip, or q")

    # ----- CLI reviewer (post-publish, subscription-bound) -----
    # Detects `claude` / `codex` on PATH and asks which (if any) to run
    # after the Draft PR is published. The chosen tool's review is posted
    # as a single comment on the PR; it uses the operator's interactive
    # subscription rather than API credits.
    from . import cli_reviewer

    cli_reviewer_flag = getattr(args, "cli_reviewer", "auto")
    cli_reviewer_explicit = _has_flag_in(argv_for_explicit_check, "--cli-reviewer")
    if not cli_reviewer_explicit:
        available = cli_reviewer.detect_available()
        if available:
            print()
            chosen = cli_reviewer.resolve_choice(
                flag_value=cli_reviewer_flag,
                available=available,
                tty=True,
            )
        else:
            chosen = "none"
        args.cli_reviewer = chosen
        if forwarded_to_subprocess is not None and chosen != "auto":
            forwarded_to_subprocess.extend(["--cli-reviewer", chosen])

    # ----- confirm -----
    print()
    print(_RULE)
    print(f"  agent   {_b(args.agent_model)}")
    print(f"  sim     {_b(args.sim_model)}")
    extra_model = getattr(args, "extra_reviewer_model", None)
    if extra_model:
        print(f"  extra   {_b(extra_model)}")
    cli_reviewer_choice = getattr(args, "cli_reviewer", "none")
    if cli_reviewer_choice in ("codex", "claude"):
        print(f"  review  {_b(cli_reviewer_choice)}  {_d('(post-publish, subscription)')}")

    # ----- pre-flight ping -----
    # Free-tier Zen models occasionally land in the catalog while the
    # operator's per-model quota is already spent. Without this probe the
    # first agent turn burns the full 1800s timeout (or just hangs until
    # SIGTERM) while opencode retries a 429 internally. One tiny chat
    # completion catches it now and lets the operator pick another model.
    #
    # Zen-only: probing happens against `https://opencode.ai/zen/v1/chat/
    # completions`, which is the endpoint opencode itself uses for the
    # `opencode/*` prefix. Models with a different prefix are skipped.
    probe_targets: list[tuple[str, str]] = []
    seen_models: set[str] = set()
    for role, model in (
        ("agent", args.agent_model),
        ("sim", args.sim_model),
        ("extra", extra_model),
    ):
        if not model or not model.startswith("opencode/"):
            continue
        if model in seen_models:
            continue
        seen_models.add(model)
        probe_targets.append((role, model))
    quota_blockers: list[tuple[str, str]] = []
    if probe_targets:
        print()
        print(f"  {_d('pre-flight ping …')}")
        for role, model in probe_targets:
            status, detail = _probe_zen_model(model)
            short = model.rsplit("/", 1)[-1]
            if status == "ok":
                print(f"    {role:<6}  {_d(short)}  ✓")
            elif status == "quota_exhausted":
                print(f"    {role:<6}  {_b(short)}  ✗  free-tier quota exhausted")
                quota_blockers.append((role, model))
            else:
                # Network / unexpected status — log and move on. The in-run
                # fast-fail still catches a real quota error if this missed it.
                print(f"    {role:<6}  {_d(short)}  ?  {_d(detail or 'probe failed')}")

    print()
    if quota_blockers:
        print(f"  {_b('free-tier quota exhausted for:')}")
        for role, model in quota_blockers:
            print(f"    {role}  {model}")
        print(f"  {_d('try again later, or pick a different model with --' + 'agent-model/--sim-model.')}")
        print()
        try:
            reply = input("  proceed anyway? [y/N] ").strip().lower()
        except EOFError:
            return False
        print()
        return reply in ("y", "yes")

    print(f"  CONTREMAITRE WILL RUN AUTONOMOUSLY AND CREATE A DRAFT PR ON {source_url} — Ctrl-C to abort")
    print()
    try:
        reply = input("  proceed? [Y/n] ").strip().lower()
    except EOFError:
        return True
    print()
    return reply in ("", "y", "yes")


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
    """Auto-build a known contremaitre image before opencode-mode runs.

    Fires for `--actor opencode` and any image name that matches a shipped
    variant. Rebuilds when:
    - The image doesn't exist, OR
    - Its `contremaitre.dockerfile-sha256` label is missing or mismatches the
      current Dockerfile contents (catches "Dockerfile edited, image not
      rebuilt" — the failure mode that left python3/uv missing in the live
      image even after the Dockerfile was updated).

    Custom / third-party images are the operator's responsibility — preflight
    will surface a clean failure with the build hint.
    """

    if config.actor_mode != ActorMode.OPENCODE:
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
        return _build_image_inline(image_name=config.docker_image, dockerfile=dockerfile, no_cache=False)
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
        return _build_image_inline(image_name=config.docker_image, dockerfile=dockerfile, no_cache=False)
    actual_hash = inspect.stdout.strip()
    if actual_hash == expected_hash:
        return 0
    print(
        f"contremaitre: image {config.docker_image} stale "
        f"(label={actual_hash or '<missing>'}, dockerfile={expected_hash}) — rebuilding",
        file=sys.stderr,
    )
    return _build_image_inline(image_name=config.docker_image, dockerfile=dockerfile, no_cache=False)


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
            return item[len(prefix) :]
    return default


def _has_flag_in(argv: list[str], flag: str) -> bool:
    """True iff `--flag value` or `--flag=value` is present in argv."""

    prefix = f"{flag}="
    return any(item == flag or item.startswith(prefix) for item in argv)


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
    agent_model = _extract_flag_value(forwarded, "--agent-model", "openrouter/deepseek/deepseek-v4-flash")
    sim_model = _extract_flag_value(forwarded, "--sim-model", "openrouter/deepseek/deepseek-v4-flash")
    extra_reviewer_model = _extract_flag_value(forwarded, "--extra-reviewer-model", "") or None
    cli_reviewer_choice = _extract_flag_value(forwarded, "--cli-reviewer", "auto")
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
    )
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
