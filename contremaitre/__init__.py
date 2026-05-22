"""Contremaitre — deterministic control plane for architecture-agent PR runs.

The orchestration layer (state machine, caps, diff scan, hard gates, publisher
boundary) is deterministic Python. LLM/opencode execution lives in pluggable
actor adapters; git, GitHub, and credential boundaries stay host-owned.
"""

__all__ = ["__version__"]

__version__ = "0.1.1"
