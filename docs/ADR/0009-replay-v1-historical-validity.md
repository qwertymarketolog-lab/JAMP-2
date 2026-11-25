# ADR-0009: Replay v1 historical validity and policy binding

- Status: Accepted (contract); implementation pending
- Scope: JAMP 2 deterministic, no-network replay of persisted verdicts
- Decision date: 2026-11-25
- Depends on: ADR-0007 (source metadata and evidence hash schema), ADR-0008 (evidence-first arbitration pipeline)

## Context

Replay is an audit of a previously recorded decision. It is not a new source
query and it is not a request to decide whether old evidence is fresh now.

Two different questions MUST remain separate:

- **Historical validity:** Was the exact evidence set eligible under the exact
  policy that governed the original verdict, at that verdict's
  `decided_at_utc`?
- **Current freshness:** Would the evidence be fresh enough to support a new
  decision at the time of that new request?

Replay v1 answers the first question only. A separate new-decision path
evaluates current freshness using its own decision time. Replay MUST NOT
substitute the replay execution time, wall clock, or current request time for
the original `decided_at_utc`.

The current implementation records `policy_id` and `policy_version` on a
verdict, but the replay implementation uses hard-coded provider comparison
limits and does not execute the source temporal/provenance policy gate.
A version string alone does not prove the complete policy parameters that
were applied. This ADR defines the required contract; it does not claim that
the current runtime already satisfies it.

## Decision

### 1. Historical decision-time evaluation

Replay MUST evaluate temporal validity against the immutable
`VerdictRecord.decided_at_utc` from the original verdict.

For every required source observation whose policy requires upstream and
capture timestamps:

1. `provider_observed_at` MUST be present, parseable, and timezone-aware.
2. `fetched_at` MUST be present, parseable, and timezone-aware.
3. The temporal order MUST satisfy
   `provider_observed_at <= fetched_at <= decided_at_utc`.
4. The historical age MUST satisfy
   `decided_at_utc - provider_observed_at <= max_age_seconds_for_source`,
   where the source-specific limit takes precedence over the policy default.
5. For multi-source policies, the maximum minus minimum
   `provider_observed_at` across the required eligible sources MUST NOT
   exceed the policy's `max_time_skew_seconds`.
6. Required market-calendar, identity, quote-currency, price-type, and
   provenance-independence conditions MUST be evaluated from the same
   historical policy snapshot.

All timestamps are compared as instants normalized to UTC. Missing, malformed,
naive, future-relative-to-decision, or otherwise unverifiable required
metadata MUST NOT be repaired using receipt time, source date, current time,
or inference.

If a required temporal or policy condition cannot be verified, Replay MUST
return `INCONCLUSIVE` with a specific reason code. It MUST NOT return
`VERIFIED` or `CONTRADICTED` on the basis of raw price comparison alone.

### 2. Immutable policy snapshot binding

Every verdict eligible for a conclusive Replay MUST be bound to an immutable
policy snapshot that captures every parameter capable of affecting source
eligibility or verdict calculation.

At minimum, the snapshot MUST include:

- policy identifier and immutable version;
- required provider/source identifiers and verification mode;
- asset class, required subject identifiers, quote currency, and price type;
- market-calendar requirement and the exact calendar qualification state or
  referenced immutable calendar contract;
- per-source and default maximum age;
- maximum cross-source time skew;
- provenance and independence requirements, including failure-domain fields;
- comparison mode and all comparison parameters/thresholds;
- required-field and completeness rules;
- parser, provider-adapter, and policy-evaluator version identifiers where
  they affect interpretation of the evidence.

The verdict MUST carry a durable reference to the exact snapshot and its
cryptographic digest (or an equivalent immutable, content-addressed identity).
The snapshot bytes and digest MUST be verifiable during Replay. The digest
MUST cover the complete canonical snapshot representation, not only its
identifier or version label. Canonical serialization and digest construction
MUST use a declared, versioned project contract; implementations MUST NOT
silently invent a second canonicalization scheme.

A mutable policy registry entry, a current policy object, or a version string
without a verifiable immutable snapshot is insufficient. Replay MUST NOT
replace a missing or unverifiable historical snapshot with the current policy,
a default policy, or hard-coded thresholds.

If the snapshot is absent, unsupported, corrupted, or its digest does not
match, Replay MUST return `INCONCLUSIVE` with a specific policy-binding reason
code. Unknown policy versions fail closed.

### 3. Evidence integrity and deterministic execution

Before recalculation, Replay MUST verify:

