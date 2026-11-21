# ADR-0006 — Historical v1 Compatibility Audit

**Status:** PRELIMINARY / HOLD FOR EXECUTABLE VERIFICATION  
**Audit base:** `main` at `dbc0dd532f520bb2af382d028f52b051aa12ad10`  
**Audit branch:** `audit/adr-0006-v1-freeze`  
**Scope:** read-only inspection of canonicalization, digest, Evidence/Verdict models, Replay, PostgreSQL ledger, migrations, and existing tests. No production code or database was changed.

## 1. Executive result

The current implementation records canonicalization/parser version labels, but the hash and replay paths are not version-dispatched. Therefore v1 must be preserved as an immutable implementation before v2 is introduced.

**Historical compatibility is not yet VERIFIED.** The repository has no fixed golden hash assertions for representative Evidence/Verdict records, and the PostgreSQL integration tests require `DATABASE_URL`. The values below are independently reproduced candidate golden vectors from the current source contract; they have not yet been executed against the package test suite or a populated PostgreSQL ledger.

## 2. Source inventory (observed at audit base)

| Component | Path | Blob SHA |
|---|---|---|
| Canonicalizer | `src/jamp2/evidence/canonicalize.py` | `9ebc9ccb0e0f12f65b7881a6ad9e2a7bdbc5f3a8` |
| Evidence digest | `src/jamp2/evidence/digest.py` | `477df0a0ea738244bf917cfa0b41c8a8d10de19d` |
| Evidence model | `src/jamp2/evidence/model.py` | `75c4bad146590e427167a5e3d67d5eba240e53dc` |
| Replay | `src/jamp2/evidence/replay.py` | `6e1224e54a3aecb78324557ed073f642a95de44d` |
| Verdict model | `src/jamp2/evidence/verdict.py` | `58af224a3544e944838cb61611dc0d6f313a9826` |
| PostgreSQL ledger | `src/jamp2/ledger/postgres.py` | `6c2a7aa7ccf5f2dd10ecc880863178a128b89b62` |
| Evidence unit tests | `tests/unit/test_ledger.py` | `8f749a738431a77291e9e4e0095f092bd69b5b8f` |
| Verdict unit tests | `tests/unit/test_verdict_record.py` | `952135b6c5c44f9cfe76fdac5992c393eaf21670` |
| Replay unit tests | `tests/unit/test_replay.py` | `2002158ce3b0969986cd10f73310a6fc79ae9654` |
| PostgreSQL Evidence tests | `tests/integration/test_postgres_ledger.py` | `a54396de665d33b06f8ac90ac57c1d8a10b67d6b` |
| PostgreSQL Verdict tests | `tests/integration/test_postgres_verdicts.py` | `7272992b209b75d09b8380ad1ba1eb48a63e5dde` |

## 3. Findings

### F1 — v1 canonicalizer is concrete and must be frozen

Observed implementation identifier: `jamp-json-nfc-v1`.

It recursively NFC-normalizes string values and object keys, rejects key collisions after NFC normalization, and serializes using Python `json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)`.

This is the historical algorithm. It must be retained byte-for-byte or behavior-for-behavior under an explicitly named v1 implementation. Do not silently replace its global implementation with JCS.

### F2 — hash functions do not dispatch by recorded version

Observed in `digest.py`:

- `compute_record_hash(record)` builds the full Evidence payload and calls the one imported global `canonicalize(payload)`.
- `compute_verdict_hash(record)` does the same for Verdict.
- Both payloads include `canonicalization_version`, so changing the label changes the hash, but the label does not select a canonicalizer.

**Consequence:** changing the global canonicalizer would change historical hash verification. A dispatcher keyed by each record's recorded version is required. Unknown/missing versions must fail closed.

### F3 — Replay is globally pinned, not historically dispatched

Observed in `replay.py`:

- Replay compares the Verdict version to the one global `CANONICALIZATION_VERSION`.
- It requires each Evidence record's canonicalization version to equal the Verdict version.
- It checks the parser label against one global `PARSER_VERSION`.
- Provider payloads are reparsed with standard `json.loads()`; the recorded parser version does not choose a parser implementation.

**Consequence:** this is not yet a mixed-version historical replay contract. ADR-0006's canonicalizer/parser dispatch is necessary. The current parser path also inherits Python's duplicate-key last-value-wins behavior.

### F4 — existing tests are invariant tests, not golden historical proofs

