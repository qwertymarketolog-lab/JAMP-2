# JAMP 2 — Migration Ledger

**Status:** APPROVED — inventory and migration decisions accepted; no source code migrated.  
**Source:** `qwertymarketolog-lab/JAMP`  
**Inventory baseline:** `main` = `d67ceb0d60818b6926a00ce10431b611ffa24970`  
**PR #629:** merged; head `f5a5722749a8197cc0743d6e84dcf75492e5afcb`; merge/main `d67ceb0d60818b6926a00ce10431b611ffa24970`.

## 1. Decision

Create a clean JAMP 2 repository with no JAMP 1 Git history. JAMP 1 remains unchanged as the historical source and research archive.

- **KEEP:** viable bounded components; still require isolated JAMP 2 tests.
- **ADAPT:** useful capability requiring redesign due to coupling, API, persistence, security, or UI.
- **ARCHIVE:** retain in JAMP 1; exclude from initial JAMP 2 product.
- **REJECT:** explicitly excluded from JAMP 2 v1; not a deletion instruction for JAMP 1.

## 2. Verified source baseline

PR #629 introduced the approved Epistemic Contract:
- Contract blob: `ff820c27316444b8722cffd9fad86810085b01e3`.
- `config/status_definitions.json` blob: `767294c2efda3be0a7096fdab3e932748f1e756b`.

Post-merge CI recorded for `d67ceb0d60818b6926a00ce10431b611ffa24970`:

| Workflow | Run ID | Job ID | Conclusion | Artifact |
|---|---:|---:|---|---|
| Research Loop v0 Gate (main) | 44594191651 | 135807950541 | SUCCESS | 14565940326 |
| G1 Trusted Lineage (main) | 44594191980 | 135807951806 | SUCCESS | 14565505728 |
| Run Pytest | 44594191561 | 135807949963 | SUCCESS | 14564507619 |
| SBOM | 44594191670 | 135807950404 | SUCCESS | 14564567620 |
| Developer Quality | 44594191671 | 135807950581 | SUCCESS | none |

Developer Quality Docker Smoke was skipped. Known orphaned `import-historical.yml` run with `failure` and `jobs=[]` is not treated as a required gate.

Frozen Core `src/jamp/run.py` matched locked blob `0fee0e1c5c1a1548361965ac51eacdeba62bfe8a` at this baseline.

## 3. KEEP — first migration candidates

| Source | Observed blob | Target | Rationale / acceptance conditions |
|---|---|---|---|
| `src/jamp/provider/moex_verify.py` | `492595d62e178709957e7fbe346f4aa76497b290` | `src/jamp2/providers/moex.py` | Bounded MOEX ISS verifier; test source contract, raw bytes, units, and 5% relative tolerance. |
| `src/jamp/provider/coingecko_verify.py` | `5a1667c4594a2e00b0ae6215832ecc2e17f2bdc4` | `src/jamp2/providers/coingecko.py` | Bounded existence/price/market-cap verifier; preserve 10%/20% source-specific thresholds and limitations. |
| `src/jamp/runtime/verdict.py` | `791094d9de30cebf4656a4b134b15f12fa985667` | `src/jamp2/domain/verdict.py` | Port bounded verdict parsing/aggregation; test status parsing, precedence, empty and critical/non-critical cases. |
| `docs/EPISTEMIC_CONTRACT.md` | `ff820c27316444b8722cffd9fad86810085b01e3` | `docs/EPISTEMIC_CONTRACT.md` | Preserve approved contract baseline; amend only with change control. |

## 4. ADAPT

| Source area | Required JAMP 2 action |
|---|---|
| FastAPI app (`src/jamp/api/app.py`) | Design a minimal API from JAMP 2 domain contracts; do not copy the monolith. |
| Auth / organization context | Define identity, roles, tenant isolation, key lifecycle, and audit ownership before external exposure. |
| Persistence / PostgreSQL | Design append-only evidence/verdict records, raw-body storage, retention, and transaction semantics anew. |
| Claim extraction / E4 adapter | Keep extraction separate from verification; map into explicit JAMP 2 domain types. |
| Dashboard | Rebuild minimal UI; unknown values neutral; no ungrounded status mapping. |
| Docker / package config | Recreate only what the new architecture needs; no Redis without a justified requirement. |

## 5. ARCHIVE

- `src/jamp/runtime/router.py`: Task/Capability Router and model-selection logic (not the API 501 stub).
- AnyModel/OpenRouter/Groq/fallback execution providers.
- 87-model catalogs, matrices, crosswalks, probes, qualification and result datasets.
- Model probe/qualification and unrelated research-loop workflows.
- `research/`, `experiments/`, `track_a/`, `benchmarks/`, `artifacts/`, `data/`.

## 6. REJECT — excluded from JAMP 2 v1

- `src/jamp/api/router.py`: `/chat` and `/execute` return HTTP 501; not a functioning Trust Layer capability.
- `src/jamp/transport/telegram.py`, `telegram_updates.py`, `telegram_webhook.py`: exclude until identity, roles, tenant authorization, webhook trust, and audit responsibility are designed.
- Chat/execute/attachment DTOs and Telegram/OpenRouter wiring.

P31 provenance remains **AMBIGUOUS / UNKNOWN**: historical audit notes conflict; file presence in JAMP 1 main does not prove a specific P31 commit lineage or CI chain.

## 7. Approved sequence

1. Create clean JAMP 2 repository with documentation only.
2. Add minimal Python metadata and independent CI.
3. Verify documentation-only baseline on GitHub Actions.
4. First code PR contains only `verdict.py`, `moex.py`, `coingecko.py`, and focused tests.
5. Do not import JAMP 1 `app.py`, `auth.py`, or `persistence.py`; design API and storage anew.
6. Implement evidence ledger and API vertical slice after provider/domain contracts pass.

## 8. Open design questions

Persisted status mapping; evidence storage and retention; source quote time vs capture time; tenant roles; provider outage policy; conflict policy; minimal Dashboard scope. These are not blockers for repository bootstrap, but must be resolved before the corresponding features ship.

## 9. Audit conclusion

**OBSERVED:** baseline commit, source blob identities, and recorded post-merge CI are listed above.  
**INFERRED:** MOEX, CoinGecko, verdict logic, and epistemic contract are the strongest first migration candidates.  
**UNKNOWN / HOLD:** complete persisted Dashboard mapping, universal freshness, global source precedence, and production tenant/role semantics are not established.

**STATE:** `AUTHORING AUTHORIZED / DOCUMENTATION + BOOTSTRAP CI FIRST / CODE MIGRATION DEFERRED TO FIRST PR`.
