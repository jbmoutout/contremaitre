# Glossary — `improve-codebase-architecture` skill

Vocabulary used by the [improve-codebase-architecture](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture) skill, drawn from its `SKILL.md` and `LANGUAGE.md`. The skill insists these exact terms be used rather than common substitutes like "component," "service," "API," or "boundary."

## Core architectural vocabulary

- **Module** — Anything that has an interface and an implementation. Scale-agnostic: a function, class, package, or a slice spanning tiers all count. (Avoid "unit," "component," "service.")
- **Interface** — Everything a caller must know to use the module correctly. Broader than a type signature: it also includes invariants, ordering constraints, error modes, required configuration, and performance characteristics. (Avoid "API" or "signature.")
- **Implementation** — The code inside a module; its body. Distinct from an adapter — you can have a small adapter with a large implementation (a Postgres repo) or a large adapter with a small implementation (an in-memory fake).
- **Depth** — Leverage at the interface: how much behaviour a caller or test can exercise per unit of interface they must learn. **Deep** = a lot of behaviour behind a small interface. **Shallow** = interface nearly as complex as the implementation. Depth is a property of the interface, not the implementation.
- **Seam** — A point where you can swap one implementation for another, so behaviour changes by *substitution* rather than by editing code in place. It's also where the interface lives, and where tests plug in. (Borrowed from Michael Feathers. Use this, not "boundary.")
- **Adapter** — A concrete thing that satisfies an interface at a seam. Describes a role (what slot it fills), not substance (what's inside).
- **Leverage** — What callers get from depth: more capability per unit of interface they must learn. One implementation pays back across many call sites and tests.
- **Locality** — What maintainers get from depth: change, bugs, knowledge, and verification concentrate in one place. Fix once, fixed everywhere.
- **Deepening opportunity** — The skill's core output: a refactor that turns a shallow module into a deep one, for better testability and AI-navigability.

## Principles and tests

- **Deletion test** — Imagine deleting the module. If complexity vanishes, it was a pass-through. If complexity reappears scattered across N callers, it was earning its keep.
- **The interface is the test surface** — Callers and tests cross the same seam. If you want to test *past* the interface, the module is the wrong shape.
- **One adapter = hypothetical seam; two adapters = real seam** — Don't introduce a seam unless something actually varies across it.
- **Internal seam vs. external seam** — A module can have internal seams (private to its implementation, used by its own tests) as well as the external seam at its interface.

## Workflow / process terms

- **CONTEXT.md** — The project's domain glossary. Used for *domain* language (e.g. "the Order intake module," not "the FooBarHandler").
- **LANGUAGE.md** — The file holding the architectural vocabulary above. Used for *architecture* language.
- **ADR (Architecture Decision Record)** — Records of past decisions in `docs/adr/`. The skill reads them so it doesn't re-litigate settled decisions, and offers to write a new one when a candidate is rejected for a load-bearing reason.
- **Grilling loop** — The interactive phase after a candidate is picked: walking the design tree over constraints, dependencies, the shape of the deepened module, what sits behind the seam, and which tests survive.
- **Recommendation strength** — A badge on each candidate: `Strong`, `Worth exploring`, or `Speculative`.
- **Before/after visualisation** — A required side-by-side diagram for each candidate, showing the shallowness and the proposed deepening.

## Rejected framings

- **Depth as a ratio of implementation-lines to interface-lines** (Ousterhout) — rejected because it rewards padding the implementation; the skill uses depth-as-leverage instead.
- **"Interface" as only TypeScript's `interface` keyword or a class's public methods** — too narrow.
- **"Boundary"** — overloaded with DDD's bounded context. Say *seam* or *interface*.