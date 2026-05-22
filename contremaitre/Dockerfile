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

FROM node:24-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        jq \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# opencode CLI.
RUN curl -fsSL https://opencode.ai/install | bash
ENV PATH="/root/.opencode/bin:${PATH}"

# mattpocock/skills installed globally so /improve-codebase-architecture
# is available to opencode regardless of which target repo is mounted.
RUN npx -y skills@latest add mattpocock/skills --all --global

WORKDIR /app
