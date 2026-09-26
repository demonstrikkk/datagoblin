# 32 — JEV USAGE MAP (CORE decision layer: 4 families — GENERATE→ORCHESTRATE→JUDGE→EXECUTE→PROVE)

Jev = TypeSafe System One typed judgments (Choice/Score/Noul + probabilities, skills/jev), NOT prose generation, NOT a pipeline stage. Gemini/Groq GENERATE; LangGraph ORCHESTRATES the loop; Jev JUDGES at boundaries; Python EXECUTES; evidence PROVES. Jev outputs are probabilities with fallbacks — deterministic policy maps them → verified/unverified/conflicting (never "Jev says TRUE").

## A. Source screening (post-discovery, biggest saver)

Q: Is this source likely relevant to entity/fields? → YES / NO / UNCERTAIN. NO → skip fetch+extract entirely. `jev.source_screening()`.

## B. Evidence verification (post-extract, feeds Proof Drawer)

Q: Does this quote support this claim? → SUPPORTED (show "$15M — SUPPORTED 0.97") / NOT_SUPPORTED / UNCERTAIN. Deterministic quote⊂text pre-check runs first; Jev judges semantics. `jev.evidence_verification()`.

## C. Conflict resolution (bounded A/B/C/D)

Q: Which evidence state? → A (source A better) / B / CONFLICT (genuine — mark CONFLICTING, never merge) / INSUFFICIENT. `jev.conflict_triage()`. Extra search only on INSUFFICIENT.

## D. Research continuation (inside LangGraph loop)

Q: Coverage state? → sufficient → FETCH / insufficient → REFINE / uncertain → REVIEW-broader-search. LangGraph moves state; Jev judges; Runner executes ≤3 iterations. `jev.research_continuation()` (+ legacy alias `coverage_gate`).

Forbidden: planning, extraction prose, validation math, dedupe, normalization, export, transitions, budgets. Key absent → deterministic stubs.
