# ADR-0002: Target Architecture for the JAMP 2 Trust Layer

- **Status:** ACCEPTED — approved by project owner on 2026-11-20
- **Date:** 2026-11-20
- **Decision owner:** JAMP project owner
- **Supersedes:** None
- **Related:** ADR-0001, `docs/CHARTER.md`, `docs/EPISTEMIC_CONTRACT.md`, `docs/MIGRATION_LEDGER.md`
- **Baseline:** `main` at `d0ab1b521c8ed6772387e994d693c160cc59ba5e`

## 1. Context

ADR-0001 established a clean JAMP 2 repository and a narrow Trust Layer boundary. The independent JAMP → JAMP 2 audit recommends keeping that direction but adjusting the implementation sequence and the API, Claim, Evidence, and Verdict boundaries.

The current first persistence/API slice (PR #3) exposes `POST /v1/evidence` with client-supplied provider metadata, capture time, raw bytes, parsed value, and verdict status. A matching client-supplied SHA-256 proves only that the submitted bytes match the submitted digest. It does not prove that JAMP acquired the response from the claimed provider, that the timestamp is authentic, or that the parsed value and verdict follow the approved policy.

This ADR records the proposed target contract before further implementation. It does not authorize source-code changes or PR #3 merge.

## 2. Decision summary

Adopt the following target flow for the first complete product vertical slice:

```text
User / Client
    |
    v
Text or Explicit Claim
    |
    v
Claim Intake / Normalization
    |
    v
Verification Orchestrator
    |
    +---- Provider Adapter (MOEX)
    |
    +---- Provider Adapter (CoinGecko)
    |
    v
Raw Provider Observation
    |
    v
Evidence Record (append-only)
    |
    v
Verdict Engine + Versioned Policy
    |
    v
Verdict Record (append-only, references Evidence IDs)
    |
    v
Integrity Check / Deterministic Replay
    |
    v
Versioned API
    |
    v
Minimal Dashboard
```

The Trust Layer, not an untrusted client, owns provider acquisition, validation, verdict production, and persistence of the resulting evidence trail.

## 3. Architectural decisions

### 3.1 Claim intake and extraction boundary

1. JAMP 2 must accept an explicitly constructed, typed Claim.
2. Text-to-Claim extraction is **ADAPT**, not a capability inside the trusted verification core. It may be provided by a separate extractor component or service.
3. Extractor output is an untrusted proposal. It is not evidence, is not a verdict, and must not be promoted to a verified Claim without validation/acceptance at the Claim intake boundary.
4. Extraction must be independently replaceable and must not introduce a runtime dependency on AnyModel, the JAMP 1 Task Router, Telegram, or model qualification catalogs.
5. The first vertical slice may begin with an explicit Claim. Text extraction is a subsequent input adapter, not a prerequisite for proving provider acquisition and verification.

### 3.2 Provider-independent Claim

1. The core Claim identity and lifecycle must not require provider-specific fields such as `ticker` or `currency`.
2. Claim must express a bounded assertion and its typed subject/field/value/context.
3. Provider-specific request parameters belong in typed claim payloads or provider mapping/adapters, not as mandatory universal fields.
4. Do not over-generalize the first implementation. The initial supported claim type may remain price-related, provided the domain boundary permits additional claim types without rewriting the Claim identity/lifecycle contract.
5. Unsupported claim types must fail closed as unsupported/out-of-scope, never as contradicted.

### 3.3 Provider acquisition and evidence trust boundary

1. For externally exposed verification, the server-side Provider adapter obtains the source response itself.
2. Client-supplied raw evidence must never be treated as provider-authenticated evidence merely because its digest matches.
3. The existing `POST /v1/evidence` ingestion route must not be exposed as a public client operation. It must either be removed from the public router or restricted to an explicitly trusted internal boundary with authentication and authorization. The exact implementation is deferred to a separately approved change.
4. Provider adapters capture raw response bytes before parsing, calculate the digest server-side, and retain source/request metadata and source timestamps where available.
5. Integrity, source identity/authority, semantic correctness, and freshness are separate properties. A digest proves byte integrity relative to the digest; it does not prove source truth or freshness.
6. Provider outages, malformed responses, missing values, unsupported scope, and conflicting observations must remain explicit non-positive outcomes.

### 3.4 Evidence Record and Verdict Record are separate

**Evidence Record** represents an immutable observation. It should preserve, at minimum:
- stable evidence identifier;
- Claim identifier or correlation reference;
- provider/source identity and request context;
- raw response bytes (or a content-addressed reference to them);
- server-computed raw-body digest;
- capture timestamp;
- source timestamp/quote time where supplied by the source;
- parsed representation as a derived field, not a replacement for raw bytes;
- canonicalization version and record/hash format version;
- parent-chain reference where the append-only chain is used.

**Verdict Record** represents a decision made from one or more Evidence Records. It should preserve, at minimum:
- stable verdict identifier and Claim identifier;
- outcome and explicit reasons/limitations;
- referenced Evidence Record identifiers;
- policy/rule-set version;
- application/code version;
- decision timestamp;
- replay/derivation metadata sufficient to reproduce the decision.

Evidence and Verdict records must be append-only. A new decision under a changed policy creates a new Verdict Record; it must not overwrite the previous decision.

### 3.5 Deterministic replay and independent verification

1. Replay means recomputing the verdict from the stored raw evidence and the recorded policy/rule version; it does not mean silently re-querying a live provider.
2. Integrity verification checks raw bytes, digests, canonical records, and chain linkage independently from semantic replay.
3. Replay must identify when the required policy/code implementation is unavailable. It must return an explicit unreproducible/unknown result rather than fabricate a matching verdict.
4. The API must make the distinction visible between original verdict, integrity verification result, and replay result.

### 3.6 Canonicalization and versioning

1. Every persisted Evidence Record must identify its canonicalization/hash-format version.
2. The initial version must freeze the exact canonicalization algorithm, Unicode normalization, timestamp representation, field inclusion/order, and hash preimage. It must not be described as RFC 8785 unless it actually conforms to that standard.
3. Any future canonicalization change requires a forward migration and a compatibility plan that preserves verification of historical records.
4. The initial version identifier and exact algorithm are implementation details to be specified and tested before the first production migration is merged.
5. Existing PR #3 migration must not be treated as production-final until this contract is resolved.

### 3.7 Verdict vocabulary and conflicts

1. Provider E4 statuses, claim-level verdicts, aggregate verdicts, and Dashboard labels remain distinct vocabularies as defined by the Epistemic Contract.
2. `AMBIGUOUS` is required when relevant source observations conflict and no approved domain-specific conflict policy determines precedence.
3. Conflicting observations must be preserved. No provider may silently win by arrival order, convenience, or an undocumented global hierarchy.
4. `UNKNOWN`, `INCONCLUSIVE`, `OUT_OF_SCOPE`, and `AMBIGUOUS` are not synonyms for `CONTRADICTED`.
5. Any status mapping or aggregation rule must be explicit, versioned, and contract-tested before being exposed as a user-facing result.

### 3.8 Freshness and timestamps

1. Preserve capture time separately from source/quote time.
2. Expose the age of the observation when it can be computed from trustworthy timestamps.
3. No universal `max_age` or freshness guarantee is introduced by this ADR.
4. A freshness threshold must be claim/provider/policy-specific and explicitly versioned. If freshness cannot be established, the result must say so; stale or unknown age must not be silently treated as current.

### 3.9 Authentication and tenant boundary

1. No externally exposed verification API may operate without authentication.
2. API-key authentication is the proposed minimum for the first controlled pilot.
3. Key ownership, lifecycle/rotation, request audit, and authorization boundaries must be specified before external exposure.
4. Tenant/org isolation is required before multi-user or third-party data exposure; it must not be postponed until after a public Dashboard.
5. The exact identity, role, tenant, and key-management model requires a security design and tests. This ADR does not claim those controls already exist.

### 3.10 Ledger integrity and residual threat model

1. Append-only application semantics, database UPDATE/DELETE protection, and hash-chain verification are required controls, but they do not make a database administrator technically incapable of rewriting the entire database and recomputing the chain.
2. The MVP must document this residual trust assumption.
3. External anchoring, independent replication, or public-key signatures may be evaluated later. They are not prerequisites for the first bounded vertical slice unless the threat model changes.
4. HMAC or another signature is supplementary authenticity evidence; it does not replace raw evidence, chain verification, or deterministic replay.

## 4. Target API boundary

The target public workflow is conceptually:

- `POST /v1/claims/verify` — accept a typed Claim (and, in a later adapter, text whose extracted Claims are explicitly surfaced for acceptance); JAMP performs provider acquisition, verification, and persistence.
- `GET /v1/verdicts/{verdict_id}` — return the immutable Verdict Record and its referenced evidence metadata.
- `GET /v1/verdicts/{verdict_id}/replay` — verify/recompute from stored raw evidence and the recorded policy version.

These paths describe the target contract, not an authorization to implement them immediately. Exact request/response schemas, status codes, idempotency semantics, rate limits, and API version compatibility require a follow-up API contract.

A client must not be able to submit arbitrary provider observations and choose the verdict that JAMP then presents as its own verification.

## 5. Product boundary and first vertical slice

The first product scenario remains deliberately narrow: a user supplies a bounded market-price Claim (initially MOEX or supported CoinGecko scope), and receives:
- the normalized Claim and declared scope;
- provider/source identity;
- source and capture timestamps where available;
- raw evidence digest and integrity-check result;
- bounded Verdict and explanation;
- policy/code version references;
- a deterministic replay result or an explicit reason replay is unavailable.

Text extraction is a separate, replaceable intake adapter. The product must not claim general hallucination detection, universal factual verification, universal source authority, or universal freshness.

Product demand remains unvalidated until user interviews or observed pilot usage provide evidence.

## 6. Delivery order

1. **Architecture contract approval:** approve this ADR and reconcile any required changes to Charter/Epistemic Contract.
2. **PR #3 remains HOLD:** do not merge the current client-write evidence API as a public Trust Layer boundary. Resolve the route trust boundary, authentication requirement, canonicalization version, and real PostgreSQL CI evidence under a separately approved implementation scope.
3. **Persistence contract:** freeze Evidence/Verdict record schemas, canonicalization version, migration compatibility, and replay semantics before production migration.
4. **First end-to-end vertical slice:** Claim → server-side Provider acquisition → raw Evidence → Verdict Record → Ledger → integrity/replay API.
5. **Security boundary:** authentication and tenant isolation before any external pilot or multi-user exposure.
6. **Text extraction adapter:** add separately after explicit-Claim verification is proven; extractor remains untrusted and replaceable.
7. **Minimal Dashboard:** only after the vertical API contract and end-to-end result are stable.
8. **Operational hardening:** backups/recovery, monitoring, retention, rate limits, external anchoring, and performance work according to measured risk.

No expansion to additional providers, model routing, messaging transports, or broad agent functionality until the first vertical slice meets its Definition of Done.

## 7. Definition of Done for the first vertical slice

The first vertical slice is complete only when evidence demonstrates all of the following:

- A bounded Claim enters through the supported API.
- JAMP obtains the provider response itself.
- Raw response bytes and server-computed digest are persisted before/alongside parsing.
- Capture time and source time are not conflated.
- Evidence and Verdict are separate immutable records.
- Verdict references exact Evidence IDs and policy/code versions.
- Canonicalization/hash version is stored and covered by compatibility tests.
- Missing, malformed, unavailable, out-of-scope, stale-under-policy, and conflicting observations fail closed with explicit outcomes.
- Integrity verification can be independently reproduced from stored bytes.
- Deterministic replay uses stored evidence and a recorded policy version.
- Authentication is enforced at the exposed API boundary.
- PostgreSQL integration tests run against a real PostgreSQL service in terminal CI.
- CI evidence includes Run → Job → Conclusion and test execution output; missing jobs or zero collected tests are not success.
- No synthetic status, score, source observation, or replay result is introduced.

## 8. Consequences

**Expected benefits**
- JAMP owns the evidence-acquisition and verdict boundary instead of merely persisting client assertions.
- Claim, Provider, Evidence, Verdict, and API responsibilities are explicit.
- Historical decisions remain inspectable when policies evolve.
- Extraction can be added without granting AI output evidence authority.
- The product path is one narrow end-to-end scenario rather than parallel expansion of unrelated platform components.

**Costs and trade-offs**
- PR #3 may require a revised scope or follow-up implementation work before merge.
- Separate Evidence and Verdict records increase schema and migration complexity.
- Reproducible replay requires policy/version retention and compatibility discipline.
- Authentication and tenant isolation move earlier in the roadmap.
- External anchoring and universal freshness remain out of scope for the MVP.

## 9. Alternatives considered

1. **Keep client-submitted Evidence as the public verification API.** Rejected: digest equality does not authenticate source acquisition, timestamp, parsed value, or verdict.
2. **Put text extraction inside the trusted core.** Rejected: extractor output is an untrusted proposal and must remain replaceable.
3. **Keep Verdict only as a field on Evidence.** Rejected: it conflates observation with decision and cannot cleanly preserve multiple policy-versioned decisions over the same evidence.
4. **Build Dashboard before the end-to-end verification path.** Rejected: UI would expose an incomplete trust contract.
5. **Add a universal provider hierarchy/freshness rule now.** Rejected: current evidence does not establish one; domain-specific policies must be explicit.

## 10. Impact and change control

This ADR affects future design of:
- Claim domain types and provider request mapping;
- Provider acquisition and trust boundary;
- Evidence and Verdict schemas;
- PostgreSQL/Alembic migrations and canonicalization compatibility;
- verification/replay services;
- API schemas, authentication, authorization, and tenant isolation;
- status aggregation and conflict policy;
- tests, CI evidence, and Dashboard interpretation.

It does **not** change source code, database migrations, API behavior, CI configuration, or the JAMP 1 repository.

Any implementation must be split into narrowly scoped changes, reviewed against this ADR, and supported by fresh terminal evidence. Changes to the approved contract require a follow-up ADR or an explicit amendment with impact analysis.

## 11. Open items requiring owner approval or follow-up design

- Approve or revise the Claim typed-payload shape.
- Choose and freeze the initial canonicalization/hash-format identifier and exact preimage.
- Decide whether the first authenticated pilot is single-tenant or requires tenant isolation immediately; no external exposure before this is resolved.
- Define idempotency key scope and retry semantics for `POST /v1/claims/verify`.
- Define exact API response/status contracts.
- Define the policy-version artifact format and retention needed for replay.
- Define the initial conflict aggregation truth table and tests.
- Define which source timestamps are trustworthy for each initial Provider.
- Reconcile Charter and Epistemic Contract wording with this ADR after approval.

## 12. Current state

- **ADR status:** ACCEPTED — approved by project owner on 2026-11-20.
- **PR #3:** OPEN / HOLD; no merge authorized.
- **Source code:** unchanged by this ADR.
- **JAMP 1:** unchanged; Frozen Core remains governed by ADR-0001 and its existing invariant.

**Governing principle: Proof over confidence. AI proposes; JAMP acquires, verifies, and preserves evidence.**
