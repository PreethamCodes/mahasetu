SET LOCAL search_path TO mahasetu, pg_catalog;

-- Display data for the applicant-facing pages and messages.
ALTER TABLE registered_services ADD COLUMN display_name text;
UPDATE registered_services SET display_name = service_id WHERE display_name IS NULL;
ALTER TABLE registered_services ALTER COLUMN display_name SET NOT NULL;

CREATE TABLE evidence_categories (
    provider_id text NOT NULL REFERENCES registered_providers,
    category text NOT NULL,
    label text NOT NULL,
    description text NOT NULL,
    PRIMARY KEY (provider_id, category)
);
ALTER TABLE evidence_categories ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON evidence_categories TO mahasetu_runtime;
CREATE POLICY service_access ON evidence_categories TO mahasetu_runtime USING (true) WITH CHECK (true);

-- Requests: the applicant's mobile (for codes and result messages), the launch reference (hash only) and completion time.
ALTER TABLE verification_requests
    ADD COLUMN applicant_mobile text CHECK (applicant_mobile ~ '^[6-9][0-9]{9}$'),
    ADD COLUMN launch_hash text,
    ADD COLUMN launch_expires_at timestamptz,
    ADD COLUMN completed_at timestamptz,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
CREATE UNIQUE INDEX verification_requests_launch_hash ON verification_requests (launch_hash) WHERE launch_hash IS NOT NULL;

-- Short-lived applicant sessions. Only a hash of the bearer token is stored.
CREATE TABLE applicant_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id uuid NOT NULL REFERENCES verification_requests,
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
ALTER TABLE applicant_sessions ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON applicant_sessions TO mahasetu_runtime;
CREATE POLICY service_access ON applicant_sessions TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON applicant_sessions (request_id);

-- One-time-code challenges per transaction and department. Only a keyed hash of the code is stored.
CREATE TABLE otp_challenges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id uuid NOT NULL REFERENCES verification_requests,
    provider_id text NOT NULL REFERENCES registered_providers,
    code_hash text NOT NULL,
    attempts int NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','VERIFIED','EXHAUSTED','SUPERSEDED','EXPIRED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    resend_after timestamptz NOT NULL,
    verified_at timestamptz,
    CHECK ((status = 'VERIFIED') = (verified_at IS NOT NULL))
);
ALTER TABLE otp_challenges ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON otp_challenges TO mahasetu_runtime;
CREATE POLICY service_access ON otp_challenges TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON otp_challenges (request_id, provider_id, created_at);

-- Retrieval progress per department. run_id lets Core ignore a response from a superseded or stopped run.
CREATE TABLE department_checks (
    request_id uuid NOT NULL REFERENCES verification_requests,
    provider_id text NOT NULL REFERENCES registered_providers,
    status text NOT NULL CHECK (status IN ('RETRIEVING','COMPLETED','UNAVAILABLE','STOPPED')),
    run_id uuid,
    detail text CHECK (detail IS NULL OR length(detail) <= 240),
    attempts int NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (request_id, provider_id)
);
ALTER TABLE department_checks ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON department_checks TO mahasetu_runtime;
CREATE POLICY service_access ON department_checks TO mahasetu_runtime USING (true) WITH CHECK (true);

-- Simulated SMS outbox. A sender interface marks messages SENT; a real gateway can replace the simulator.
CREATE TABLE message_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id uuid REFERENCES verification_requests,
    kind text NOT NULL CHECK (kind IN ('OTP','VERIFICATION_RESULT')),
    recipient_mobile text NOT NULL CHECK (recipient_mobile ~ '^[6-9][0-9]{9}$'),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 480),
    reason_codes text[] NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','SENT','FAILED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    CHECK ((status = 'SENT') = (sent_at IS NOT NULL))
);
ALTER TABLE message_outbox ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON message_outbox TO mahasetu_runtime;
CREATE POLICY service_access ON message_outbox TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON message_outbox (created_at DESC, id DESC);
CREATE INDEX ON message_outbox (request_id);
-- Exactly one result message per transaction.
CREATE UNIQUE INDEX one_result_message ON message_outbox (request_id) WHERE kind = 'VERIFICATION_RESULT';

-- Standardized evidence: one record per category, current unless superseded or redacted on revocation.
ALTER TABLE canonical_records
    ADD COLUMN category text NOT NULL DEFAULT 'unspecified',
    ADD COLUMN evidence_status text NOT NULL DEFAULT 'PRESENT'
        CHECK (evidence_status IN ('PRESENT','MISSING','INVALID','EXPIRED','UNAVAILABLE')),
    ADD COLUMN superseded_at timestamptz;
ALTER TABLE canonical_records ALTER COLUMN category DROP DEFAULT, ALTER COLUMN evidence_status DROP DEFAULT;
CREATE INDEX canonical_records_current ON canonical_records (request_id) WHERE superseded_at IS NULL;

-- Results carry the department and the plain-language reason. provider_id is 'mahasetu' for transaction-level reasons.
ALTER TABLE verification_results
    ADD COLUMN provider_id text NOT NULL DEFAULT 'mahasetu',
    ADD COLUMN message text NOT NULL DEFAULT 'No reason recorded.' CHECK (length(message) BETWEEN 1 AND 240),
    ADD COLUMN position int NOT NULL DEFAULT 0,
    ADD COLUMN severity text NOT NULL DEFAULT 'REQUIRED' CHECK (severity IN ('REQUIRED','ADVISORY')),
    ADD COLUMN recorded_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE verification_results ALTER COLUMN provider_id DROP DEFAULT, ALTER COLUMN message DROP DEFAULT,
    ALTER COLUMN severity DROP DEFAULT;
CREATE UNIQUE INDEX verification_results_position ON verification_results (request_id, position);

-- Department calls: technical metadata only, never raw department bodies.
ALTER TABLE provider_call_logs
    ADD COLUMN attempts int,
    ADD COLUMN duration_ms int,
    ADD COLUMN detail text CHECK (detail IS NULL OR length(detail) <= 240);

GRANT USAGE ON ALL SEQUENCES IN SCHEMA mahasetu TO mahasetu_runtime;
