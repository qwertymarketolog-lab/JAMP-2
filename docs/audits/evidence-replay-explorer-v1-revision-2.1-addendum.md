# JAMP-2 — Evidence & Replay Explorer v1.0
## Revision 2.1 Addendum

**Status:** DRAFT — ADDENDUM TO REVISION 2; NOT IMPLEMENTATION AUTHORIZATION  
**Specification state:** HOLD — independent review and executable verification remain outstanding  
**Target branch:** `audit/adr-0006-v1-freeze`  
**Audit base:** `main` at `96f65935f91983f854bfb0c157329fab2936bd60` (observed before publication)  
**Related audit:** [ADR-0006 — Historical v1 Compatibility Audit](adr-0006-v1-compatibility-audit.md)  
**Normative relationship:** this document supplements Evidence & Replay Explorer v1.0, Revision 2. Revision 2 remains unchanged. If wording in Revision 2 is ambiguous on the points below, this Addendum controls those points only.

---

## 0. Purpose and change boundary

This Addendum resolves the outstanding review findings F1–F6 for the proposed CLI-first Evidence & Replay Explorer v1.0. It does not close the remaining golden-vector verification requirement (F7).

The Addendum is a documentary clarification only. It does not authorize implementation, production changes, or merge.

**Explicitly out of scope:**
- production source changes;
- changes to `src/jamp2/evidence/digest.py`, `canonicalize.py`, `replay.py`, API routes, or domain models;
- database schema changes, migrations, data rewrites, or rehashing;
- a second canonicalizer, digest engine, parser, or Replay engine;
- JCS v2, strict parser v2, or activation of any v2 format;
- a new HTTP endpoint or provider/network refetch;
- merge of this branch into `main`.

**Frozen boundary:** production source, migrations, schema, and historical records remain unchanged. Required production delta: **Δ = 0**.

## 1. F1 — Full global chain scope

### 1.1 Chain scope is table-global

A full-chain Explorer run MUST verify the complete global Evidence chain and the complete global Verdict chain. The chain scope is not limited to a selected claim, a selected Verdict, or the Evidence referenced by one Verdict.

The current ledger sequence is global at the table level. Therefore:

1. Read every Evidence row in the Evidence table, ordered by its SQL `sequence_id`.
2. Read every Verdict row in the Verdict table, ordered by its SQL `sequence_id`.
3. Verify each table's complete ordered chain, including first-row/root semantics, parent links, duplicate record identifiers, and record hashes.
4. Verify Evidence raw-byte SHA-256 for every Evidence row.
5. Verify every Verdict-to-Evidence reference across the complete Verdict set:
   - referenced Evidence identifier exists in the same snapshot;
   - referenced Evidence belongs to the same `claim_id` as its Verdict;
   - duplicate or malformed references are reported as integrity failures, not silently deduplicated.
6. A claim filter may limit the displayed detail or replay target, but MUST NOT narrow the underlying global chain-integrity scan.

`sequence_id` is a storage ordering field. It MUST be read directly from SQL and MUST NOT be added to or synthesized in domain records.

### 1.2 Hash implementation ownership

The Explorer MUST use the repository's existing `src/jamp2/evidence/digest.py` functions for record-hash calculations. It MUST NOT implement another canonicalizer or digest algorithm.

Before invoking any digest/canonicalization function, the Explorer MUST apply the version gate in §3. If any row in either global chain has a missing or unsupported canonicalization version, the Explorer MUST NOT claim complete global hash-chain verification. It MUST return the corresponding fail-closed version status.

### 1.3 Cross-table verification is part of global integrity

A successful global integrity result requires both complete table chains and all Verdict→Evidence references to pass. A valid per-table hash chain with a missing, cross-claim, or invalid reference is not `INTEGRITY_OK`.

## 2. F2 — Raw bytes, parsed values, and Replay semantics

The Explorer MUST keep three questions distinct:

1. **Raw-byte integrity:** do the stored raw bytes match the stored raw SHA-256?
2. **Record-hash integrity:** does the stored record hash match the existing digest calculation over the stored record fields?
3. **Replay result:** does the existing `replay_verdict()` function reproduce the expected result from the stored records under the currently supported replay implementation?

These checks are not interchangeable.

### 2.1 Explicit limitation

A valid Evidence record hash does not prove that `parsed_value` was originally derived from `raw_body_bytes`. A valid raw SHA-256 proves only the integrity of the stored raw bytes relative to that digest. The current Replay path uses stored parsed fields for claim-snapshot derivation and separately reparses provider payload bytes. It does not establish historical equality between the stored `parsed_value` and a parse of the stored raw bytes.

Accordingly:

