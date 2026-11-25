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

**Production downgrade prohibition:** Migration 008 downgrade is destructive
and is valid only in development environments. Production deployments MUST
NOT downgrade migration 008. Any production rollback involving migration 008
requires a data-migration plan approved by the architecture owner and verified
before execution.

## Provider rules

- MOEX ISS securities `marketdata.SYSTIME` is retained as raw source data only.
  The public ISS overview and consulted field references do not establish its
  format or timezone for the exact shares/securities endpoint used here.
  JAMP MUST NOT combine a time-only value with `TRADEDATE`, attach
  `Europe/Moscow`, or parse an ISO-looking value as an upstream timestamp
  without an authoritative field-level contract for this endpoint. The raw
  value and diagnostic status are retained; `provider_observed_at` remains
  unknown and the freshness gate blocks the source. `TRADEDATE` is retained
  as `source_date` only after strict `YYYY-MM-DD` calendar validation.
  MOEX ISS overview: https://www.moex.com/a8531.
  The separate FutOI ISS description identifies its own `SYSTIME` as
  publication time and shows a time-only sample, but that contract is specific
  to the FutOI endpoint and is not transferable to securities marketdata:
  https://fs.moex.com/f/12828/data-description-en.pdf.
- CoinGecko `/simple/price` requests `include_last_updated_at=true`.
  The documented `last_updated_at` value is a UNIX timestamp in seconds and
  is parsed only as a finite numeric timestamp, converted to UTC. Missing,
  wrong-type, non-finite, or out-of-range values remain unknown with explicit
  diagnostics; no receipt-time substitution is permitted.
  API contract: https://docs.coingecko.com/reference/simple-price.
- Production MOEX without a confirmed market calendar is not reducer-eligible
  and cannot be upgraded by fabricating `market_calendar_valid=True`.
