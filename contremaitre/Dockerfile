# Contremaitre runtime image.
#
# Generic opencode-in-Docker base. No target codebase baked in — the
# orchestrator mounts the per-run worktree at /app (read-write for the
# agent, read-only for the SIM) at run time. /app/node_modules is
# overlaid with a lockhash-keyed named volume populated once per
# lockfile by contremaitre.runtime_image.ensure_deps_volume.
#
# Built via `contremaitre image build`. The Dockerfile ships inside the
# package so it's available after pip install.

FROM node:24-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        jq \
        ca-certificates \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Python project tooling. uv lands at /root/.local/bin/uv; poetry via pip
# (Debian's python3-pip refuses system-site installs without the
# --break-system-packages escape hatch). Both are needed by
# runtime_image.ensure_deps_volume's install one-shot and by agents
# driving `uv run …` / `poetry run …` inside the worktree.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    pip3 install --break-system-packages --no-cache-dir poetry
ENV PATH="/root/.local/bin:${PATH}"

# opencode CLI.
RUN curl -fsSL https://opencode.ai/install | bash
ENV PATH="/root/.opencode/bin:${PATH}"

# Frontier CLIs for ActorMode.CLI (run codex/claude headless AS agent/SIM on the
# operator's subscription) + ripgrep, which both CLIs reach for by default
# (without it codex falls back to slower grep).
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm i -g @openai/codex @anthropic-ai/claude-code

# mattpocock/skills installed globally so /improve-codebase-architecture
# is available to opencode regardless of which target repo is mounted.
RUN npx -y skills@latest add mattpocock/skills --all --global

WORKDIR /app
