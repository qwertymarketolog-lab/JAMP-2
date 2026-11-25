# J2-003 — Live E2E evidence specification

- State: implementation proposal on branch; not yet CI-verified
- Base: `4e44229065abdf9f33336ceda589c30c50b10936`
- Scope: real provider acquisition, immutable PostgreSQL persistence, API readback, and baseline replay
- Explicitly out of scope: ADR-0009 Replay v1 conformance

## 1. Purpose

J2-003 proves that the existing JAMP-2 API can acquire current source responses
from the real CoinGecko and MOEX endpoints, preserve the exact response bytes,
persist Evidence and Verdict records in PostgreSQL, and read those records back
through the chain-verifying API.

CoinGecko is the positive scenario. A conclusive result may be
`VERIFIED` or `CONTRADICTED`: both prove that a real observation was acquired
and evaluated. `INCONCLUSIVE`, `OUT_OF_SCOPE`, a missing response, a provider
HTTP error, or absent required evidence fails the CoinGecko positive scenario.

MOEX is deliberately separate. It must capture at least one real HTTP 200
response and persist its raw bytes, while the verdict remains `INCONCLUSIVE`
because the securities endpoint's `SYSTIME` contract and required market
calendar qualification are not established. It must never be upgraded to
`VERIFIED` by this test.

## 2. What this milestone proves

- A real outbound HTTPS request to the fixed provider host.
- Provider HTTP status and exact raw response bytes are captured.
- SHA-256 is independently recomputed from the PostgreSQL `raw_body_bytes`.
- Evidence rows are persisted and linked to the same `claim_id`.
- Verdict row is persisted with the same `claim_id` and exact `evidence_ids`.
- Direct PostgreSQL SELECT results agree with the API response.
- Evidence and Verdict API read paths succeed. These read paths validate the
  complete append-only hash chains and cross-record Evidence references.
- CoinGecko's current baseline Replay returns the same status as the recorded
  verdict and reports zero network calls. This is only a baseline regression
  check of the currently implemented replay function.

## 3. What this milestone does not prove

J2-003 is not Replay v1 conformance and does not satisfy ADR-0009. In particular,
it does not prove:

- historical freshness evaluated against original `decided_at_utc`;
- immutable Policy Snapshot bytes or their digest;
- complete policy parameter binding;
- wall-clock independence;
- the Replay v1 scenario matrix;
- production readiness, multi-tenant isolation, or public SaaS safety.

The CoinGecko test checks that the upstream timestamp and server `fetched_at`
were captured. It does not calculate historical age or declare historical
freshness. The MOEX test preserves unknown timestamp/calendar facts and does
not infer them.

## 4. Isolation and fail-closed execution

- `.github/workflows/live-e2e.yml` is `workflow_dispatch` only.
- `confirm_live` must equal the literal `yes`; otherwise the workflow fails.
- The workflow runs only the selected target and defaults to no automatic
  execution on push or pull request.
- Tests are marked `live` and are skipped by ordinary CI unless
  `JAMP_RUN_LIVE=1` is explicitly set.
- The API runs locally on the Actions runner against an ephemeral PostgreSQL 16
  service. The test client uses local TCP HTTP, not ASGITransport.
- The provider network guard allows HTTPS only to `api.coingecko.com` and
  `iss.moex.com`. API and PostgreSQL traffic remain local.
- A selected live scenario is never converted to a skip because a provider is
  unavailable. DNS, rate limit, timeout, non-200 provider status, missing
  evidence, or a failed assertion makes the scenario FAIL.

The workflow uses a run-scoped API key for this ephemeral test service. It is
not a production credential.

## 5. Artifact contract

Each scenario writes one JSON artifact, including on test failure where the
test process reached its finalizer. Artifacts bind to `GITHUB_SHA` and
`GITHUB_RUN_ID`.

CoinGecko artifact: `coingecko_live_e2e.json`.
MOEX artifact: `moex_live_e2e.json`.

For every persisted Evidence row, include:

- `evidence_id`, `claim_id`, provider and exact request URL;
- provider HTTP status;
- exact raw body as base64, byte size, and independently checked SHA-256;
- `fetched_at`, `provider_observed_at` when known, and `captured_at_utc`;
- body completeness and adapter version;
- record parent hash and record hash.

For Verdict, include `verdict_id`, `claim_id`, status, ordered
`evidence_ids`, policy/code versions, and record hash. Also include API
readback, baseline replay where applicable, test outcome, and diagnostic
failure details.

Raw response bytes are included because a digest alone cannot reconstruct or
independently inspect the captured source response.

## 6. Acceptance criteria

### CoinGecko

1. Real provider response is HTTP 200.
2. At least one Evidence row is returned by the API and found by PostgreSQL
   SELECT; every row belongs to the returned `claim_id`.
3. Every raw body is complete and its recomputed SHA-256 equals `raw_sha256`.
4. Upstream `last_updated_at` and server `fetched_at` are both present.
5. Recorded verdict is `VERIFIED` or `CONTRADICTED`, never silently accepted
   as `INCONCLUSIVE`.
6. PostgreSQL Verdict SELECT matches API `claim_id`, status, and ordered
   `evidence_ids`.
7. Evidence and Verdict API reads succeed and thereby verify the full ledger
   hash chains and Evidence references.
8. Baseline Replay returns the original status and `network_calls=0`.

### MOEX

1. At least one real MOEX provider response is HTTP 200 and saved as complete
   Evidence in PostgreSQL.
2. Every saved raw body has an independently recomputed matching SHA-256.
3. Verdict is exactly `INCONCLUSIVE`; reason codes are retained.
4. PostgreSQL Verdict SELECT matches the API's Claim and Evidence references.
5. Evidence and Verdict API reads verify the ledger chains.

### Workflow evidence

- Both scenario JSON files are uploaded when `target=both`, including failure
  diagnostics where generated.
- A terminal workflow Run and Job must conclude successfully.
- The artifact must be downloaded and its SHA-256 calculated independently.
- PR CI does not replace this manual Live E2E run. Post-merge CI is a separate
  requirement and is not implied by this workflow.

## 7. Completion semantics

J2-003 may be marked CLOSED only after all selected mandatory scenarios pass,
both artifacts are inspected, the terminal Run -> Job -> Artifact chain is
verified, and artifact SHA-256 values are recorded. A failed or missing live
scenario leaves J2-003 HOLD/FAILED for investigation. A successful J2-003 run
does not close ADR-0009 or authorize Replay v1 claims.
