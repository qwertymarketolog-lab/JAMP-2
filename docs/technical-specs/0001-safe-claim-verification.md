# Technical Specification 0001 — Safe Claim Verification and Evidence Persistence

**Status:** READY FOR IMPLEMENTATION REVIEW  
**Governing decision:** ADR-0002 (accepted 2026-11-20)  
**Target:** Rework PR #3 (`feat/postgres-ledger-api`) without merging it  
**Baseline:** `main` = `d0ab1b521c8ed6772387e994d693c160cc59ba5e`  
**Current PR #3 head at authoring:** `f060d4d5424e3c874539aee3eb08f7ee8e8a389d`  
**Frozen Core:** JAMP 1 is out of scope; no files in `qwertymarketolog-lab/JAMP` may be changed.

This specification is an implementation contract, not evidence that the behavior already exists. The current PR #3 is not mergeable under this target.

## 1. Objective

Replace client-authored evidence ingestion with a bounded, authenticated, server-side verification flow:

`authenticated Claim → normalization → provider resolution → server-side source fetch → immutable EvidenceRecord(s) → versioned VerdictRecord → append-only PostgreSQL ledger → deterministic replay/read API`.

The caller submits a claim, never a claimed source response, raw body, digest, verdict, provider URL, or capture timestamp. JAMP owns acquisition, parsing, digest calculation, verdict calculation, and persistence.

The first vertical slice remains limited to supported price claims and the existing MOEX ISS / CoinGecko adapters. This is not a general web fact-checker and does not claim universal hallucination detection.

## 2. Current gap that this specification closes

Observed in PR #3 at `f060d4d`:

- `POST /v1/evidence` accepts client-supplied provider, URL, raw bytes, raw digest, capture time, parsed value, and verdict status.
- Recomputing a digest can establish consistency of submitted bytes and digest; it cannot establish that JAMP fetched those bytes from the named source.
- The route has no authentication boundary.
- `Claim` currently requires ticker and is therefore coupled to one provider's identifier model.
- Evidence stores `verdict_status` in the same record; a separate immutable verdict with policy/code versions and evidence references is absent.
- The canonicalizer uses Python `json.dumps` with sorted keys and Unicode NFC. It is not RFC 8785 JCS.
- The latest observed PR #3 CI run failed during Pytest collection; see §10.

These are contract gaps, not reasons to weaken tests or accept a synthetic result.

## 3. Explicit non-goals

Do not add in this change:

- LLM extraction, model routing, AnyModel, Telegram, autonomous agents, or general-purpose chat;
- public multi-tenant SaaS, user management, billing, Dashboard, webhooks, or deployment;
- arbitrary URL fetching, arbitrary provider plugins, or user-defined provider endpoints;
- universal source hierarchy, universal freshness thresholds, or a claim that a digest proves truth;
- external chain anchoring, Ed25519, or protection against a privileged database administrator rewriting and re-hashing the database;
- changes to JAMP 1 or its Frozen Core.

## 4. Domain and request contract

### 4.1 Provider-independent Claim

The core Claim must not require `ticker`, `currency`, or a provider name as universal mandatory fields. It must represent:

