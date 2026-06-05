# Contremaitre Context

Contremaitre is a deterministic host control plane for architecture-agent PR runs. This file captures project-specific language used by agents when changing the codebase.

## Language

**Agent event interpretation**:
Interpreting opencode agent `tool_use` events into run artifact signals such as settled design, implementation completion, self-verification, and phase counts.
_Avoid_: TUI phase parsing, flow-use parsing, artifact helper
