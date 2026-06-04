# contremaitre task runner
#
# Install just: brew install just
# List recipes:  just            (or `just --list`)

set shell := ["bash", "-uc"]

# Stable defaults — override per-recipe or on the CLI:
#   just agent_model=openrouter/anthropic/claude-sonnet-4.6 my-repo
# `base` and `fork` are intentionally not defaulted: the operator must
# state both every run. Contremaitre clones the target lazily into
# `~/.cache/contremaitre/<host>-<owner>-<repo>/` and fetches
# `origin/<base>` fresh, so the operator never needs a parallel local
# checkout — only the URL.
publish_mode    := "gh"
actor           := "opencode"
max_turns       := "20"
max_wall_min    := "45"
max_cost_usd    := "5"
# Models — unset by default; CLI provides its own defaults. Use a preset
# (e.g. `deepdeep`) to pin a model pair, or override on the CLI:
#   just agent_model=openrouter/anthropic/claude-sonnet-4.6 my-repo
agent_model     := ""
sim_model       := ""
docker_image    := ""   # e.g. "contremaitre-agent-rust:latest"; empty → CLI default

# Default recipe: show available recipes
default:
    @just --list

# Run ruff (lint + auto-fix, then format). Catches trailing whitespace /
# missing EOF newlines and PEP 8 blank-line spacing so `git diff --check`
# and the CI format gate don't fail in review.
lint:
    uvx ruff check --fix .
    uvx ruff format .

# Install the local git pre-commit hook (runs ruff before each commit).
# One-time per clone.
install-hooks:
    uvx pre-commit install

# Generic TUI run. Required: base, fork. Optional: check_cmd (no default —
# pass an ecosystem-appropriate check or omit to skip L1 gating entirely).
#   just tui-run main git@github.com:me/foo.git "npx tsc --noEmit"
#   just tui-run main git@github.com:me/foo.git "poetry run pytest -q"
#   just tui-run main git@github.com:me/foo.git   # no check
tui-run base fork check_cmd="":
    GITHUB_TOKEN=$(gh auth token) python3 -m contremaitre tui run -- \
        --actor {{actor}} \
        --base {{base}} \
        --fork {{fork}} \
        {{ if check_cmd != "" { "--check-cmd " + quote(check_cmd) } else { "" } }} \
        --publish-mode {{publish_mode}} \
        {{ if agent_model != "" { "--agent-model " + agent_model } else { "" } }} \
        {{ if sim_model != "" { "--sim-model " + sim_model } else { "" } }} \
        {{ if docker_image != "" { "--docker-image " + docker_image } else { "" } }} \
        --allow-open-egress \
        --max-turns {{max_turns}} \
        --max-wall-minutes {{max_wall_min}} \
        --max-cost-usd {{max_cost_usd}}

# Example: copy + rename per target you run against often, e.g.
#
#   my-repo:
#       @just tui-run main git@github.com:<you>/my-repo.git "npx tsc --noEmit"
#
# Then: `just my-repo`  (or `just deepdeep my-repo` to pin models).

# === Model presets ============================================================
# Presets wrap any recipe with a pinned (agent_model, sim_model) pair. Compose:
#   just deepdeep tui-run main git@github.com:me/foo.git
# Add more presets (e.g. claude-claude, gpt-claude) by copying the pattern.

# Preset: deepseek-v4-flash for both agent + sim (cheap, fast).
deepdeep target *args:
    @just agent_model="openrouter/deepseek/deepseek-v4-flash" \
          sim_model="openrouter/deepseek/deepseek-v4-flash" \
          {{target}} {{args}}

# === Runtime-image presets ====================================================
# These wrap any recipe with --docker-image so the Rust-capable image
# is used. Build the image first: `contremaitre image build --variant rust`

# Use the Rust-capable image (contremaitre-agent-rust:latest).
# Example:
#   just rust tui-run main git@github.com:me/rust-repo.git "cargo check --all-targets"
rust target *args:
    @just docker_image="contremaitre-agent-rust:latest" {{target}} {{args}}

# === Eval canary ==============================================================
# v0 regression canary. Real opencode mode against pinned target+SHA. See
# golden_cases/README.md.

# Run every golden case n=3 and compare to its baseline. Exits non-zero on any
# regression. Manual trigger — invoke after a prompt edit OR a model swap
# (never both, per EVAL_ROADMAP §5). One case × n=3 takes ~45min on opencode.
eval n="3":
    uv run contremaitre eval all --n {{n}}

# Single-case run + compare. Default: the sqlite-utils canary.
#   just eval-one case_01_sqlite_utils_8f0c06e 3
eval-one case n="3":
    uv run contremaitre eval run {{case}} --n {{n}}
    uv run contremaitre eval compare {{case}} --n {{n}}
