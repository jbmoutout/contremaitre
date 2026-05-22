"""Command-line interface for Contremaitre."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .envfile import load_dotenv_defaults
from .fixture import init_fixture
from .models import ActorMode, Caps, PublishMode, RunConfig
from .orchestrator import run
from .paths import slugify
from .preflight import run_preflight


_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_DOCKERFILE = _PACKAGE_DIR / "Dockerfile"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contremaitre",
        description="Deterministic control plane for architecture-agent PR runs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the WORK + REVIEW loop")
    run_p.add_argument("--repo", required=True, type=Path, help="Local source checkout used for git worktree add")
    run_p.add_argument("--fork", default=None, help="Push remote for the run branch. Required for --publish-mode gh.")
    run_p.add_argument("--upstream", default=None, help="Canonical (read-only) remote, mounted as `upstream`.")
    run_p.add_argument("--base", default="main", help="Base branch for worktree and diff")
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
    run_p.add_argument("--runs-root", type=Path, default=Path(".contremaitre/runs"))
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
    run_p.add_argument("--docker-image", default="contremaitre-agent:latest")
    run_p.add_argument("--opencode-config", type=Path, default=None)
    run_p.add_argument("--openrouter-env-var", default="OPENROUTER_API_KEY")
    run_p.add_argument("--container-user", default=None, help="Optional docker --user value, e.g. $(id -u):$(id -g)")
    run_p.add_argument("--docker-network", default=None, help="Optional docker --network value")
    run_p.add_argument("--http-proxy", default=None, help="Optional HTTP_PROXY value passed by env name to containers")
    run_p.add_argument("--https-proxy", default=None, help="Optional HTTPS_PROXY value passed by env name to containers")
    run_p.add_argument("--no-proxy", default=None, help="Optional NO_PROXY value passed by env name to containers")
    run_p.add_argument("--skip-preflight", action="store_true", help="Bypass operational preflight checks")
    run_p.add_argument("--allow-open-egress", action="store_true", help="Allow opencode containers without explicit network/proxy policy")
    run_p.add_argument("--skip-openrouter-key-check", action="store_true", help="Do not query OpenRouter key metadata")
    run_p.add_argument("--allow-unlimited-openrouter-key", action="store_true", help="Allow OpenRouter keys with no provider-side credit limit")
    run_p.add_argument("--openrouter-key-url", default="https://openrouter.ai/api/v1/key")
    run_p.add_argument("--agent-timeout-seconds", type=int, default=1800)
    run_p.add_argument("--sim-timeout-seconds", type=int, default=900)
    run_p.add_argument("--gh-repo", default=None, help="Optional owner/repo for gh pr create --repo")
    run_p.add_argument("--pr-title", default=None)
    run_p.add_argument("--pr-body", default=None)
    run_p.add_argument("--max-turns", type=int, default=30)
    run_p.add_argument("--max-wall-minutes", type=int, default=180)
    run_p.add_argument("--max-cost-usd", type=float, default=30.0)
    run_p.add_argument("--no-progress-turns", type=int, default=5)
    run_p.add_argument("--malformed-verdict-retries", type=int, default=2)
    run_p.add_argument("--max-review-rounds", type=int, default=3)
    run_p.set_defaults(func=_run_cmd)

    doctor_p = sub.add_parser("doctor", help="Validate live-run operational prerequisites")
    doctor_p.add_argument("--repo", required=True, type=Path, help="Local source checkout used for git worktree add")
    doctor_p.add_argument("--base", default="main")
    doctor_p.add_argument("--actor", choices=[mode.value for mode in ActorMode], default=ActorMode.OPENCODE.value)
    doctor_p.add_argument("--runs-root", type=Path, default=Path(".contremaitre/runs"))
    doctor_p.add_argument("--run-slug", default="doctor")
    doctor_p.add_argument("--docker-image", default="contremaitre-agent:latest")
    doctor_p.add_argument("--opencode-config", type=Path, default=None)
    doctor_p.add_argument("--openrouter-env-var", default="OPENROUTER_API_KEY")
    doctor_p.add_argument("--docker-network", default=None)
    doctor_p.add_argument("--http-proxy", default=None)
    doctor_p.add_argument("--https-proxy", default=None)
    doctor_p.add_argument("--no-proxy", default=None)
    doctor_p.add_argument("--allow-open-egress", action="store_true")
    doctor_p.add_argument("--skip-openrouter-key-check", action="store_true")
    doctor_p.add_argument("--allow-unlimited-openrouter-key", action="store_true")
    doctor_p.add_argument("--openrouter-key-url", default="https://openrouter.ai/api/v1/key")
    doctor_p.add_argument("--max-cost-usd", type=float, default=30.0)
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
    image_build.add_argument("--image-name", default="contremaitre-agent:latest", help="Tag for the built image")
    image_build.add_argument(
        "--dockerfile",
        type=Path,
        default=None,
        help=f"Override Dockerfile path (default: {_DEFAULT_DOCKERFILE})",
    )
    image_build.add_argument("--no-cache", action="store_true")
    image_build.set_defaults(func=_image_build_cmd)

    return parser


def _run_cmd(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    result = run(config)
    print(f"{result.verdict.value}: {result.reason}")
    print(f"run_dir={result.run_dir}")
    if result.pr_created:
        return 0
    return 2 if result.verdict.value.startswith("NO_PR") else 1


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
        opencode_config=args.opencode_config.resolve() if args.opencode_config else None,
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


def _image_build_cmd(args: argparse.Namespace) -> int:
    dockerfile = (args.dockerfile or _DEFAULT_DOCKERFILE).resolve()
    if not dockerfile.exists():
        print(f"contremaitre: Dockerfile not found: {dockerfile}", file=sys.stderr)
        return 1
    contents = dockerfile.read_text(encoding="utf-8")
    # Stream the Dockerfile via stdin so docker build has no host-side
    # build context — the image is self-contained (no COPY directives).
    # `docker build -` reads the Dockerfile directly from stdin.
    cmd = ["docker", "build", "-t", args.image_name]
    if args.no_cache:
        cmd.append("--no-cache")
    cmd.append("-")
    print(f"contremaitre: building {args.image_name} from {dockerfile}", file=sys.stderr)
    try:
        proc = subprocess.run(cmd, input=contents.encode("utf-8"), check=False)
    except FileNotFoundError:
        print("contremaitre: docker binary not found in PATH", file=sys.stderr)
        return 1
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    load_dotenv_defaults()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"contremaitre: {exc}", file=sys.stderr)
        return 1
