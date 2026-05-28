# Smoke cases — fake-actor integration scaffolds

These three cases (`case_01_happy_path`, `case_02_malformed_retry`, `case_03_forbidden_path`) were the first attempt at a v0 eval canary. They run with the **fake actor** ([fake_actor.py](../contremaitre/fake_actor.py)) which hand-writes the same implementation every invocation regardless of agent prompts or models. **They are integration tests of the orchestrator state machine, not evals.**

They are kept here for two reasons:
1. **Fast smoke**: ~5 seconds for n=3, no docker, no models, no spend. Useful to catch state-machine / hard-gate / manifest regressions in pre-commit CI without paying for an opencode run.
2. **Negative-path coverage**: `case_03_forbidden_path` exercises diff-scan's L0 hard gate by writing a `.env` file. That path is genuinely worth canarying.

## Why they're not under `golden_cases/`

`golden_cases/` is reserved for **real-mode evals**: opencode actor + real prompts + real models + real cli_reviewer against pinned targets. Those measure emergent agent/reviewer behavior. Fake-mode cases cannot — the fake agent ignores prompts and models entirely.

See [`golden_cases/README.md`](../golden_cases/README.md) for the actual eval canary.

## Wiring

These cases are not picked up by `contremaitre eval` (which reads `golden_cases/`). They're intentionally orphaned scaffolds — if we want fast pre-commit integration coverage later, the right home is `tests/integration/` driven by pytest, not a parallel CLI subcommand.

## Schema (legacy)

```toml
id = "case_01_happy_path"
description = "..."

[actor]
mode = "fake"
agent_scenario = "normal"           # menu in fake_actor.py
sim_scenario = "approved"

[expected]
allowed_terminals = [...]
must_pass_gates = [...]
```

This schema is **incompatible** with the current `eval.py` — keeping it here for archival reference only.
