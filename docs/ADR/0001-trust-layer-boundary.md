# ADR-0001: Separate JAMP Trust Layer into JAMP 2

- **Status:** Accepted
- **Date:** 2026-11-20
- **Decision owner:** JAMP project owner
- **Source baseline:** JAMP 1 main `d67ceb0d60818b6926a00ce10431b611ffa24970`

## Context

JAMP 1 accumulated several distinct concerns in one repository: evidence acquisition and verification, API/transport, model routing and execution, research experiments, qualification catalogs, and Dashboard behavior. Their coupling made it harder to audit whether a claim was supported by source evidence and whether a displayed status represented a verified runtime result.

The inventory identified a narrow Trust Layer core: MOEX ISS and CoinGecko verifiers, bounded verdict logic, and the Epistemic Contract. Other capabilities require redesign or are outside the Trust Layer boundary.

## Decision

Create a clean repository, `qwertymarketolog-lab/JAMP-2`, with no Git history copied from JAMP 1. Start with documentation and independent quality automation.

The first code migration is a separate PR limited to:
- `src/jamp2/domain/verdict.py`
- `src/jamp2/providers/moex.py`
- `src/jamp2/providers/coingecko.py`
- focused unit and provider contract tests.

Do not copy JAMP 1 `app.py`, `auth.py`, or `persistence.py`. Design API, authorization, and evidence storage anew from the approved domain contracts.

## Consequences

**Benefits:** explicit Trust Layer scope; clean Git history; no runtime dependency on JAMP 1; provider/domain behavior tested independently before API and storage.

**Costs:** integration behavior must be rebuilt; source components require isolated tests and provenance; old Dashboard labels cannot be treated as authoritative verdicts without a mapping; freshness, conflict policy, retention, and tenant roles remain design work.

## Alternatives rejected

1. Fork/clone JAMP 1 — carries unrelated history and mixed concerns.
2. Mass-copy runtime — preserves application/auth/persistence coupling.
3. Continue Trust Layer inside JAMP 1 — rejected in favor of a clean boundary.

## Invariants

- JAMP 1 remains unchanged as the historical source repository.
- Frozen Core `src/jamp/run.py` remains at blob `0fee0e1c5c1a1548361965ac51eacdeba62bfe8a` (`Δ=0`).
- No provider/API result becomes `VERIFIED` without required evidence.
- Unknown, unavailable, malformed, unsupported, or conflicting evidence fails closed.
