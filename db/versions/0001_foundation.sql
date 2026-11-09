CREATE SCHEMA IF NOT EXISTS mahasetu;
SET LOCAL search_path TO mahasetu, pg_catalog;
CREATE ROLE mahasetu_runtime NOLOGIN;
REVOKE ALL ON SCHEMA mahasetu FROM PUBLIC;
GRANT USAGE ON SCHEMA mahasetu TO mahasetu_runtime;

CREATE TABLE registered_services (
    service_id text primary key,
    credential_ref text unique not null,
    return_url text not null
);
ALTER TABLE registered_services ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON registered_services TO mahasetu_runtime;
CREATE POLICY service_access ON registered_services TO mahasetu_runtime USING (true) WITH CHECK (true);

CREATE TABLE registered_providers (
    provider_id text primary key,
    display_name text not null
);
ALTER TABLE registered_providers ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON registered_providers TO mahasetu_runtime;
CREATE POLICY service_access ON registered_providers TO mahasetu_runtime USING (true) WITH CHECK (true);

CREATE TABLE provider_endpoints (
    id bigint generated always as identity primary key,
    provider_id text not null references registered_providers,
    url text not null,
    schema_version text not null,
    unique(provider_id,url,schema_version)
);
ALTER TABLE provider_endpoints ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON provider_endpoints TO mahasetu_runtime;
CREATE POLICY service_access ON provider_endpoints TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON provider_endpoints (provider_id);

CREATE TABLE access_policies (
    service_id text references registered_services,
    provider_id text references registered_providers,
    category text,
    purpose text,
    basis text not null,
    authorization_required boolean not null,
    primary key(service_id,provider_id,category,purpose)
);
ALTER TABLE access_policies ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON access_policies TO mahasetu_runtime;
CREATE POLICY service_access ON access_policies TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON access_policies (service_id);
CREATE INDEX ON access_policies (provider_id);

CREATE TABLE verification_requests (
    id uuid primary key default gen_random_uuid(),
    applicant_ref text not null,
    application_ref text not null,
    service_id text not null references registered_services,
    purpose text not null,
    requested_evidence jsonb not null,
    idempotency_key text not null,
    payload_digest text not null,
    state text not null check(state in ('CREATED','AUTHENTICATION_PENDING','AUTHORIZED','PROCESSING','VERIFIED','FAILED_CHECK','PARTIAL','MANUAL_REVIEW','INSUFFICIENT_DATA','RULES_NOT_CONFIGURED','ACCESS_DENIED','PROVIDER_ERROR','CANCELLED')),
    created_at timestamptz not null default now(),
    unique(service_id,idempotency_key)
);
ALTER TABLE verification_requests ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON verification_requests TO mahasetu_runtime;
CREATE POLICY service_access ON verification_requests TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON verification_requests (service_id);

CREATE TABLE authorization_records (
    id uuid primary key default gen_random_uuid(),
    request_id uuid not null references verification_requests,
    provider_id text not null references registered_providers,
    categories text[] not null check(cardinality(categories)>0),
    status text not null check(status in ('PENDING','GRANTED','DENIED','REVOKED','EXPIRED')),
    decided_at timestamptz,
    expires_at timestamptz,
    revoked_at timestamptz,
    check(status='PENDING' or decided_at is not null),
    check(status <> 'REVOKED' or revoked_at is not null),
    unique(request_id,provider_id)
);
ALTER TABLE authorization_records ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON authorization_records TO mahasetu_runtime;
CREATE POLICY service_access ON authorization_records TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON authorization_records (request_id);
CREATE INDEX ON authorization_records (provider_id);

CREATE TABLE provider_call_logs (
    id bigint generated always as identity primary key,
    request_id uuid not null references verification_requests,
    provider_id text not null references registered_providers,
    outcome text not null,
    recorded_at timestamptz not null default now()
);
ALTER TABLE provider_call_logs ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON provider_call_logs TO mahasetu_runtime;
CREATE POLICY service_access ON provider_call_logs TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON provider_call_logs (request_id);
CREATE INDEX ON provider_call_logs (provider_id);

CREATE TABLE audit_events (
    id bigint generated always as identity primary key,
    request_id uuid references verification_requests,
    actor_ref text not null,
    event_type text not null,
    detail jsonb not null default '{}',
    recorded_at timestamptz not null default now()
);
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON audit_events TO mahasetu_runtime;
CREATE POLICY service_access ON audit_events TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON audit_events (request_id);

CREATE TABLE canonical_records (
    id uuid primary key default gen_random_uuid(),
    request_id uuid not null references verification_requests,
    provider_id text not null references registered_providers,
    source_record_reference text,
    retrieved_at timestamptz not null,
    evidence jsonb not null
);
ALTER TABLE canonical_records ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON canonical_records TO mahasetu_runtime;
CREATE POLICY service_access ON canonical_records TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON canonical_records (request_id);
CREATE INDEX ON canonical_records (provider_id);

