# W3 — JAMP 2 Pilot Dashboard

**Status:** implementation baseline on \`feat/w3-dashboard-pilot\`  
**Base:** \`main\` at \`c0561f82c3cb995a8d72bf343d121142a5afd13f\`  
**Scope:** one operator, structured Claim submission, Verdict view, Evidence view, and Replay.

## Architecture decision

Jinja2 server-rendered HTML is served by the existing FastAPI application. No Node/npm build is introduced. The Dashboard is an authenticated operator interface with limited actions, not a public read-only portal: Claim submission creates records and Replay recalculates from stored evidence.

## Security boundary

- The browser never receives \`JAMP2_API_KEY\`; the credential remains in the FastAPI process.
- Dashboard access requires \`JAMP2_DASHBOARD_PASSWORD\` and HTTP Basic username \`pilot\`.
- Missing password configuration fails closed with HTTP 503; invalid credentials return 401.
- Claim submission and Replay require same-origin POST, exact Origin, and \`X-JAMP-Dashboard-Request: 1\`.
- Dashboard actions call existing authenticated \`/v1/*\` routes through HTTPX ASGI transport in-process; no loopback network request is made.
- Do not expose this pilot interface on an untrusted network. Public deployment requires HTTPS, rate limiting, credential lifecycle, and a reviewed session/CSRF model.

## Screens and behavior

| Screen | Route | Behavior |
|---|---|---|
| Claim form | \`GET /\` | Structured Claim form; supported pairs are crypto_asset/USD and equity_security/RUB |
| Verdict | \`GET /verdicts/{id}\` | Status, explanation, reason codes, versions, and linked evidence |
| Evidence | \`GET /evidence/{id}\` | Raw bytes decoded for display, independently recalculated SHA-256, timestamps, versions, chain metadata |

Replay is a same-origin POST at \`/verdicts/{id}/replay\`. The UI displays the actual API response, including \`network_calls\`; it never synthesizes a zero.

## Evidence rules

- Known verdict statuses receive their specified color; unknown statuses are neutral and explicitly described.
- HTTP status is transport metadata, not a verdict.
- Evidence SHA-256 is recalculated from exact stored bytes before integrity confirmation.
- UI states “integrity ≠ truth”.
- captured_at_utc, fetched_at, provider_observed_at, source_date, and legacy source_timestamp remain distinct.
- Missing links, ledger failures, and digest mismatches are errors, not empty success states.

## Dependencies

- \`jinja2>=3.1,<4\` for templates.
- \`httpx>=0.27,<1\` moved into runtime dependencies for in-process ASGI calls to the authenticated API.
- No frontend framework or build system.

## Acceptance criteria

1. Dashboard returns 503 when password configuration is missing and 401 for invalid credentials.
2. Home, Verdict, and Evidence pages render from server-side ledger data.
3. Claim submission and Replay call existing authenticated API routes in-process; browser never receives API key.
4. POST actions reject missing/cross-origin Origin and missing request marker.
5. Evidence digest is recalculated from exact stored bytes before confirmation.
6. Unknown statuses are neutral.
7. Errors remain explicit; no empty-result or synthetic verdict fallback.
8. Ruff, Mypy, unit tests, and PostgreSQL integration suite pass in terminal GitHub Actions.
9. Changes remain confined to this branch; main, PR #24, and JAMP-1 are unchanged.

## Exclusions

No MOEX TLS changes, multi-source expansion, AI Claim extractor, users/organizations/roles, billing, marketplace integrations, history browsing, public deployment, or JAMP-1 changes.
