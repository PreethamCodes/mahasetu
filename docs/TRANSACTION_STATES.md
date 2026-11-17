# Verification lifecycle

The authoritative transition graph is mahasetu-core/contracts/v1/states.json. Persist transitions and audit events atomically. Concurrent cancellation or completion uses a state precondition so exactly one wins. Terminal states have no outgoing edges. A new attempt after a terminal result uses a new transaction and key.

|State|Meaning|
|---|---|
|CREATED|Authenticated request accepted and correlated.|
|AUTHENTICATION_PENDING|Waiting for applicant identity or department authorization checks.|
|AUTHORIZED|Permitted scope has been established for at least one source.|
|PROCESSING|Provider calls, normalization and rule checks are in progress.|
|VERIFIED|All required checks completed successfully.|
|FAILED_CHECK|Valid evidence definitively failed an applicable approved rule.|
|PARTIAL|Some usable results exist and other checks are unresolved.|
|MANUAL_REVIEW|Conflicting identifiers or ambiguous evidence requires an officer.|
|INSUFFICIENT_DATA|Required evidence is missing.|
|RULES_NOT_CONFIGURED|No approved, applicable rule version exists.|
|ACCESS_DENIED|Policy or authorization blocks the required access.|
|PROVIDER_ERROR|Provider failure prevents completion.|
|CANCELLED|The requester cancelled before completion.|

The first four states are active. Invalid requester credentials receive HTTP 401 before creation. Retry transient provider failures within PROCESSING with backoff and a bounded attempt budget. Stop new calls on cancellation or revocation. Ignore late responses for decision purposes and audit their arrival. Repeating a cancellation of CANCELLED is idempotent; other completed requests return 409. A mixed result with unresolved required evidence goes to review unless an approved decisive rule explicitly determines the outcome. Technical faults never count as failed eligibility checks.

## Applicant flow mapping

The graph above is unchanged. The applicant flow (see [APPLICANT_FLOW.md](APPLICANT_FLOW.md)) uses it as follows. Exchanging the launch reference moves CREATED to AUTHENTICATION_PENDING. The first verified one-time code moves it to AUTHORIZED. The first department check moves it to PROCESSING. Further codes and checks for other departments are accepted while the state is AUTHENTICATION_PENDING, AUTHORIZED or PROCESSING. Completing the session evaluates the evidence and ends in a terminal state. If no department was ever granted, the transaction goes from AUTHENTICATION_PENDING to ACCESS_DENIED. If at least one grant exists but no check ran, completion passes through PROCESSING first, so every evaluated outcome (including INSUFFICIENT_DATA) is reached from PROCESSING as the graph requires. Success is VERIFIED, failure is FAILED_CHECK, and PARTIAL means some checks passed while others failed or stayed unresolved.

## How Core chooses the terminal state

Only REQUIRED checks decide. Core evaluates the approved, applicable rules over the standardized evidence and applies the first matching row: nothing authorized or retrieved gives ACCESS_DENIED; no applicable rule gives RULES_NOT_CONFIGURED; a definitive failure on valid evidence gives FAILED_CHECK (even if another department was unreachable); a record for a different student gives MANUAL_REVIEW; an unreachable department with no failure gives PROVIDER_ERROR; unresolved checks where a department returned no usable record at all give INSUFFICIENT_DATA; unresolved checks where every department returned something usable (for example a pending enrolment) give PARTIAL; otherwise VERIFIED. The same table is in mahasetu-core/README.md. A status such as pending or withheld is unresolved, not failed; an unrecognised status is unresolved too.
