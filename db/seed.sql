-- Synthetic fixtures; snapshot date 2026-11-09.
BEGIN;
SET LOCAL search_path TO mahasetu, pg_catalog;
INSERT INTO registered_services VALUES ('DEMO-PMS', 'secret-ref/scholarship', 'http://localhost:3000/verification/return') ON CONFLICT DO NOTHING;
INSERT INTO registered_providers VALUES ('revenue', 'Demo revenue') ON CONFLICT DO NOTHING;
INSERT INTO provider_endpoints(provider_id,url,schema_version) VALUES ('revenue','http://revenue-api:8001/soap/v1','1') ON CONFLICT DO NOTHING;
INSERT INTO access_policies VALUES ('DEMO-PMS', 'revenue', 'income', 'SCHOLARSHIP_VERIFICATION', 'DEMO_AUTHORIZATION', 'true') ON CONFLICT DO NOTHING;
INSERT INTO registered_providers VALUES ('examination', 'Demo examination') ON CONFLICT DO NOTHING;
INSERT INTO provider_endpoints(provider_id,url,schema_version) VALUES ('examination','http://examination-api:8002/api/v1','1') ON CONFLICT DO NOTHING;
INSERT INTO access_policies VALUES ('DEMO-PMS', 'examination', 'marks', 'SCHOLARSHIP_VERIFICATION', 'DEMO_AUTHORIZATION', 'true') ON CONFLICT DO NOTHING;
INSERT INTO registered_providers VALUES ('university', 'Demo university') ON CONFLICT DO NOTHING;
INSERT INTO provider_endpoints(provider_id,url,schema_version) VALUES ('university','http://university-api:8003/api/v2','2') ON CONFLICT DO NOTHING;
INSERT INTO access_policies VALUES ('DEMO-PMS', 'university', 'enrolment', 'SCHOLARSHIP_VERIFICATION', 'DEMO_AUTHORIZATION', 'true') ON CONFLICT DO NOTHING;
INSERT INTO access_policies VALUES ('DEMO-PMS','revenue','caste','SCHOLARSHIP_VERIFICATION','DEMO_AUTHORIZATION',true) ON CONFLICT DO NOTHING;
INSERT INTO access_policies VALUES ('DEMO-PMS','university','bonafide','SCHOLARSHIP_VERIFICATION','DEMO_AUTHORIZATION',true) ON CONFLICT DO NOTHING;
COMMIT;
