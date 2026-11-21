# ADR-0004: Lease-based idempotency ownership

- Status: Proposed for PR #9
- Date: 2026-11-21
- Scope: PostgreSQL idempotency reservation recovery and terminal-state fencing.

## Context

A PENDING idempotency reservation could remain indefinitely after a process crash. A retry could not distinguish a live owner from a dead one. The reservation therefore gains an opaque owner token, a database-clock lease expiry, and an attempt counter.

## Decision

Migration 007 adds nullable `owner_token` and `lease_expires_at`, plus non-null `attempt_count` (server default 1). Legacy PENDING rows are backfilled with `owner_token = key_hash` and an expiry one second before `created_at_utc`, making them immediately eligible for takeover.

The lease TTL is 60 seconds. Lease issuance, expiry comparisons, and completion timestamps use PostgreSQL `clock_timestamp()`; application-host clocks are not authoritative.

A single INSERT .. ON CONFLICT DO UPDATE statement arbitrates creation and takeover. Takeover is allowed only for an expired PENDING row whose request_digest matches. It replaces owner_token, lease_expires_at, and claim_id, and increments attempt_count. request_digest is deliberately not updated. The current idempotency row therefore identifies the current owner's Claim; append-only records written by an earlier zombie retain their original Claim association.

## State machine

| From | To | Condition |
| --- | --- | --- |
| absent | PENDING | First INSERT; attempt_count=1 |
| PENDING | PENDING | lease_expires_at < clock_timestamp() and matching request_digest; atomic takeover |
| PENDING | COMPLETED | Matching owner_token and unexpired lease |
| PENDING | FAILED | Matching owner_token and unexpired lease |
| COMPLETED | terminal | No outgoing transition |
| FAILED | terminal | No outgoing transition |

Lease expiry is a transient condition, not a persisted state. An expired PENDING is atomically replaced by a new PENDING owned by the takeover winner. The database status CHECK remains PENDING, COMPLETED, or FAILED; there is no LEASE_EXPIRED status.

A digest mismatch returns KEY_REUSED (HTTP 409), regardless of lease expiry. COMPLETED and FAILED are terminal and cannot be taken over. A missing, stale, or expired owner token cannot complete or fail the reservation; a zero-row update raises IntegrityViolation.

## Known limitations

The current route performs provider arbitration, Evidence append, Verdict append, and idempotency completion in separate transactions. Consequently, an expired zombie owner can append Evidence and Verdict before its final complete() is rejected. Those records are append-only and remain in the ledger, associated with the zombie owner's original Claim. After takeover, multiple Evidence/Verdict rows may exist for the same client idempotency key across attempts. Only the current idempotency owner can complete the idempotency record, and the client receives the winning owner's cached response.

This is an explicit MVP limitation, not full zombie-write protection. Full fencing requires propagating owner_token into Evidence append and Verdict append and validating the active lease atomically inside those append transactions. That work is deferred to a separate P2 issue.

The 60-second lease can expire during a legitimately long provider operation. The old owner then cannot complete or fail the reservation; the client must retry and allow a new owner to proceed. No automatic lease renewal is implemented in this ADR.

## Consequences

- Crashed PENDING requests become recoverable after 60 seconds.
- Concurrent takeover has one winner because PostgreSQL serializes the conflicting row update.
- The request digest remains immutable across attempts.
- claim_id tracks the current owner after takeover.
- This mechanism fences terminal idempotency state transitions, but does not fence append-only ledger writes (see Known limitations).