- `claim_id`: server-generated stable identifier;
- `text`: original bounded statement;
- `subject`: typed subject, at minimum `kind` and `identifier`;
- `predicate`: supported fact being checked (initially `price`);
- `expected_value`: finite positive numeric value for price;
- `unit`: explicit currency/unit (initially USD for CoinGecko; MOEX currency handling must follow the existing adapter's supported semantics and must not be guessed);
- `submitted_at_utc`: server timestamp, not client assertion.

Provider-specific identifiers belong in a resolver/adapter mapping, not as mandatory Claim fields. Unsupported subject/predicate/unit combinations return `OUT_OF_SCOPE` or `INCONCLUSIVE`; never infer a provider or currency silently.

### 4.2 API

Replace `POST /v1/evidence` with:

- `POST /v1/claims/verify`: accepts only the Claim request; fetches from a configured supported provider server-side; persists the raw observation(s) and a separate verdict; returns their identifiers and bounded result.
- `GET /v1/verdicts/{verdict_id}`: returns the immutable verdict and linked evidence metadata.
- `GET /v1/evidence/{evidence_id}`: returns the stored evidence record, including exact raw bytes (base64) and integrity metadata.
- `POST /v1/verdicts/{verdict_id}/replay`: re-evaluates the stored evidence using the exact recorded policy and canonicalization versions; it MUST NOT call a live provider.

Exact Pydantic request/response schemas, field limits, and status-code table must be implemented as contract tests before endpoint code is considered complete. Unknown fields are rejected. No endpoint accepts a caller-supplied verdict or EvidenceRecord.

### 4.3 Provider acquisition boundary

- Provider selection is deterministic from supported Claim subject/predicate semantics.
- Only fixed, code-owned MOEX ISS and CoinGecko base URLs/endpoints may be called. User input must never become an arbitrary URL or hostname.
- Preserve exact response body bytes before parsing; calculate SHA-256 on the server from those bytes.
- Store requested URL, provider identifier, HTTP status, capture timestamp (server clock), source timestamp/trade date when supplied, and body completeness separately.
- A successful HTTP response is not a verified claim. Parsing, entity matching, field matching, unit/currency matching, and source-specific comparison must all succeed.
- Existing synchronous adapters must not block the async event loop; use an explicit threadpool boundary or make the adapter async with focused tests.
- Timeout, malformed payload, missing required field, source unavailability, or unsupported coverage fails closed.

## 5. Authentication and exposure

Initial pilot is single-tenant and not a public SaaS launch.

- All verification and read/replay endpoints require an API key from `JAMP2_API_KEY`.
- Compare credentials using constant-time comparison (`hmac.compare_digest`); never log or return the key.
- Missing server configuration must fail closed (service unavailable/not configured); it must never disable authentication.
- Missing or invalid caller credentials return `401` with a stable error code and no source fetch or database write.
- Tests may inject a test key through dependency/config injection; no hard-coded production key.
- No public deployment is authorized by this specification. Any later external exposure requires TLS termination, rate limiting, key rotation/revocation, tenant isolation, and a separate threat review.

## 6. Immutable records and versioned integrity

### 6.1 EvidenceRecord

Evidence is a source observation, not a verdict. It contains at minimum:

- evidence ID, claim ID, provider ID, fixed request URL/context;
- raw response bytes and server-computed SHA-256;
- HTTP status and body-completeness flag;
- server capture timestamp and source timestamp/trade date (nullable, distinct fields);
- parsed observation as derived data;
- `canonicalization_version`, parser version, and provider adapter version.

Do not store `verdict_status` as the authoritative verdict field on EvidenceRecord.

### 6.2 VerdictRecord

A separate append-only immutable record contains:

- verdict ID and claim ID;
- verdict status: `VERIFIED`, `CONTRADICTED`, `INCONCLUSIVE`, `OUT_OF_SCOPE`, or `AMBIGUOUS`;
- ordered evidence IDs used by the decision;
- policy identifier and immutable `policy_version`;
- application `code_version` (commit SHA or immutable build identifier);
- canonicalization version and decision timestamp;
- machine-readable reason codes and bounded explanation.

A verdict must not exist without at least one linked evidence record, except an explicit pre-acquisition refusal which must be represented as a refusal/attempt record and must never be mislabeled VERIFIED.

Multiple verdicts may reference the same EvidenceRecords when policy versions differ; prior verdicts are never overwritten.

### 6.3 Canonicalization

- Freeze the currently observed algorithm under an explicit identifier, e.g. `jamp-json-nfc-v1`: recursively NFC-normalize string values and object keys; reject key collisions after normalization; serialize UTF-8 JSON with sorted keys, no insignificant whitespace, `ensure_ascii=False`, and `allow_nan=False`.
- The hash preimage must be an explicit versioned envelope. Include record type, canonicalization version, all immutable semantic fields, and parent hash where chain-linked. Raw bytes are represented losslessly (base64) or included through a separately verified raw-byte digest; the exact preimage must be documented and tested against independent vectors.
- Do not call this RFC 8785/JCS. Python float formatting is part of this version's compatibility contract; if cross-language numeric canonicalization is required, introduce a new version and migration rather than silently changing v1.
- Every record stores its canonicalization version. Replay dispatches to the exact historical canonicalizer.
- Since PR #3 is unmerged, adjust its initial migration/schema before merge. If any non-test data already exists in an environment, stop and produce a forward-only migration plan; never silently rewrite hashes or drop records.

## 7. Verdict semantics and conflicts

For one supported provider observation:

| Provider result | Claim verdict | Required condition |
|---|---|---|
| `E4_VERIFIED` | `VERIFIED` | Entity, field, unit and source-specific comparison all match. |
| `E4_CONTRADICTED` | `CONTRADICTED` | Usable source evidence directly contradicts the bounded expected value under the named policy. |
| `E4_OUT_OF_SCOPE` | `OUT_OF_SCOPE` | Provider explicitly does not cover this claim. |
| `E4_INCONCLUSIVE` or acquisition/parse failure | `INCONCLUSIVE` | Evidence cannot establish either truth or contradiction. |

Conflict rule for any future multi-source or multi-observation decision:

- preserve every observation;
- if two independently usable observations for the same normalized fact disagree and no approved domain policy resolves the disagreement, return `AMBIGUOUS`;
- do not average values, choose the newest source, or prefer one provider without an explicit policy;
- unavailable/out-of-scope evidence is not a contradictory observation;
- a conflict must remain inspectable in the Evidence Ledger and replay output.

The first implementation must test the conflict reducer with controlled evidence fixtures, but must not pretend that the initial single-provider route already performs cross-provider consensus.

## 8. Freshness and timestamps

- Preserve `captured_at_utc` (server time) separately from `source_timestamp` / trade date (source-provided).
- Never trust a client-supplied capture time.
- Do not add a universal max-age threshold. For the initial price pilot, expose source timestamp/trade date and mark freshness as `UNKNOWN` unless an explicit provider-specific policy defines it.
- A stale or missing source timestamp cannot be promoted to fresh/VERIFIED by inference.

## 9. PostgreSQL Ledger, idempotency, and replay

- Keep append-only DB protections and full chain verification.
- Persist EvidenceRecord and VerdictRecord in separate tables with foreign-key linkage; each record is immutable at the database layer.
- Serialize appends transactionally; concurrent requests must not create forks or duplicate idempotency results.
- Add an idempotency key scoped to the single pilot API key. Same key + same normalized request returns the original result; same key + different request returns `409 IDEMPOTENCY_KEY_REUSED`. Persist the request digest and result IDs atomically.
- Replay reads only stored records. It verifies raw-byte digests, record hashes, parent links, evidence references, and policy/code/canonicalization versions before recalculating the verdict. Missing policy artifact or unsupported historical version returns `INCONCLUSIVE`/replay unavailable, never a substitute current policy.
- Keep the existing `PostgresLedger` public seam where practical; API must depend on repository/service protocols, not SQLAlchemy models or SQL queries.

## 10. CI and verification contract

Current observed state at authoring:

- PR #3 head: `f060d4d5424e3c874539aee3eb08f7ee8e8a389d`.
- Latest PR #3 workflow: `JAMP 2 CI`, Run `44608939658`, Job `135860031664`, conclusion `failure`; Pytest failed during collection, so no tests executed.
- PR #3 must not be considered green from older successful runs.
- Main workflow currently accepts Pytest exit code 5 when zero tests are collected. This bootstrap exception must be removed; zero collected tests is a failure.
- PR #3 workflow includes PostgreSQL 16 and migration-before-tests, but the current terminal run still fails at collection.

Required terminal evidence for the reworked PR:

1. PostgreSQL 16 service healthy and migration succeeds.
2. Ruff succeeds.
3. Mypy succeeds.
4. Pytest executes a non-zero, explicitly reported test count and succeeds; exit code 5 is failure.
5. Required tests include auth/no-side-effects, invalid Claim, provider resolver allowlist, provider fetch capture/hash, all E4→verdict mappings, conflict→AMBIGUOUS, DB immutability, persistence/restart, concurrent/idempotent requests, chain tampering, evidence/verdict linkage, and deterministic replay without network access.
6. Upload machine-readable test report and relevant verification evidence as workflow artifacts. Missing artifacts are reported as missing evidence, not silently treated as PASS.
7. After any eventual merge (not authorized here), verify a separate post-merge main run.

No threshold reduction, skipped tests, disabled Ruff/Mypy, or accepted empty test suite is allowed.

## 11. Implementation sequence and change boundaries

Rework PR #3 in dependency order; keep one integration owner:

1. **P0 — restore a runnable baseline:** fix the FastAPI route response-model collection error; remove the zero-test CI success exception; prove a non-zero test suite runs on PostgreSQL 16.
2. **P1 — close the trust boundary:** Claim request schema, server-side provider resolver/fetch, no arbitrary URLs, API-key authentication, server-owned capture metadata, raw-byte capture and digest.
3. **P2 — separate immutable domain records:** EvidenceRecord and VerdictRecord, policy/code/canonicalization versioning, forward-only schema migration, explicit verdict truth table.
4. **P3 — replay and operational correctness:** deterministic no-network replay, idempotency, conflict preservation, DB tampering/immutability, concurrent append.
5. **P4 — independent verification:** inspect final diff; run Ruff/Mypy/Pytest against PostgreSQL 16; upload artifacts; verify Frozen Core remains untouched by confirming the JAMP 1 repository was not modified.

Do not add Dashboard, external multi-tenant access, or other killer features to PR #3. Those are separate future slices after this trust contract is proven.

## 12. Acceptance criteria / Definition of Done

The reworked PR is ready for review only when:

- the only public write input is a bounded Claim; no client can submit source evidence or a verdict;
- every source observation is fetched by a fixed server-side adapter and stored as raw bytes with server-computed digest;
- API authentication fails closed and invalid credentials cause no fetch/write side effects;
- Claim is provider-independent at the domain boundary;
- EvidenceRecord and VerdictRecord are separate, immutable, versioned, and linked;
- every verdict references the exact evidence and policy/code versions used;
- conflict is preserved as AMBIGUOUS; unknown/out-of-scope/unavailable never becomes VERIFIED or CONTRADICTED by default;
- canonicalization version and hash preimage have independent test vectors; no RFC 8785 claim is made;
- replay uses only stored evidence and the recorded policy/canonicalization version;
- PostgreSQL 16 integration tests cover persistence, concurrency, immutability, tampering, idempotency, and replay;
- CI terminal evidence proves Ruff, Mypy, and a non-zero Pytest suite; artifacts are inspectable;
- PR #3 remains unmerged until a separate owner decision after review.

**Owner authorization boundary:** This specification authorizes preparation and review of the technical contract only. Implementation commits, PR #3 updates, and merge remain separate execution steps. The broad project authorization does not waive CI evidence or the no-merge rule.
