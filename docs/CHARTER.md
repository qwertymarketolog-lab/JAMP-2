# JAMP 2 — Charter

**Status:** APPROVED  
**Product:** JAMP 2 — Trust Layer  
**Source repository:** `qwertymarketolog-lab/JAMP`  
**Inventory baseline:** `main` at `d67ceb0d60818b6926a00ce10431b611ffa24970`

## Mission

JAMP 2 is an evidence infrastructure layer. It evaluates bounded factual claims against inspectable source observations and preserves the evidence and decision trail.

**Proof over confidence. AI searches; JAMP verifies.**

## Product boundary

v1 is limited to:
1. Accepting a bounded factual claim.
2. Determining whether it is within a supported provider's scope.
3. Acquiring and preserving raw source observations.
4. Verifying entity, field, value, and source-specific comparison rules.
5. Producing an explicit bounded verdict and uncertainty state.
6. Persisting evidence/verdict history for independent inspection.
7. Exposing results through a minimal API and Dashboard.

Initial providers: MOEX ISS and CoinGecko. Provider coverage is narrow and explicit; no universal source hierarchy or freshness guarantee is implied.

## Epistemic contract

- Provider adapter statuses (`E4_VERIFIED`, `E4_CONTRADICTED`, `E4_OUT_OF_SCOPE`, `E4_INCONCLUSIVE`) are not claim verdicts.
- Claim-level verdicts (`VERIFIED`, `CONTRADICTED`, `INCONCLUSIVE`) are not aggregate verdicts.
- Aggregate statuses include `VERIFIED`, `CONTRADICTED`, `INCONCLUSIVE`, and `PARTIAL`.
- Persisted Dashboard labels (`CLEAN`, `SUSPICIOUS`, `HALLUCINATION_CAUGHT`, `INCONCLUSIVE`, `INTEGRITY_VIOLATED`) are a separate vocabulary. A complete mapping from aggregate verdicts to persisted labels was not established in JAMP 1.
- A digest proves byte integrity relative to a digest, not source truth or freshness.
- HTTP success is transport evidence, not semantic verification.
- Unknown, unsupported, unavailable, malformed, or out-of-scope evidence must not become contradiction.
- Conflicting observations remain visible; do not silently select a winner.

Observed JAMP 1 defaults: MOEX price tolerance 5% (`0.05` relative fraction); CoinGecko price tolerance 10%; market-cap tolerance 20%. These are source-specific migration baselines, not universal constants.

## Required states

JAMP 2 must distinguish `VERIFIED`, `CONTRADICTED`, `INCONCLUSIVE`, `OUT_OF_SCOPE`, `PARTIAL`, `AMBIGUOUS`, and `UNKNOWN`. Exact API vocabulary and transitions require contract tests before implementation.

## Fail-closed behavior

Absent, malformed, conflicting, unavailable, or unsupported evidence cannot produce a positive verification. Internal exceptions, empty evidence sets, and HTTP success must never be promoted to `VERIFIED`.

## Non-goals for v1

- AnyModel catalog, model routing/qualification, Task Router, model execution orchestration.
- Telegram or other messaging transports before identity, roles, tenant authorization, and audit ownership are designed.
- Formal-state research and unrelated Research Loop experiments.
- General-purpose chat, autonomous agent execution, or claims of detecting all hallucinations.

## Architecture principles

1. Evidence before verdict; every verdict links to observations.
2. Raw before derived; preserve response bytes and digest before normalization.
3. Reproducibility: record provider, request context, timestamps where available, parser/policy version, and code version.
4. Bounded claims: every verdict declares scope and limitations.
5. Separate domain, acquisition, verification, persistence, API, and UI.
6. Preserve conflicts; return `AMBIGUOUS`/HOLD when policy is absent.
7. Start as a minimal modular monolith.
8. Architectural decisions require an ADR and impact analysis (modules, data, API, security, legal/privacy, compatibility).

## First vertical slice — Definition of Done

`claim → provider → raw observation → integrity metadata → source-specific comparison → bounded verdict → append-only audit record → API → Dashboard`

Complete only when unit, contract, and integration tests cover valid, contradictory, out-of-scope, unavailable, malformed, and boundary cases; raw bytes/digest can be independently checked; verdicts link to evidence and policy versions; missing/conflicting evidence fails closed; and no synthetic score/status is used.

## Repository boundary and governance

JAMP 1 remains the historical source repository. JAMP 2 receives reviewed, minimal components with source path and blob identity, migration rationale, focused tests, known limitations, and explicit acceptance. No mass copy and no deletion from JAMP 1.

JAMP 1 Frozen Core `src/jamp/run.py` remains at blob `0fee0e1c5c1a1548361965ac51eacdeba62bfe8a` (`Δ=0`).
