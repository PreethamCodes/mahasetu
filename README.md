# mahasetu-core

Requester gateway, authorization, standardization, orchestration, rule execution, reports and audit for MahaSetu. It owns only the `mahasetu` schema and its Alembic revisions, and talks to the departments only through their APIs.

The authoritative API is [contracts/v1/openapi.json](contracts/v1/openapi.json) (version 1.1.0). [docs/APPLICANT_FLOW.md](../docs/APPLICANT_FLOW.md) explains the sequence. This service implements every operation in it.

## What it does

| Area | Behaviour |
|---|---|
| Requesting service | `POST /v1/verification-requests` (idempotent), status, cancel, results, requirements. One bearer token per registered service; a service sees only its own transactions. |
| Applicant session | A launch reference is exchanged by the website's server for a short-lived session token bound to one transaction. Only hashes of launch references and session tokens are stored. |
| One-time code and consent | Six digits, five minutes, three attempts, per department. A verified code is the applicant's consent for that department only; it can be withdrawn. Only a keyed hash of the code is stored. |
| Department checks | Revenue (SOAP 1.2 with HTTP Basic), Examination (flat JSON with `X-API-Key`) and University (nested JSON with an expiring token that Core refreshes). Calls run in the background; the website polls. Transient faults (timeouts, 5xx, connection errors) are retried within three attempts; other faults are not. |
| Standardization | Each department answer becomes per-category canonical records that validate against `contracts/v1/canonical.schema.json`. Only the requested categories are kept; a legitimate zero stays zero; a record for a different student is dropped. Raw department bodies are never stored or logged. |
| Rules | Only **approved, applicable** rule versions execute (scheme, effective dates, valid operands). The engine is deterministic. See "Decisions" below. |
| Reports and messages | A report lists each check with a plain-language reason. A result other than `VERIFIED` or `CANCELLED` queues one simulated SMS with the reasons; one-time codes use the same outbox. |

## Decisions

Only REQUIRED checks decide the state, and the first rule that matches wins:

| Order | Condition | State |
|---|---|---|
| 1 | Nothing was authorized or retrieved | `ACCESS_DENIED` |
| 2 | No approved applicable rule exists | `RULES_NOT_CONFIGURED` |
| 3 | A required check definitively failed on valid evidence (expired, over the limit, not a pass) | `FAILED_CHECK` |
| 4 | A department's record did not match the applicant's identifier | `MANUAL_REVIEW` |
| 5 | A department was unreachable and nothing failed | `PROVIDER_ERROR` |
| 6 | A required check is unresolved and a department gave no usable record at all | `INSUFFICIENT_DATA` |
| 7 | A required check is unresolved but every department gave something usable (for example a pending enrolment) | `PARTIAL` |
| 8 | Every required check passed | `VERIFIED` |

An unreachable department is `NEEDS_REVIEW` with a `PROVIDER_` reason code and is never counted as a failed check. Declined, withdrawn or skipped departments count as missing evidence. The seeded demonstration rules are pre-approved synthetic rules (see `db/seed.sql`); AI drafting and human approval screens return in a later stage, and the database already stops the runtime identity from approving its own candidates.

## Run locally

Core needs a migrated and seeded `mahasetu` schema. The easiest route is the deployment project's `scripts/bootstrap_local_db.py`, which also generates the credentials below. Otherwise:

```sh
pip install -r requirements.txt            # add requirements-test.txt for tests
export MIGRATION_DATABASE_URL=postgresql+psycopg://...   # migration identity
alembic upgrade head                        # applies mahasetu_0001 and mahasetu_0002
psql "$MIGRATION_DATABASE_URL" -f db/seed.sql
cp .env.example .env                        # then fill in DATABASE_URL and the credentials; never commit .env
uvicorn mahasetu_core.main:app --port 8000
```

`GET /health` reports database readiness. Configuration is described in `.env.example`: the runtime `DATABASE_URL` (scoped to the `mahasetu` schema), `DASHBOARD_CLIENT_TOKEN`, `OTP_HASH_KEY`, one `REQUESTER_TOKEN_<NAME>` per registered service (derived from its `credential_ref`), the department credentials, and `OTP_DEMO_DISCLOSURE` (demonstration only; refused when `CORE_ENVIRONMENT=production`). Secrets must be at least 24 characters. `RETURN_URL_<SERVICE_ID>` (for example `RETURN_URL_DEMO_PMS`) replaces the return address stored for a registered service, so one seeded database can serve a localhost and a hosted portal; it must be http(s) without credentials and https in production. On hosts that sleep when idle, raise `PROVIDER_TIMEOUT_SECONDS` (for example 30) so a department that is waking is not reported as unavailable.

## Tests

```sh
export CORE_TEST_DATABASE_URL=postgresql+psycopg://<runtime login>@host/db   # a migrated, seeded database
pytest tests
```

The suite uses the runtime role only, so it also proves the scoped grants and row-level security are sufficient. `tests/fakes.py` provides in-process departments that speak the real protocols; unit tests need no database. Tests add a few clearly named `TEST-*` registry rows and one pending rule version to the database they run against, so run them on a synthetic database only.

## Boundaries and known limits

The simulated SMS outbox keeps one-time-code bodies so a demonstration inbox can show them; a real gateway must replace it and must not persist codes (the listing already withholds them when `CORE_ENVIRONMENT=production`). Rate limiting beyond the per-department code limits is not implemented. Evaluation dates use India Standard Time. Rule `academic_year` is informational: applicability is decided by scheme and effective dates. The requirements operation reports the first purpose when a service has several. Public hosting and deployment remain the project owner's decision.
