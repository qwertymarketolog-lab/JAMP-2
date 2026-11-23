# ADR-0007: Source metadata and evidence hash schema

- Status: Accepted
- Scope: JAMP 2 evidence capture and append-only ledger

## Decision

Every captured provider response records server-side `fetched_at`, upstream
`provider_observed_at` when the provider supplies it, and `source_date` when
the provider supplies a source trading date. These are distinct facts and are
never substituted for one another.

Evidence hash schema v1 is frozen byte-for-byte for historical compatibility.
New records use v2; v2 adds `hash_schema_version`, `fetched_at`,
`provider_observed_at`, and `source_date` to the canonical payload. The v2
schema version identifies the canonical payload shape, not capture-metadata
completeness: all three metadata fields remain independently nullable, and a
v2 record may contain complete, partial, or absent capture metadata. Missing
values are hashed as explicit nulls; the schema version must never be treated
as a completeness or qualification flag. Unknown schema versions fail closed.
Canonicalization remains `jamp-json-nfc-v1`.

Capture completeness is evaluated separately from hash serialization. The
source policy gate requires valid `fetched_at` and `provider_observed_at`
values before freshness eligibility; a missing required timestamp blocks the
source. `source_date` is an optional calendar-date fact and is never inferred
from receipt time or substituted for either timestamp.

Migration 008 is additive and linear after 007. Existing rows receive v1 through
the database default; no historical evidence or verdict hashes are rewritten.
The migration is safe on populated ledgers. Downgrade removes the new metadata
columns and therefore is not a preservation mechanism for v2-only metadata.

## Provider rules

- MOEX timestamps are interpreted in Europe/Moscow. `SYSTIME` is optional;
  absent or unparseable `SYSTIME` means unknown observation time, not a guessed
  timestamp. `TRADEDATE` is retained as `source_date`.
- CoinGecko requests `include_last_updated_at=true`; its provider timestamp is
  parsed as UTC. A missing upstream timestamp remains unknown.
- Production MOEX without a confirmed market calendar is not reducer-eligible
  and cannot be upgraded by fabricating `market_calendar_valid=True`.