- `REPLAY_MATCH` means only that the existing Replay function returned a match using the stored record fields and the currently supported replay logic.
- `REPLAY_MATCH` MUST NOT be described as proof that `parsed_value == parse(raw_body_bytes)`.
- `REPLAY_MATCH` MUST NOT be described as proof of full historical parser reproduction or proof that the original ingestion parser produced the stored parsed value.
- v1.0 MUST NOT add a second parser or an independent raw-to-parsed equivalence engine to close this gap.

### 2.2 Mandatory output disclosure

Every JSON result that reports Replay MUST include:

```json
{
  "raw_parsed_binding": "NOT_VERIFIED"
}
```

The fixed value `NOT_VERIFIED` is mandatory for v1.0. It MUST NOT be changed to `VERIFIED` based only on raw SHA, record hash, or `REPLAY_MATCH`.

The human-readable output MUST carry the equivalent explicit limitation. Consumers must be able to distinguish a Replay match from proof of raw-to-parsed binding.

## 3. F3 — Version gate before digest

The current v1 implementation uses a global canonicalizer and does not dispatch historical digest behavior by each record's stored version. The Explorer MUST therefore fail closed before invoking digest functions.

For every Evidence and Verdict row, before any record-hash calculation:

1. Require a non-empty, recognized canonicalization version.
2. For this v1.0 Explorer, accept only the currently supported historical identifier `jamp-json-nfc-v1`.
3. Require the parser version to be the one supported by the current v1 Replay implementation before Replay is invoked.
4. Reject missing, unknown, or unsupported version labels without attempting a digest using the global implementation.

Required outcomes:

- Missing version metadata: `UNKNOWN_VERSION`.
- Present but unsupported version metadata: `UNSUPPORTED_VERSION`.
- Any such row prevents a global `INTEGRITY_OK` result.
- The Explorer MUST identify the affected table and record identifier where safely available.
- No automatic fallback, version relabeling, rehashing, or “best effort” verification is permitted.

This gate is a compatibility guard, not a version dispatcher. v1.0 supports only the existing v1 implementation. No v2 canonicalizer or parser is introduced by this Addendum.

## 4. F4 — Current code-version resolution

The Explorer CLI MUST resolve the current immutable build identifier using these supported sources:

1. explicit CLI argument;
2. environment variable `JAMP2_CODE_VERSION`;
3. environment variable `GITHUB_SHA` as fallback.

The resolved identifier MUST be a full 40-character hexadecimal Git commit SHA. Abbreviated SHAs, tags, branch names, empty values, and malformed values are not valid build identifiers.

### 4.1 Conflicting sources

If two or more of the supported sources are simultaneously non-empty and their normalized values differ, the Explorer MUST fail closed. It MUST NOT silently select the higher-priority value and ignore the conflict.

For v1.0, report:

- replay status: `REPLAY_CODE_VERSION_UNKNOWN`;
- reason: `CODE_VERSION_SOURCE_CONFLICT`.

The same replay status applies when no valid source can be resolved; use a specific reason such as `CODE_VERSION_MISSING` or `CODE_VERSION_INVALID`.

This outcome is a Replay/build-identity limitation. It does not, by itself, mean that the Evidence or Verdict hash chain is corrupt.

### 4.2 Comparison with stored Verdict

Compare the resolved current build SHA with the Verdict's stored `code_version`.

- Equal valid SHAs: code-version check may proceed.
- Different valid SHAs: `REPLAY_CODE_VERSION_MISMATCH`.
- Missing or invalid current or stored value: `REPLAY_CODE_VERSION_UNKNOWN`.

A mismatch means the current build cannot claim that the Verdict was reproduced under the same code version. If all hash-chain checks passed, the independent integrity result may still be `INTEGRITY_OK`; do not convert a code-version mismatch into a hash-chain failure.

### 4.3 API boundary

This Addendum defines the Explorer CLI contract only. It does not change the existing API route's code-version resolution behavior and does not authorize an API refactor.

## 5. F5 — One consistent read-only database snapshot

A full Explorer run MUST use one database connection and one explicit transaction with:

- isolation level: `REPEATABLE READ`;
- transaction mode: `READ ONLY`.

The transaction MUST begin before the first SQL `SELECT`. It MUST remain open until all required data has been collected, including:

- all Evidence rows and their `sequence_id` values;
- all Verdict rows and their `sequence_id` values;
- all cross-table reference data needed to verify Verdict→Evidence links;
- all fields needed for raw SHA, version gating, record-hash verification, and selected Replay.

No relevant reads may be performed through separate ledger calls or separate transactions. No additional database reads may occur after the snapshot transaction ends. All calculations that need no database access may run after the rows have been materialized from that snapshot.

The database identity used by the Explorer MUST have SELECT-only permissions for the required tables. The Explorer MUST NOT write, update, delete, truncate, migrate, or otherwise mutate the database.