- the ordered evidence references exactly match the verdict's evidence IDs;
- every record belongs to the verdict's claim;
- every required raw body is complete;
- each raw body digest matches the stored bytes;
- canonicalization, hash-schema, parser, and adapter versions are supported
  and consistent with the bound policy;
- the claim snapshot is consistent across the evidence set;
- provider, subject, and evidence identities satisfy the policy snapshot.

Replay MUST derive its result from the exact persisted raw bytes and immutable
metadata. It MUST perform zero provider/network calls and MUST NOT mutate the
ledger, verdict, evidence, or policy snapshot.

A mismatch, missing required record, unsupported interpretation version, or
integrity failure MUST fail closed as `INCONCLUSIVE`; it MUST NOT be silently
skipped or repaired.

### 4. Historical validity is not current freshness

Evidence that was valid at the original `decided_at_utc` remains historically
valid for that decision even if it is stale when Replay runs later. Replay
MUST produce the same historical result for the same immutable verdict,
evidence, policy snapshot, and supported replay implementation, independent
of the current wall clock.

Conversely, evidence that was already stale or temporally invalid at the
original decision time cannot be made valid by replaying it immediately,
refreshing it later, or substituting a new timestamp.

A new business request MUST use a separate current-freshness evaluation with
the new request's decision time and the policy version selected for that new
decision. It MUST create new evidence; it MUST NOT rewrite the historical
verdict or its evidence.

### 5. Replay outcome semantics

- A conclusive replay status is permitted only after evidence integrity,
  policy binding, historical temporal validity, and deterministic
  recalculation have all succeeded.
- If any required proof is missing or cannot be interpreted, the replay
  status is `INCONCLUSIVE`, with a diagnostic reason code.
- `matches_original` is true only when the conclusive replay status equals
  the recorded verdict status. An inconclusive replay is not a match and is
  not evidence that the original verdict was wrong.
- Replay MUST preserve the distinction between a verified historical
  decision and a new decision's current freshness result.

## Required verification matrix

The implementation MUST add or update fixtures and tests for at least these
cases. Fixtures that omit required temporal metadata MUST NOT continue to
produce a conclusive replay merely because older tests expected it.

| Case | Required replay behavior |
|---|---|
| Complete valid timestamps; all policy conditions pass at decision time | Recalculate the original status |
| Missing `provider_observed_at` | `INCONCLUSIVE` |
| Missing `fetched_at` | `INCONCLUSIVE` |
| `provider_observed_at > fetched_at` | `INCONCLUSIVE` |
| `fetched_at > decided_at_utc` | `INCONCLUSIVE` |
| Source age exceeds the bound at `decided_at_utc` | `INCONCLUSIVE` |
| Multi-source timestamp skew exceeds the snapshot limit | `INCONCLUSIVE` |
| Required market calendar is absent or unconfirmed | `INCONCLUSIVE` |
| Required provenance/independence cannot be established | `INCONCLUSIVE` |
| Unknown policy version or missing policy snapshot | `INCONCLUSIVE` |
| Policy snapshot bytes are altered without a matching digest | `INCONCLUSIVE` |
| Evidence raw bytes or stored digest are altered | `INCONCLUSIVE` |
| Evidence was valid at decision time but is stale when replay runs | Historical result unchanged |
| Same immutable inputs replayed repeatedly and at different wall-clock times | Identical replay result |

## Compatibility and migration constraints

- This ADR changes the contract only. It does not authorize runtime-code,
  database-schema, migration, ledger, or existing verdict mutation.
- Existing historical verdicts without a verifiable policy snapshot cannot be
  upgraded retroactively by guessing their original parameters. Unless an
  independently verifiable historical policy artifact exists, their Replay
  result remains `INCONCLUSIVE`.
- Existing tests and fixtures must be brought into conformance with this
  contract in a later implementation change. Tests MUST NOT be weakened or
  removed to preserve obsolete conclusive outcomes.
- No comparison threshold may be changed as part of implementing Replay v1.
  Thresholds must be sourced from the immutable policy snapshot.
- Any required schema or ledger change must be proposed separately with
  explicit migration and backward-compatibility analysis.

## Implementation acceptance criteria

Replay v1 implementation is not complete until:

1. Verdicts reference an immutable, digest-verifiable policy snapshot.
2. Historical temporal and provenance checks use only the original
   `decided_at_utc`.
3. Current freshness evaluation is isolated from historical replay.
4. The verification matrix above is implemented with deterministic tests.
5. Replay performs no network access and does not mutate persisted records.
6. CI provides terminal evidence for the exact implementation commit.

Until these criteria are met, this ADR is an accepted target contract, not a
claim of runtime conformance.
