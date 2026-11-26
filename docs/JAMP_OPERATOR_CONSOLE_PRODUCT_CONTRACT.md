# JAMP Operator Console — Product Contract

**Status:** BASELINE / APPROVED BY PRODUCT OWNER
**Branch:** feat/jamp-operator-console
**Product:** JAMP-2 · Trust Layer

## 1. Product decision

The dashboard is the **JAMP Operator Console**: one operator-facing control center for evidence research, provider operations, AI-assisted investigations, verification pipeline, immutable evidence, conflicts, and history.

Canonical visual baseline: [JAMP Operator Console concept](./design/jamp-operator-console.svg).

This contract and visual are the default reference for future dashboard work. They may be improved, extended, corrected, and populated. They are a starting product baseline, not a claim that these capabilities already exist. Do not silently replace this direction with a narrower pilot form.

## 2. Product principles

1. **AI researches; JAMP verifies.** AI may plan, search, summarize, and propose structured Claims. Only JAMP verification and arbitration may produce a Verdict.
2. **Evidence before conclusions.** Every factual result shown as verified links to its Claim, source acquisition, raw Evidence, policy/version, and Verdict.
3. **No synthetic state.** Counts, charts, provider health, queues, and activity come from persisted records or timestamped live checks. Missing data is UNKNOWN / NOT CONFIGURED, never fabricated zeroes or green indicators.
4. **Fail closed.** Missing, incomplete, stale, conflicting, or unverifiable evidence cannot become VERIFIED.
5. **Preserve provenance.** Raw source bytes and immutable ledger records are not rewritten by UI actions, AI summaries, or operator annotations.
6. **Separate provider classes.** Evidence/data providers (CoinGecko, MOEX, etc.) and AI/model providers (OpenRouter, direct model APIs, etc.) have separate registries, credentials, health, policies, and audit trails.
7. **Operator control.** AI cannot change policies, credentials, provider enablement, permissions, or verdicts. Sensitive actions require explicit operator authorization and audit logging.
8. **Extensible architecture.** Add source categories, AI vendors, workflows, and screens without rewriting the Trust Layer core.

## 3. Primary navigation and screens

| Area | Purpose | Minimum content / behavior |
|---|---|---|
| Overview | Operational state at a glance | Real verification totals by verdict, active/failed tasks, provider health, recent activity, freshness and system health |
| AI Chat | Natural-language research workspace | Conversations, task plan, tool activity, proposed Claims, linked evidence and verdict references, errors and usage/cost where available |
| Pipeline | Trace an investigation end to end | Claim → Acquisition → Evidence → Arbitration → Verdict → Replay; show each stage, timestamps, inputs, outputs, and stop reasons |
| Providers | Operate external sources | Separate Evidence Sources and AI Providers; registry, configuration state, capabilities, health checks, limits, last attempt, error history |
| Evidence Vault | Inspect canonical evidence | Search/filter by Claim, provider, time, status, completeness, digest; raw observation view, chain/provenance validation, safe export |
| Conflicts | Surface operational ambiguity without rewriting evidence | AMBIGUOUS / INCONCLUSIVE cases, source disagreement, missing required sources, stale observations, operator annotations and disposition history |
| History | Find prior work | Searchable Claims, conversations, runs, Verdicts, Evidence and operator actions |
| Settings | Govern the console | Versioned policies, model routing, permissions, secret references, limits, retention and audit configuration |

## 4. AI Operator contract

AI Chat is an internal operator assistant, not an autonomous truth authority.

Allowed:
- clarify an operator request and propose an investigation plan;
- use explicitly allow-listed research tools and configured providers;
- prepare structured Claim candidates with assumptions clearly marked;
- request JAMP verification through an audited tool boundary;
- explain results by citing concrete Claim ID, Evidence ID, Verdict ID, source and policy version;
- report unavailable tools, provider failures, incomplete evidence and uncertainty.

Forbidden:
- manufacture or edit raw Evidence;
- assign or promote a Verdict status;
- describe AI-generated text as a source observation;
- silently omit conflicting or failed providers;
- change policies, provider configuration, secrets, permissions, or account settings;
- run unapproved external or destructive actions.

