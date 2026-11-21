# ADR-0003: Evidence Ledger — Known Limitations

- **Status:** ACCEPTED (MVP v1)
- **Date:** 2026-11-21
- **Decision owner:** JAMP project owner
- **Related:** ADR-0002, PR #3, PR #5, PR #7

## Context

The Evidence and Verdict ledgers use PostgreSQL triggers `evidence_immutable` and `verdict_immutable` with `BEFORE UPDATE OR DELETE` semantics. Migration 006 adds statement-level `BEFORE TRUNCATE` triggers to both ledger tables. These controls protect against ordinary application-role mutations, but do **not** make either ledger absolutely immutable.

This ADR records the residual limitations observed in the MVP implementation. It does not claim production-grade tamper resistance, multi-tenant isolation, or automatic crash recovery.

## Known limitations

### 1. TRUNCATE protection can be bypassed by a privileged database role

Migration 006 installs `BEFORE TRUNCATE` triggers on `evidence` and `verdicts`. A normal application role cannot truncate these ledgers while the triggers are enabled.

A sufficiently privileged role with table ownership/trigger-management rights can run `ALTER TABLE ... DISABLE TRIGGER USER`, then truncate the table. PostgreSQL superusers and database administrators can also disable or remove triggers and alter the underlying data. Test cleanup fixtures use `ALTER TABLE ... DISABLE TRIGGER USER` inside a transaction before truncating, and re-enable user triggers in a `finally` block. This is a test-only bypass, not an application operation.

**Mitigation:** before production use, run the application under a dedicated database role without table ownership, DDL, trigger-management, or superuser privileges. External anchoring or independent replication may be evaluated separately. Full protection against a privileged database administrator is outside the MVP trust boundary and may be revisited in ADR-0004.

### 2. Idempotency PENDING has no TTL or automatic recovery

`begin_idempotency()` creates a `PENDING` record. `fail_idempotency()` marks it failed on handled errors. If the process terminates after creating `PENDING` and before completing or failing the record, the current implementation has no lease, TTL, or periodic recovery mechanism to release it automatically.

**Mitigation (TODO):** implement a bounded pending lease/TTL and safe recovery in a separate PR. Recovery must not cause duplicate provider acquisition or ledger writes. The pilot accepts this operational limitation; it is not a claim that stuck records self-heal.

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
- No automatic recovery of stale `PENDING` idempotency records.

These limitations must remain visible in the PR description and must be revisited before any production or multi-user exposure.

## Consequences

- The append-only controls are accurately described as protection against ordinary UPDATE/DELETE/TRUNCATE operations while database triggers remain enabled, not absolute immutability.
- The current MVP may proceed with these limitations documented, subject to the PR's required review and CI checks.
- Follow-up hardening must be delivered as small, independently reviewed PRs with PostgreSQL integration tests where database behavior is involved.
- No threshold, test, or existing security control is weakened by this ADR.

**Governing principle:** Proof over confidence. State the protection that exists; do not claim protection against threats the implementation does not cover.
