# ADR-0008: Evidence-first arbitration pipeline

- Status: Accepted
- Scope: JAMP 2 provider verification route

## Decision

The route follows four ordered stages:

1. Fetch every required provider result without making a verdict.
2. Validate and append every raw response, including failed attempts, to the
   append-only evidence ledger.
3. Evaluate the complete source set exactly once through the source policy gate.
4. Run the conflict reducer only over source outcomes explicitly eligible under
   that gate.

`arbitrate_from_evidence` receives the claim, policy, fetched outcomes, and
ledger evidence records. It performs no network access. Evidence records are
provenance context; they are not reducer inputs.

The legacy async `arbitrate` entry point is retained as a deprecated
interface-compatibility wrapper: it preserves the callable signature and
fetches provider outcomes, but it does not run policy evaluation or conflict
reduction. Because this wrapper has no persisted ledger records to verify, it
always returns `INCONCLUSIVE` with `POLICY_GATE_BLOCKED` and
`EVIDENCE_LEDGER_REQUIRED`. Callers requiring arbitration must append and
read back evidence records, then invoke `arbitrate_from_evidence`; the wrapper
must not be used to obtain a verdict outside the evidence-ledger pipeline.

Partial policy failure is AMBIGUOUS. All required sources failed is INCONCLUSIVE.
Unknown provider timestamps and unconfirmed market calendars remain unknown;
neither may be filled by inference.