CREATE TABLE guideline_documents (
    id text primary key,
    object_path text unique not null,
    content_sha256 text not null check(length(content_sha256)=64)
);
ALTER TABLE guideline_documents ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON guideline_documents TO mahasetu_runtime;
CREATE POLICY service_access ON guideline_documents TO mahasetu_runtime USING (true) WITH CHECK (true);

CREATE TABLE rule_versions (
    rule_id text,
    version int check(version>0),
    scheme_id text not null,
    guideline_id text not null references guideline_documents,
    definition jsonb not null,
    approval_status text not null default 'PENDING' check(approval_status in ('PENDING','APPROVED','REJECTED')),
    approved_by uuid,
    approved_at timestamptz,
    check((approval_status='APPROVED')=(approved_by is not null and approved_at is not null)),
    primary key(rule_id,version)
);
ALTER TABLE rule_versions ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON rule_versions TO mahasetu_runtime;
CREATE POLICY service_access ON rule_versions TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON rule_versions (guideline_id);

CREATE TABLE verification_results (
    id uuid primary key default gen_random_uuid(),
    request_id uuid not null references verification_requests,
    rule_id text,
    version int,
    outcome text not null,
    reason_code text not null,
    evidence_refs uuid[] not null default '{}',
    foreign key(rule_id,version) references rule_versions match full
);
ALTER TABLE verification_results ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON verification_results TO mahasetu_runtime;
CREATE POLICY service_access ON verification_results TO mahasetu_runtime USING (true) WITH CHECK (true);
CREATE INDEX ON verification_results (request_id);
-- Keep reviewer privileges separate from the credential used to propose candidates.
CREATE ROLE mahasetu_reviewer NOLOGIN;
GRANT USAGE ON SCHEMA mahasetu TO mahasetu_reviewer;
GRANT SELECT ON rule_versions TO mahasetu_reviewer;
GRANT UPDATE (approval_status, approved_by, approved_at) ON rule_versions TO mahasetu_reviewer;
CREATE POLICY reviewer_read ON rule_versions FOR SELECT TO mahasetu_reviewer USING (true);
CREATE POLICY reviewer_decision ON rule_versions FOR UPDATE TO mahasetu_reviewer
    USING (approval_status = 'PENDING') WITH CHECK (approval_status IN ('APPROVED','REJECTED'));
ALTER POLICY service_access ON rule_versions USING (true) WITH CHECK (
    approval_status = 'PENDING' AND approved_by IS NULL AND approved_at IS NULL
);
REVOKE UPDATE ON rule_versions FROM mahasetu_runtime;
GRANT UPDATE (definition) ON rule_versions TO mahasetu_runtime;
ALTER TABLE rule_versions ADD CONSTRAINT decision_metadata CHECK (
    approval_status = 'APPROVED' OR (approved_by IS NULL AND approved_at IS NULL)
);
CREATE FUNCTION protect_reviewed_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.approval_status <> 'PENDING' THEN
        RAISE EXCEPTION 'Reviewed versions are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER protect_reviewed_version BEFORE UPDATE ON rule_versions
    FOR EACH ROW EXECUTE FUNCTION protect_reviewed_version();
ALTER TABLE verification_results ADD CONSTRAINT check_outcome CHECK (
    outcome IN ('PASS','FAIL','NEEDS_REVIEW') AND (outcome = 'NEEDS_REVIEW' OR rule_id IS NOT NULL)
);
CREATE FUNCTION require_approved_result_rule() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.rule_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM mahasetu.rule_versions WHERE rule_id = NEW.rule_id
        AND version = NEW.version AND approval_status = 'APPROVED'
    ) THEN
        RAISE EXCEPTION 'Verification results require an approved rule version';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER approved_result_rule BEFORE INSERT ON verification_results
    FOR EACH ROW EXECUTE FUNCTION require_approved_result_rule();
GRANT USAGE ON ALL SEQUENCES IN SCHEMA mahasetu TO mahasetu_runtime;
INSERT INTO storage.buckets (id,name,public) VALUES ('mahasetu-documents','mahasetu-documents',false);
GRANT USAGE ON SCHEMA storage TO mahasetu_runtime;
GRANT SELECT, INSERT ON storage.objects TO mahasetu_runtime;
CREATE POLICY mahasetu_read ON storage.objects FOR SELECT TO mahasetu_runtime USING (bucket_id='mahasetu-documents');
CREATE POLICY mahasetu_upload ON storage.objects FOR INSERT TO mahasetu_runtime WITH CHECK (bucket_id='mahasetu-documents');
