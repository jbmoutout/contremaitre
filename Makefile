# contremaitre — task runner
# Usage:  make [target] [VAR=value ...]
# Setup:  make doctor BASE=main FORK=git@...
# Run:    make run     BASE=main FORK=git@...        (TUI, interactive)
#         make run-log BASE=main FORK=git@...        (headless, CI)

# ── Required per run (no default) ────────────────────────────────────────────
# BASE  target branch             e.g. main
# FORK  push remote URL           e.g. git@github.com:you/repo.git

# ── Who drives each role ─────────────────────────────────────────────────────
# AGENT: claude | codex | opencode | fake
AGENT        := claude
# SIM:   claude | codex | opencode   (empty = same as AGENT)
SIM          :=
# CLI_REVIEWER: claude | codex | auto | none
CLI_REVIEWER := claude

# ── claude settings (when any role = claude) ─────────────────────────────────
CLAUDE_MODEL  :=         # empty = account default; e.g. opus | claude-opus-4-8
CLAUDE_EFFORT := high    # low | medium | high | max

# ── codex settings (when any role = codex) ───────────────────────────────────
CODEX_MODEL   := gpt-5.5
CODEX_EFFORT  := high    # minimal | low | medium | high | xhigh

# ── opencode model (only used when AGENT=opencode or SIM=opencode) ───────────
# Ignored for claude/codex roles.
# Leave empty → interactive picker shows live OpenCode free models at launch.
# Set to an OpenRouter slug to skip the picker, e.g. qwen/qwen3.7-max
#   (see https://openrouter.ai/models — requires OPENROUTER_API_KEY in .env)
AGENT_MODEL  :=
SIM_MODEL    :=          # empty = same as AGENT_MODEL (picker proposes it as default)

# ── Post-PR review loop ───────────────────────────────────────────────────────
MAX_CLI_REVIEW_ROUNDS := 3
MAX_REVIEW_ROUNDS     := 3

# ── Run limits ────────────────────────────────────────────────────────────────
PUBLISH_MODE := gh
MAX_TURNS    := 30
MAX_WALL_MIN := 45
MAX_COST_USD := 5

# ── Docker ────────────────────────────────────────────────────────────────────
# DOCKER_IMAGE: empty = default; contremaitre-agent-rust:latest for Rust repos
DOCKER_IMAGE :=
# CHECK_CMD: shell command that must pass before the PR is published.
#   Runs in a Docker sidecar scoped to changed files.
#   e.g. "npx tsc --noEmit"  |  "uv run pytest -q"  |  "cargo check --all-targets"
#   For multiple checks: CHECK_CMD="cmd1 && cmd2"
#   Leave empty to skip the L1 gate.
CHECK_CMD    :=
# ALLOW_OPEN_EGRESS: set non-empty to run with open egress (required for opencode).
# For CLI roles (claude/codex) this overrides the default locked egress — warned,
# since the in-container token is exfiltratable.
ALLOW_OPEN_EGRESS :=

# ── Cross-fork (uncomment + set when fork ≠ upstream) ────────────────────────
# UPSTREAM :=          # canonical read-only remote
# GH_REPO  :=          # owner/repo for gh pr create --repo

# ── Internal flag assembly ────────────────────────────────────────────────────
_image_flag    := $(if $(DOCKER_IMAGE),--docker-image $(DOCKER_IMAGE))
_check_flag    := $(if $(CHECK_CMD),--check-cmd "$(CHECK_CMD)")
_cmodel_flag   := $(if $(CLAUDE_MODEL),--claude-model $(CLAUDE_MODEL))
_amodel_flag   := $(if $(AGENT_MODEL),--agent-model $(AGENT_MODEL))
_smodel_flag   := $(if $(SIM_MODEL),--sim-model $(SIM_MODEL))
_sim_flag      := $(if $(SIM),--sim $(SIM))
_egress_flag   := $(if $(ALLOW_OPEN_EGRESS),--allow-open-egress)
_upstream_flag := $(if $(UPSTREAM),--upstream $(UPSTREAM))
_ghrepo_flag   := $(if $(GH_REPO),--gh-repo $(GH_REPO))

_run_flags = \
    --base $(BASE) --fork $(FORK) $(_upstream_flag) $(_ghrepo_flag) \
    --agent $(AGENT) $(_sim_flag) \
    $(_cmodel_flag) --claude-effort $(CLAUDE_EFFORT) \
    --codex-model $(CODEX_MODEL) --codex-effort $(CODEX_EFFORT) \
    $(_amodel_flag) $(_smodel_flag) \
    --cli-reviewer $(CLI_REVIEWER) \
    --max-cli-review-rounds $(MAX_CLI_REVIEW_ROUNDS) \
    --max-review-rounds $(MAX_REVIEW_ROUNDS) \
    --publish-mode $(PUBLISH_MODE) \
    $(_image_flag) $(_check_flag) $(_egress_flag) \
    --max-turns $(MAX_TURNS) --max-wall-minutes $(MAX_WALL_MIN) \
    --max-cost-usd $(MAX_COST_USD)

.PHONY: help run run-log doctor models lint install-hooks eval

help:
	@grep -E '^[a-zA-Z_-]+:' Makefile | sed 's/:.*//' | sort

run:
	@test -n "$(BASE)" || (echo "error: BASE required — make run BASE=main FORK=git@..."; exit 1)
	@test -n "$(FORK)" || (echo "error: FORK required — make run BASE=main FORK=git@..."; exit 1)
	GITHUB_TOKEN=$$(gh auth token) python3 -m contremaitre tui run -- $(_run_flags)

run-log:
	@test -n "$(BASE)" || (echo "error: BASE required"; exit 1)
	@test -n "$(FORK)" || (echo "error: FORK required"; exit 1)
	GITHUB_TOKEN=$$(gh auth token) python3 -m contremaitre run $(_run_flags)

doctor:
	@test -n "$(BASE)" || (echo "error: BASE required"; exit 1)
	@test -n "$(FORK)" || (echo "error: FORK required"; exit 1)
	GITHUB_TOKEN=$$(gh auth token) python3 -m contremaitre doctor \
	    --base $(BASE) --fork $(FORK) --agent $(AGENT) $(_sim_flag) $(_egress_flag)

models:
	python3 -m contremaitre models

lint:
	uvx ruff check --fix .
	uvx ruff format .

install-hooks:
	uvx pre-commit install

eval:
	uv run contremaitre eval all --n $(or $(n),3)

# ── Project shortcuts ─────────────────────────────────────────────────────────
# Copy + rename per target repo:
#
# my-repo:
#     $(MAKE) run BASE=main FORK=git@github.com:me/my-repo.git \
#             CHECK_CMD="npx tsc --noEmit"
#
# rust-repo:
#     $(MAKE) run BASE=main FORK=git@github.com:me/rust-repo.git \
#             DOCKER_IMAGE=contremaitre-agent-rust:latest \
#             CHECK_CMD="cargo check --all-targets"
