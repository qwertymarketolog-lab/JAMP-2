# ADR-0003: Evidence Ledger — Known Limitations

- **Status:** ACCEPTED (MVP v1)
- **Date:** 2026-11-21
- **Decision owner:** JAMP project owner
- **Related:** ADR-0002, ADR-0004, PR #3, PR #5, PR #8, PR #9

## Context

The Evidence and Verdict ledgers use PostgreSQL triggers `evidence_immutable` and `verdict_immutable` with `BEFORE UPDATE OR DELETE` semantics. PR #8 adds statement-level `BEFORE TRUNCATE` triggers to both ledger tables (migration 006). These controls protect against ordinary application-role mutations, but do **not** make either ledger absolutely immutable.

Idempotency reservation recovery was subsequently implemented in PR #9 and is specified in ADR-0004. This ADR records the remaining MVP limitations after that change. It does not claim production-grade tamper resistance, multi-tenant isolation, automatic lease renewal, or strict fencing of every Ledger write.

## Known limitations

### 1. TRUNCATE protection can be bypassed by a privileged database role

Migration 006 installs `BEFORE TRUNCATE` triggers on `evidence` and `verdicts`. A normal application role cannot truncate these ledgers while the triggers are enabled.

A sufficiently privileged role with table ownership/trigger-management rights can run `ALTER TABLE ... DISABLE TRIGGER USER`, then truncate the table. PostgreSQL superusers and database administrators can also disable or remove triggers and alter the underlying data. Test cleanup fixtures use `ALTER TABLE ... DISABLE TRIGGER USER` inside a transaction before truncating, and re-enable user triggers in a `finally` block. This is a test-only bypass, not an application operation.

**Mitigation:** before production use, run the application under a dedicated database role without table ownership, DDL, trigger-management, or superuser privileges. External anchoring or independent replication may be evaluated separately. Full protection against a privileged database administrator is outside the MVP trust boundary and may be revisited separately.

### 2. Idempotency PENDING uses a bounded owner lease, but Ledger appends are not fully fenced

PR #9 adds lease-based recovery for `PENDING` idempotency reservations. The reservation stores an opaque `owner_token`, `lease_expires_at`, and `attempt_count`. The lease TTL is 60 seconds. Lease issuance, expiry comparisons, and completion timestamps use PostgreSQL `clock_timestamp()`; application-host clocks are not authoritative.

The first reservation creates `PENDING` with `attempt_count = 1`. A takeover is allowed only when the existing `PENDING` lease has expired and the incoming `request_digest` matches. The takeover atomically replaces the owner token, lease expiry, and `claim_id`, and increments the attempt counter. The `request_digest` remains immutable. `COMPLETED` and `FAILED` are terminal states and cannot be taken over. A missing, stale, or expired owner token cannot complete or fail the reservation. Lease expiry is transient; there is no persisted `LEASE_EXPIRED` status.

This mechanism recovers abandoned `PENDING` reservations after lease expiry and fences idempotency completion/failure. It does **not** provide strict distributed fencing for all append-only Ledger writes.

The current route performs provider arbitration, Evidence append, Verdict append, and idempotency completion in separate transactions. Consequently, an expired zombie owner can append Evidence and Verdict before its final completion is rejected. Those records remain append-only and retain their original Claim association. After takeover, multiple Evidence/Verdict rows may exist for the same client idempotency key across attempts. The current idempotency owner is the only owner permitted to complete the reservation; this does not imply exactly-once provider execution or prevent stale Ledger appends.

**Accepted MVP boundary:** the project owner explicitly accepts this residual zombie-append risk for the MVP on 2026-11-21, as recorded in ADR-0004. Strict Ledger-level fencing is deferred to [Issue #10](https://github.com/qwertymarketolog-lab/JAMP-2/issues/10) (P2). That follow-up must propagate `owner_token` into Evidence `append()` and Verdict `append_verdict()`, validate active ownership and unexpired lease atomically inside each append transaction, and fail closed without writing a row when ownership is missing, stale, or expired. Until that work is implemented and verified, the system must not claim full Ledger fencing, zombie-write prevention, or exactly-once behavior.

The 60-second lease can expire during a legitimately long provider operation. The old owner then cannot complete or fail the reservation; the client must retry and allow a new owner to proceed. No automatic lease renewal is implemented.

### 3. Multi-Source Replay is not supported

`replay_verdict()` supports the currently implemented single-provider replay paths. A Verdict containing Evidence from multiple providers is not replayed as a multi-source decision; the current implementation returns `INCONCLUSIVE / PROVIDER_SUBJECT_MISMATCH`.

**Mitigation (TODO):** implement deterministic Multi-Source Replay in a separate PR, preserving exact stored observations and the recorded policy version.

### 4. Missing Evidence currently maps to HTTP 500, not 422

`PostgresLedger.append_verdict()` raises `IntegrityViolation` when a Verdict references missing Evidence or Evidence belonging to another Claim. The current API error mapping returns HTTP 500 for this ledger integrity exception. A client-facing validation failure would more appropriately map to HTTP 422.

**Mitigation (TODO):** refine exception-to-HTTP mapping in a separate PR. This is an API error-contract improvement; the existing ledger-side integrity validation remains in place.

## MVP scope and non-goals

The following are known MVP limitations and are not represented as implemented capabilities:

- No multi-tenant isolation.
- No organization or role model.
- One static API key for the controlled pilot.
- No general replay engine for arbitrary Claim types or arbitrary multi-provider policies.
- No technical protection against a PostgreSQL superuser or database owner disabling triggers or rewriting the database.
- No strict owner-token fencing for Evidence and Verdict appends; this is deferred to Issue #10.
- No automatic lease renewal. The 60-second lease and reservation recovery behavior are described in ADR-0004.

These limitations must remain visible in the PR description and must be revisited before any production or multi-user exposure.

## Consequences

- The append-only controls are accurately described as protection against ordinary UPDATE/DELETE/TRUNCATE operations while database triggers remain enabled, not absolute immutability.
- Stale `PENDING` idempotency reservations are recoverable through the lease/takeover mechanism in PR #9; this does not fence all Ledger appends.
- The current MVP may proceed with these limitations documented, subject to the PR's required review and CI checks.
- Follow-up hardening must be delivered as small, independently reviewed PRs with PostgreSQL integration tests where database behavior is involved.
- No threshold, test, or existing security control is weakened by this ADR.

**Governing principle:** Proof over confidence. State the protection that exists; do not claim protection against threats the implementation does not cover.