Observed tests verify determinism, sensitivity to changed fields, hash-chain links, tamper detection, version-label binding, and replay behavior. They do not pin exact expected Evidence/Verdict record-hash constants for frozen historical records.

**Consequence:** add fixed golden vectors before any implementation change. Tests must assert exact bytes and exact hash strings, not only equality between two executions of the same implementation.

### F5 — PostgreSQL layer verifies hashes through the same global functions

Observed `PostgresLedger.append()`, `_verify_rows()`, `append_verdict()`, and `_verify_verdict_rows()` call the current global hash functions. Stored rows retain canonicalization/parser metadata, but verification does not route through those metadata values.

The database schema stores raw bytes, raw SHA-256, parsed JSONB, canonicalization version, parser version, provider adapter version, record hash, and parent hash. The ledger's append-only update/delete triggers and TRUNCATE protection are already present in migrations.

### F6 — migrations 003 and 004 deliberately refuse populated ledgers

Observed migration behavior:

- `003_evidence_canonicalization_version` aborts if the Evidence table is non-empty.
- `004_evidence_capture_metadata` also aborts if the Evidence table is non-empty.
- Their downgrade paths likewise refuse populated ledgers.

This is a safe historical guard, not evidence that an additive v2 migration already exists. Do not rerun or rewrite these migrations. Any future schema evolution must be a new forward-only migration with explicit preservation checks.

### F7 — PostgreSQL integration evidence is environment-dependent

The PostgreSQL integration modules are skipped unless `DATABASE_URL` is set. Their source includes append/read/restart, hash-chain, tamper, and immutability tests, but source inspection alone does not prove they ran against PostgreSQL in CI.

No production database contents or historical production rows were available in this read-only audit.

## 4. Candidate v1 golden vectors (independent reproduction)

The following values were independently reproduced from the current source algorithm and the exact factories in `tests/unit/test_ledger.py` and `tests/unit/test_verdict_record.py`. They are **candidate expected values**, not yet verified by running the repository package.

### Evidence chain fixture

Factory parameters: raw bytes `b'{"price":100}'`; provider `moex.iss`; URL `https://example.test/marketdata`; claim `claim-1`; captured time `2026-11-19T20:00:00+00:00`; parsed value `{"price":100}`; status `E4_VERIFIED`; default v1 metadata.

- Raw SHA-256: `fbcfc7a6ca136b21ff0a7101016fc91230798ffc2e6f5fc856df2fc29adca67a`
- Evidence `evidence-1` record hash, parent=null: `98e099906bb786bd99ee160735a3e2155be931fbac686f35d8be9b956939bd97`
- Evidence `evidence-2` record hash, parent=hash(evidence-1): `4fba110cd8e3160fb2360423df557dd9d3aee95103c371b3efdfd119677ae90c`
- Evidence `evidence-3` record hash, parent=hash(evidence-2): `a6c0c4b6715a03f60e0e00003af44ecaebf53a891f5d1178228b1cd9b0e8eebc`

### Verdict fixture

Exact factory in `tests/unit/test_verdict_record.py`, with parent=null:

- Verdict `verdict-1` record hash: `b7da3622b835993860b47d679c12eb06705a633d89ca260f692b182d07481064`

## 5. Required verification before closing Stage 1

1. Add the candidate vectors as fixed golden assertions in a dedicated test-only change.
2. Run the repository unit suite against the unchanged v1 source and confirm all exact hashes above.
3. Add a frozen fixture containing complete Evidence and Verdict fields, raw bytes, raw SHA, record hashes, parent links, and append order.
4. Verify a three-record Evidence chain and a multi-record Verdict chain from the fixture.
5. Run PostgreSQL integration against an isolated PostgreSQL instance: append, close/reopen, retrieve, verify exact hashes and chain order.
6. Confirm migration head and schema state without applying migrations to any production database.
7. Record terminal CI workflow/run/job/artifact evidence. A skipped PostgreSQL test is not a pass.

## 6. Change boundary

- Production source: unchanged.
- Database/migrations: unchanged.
- Existing historical records: not accessed or modified.
- JCS v2: not implemented or activated.
- Strict parser v2: not implemented or activated.
- This audit does not authorize production migration or global canonicalizer replacement.

**Stage 1 verdict: HOLD.** Source-level incompatibility risks are identified and candidate vectors exist, but historical v1 compatibility is not yet executable-verified.
