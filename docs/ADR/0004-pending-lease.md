# ADR-0004: Lease-based idempotency ownership

- Status: Accepted for MVP (PR #9)
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

## Strict Ledger append fencing (Issue #10)

The Issue #10 decision supersedes the previously accepted MVP zombie-append limitation.

Both Evidence `append()` and Verdict `append_verdict()` require the caller's `key_hash` and `owner_token`. Each append transaction performs the following operations in this order:

1. Lock the matching `idempotency_records` row with `SELECT ... FOR UPDATE`.
2. Fail closed if the reservation is missing, the owner token differs, the reservation claim_id differs from the record claim_id, or status is not PENDING.
3. After acquiring the row lock, obtain a fresh PostgreSQL `clock_timestamp()` and reject a null or expired `lease_expires_at`.
4. Only after successful fencing, acquire the relevant advisory chain lock, verify the existing chain and references, and insert the immutable record in the same transaction.

The reservation row lock is deliberately acquired before the Evidence or Verdict advisory lock. This serializes append against takeover using the same reservation row and ensures an append waiting behind a takeover validates the new owner rather than the former owner. The database clock is evaluated after the row lock is acquired, not before a potentially blocking lock wait.

The append transaction does not cover provider network calls. Provider acquisition and arbitration remain outside the reservation lock; every individual Evidence append and the Verdict append independently revalidate the reservation.

A missing, stale, wrong, terminal, claim-mismatched, or expired reservation raises `StaleLeaseError`, a subclass of `IntegrityViolation`, before any append-only row is inserted. Existing API integrity handling therefore remains fail-closed.

No schema migration is required by this decision: the fencing contract uses the existing `key_hash`, `owner_token`, `claim_id`, `status`, and `lease_expires_at` columns introduced by migration 007. This conclusion is conditional on the deployed schema matching the repository's existing migration history.

The previous MVP risk statement remains historical context only: PR #9 fenced completion/failure but did not fence append operations. Issue #10 removes that limitation once its PostgreSQL integration tests and terminal CI evidence pass. The project must not claim verified strict fencing solely from this ADR or from mocked unit tests.

## Consequences

- Crashed PENDING requests become recoverable after 60 seconds.
- Concurrent takeover has one winner because PostgreSQL serializes the conflicting row update.
- The request digest remains immutable across attempts.
- claim_id tracks the current owner after takeover.
- Reservation ownership now fences both terminal idempotency transitions and append-only Evidence/Verdict writes when Issue #10 implementation and terminal CI evidence are verified.
