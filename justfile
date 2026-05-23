# contremaitre task runner
#
# Install just: brew install just
# List recipes:  just            (or `just --list`)

set shell := ["bash", "-uc"]

# Stable defaults — override per-recipe or on the CLI:
#   just agent_model=openrouter/anthropic/claude-sonnet-4.6 my-repo
# `base` is intentionally not defaulted — the operator must state the
# branch every run, and contremaitre fetches `origin/<base>` fresh
# rather than trusting the source repo's local ref.
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

# Default recipe: show available recipes
default:
    @just --list

# Generic TUI run. Required: repo, base, fork. Optional: check_cmd (default: tsc).
#   just tui-run ~/code/foo main git@github.com:me/foo.git "pnpm typecheck"
tui-run repo base fork check_cmd="npx tsc --noEmit":
    GITHUB_TOKEN=$(gh auth token) python3 -m contremaitre tui run -- \
        --actor {{actor}} \
        --repo {{repo}} \
        --base {{base}} \
        --fork {{fork}} \
        --check-cmd {{quote(check_cmd)}} \
        --publish-mode {{publish_mode}} \
        {{ if agent_model != "" { "--agent-model " + agent_model } else { "" } }} \
        {{ if sim_model != "" { "--sim-model " + sim_model } else { "" } }} \
        --allow-open-egress \
        --max-turns {{max_turns}} \
        --max-wall-minutes {{max_wall_min}} \
        --max-cost-usd {{max_cost_usd}}

# Example: copy + rename per target you run against often, e.g.
#
#   my-repo:
#       @just tui-run ~/code/my-repo main git@github.com:<you>/my-repo.git "npx tsc --noEmit"
#
# Then: `just my-repo`  (or `just deepdeep my-repo` to pin models).

# === Model presets ============================================================
# Presets wrap any recipe with a pinned (agent_model, sim_model) pair. Compose:
#   just deepdeep tui-run ~/code/foo git@github.com:me/foo.git
# Add more presets (e.g. claude-claude, gpt-claude) by copying the pattern.

# Preset: deepseek-v4-flash for both agent + sim (cheap, fast).
deepdeep target *args:
    @just agent_model="openrouter/deepseek/deepseek-v4-flash" \
          sim_model="openrouter/deepseek/deepseek-v4-flash" \
          {{target}} {{args}}