Every AI tool call must record actor/session, timestamp, tool name, sanitized arguments, result/error, correlation ID, and links to resulting records. Secrets must never appear in prompts, browser payloads, or logs.

## 5. Provider management contract

### Evidence Sources
Each source exposes stable provider ID, display name, category/capabilities, endpoint identity, configuration state, enabled state, credential reference (never the secret), timeout/rate limits, last successful/failed attempt, current health with observation time, failure phase/reason, and Evidence links.

Health states: HEALTHY, DEGRADED, UNAVAILABLE, NOT_CONFIGURED, UNKNOWN. Health is time-bound operational telemetry, not a claim that source content is true.

### AI Providers
Maintain a distinct registry for model vendors and models: provider/model IDs, supported capabilities, configuration state, routing eligibility, limits, observed latency/errors, usage/cost when available, and credential references. Do not conflate model availability with evidence-source availability.

Enable/disable and configuration changes are versioned, permission-checked, and audited. Health checks are safe and bounded and must not create a Verdict.

## 6. Overview and operational metrics

All metrics are computed from canonical database records or timestamped health observations. Define denominators, time windows, and update times. Unknown or unavailable data is rendered as unknown, not zero. Charts link to underlying filtered records. The companion visual contains illustrative layout and example values only; it is not production telemetry.

## 7. Security and access baseline

Initial delivery may target one authenticated operator. Do not imply multi-user readiness. Keep provider credentials server-side; never expose API keys to the browser. Enforce same-origin/CSRF protections for mutations, rate limits, secure secret storage, action audit, and HTTPS before untrusted-network deployment. Multi-user roles and public registration are out of initial scope until separately approved.

## 8. Delivery sequence

1. **Foundation:** map existing API, ledger, schemas, and provider interfaces to this contract; produce a screen/API/data gap matrix and ADRs for material architectural choices.
2. **Console shell:** navigation, authenticated layout, truthful Overview, responsive design, error/empty/unknown states.
3. **Operational records:** History, Pipeline, Evidence Vault, Conflicts, each backed by persisted data.
4. **Evidence provider manager:** registry, health diagnostics, attempt history, safe configuration lifecycle.
5. **AI Provider registry and AI Chat:** model configuration, conversation persistence, allow-listed tools, usage controls, audited JAMP verification handoff.
6. **Integration and hardening:** browser E2E, authorization/CSRF/security checks, failure-path tests, terminal CI, post-merge CI.

Deliver in small vertical slices on this one long-lived branch. Do not create a new dashboard branch for each screen or enhancement. Keep unrelated work (including provider TLS diagnosis) on its own branch.

## 9. Definition of Done

The Operator Console is not complete until:
- all primary navigation areas above are implemented or explicitly deferred by product decision;
- every displayed metric/status is backed by real data and exposes its time/meaning;
- Evidence and Verdict screens link to immutable canonical records;
- the pipeline exposes actual stages and explicit stop reasons;
- both provider registries are distinct and operationally auditable;
- AI Chat can execute only approved tools and cannot assign Verdicts or mutate governance;
- errors, conflicts, missing data and unknown states are visible and fail closed;
- authentication, CSRF, secret handling and audit behavior are tested;
- real browser E2E covers a complete research → Claim → acquisition → Evidence → Verdict flow and key failure paths;
- terminal GitHub Actions evidence is available for the exact commit; post-merge CI is checked separately.

A green unit-test suite alone does not satisfy product acceptance.

## 10. Change control

This is the canonical dashboard baseline. Future contributors must:
- work on feat/jamp-operator-console for dashboard scope;
- inspect current branch head and working tree before changes;
- preserve unrelated changes and existing contracts;
- update this contract or add an ADR when changing navigation, authority boundaries, provider governance, data contracts, or acceptance criteria;
- attach screenshots and E2E evidence to meaningful UI milestones;
- state what is implemented versus conceptual; never present the reference mockup as a live product.

## 11. Current implementation truth

The existing W3 pilot is a narrow Claim form, Verdict view, Evidence view and Replay. It is useful infrastructure, but it is not the Operator Console defined here. This contract does not claim AI Chat, provider management, Overview, History, Conflicts, or full Pipeline are implemented.
