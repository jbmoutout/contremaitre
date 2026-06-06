# contremaitre task runner
#
# Install just: brew install just
# List recipes:  just            (or `just --list`)

set shell := ["bash", "-uc"]

# Stable defaults — override per-recipe or on the CLI:
#   just max_cost_usd=10 my-repo
# `base` and `fork` are intentionally not defaulted: the operator must
# state both every run. Contremaitre clones the target lazily into
# `~/.cache/contremaitre/<host>-<owner>-<repo>/` and fetches
# `origin/<base>` fresh, so the operator never needs a parallel local
# checkout — only the URL.
#
# Runtime, models, and reasoning effort live in defaults.toml + the launch
# picker now — not here. `docker_image` stays because the `rust` preset pins it.
publish_mode    := "gh"
max_turns       := "30"
max_wall_min    := "45"
max_cost_usd    := "5"
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
#
# Runtime (codex / opencode), models, and reasoning effort come from the
# launch-screen pickers and ~/.config|.contremaitre/defaults.toml — not flags
# here. `--allow-open-egress` runs containers on open egress: for opencode it
# satisfies the network policy; for codex it overrides the default egress lock
# (so the agent can install deps) — drop it for a locked codex run.
tui-run base fork check_cmd="":
    GITHUB_TOKEN=$(gh auth token) python3 -m contremaitre tui run -- \
        --base {{base}} \
        --fork {{fork}} \
        {{ if check_cmd != "" { "--check-cmd " + quote(check_cmd) } else { "" } }} \
        --publish-mode {{publish_mode}} \
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
# Then: `just my-repo`.

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