If the required transaction mode or read-only permissions cannot be established, the Explorer MUST fail closed with an execution/configuration error. It MUST NOT fall back to weaker isolation or multiple snapshots.

## 6. F6 — Exit code 30 takes unconditional precedence before checks

Revision 2's exit-code contract is clarified as follows.

If configuration, environment, database connection, read-only transaction setup, or another execution prerequisite fails **before verification checks begin**, the process MUST exit with code **30**.

Code 30 is unconditional in that situation:
- do not emit exit code 0;
- do not report a successful or complete integrity result;
- do not allow a partial or default status to mask the execution failure;
- report which prerequisite failed, without exposing secrets.

If execution fails after checks have begun and before all required checks complete, the result MUST still be non-successful and MUST NOT exit 0. Use code 30 for an execution failure that prevents completion; preserve any already collected check results as partial diagnostics, not as a completed verification verdict.

Only when execution completes may the status-to-exit-code priority defined in Revision 2 be applied to completed verification results. This Addendum does not change Revision 2's numeric mapping or its priority among completed check statuses.

A single-record or claim-filtered mode still cannot return exit code 0 unless the complete global Evidence and Verdict chains were checked in the same consistent snapshot.

## 7. F7 — Golden vectors remain HOLD

The golden values remain candidate compatibility anchors until executable evidence is collected.

Observed repository state before this Addendum:
- `tests/unit/test_v1_golden_hashes.py` exists on `audit/adr-0006-v1-freeze`.
- The file contains fixed expected raw SHA, three Evidence hashes, and two Verdict hashes.
- The existence or source contents of that test file do not prove that the test has executed or passed.
- No terminal CI evidence is asserted by this Addendum.

The following requirements remain open:

1. Execute the exact golden assertions against the repository package and unchanged v1 source.
2. Capture terminal CI evidence with identifiable commit SHA, workflow, run ID, job ID, conclusion, and downloadable artifact/log evidence.
3. Execute PostgreSQL integration verification against an isolated PostgreSQL instance, including append/read/reopen and complete chain verification.
4. Confirm that PostgreSQL tests were actually executed; skipped tests are not a pass.
5. Preserve the raw test output and CI artifacts needed for independent review.

Until those requirements are met, the golden vectors are **CANDIDATE / HOLD**, not VERIFIED. This Addendum does not promote them to PASS or GREEN.

## 8. Required status separation

The Explorer output MUST keep integrity and Replay outcomes as separate fields.

Integrity status vocabulary from Revision 2 remains:
- `INTEGRITY_OK`
- `INTEGRITY_FAILED`
- `INTEGRITY_UNKNOWN`
- `UNSUPPORTED_VERSION`
- `UNKNOWN_VERSION`
- `CHAIN_NOT_CHECKED`

Replay status vocabulary from Revision 2 remains:
- `REPLAY_MATCH`
- `REPLAY_MISMATCH`
- `REPLAY_INCONCLUSIVE`
- `REPLAY_CODE_VERSION_MISMATCH`
- `REPLAY_CODE_VERSION_UNKNOWN`
- `REPLAY_NOT_EXECUTED`

The reason `CODE_VERSION_SOURCE_CONFLICT` is a reason code under `REPLAY_CODE_VERSION_UNKNOWN`, not a new top-level status. No Replay status may overwrite, downgrade, or conceal an integrity failure. No integrity status may be inferred from a Replay match.

## 9. Acceptance criteria for independent review

An independent reviewer should be able to verify that this Addendum unambiguously requires:

- [ ] complete global Evidence and Verdict chain verification, ordered by SQL `sequence_id`;
- [ ] full cross-table Verdict→Evidence reference validation;
- [ ] version gate before any digest/canonicalization call;
- [ ] explicit disclosure that raw-to-parsed binding is `NOT_VERIFIED`;
- [ ] Replay match is not represented as proof of raw/parsed equivalence;
- [ ] CLI build identity source order and conflict fail-closed behavior;
- [ ] one `REPEATABLE READ`, `READ ONLY` transaction beginning before the first SELECT;
- [ ] exit code 30 for pre-check execution/configuration/database failures, never 0;
- [ ] golden vectors remain CANDIDATE / HOLD until exact assertions and terminal CI/PostgreSQL evidence exist;
- [ ] no production implementation, migration, schema, or merge authorization.

## 10. Final state

**Addendum publication state:** documentary draft, subject to independent review.  
**Explorer specification state:** **HOLD**.  
**Golden-vector state:** **CANDIDATE / HOLD**.  
**Production implementation authorization:** **NOT GRANTED**.  
**Merge authorization:** **NOT GRANTED**.  
**Frozen boundary:** **Δ = 0**.

The next state transition requires independent review of this Addendum and then terminal executable evidence for the golden vectors and PostgreSQL integration. Publication alone is not verification.
