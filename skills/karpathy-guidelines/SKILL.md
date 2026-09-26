# karpathy-guidelines (vendored 2026-09-25)

Source: https://github.com/multica-ai/andrej-karpathy-skills (CLAUDE.md + skills/karpathy-guidelines/SKILL.md),
derived from Andrej Karpathy's observations on LLM coding pitfalls. MIT. Applied to every DATAGOBLIN change below.

## 1. Think Before Coding — Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing: state assumptions explicitly; present multiple interpretations, don't pick silently;
push back when a simpler approach exists; stop and name what's unclear.
> Applied: audit findings stated with evidence first (§audit); pushback recorded where scope exceeds need.

## 2. Simplicity First — Minimum code that solves the problem. Nothing speculative.

No features beyond asked; no single-use abstractions; no unrequested configurability;
no error handling for impossible scenarios. "Would a senior engineer call this overcomplicated?"
> Applied: LangChain wired at exactly one seam (supervisor structured output); traversal bounded, not a crawler framework.

## 3. Surgical Changes — Touch only what you must. Clean up only your own mess.

No drive-by refactors; match existing style; remove only imports/functions YOUR changes orphan.
> Applied: edits limited to listed files; orphan sweep after every change.

## 4. Goal-Driven Execution — Define success criteria. Loop until verified.

"Write tests for invalid inputs, then make them pass." Multi-step plan states `verify:` per step.
> Applied: plan below carries verify per step; loop compile → tests → sweep until green.
