"""API behaviour: credentials, validation, idempotency, expiry, one-time codes, inbox, isolation and rule governance."""
import json
import re
import uuid

import pytest
from conftest import CONTRACT, DASHBOARD_TOKEN, OTHER_TOKEN, PURPOSE, REQUESTER_TOKEN, conforms

from mahasetu_core.db import many, run


def error(response, status, code):
    assert response.status_code == status, response.text
    body = conforms("Error", response.json())
    assert body["code"] == code
    return body


def register(world, service, ref, token_env, token, evidence):
    """Register a throwaway requesting service using only the runtime role (idempotent)."""
    world.env.setenv(token_env, token)
    with world.core.db.begin() as conn:
        run(conn, """INSERT INTO mahasetu.registered_services (service_id, credential_ref, return_url, display_name)
            VALUES (:s, :r, 'http://localhost:3999/verification/return', :s) ON CONFLICT DO NOTHING""", s=service, r=ref)
        for provider, category in evidence:
            run(conn, """INSERT INTO mahasetu.access_policies VALUES (:s, :p, :c, :pu, 'DEMO_AUTHORIZATION', true)
                ON CONFLICT DO NOTHING""", s=service, p=provider, c=category, pu=PURPOSE)


# ---- credentials -------------------------------------------------------------------------------

def test_health(world):
    response = world.client.get("/health")
    assert response.status_code == 200 and conforms("Health", response.json()) == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope-nope-nope-nope-nope-nope"},
                                     {"Authorization": "Basic abc"}, {"Authorization": f"Bearer {DASHBOARD_TOKEN}"}])
def test_requester_credentials_are_enforced(world, headers):
    for call in (lambda: world.client.post("/v1/verification-requests", json=world.body(), headers=headers | {"Idempotency-Key": "k"}),
                 lambda: world.client.get(f"/v1/verification-requests/{uuid.uuid4()}", headers=headers),
                 lambda: world.client.get("/v1/services/DEMO-PMS/requirements", headers=headers)):
        response = call()
        error(response, 401, "INVALID_CREDENTIALS")
        assert response.headers["www-authenticate"] == "Bearer"


def test_each_credential_only_works_on_its_own_surface(world):
    assert world.client.get("/v1/messages", headers=world.requester()).status_code == 401
    assert world.client.post("/v1/applicant/sessions", json={"launch_reference": "x" * 40}, headers=world.requester()).status_code == 401
    assert world.client.get("/v1/applicant/session", headers=world.requester()).status_code == 401
    assert world.client.get("/v1/applicant/session", headers=world.dashboard).status_code == 401
    assert world.client.get("/v1/verification-requests/" + str(uuid.uuid4()), headers=world.dashboard).status_code == 401


def test_requirements(world):
    response = world.client.get("/v1/services/DEMO-PMS/requirements", headers=world.requester())
    assert response.status_code == 200
    body = response.json()
    assert body["purpose"] == PURPOSE and body["service_id"] == "DEMO-PMS"
    assert {r["provider_id"]: r["evidence_categories"] for r in body["requirements"]} == {
        "revenue": ["caste", "income"], "examination": ["marks"], "university": ["bonafide", "enrolment"]}
    error(world.client.get("/v1/services/OTHER/requirements", headers=world.requester()), 403, "SERVICE_MISMATCH")


# ---- creating requests ---------------------------------------------------------------------------

def test_create_validation(world):
    assert world.client.post("/v1/verification-requests", json=world.body(), headers=world.requester()).json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    error(world.create(applicant_mobile="1234567890"), 400, "INVALID_REQUEST")
    error(world.create(applicant_mobile="90000000"), 400, "INVALID_REQUEST")
    error(world.create(unexpected="x"), 400, "INVALID_REQUEST")
    error(world.create(requested_evidence=[]), 400, "INVALID_REQUEST")
    error(world.create(requested_evidence=[{"provider_id": "revenue", "evidence_categories": []}]), 400, "INVALID_REQUEST")
    twice = [{"provider_id": "revenue", "evidence_categories": ["income"]}] * 2
    error(world.create(requested_evidence=twice), 400, "DUPLICATE_PROVIDER")
    error(world.create(service_id="OTHER"), 403, "SERVICE_MISMATCH")
    error(world.create(requested_evidence=[{"provider_id": "ghost", "evidence_categories": ["x"]}]), 404, "UNKNOWN_PROVIDER")
    error(world.create(requested_evidence=[{"provider_id": "revenue", "evidence_categories": ["salary"]}]), 403, "ACCESS_POLICY_DENIED")
    error(world.create(purpose="MARKETING"), 403, "ACCESS_POLICY_DENIED")
    bad = world.client.post("/v1/verification-requests", content=b"{", headers=world.requester() | {"Idempotency-Key": "k", "Content-Type": "application/json"})
    error(bad, 400, "INVALID_REQUEST")
    assert "9000000001" not in error(world.create(applicant_mobile="9000000001", unexpected=1), 400, "INVALID_REQUEST")["message"]


def test_idempotent_replay(world):
    key, body = uuid.uuid4().hex, world.body()
    first = world.client.post("/v1/verification-requests", json=body, headers=world.requester() | {"Idempotency-Key": key})
    again = world.client.post("/v1/verification-requests", json=body, headers=world.requester() | {"Idempotency-Key": key})
    assert first.status_code == again.status_code == 201
    a, b = conforms("CreatedVerification", first.json()), conforms("CreatedVerification", again.json())
    assert a["transaction_reference"] == b["transaction_reference"] and a["launch_reference"] != b["launch_reference"]
    # the replay retired the first launch reference and only the newest one starts a session
    assert world.client.post("/v1/applicant/sessions", json={"launch_reference": a["launch_reference"]}, headers=world.dashboard).status_code == 404
    assert world.client.post("/v1/applicant/sessions", json={"launch_reference": b["launch_reference"]}, headers=world.dashboard).status_code == 201
    changed = world.client.post("/v1/verification-requests", json=body | {"purpose": PURPOSE, "applicant_mobile": "9000000009"},
                                headers=world.requester() | {"Idempotency-Key": key})
    error(changed, 409, "IDEMPOTENCY_CONFLICT")
    reordered = dict(body, requested_evidence=list(reversed(body["requested_evidence"])))  # same content, different order
    assert world.client.post("/v1/verification-requests", json=reordered, headers=world.requester() | {"Idempotency-Key": key}).status_code == 201


# ---- launch reference and session --------------------------------------------------------------

def test_launch_reference_rules(world):
    created = world.create().json()
    ref = created["launch_reference"]
    assert len(ref) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", ref)
    unknown = world.client.post("/v1/applicant/sessions", json={"launch_reference": "A" * 43}, headers=world.dashboard)
    error(unknown, 404, "LAUNCH_NOT_FOUND")
    error(world.client.post("/v1/applicant/sessions", json={"launch_reference": "short"}, headers=world.dashboard), 400, "INVALID_REQUEST")
    one = world.client.post("/v1/applicant/sessions", json={"launch_reference": ref}, headers=world.dashboard)
    two = world.client.post("/v1/applicant/sessions", json={"launch_reference": ref}, headers=world.dashboard)  # page reload
    assert one.status_code == two.status_code == 201 and one.json()["session_token"] != two.json()["session_token"]
    for grant in (one, two):
        assert world.client.get("/v1/applicant/session", headers={"Authorization": "Bearer " + grant.json()["session_token"]}).status_code == 200
    world.clock.advance(seconds=world.core.settings.launch_ttl_seconds + 1)
    error(world.client.post("/v1/applicant/sessions", json={"launch_reference": ref}, headers=world.dashboard), 410, "LAUNCH_EXPIRED")


def test_session_expiry_and_invalid_tokens(world):
    _, headers = world.begin()
    error(world.client.get("/v1/applicant/session"), 401, "SESSION_REQUIRED")
    error(world.client.get("/v1/applicant/session", headers={"Authorization": "Bearer " + "z" * 43}), 401, "SESSION_INVALID")
    world.clock.advance(seconds=world.core.settings.session_ttl_seconds + 1)
    error(world.client.get("/v1/applicant/session", headers=headers), 401, "SESSION_EXPIRED")


def test_session_is_bound_to_its_transaction(world):
    tx_a, headers_a = world.begin("MHSTU0001")
    tx_b, headers_b = world.begin("MHSTU0002")
    assert world.overview(headers_a)["transaction_reference"] == tx_a != tx_b
    assert world.overview(headers_b)["transaction_reference"] == tx_b


# ---- one-time code ---------------------------------------------------------------------------------

def test_otp_lifecycle_and_limits(world):
    tx, headers = world.begin("MHSTU0001")
    sent = world.send_otp(headers, "revenue")
    assert sent.status_code == 202
    challenge = conforms("OtpChallenge", sent.json())
    assert "demo_code" not in challenge and challenge["attempts_allowed"] == 3
    inbox = world.client.get("/v1/messages", params={"transaction_reference": tx}, headers=world.dashboard).json()["messages"]
    assert inbox[0]["kind"] == "OTP" and "Revenue Department" in inbox[0]["body"] and "9000000001" not in json.dumps(inbox)
    code = world.code_from_inbox(tx)
    with world.core.db.begin() as conn:  # the code itself is never stored
        stored = many(conn, "SELECT code_hash FROM mahasetu.otp_challenges WHERE request_id = :r", r=tx)
    assert code not in stored[0]["code_hash"] and len(stored[0]["code_hash"]) == 64
    too_soon = world.send_otp(headers, "revenue")
    error(too_soon, 429, "OTP_RESEND_TOO_SOON")
    assert int(too_soon.headers["retry-after"]) >= 1 and too_soon.json()["retryable"] is True
    wrong = "000000" if code != "000000" else "111111"
    first = world.verify_otp(headers, "revenue", challenge["challenge_reference"], wrong)
    assert error(first, 400, "INVALID_CODE") and "2 attempt" in first.json()["message"]
    error(world.verify_otp(headers, "revenue", challenge["challenge_reference"], wrong), 400, "INVALID_CODE")
    error(world.verify_otp(headers, "revenue", challenge["challenge_reference"], wrong), 429, "OTP_ATTEMPTS_EXHAUSTED")
    error(world.verify_otp(headers, "revenue", challenge["challenge_reference"], code), 429, "OTP_ATTEMPTS_EXHAUSTED")
    assert world.card(headers, "revenue")["status"] == "NOT_STARTED"
    fresh = world.send_otp(headers, "revenue")  # an exhausted challenge may be replaced at once
    assert fresh.status_code == 202
    newest = fresh.json()["challenge_reference"]
    error(world.verify_otp(headers, "revenue", newest, "12345"), 400, "INVALID_REQUEST")
    error(world.verify_otp(headers, "revenue", str(uuid.uuid4()), "123456"), 409, "NO_ACTIVE_CHALLENGE")
    assert world.verify_otp(headers, "revenue", newest, world.code_from_inbox(tx)).status_code == 200
    error(world.send_otp(headers, "revenue"), 409, "ALREADY_AUTHORIZED")
    error(world.verify_otp(headers, "revenue", newest, "123456"), 409, "ALREADY_AUTHORIZED")


def test_otp_expiry_and_supersede(world):
    tx, headers = world.begin("MHSTU0001")
    old = world.send_otp(headers, "examination").json()
    old_code = world.code_from_inbox(tx)
    world.clock.advance(seconds=world.core.settings.otp_resend_seconds + 1)
    new = world.send_otp(headers, "examination").json()
    error(world.verify_otp(headers, "examination", old["challenge_reference"], old_code), 409, "NO_ACTIVE_CHALLENGE")
    world.clock.advance(seconds=world.core.settings.otp_ttl_seconds + 1)
    error(world.verify_otp(headers, "examination", new["challenge_reference"], "123456"), 410, "OTP_EXPIRED")
    error(world.verify_otp(headers, "examination", new["challenge_reference"], "123456"), 410, "OTP_EXPIRED")


def test_otp_challenge_cap(world):
    tx, headers = world.begin("MHSTU0001")
    for _ in range(world.core.settings.otp_max_challenges):
        assert world.send_otp(headers, "university").status_code == 202
        world.clock.advance(seconds=world.core.settings.otp_resend_seconds + 1)
    error(world.send_otp(headers, "university"), 429, "OTP_LIMIT_REACHED")
    assert world.send_otp(headers, "revenue").status_code == 202  # the cap is per department


def test_demo_code_disclosure_is_opt_in(world, monkeypatch):
    import dataclasses
    tx, headers = world.begin("MHSTU0001")
    world.core.settings = dataclasses.replace(world.core.settings, demo_otp_disclosure=True)
    challenge = conforms("OtpChallenge", world.send_otp(headers, "revenue").json())
    assert challenge["demo_code"] == world.code_from_inbox(tx)
    from mahasetu_core.settings import Settings
    monkeypatch.setenv("OTP_DEMO_DISCLOSURE", "enabled")
    monkeypatch.setenv("CORE_ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="OTP_DEMO_DISCLOSURE"):
        Settings.from_env()


def test_department_path_checks(world):
    _, headers = world.begin()
    error(world.client.post("/v1/applicant/session/departments/ghost/otp", headers=headers), 404, "UNKNOWN_DEPARTMENT")
    error(world.client.post("/v1/applicant/session/departments/Bad_Id/otp", headers=headers), 400, "INVALID_REQUEST")
    error(world.client.post("/v1/applicant/session/departments/ghost/verify", headers=headers), 404, "UNKNOWN_DEPARTMENT")
    error(world.client.post("/v1/applicant/session/departments/ghost/decline", headers=headers), 404, "UNKNOWN_DEPARTMENT")
    error(world.client.post("/v1/applicant/session/departments/ghost/otp/verify", json={"challenge_reference": str(uuid.uuid4()), "code": "123456"}, headers=headers), 404, "UNKNOWN_DEPARTMENT")


# ---- message inbox ------------------------------------------------------------------------------------

def test_message_inbox_paging_and_filters(world):
    tx, headers, _ = world.run_all("MHSTU0002")
    everything = world.client.get("/v1/messages", params={"transaction_reference": tx}, headers=world.dashboard).json()
    conforms("MessageList", everything)
    assert [m["kind"] for m in everything["messages"]].count("OTP") == 3 and len(everything["messages"]) == 4
    seen, cursor = [], None
    while True:
        params = {"transaction_reference": tx, "limit": 1} | ({"cursor": cursor} if cursor else {})
        page = conforms("MessageList", world.client.get("/v1/messages", params=params, headers=world.dashboard).json())
        seen += [m["message_reference"] for m in page["messages"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [m["message_reference"] for m in everything["messages"]]  # newest first, no gaps or repeats
    only = world.client.get("/v1/messages", params={"transaction_reference": tx, "kind": "VERIFICATION_RESULT"}, headers=world.dashboard).json()
    assert len(only["messages"]) == 1 and "ref" in only["messages"][0]["body"]
    for params in ({"limit": 0}, {"limit": 101}, {"kind": "EMAIL"}, {"transaction_reference": "nope"}):
        error(world.client.get("/v1/messages", params=params, headers=world.dashboard), 400, "INVALID_REQUEST")
    error(world.client.get("/v1/messages", params={"cursor": "garbage"}, headers=world.dashboard), 400, "INVALID_CURSOR")
    assert "9000000002" not in json.dumps(everything)


# ---- isolation between requesting services -------------------------------------------------------------

def test_services_cannot_see_each_others_transactions(world):
    register(world, "TEST-OTHER", "secret-ref/other", "REQUESTER_TOKEN_OTHER", OTHER_TOKEN, [("revenue", "income")])
    tx, _ = world.begin("MHSTU0001")
    other = world.requester(OTHER_TOKEN)
    for response in (world.client.get(f"/v1/verification-requests/{tx}", headers=other),
                     world.client.get(f"/v1/verification-requests/{tx}/results", headers=other),
                     world.client.post(f"/v1/verification-requests/{tx}/cancel", headers=other)):
        error(response, 404, "REQUEST_NOT_FOUND")
    error(world.create(token=OTHER_TOKEN), 403, "SERVICE_MISMATCH")  # DEMO-PMS body with the other credential
    assert world.client.get(f"/v1/verification-requests/{tx}", headers=world.requester()).status_code == 200
    error(world.client.get(f"/v1/verification-requests/{uuid.uuid4()}", headers=world.requester()), 404, "REQUEST_NOT_FOUND")
    error(world.client.get("/v1/verification-requests/not-a-uuid", headers=world.requester()), 400, "INVALID_REQUEST")


def test_each_service_gets_its_own_return_address(world):
    register(world, "TEST-OTHER", "secret-ref/other", "REQUESTER_TOKEN_OTHER", OTHER_TOKEN, [("revenue", "income")])
    body = {"service_id": "TEST-OTHER", "application_reference": "A-1", "applicant_reference": "MHSTU0001",
            "applicant_mobile": "9000000001", "purpose": PURPOSE,
            "requested_evidence": [{"provider_id": "revenue", "evidence_categories": ["income"]}]}
    created = world.client.post("/v1/verification-requests", json=body, headers=world.requester(OTHER_TOKEN) | {"Idempotency-Key": uuid.uuid4().hex})
    assert created.status_code == 201
    grant = world.client.post("/v1/applicant/sessions", json={"launch_reference": created.json()["launch_reference"]}, headers=world.dashboard)
    headers = {"Authorization": "Bearer " + grant.json()["session_token"]}
    done = world.complete(headers).json()
    assert done["return_url"].startswith("http://localhost:3999/verification/return?ref=") and done["state"] == "ACCESS_DENIED"


# ---- rule governance -------------------------------------------------------------------------------------

def test_no_approved_rules_means_rules_not_configured(world):
    register(world, "TEST-NORULES", "secret-ref/norules", "REQUESTER_TOKEN_NORULES", "norules-token-for-tests-0123456789", [("revenue", "income")])
    body = {"service_id": "TEST-NORULES", "application_reference": "A-2", "applicant_reference": "MHSTU0001",
            "applicant_mobile": "9000000001", "purpose": PURPOSE,
            "requested_evidence": [{"provider_id": "revenue", "evidence_categories": ["income"]}]}
    created = world.client.post("/v1/verification-requests", json=body, headers=world.requester("norules-token-for-tests-0123456789") | {"Idempotency-Key": uuid.uuid4().hex})
    tx = created.json()["transaction_reference"]
    grant = world.client.post("/v1/applicant/sessions", json={"launch_reference": created.json()["launch_reference"]}, headers=world.dashboard)
    headers = {"Authorization": "Bearer " + grant.json()["session_token"]}
    world.authorize(tx, headers, "revenue")
    world.check(headers, "revenue")
    done = world.complete(headers).json()
    assert done["state"] == "RULES_NOT_CONFIGURED" and done["message_queued"]
    report = world.report(headers)
    assert report["checks"][0]["reason_code"] == "RULES_NOT_CONFIGURED" and report["recommendation"] == "MANUAL_REVIEW"
    assert "No approved rules" in world.result_messages(tx)[0]["body"]


def test_only_approved_versions_execute(world):
    with world.core.db.begin() as conn:
        definition = {"rule_id": "TEST-PENDING-STRICT", "rule_name": "Strict pending rule", "description": "Test only.",
                      "scheme_id": "DEMO-PMS", "academic_year": 2026, "evidence_field": "revenue.annual_income",
                      "operator": "LESS_THAN_OR_EQUAL", "expected_value": 1, "severity": "REQUIRED",
                      "source_document": "Test", "source_page": 1, "source_section": "T", "source_text": "Test only.",
                      "effective_from": "2026-01-01", "effective_until": "2030-12-31", "version": 1,
                      "approval_status": "PENDING", "approved_by": None, "approved_at": None}
        run(conn, """INSERT INTO mahasetu.rule_versions (rule_id, version, scheme_id, guideline_id, definition)
            VALUES ('TEST-PENDING-STRICT', 1, 'DEMO-PMS', 'DEMO-GUIDELINE-2026', CAST(:d AS jsonb))
            ON CONFLICT DO NOTHING""", d=json.dumps(definition))
    _, headers, done = world.run_all("MHSTU0001")
    assert done["state"] == "VERIFIED"  # income 150000 would fail the pending rule if it ran
    assert "TEST-PENDING-STRICT" not in {c["rule_id"] for c in world.report(headers)["checks"]}


def test_runtime_role_cannot_approve_rules(world):
    from sqlalchemy.exc import DBAPIError
    with pytest.raises(DBAPIError), world.core.db.begin() as conn:
        run(conn, """INSERT INTO mahasetu.rule_versions (rule_id, version, scheme_id, guideline_id, definition,
            approval_status, approved_by, approved_at) VALUES ('TEST-SELF-APPROVED', 1, 'DEMO-PMS', 'DEMO-GUIDELINE-2026',
            '{}'::jsonb, 'APPROVED', gen_random_uuid(), now())""")


def test_report_lists_the_seeded_rules(world):
    _, headers, _ = world.run_all("MHSTU0001")
    checks = world.report(headers)["checks"]
    assert [c["rule_id"] for c in checks] == ["DEMO-INCOME", "DEMO-INCOME-CERT", "DEMO-CASTE-CERT", "DEMO-SSC",
                                              "DEMO-INTERMEDIATE", "DEMO-REGISTRATION", "DEMO-ENROLMENT", "DEMO-BONAFIDE"]
    assert all(c["outcome"] == "PASS" and c["rule_version"] == 1 and c["rule_name"] for c in checks)
    assert {c["evidence_reference"] for c in checks} >= {"income-1", "caste-1", "ENR-1", "SSC1,INTERMEDIATE1"}


# ---- contract -----------------------------------------------------------------------------------------------

def test_routes_match_the_contract(world):
    declared = {(method.upper(), re.sub(r"\{[^}]+\}", "{}", path)) for path, item in CONTRACT["paths"].items() for method in item}
    served = {(method, re.sub(r"\{[^}:]+(:[^}]+)?\}", "{}", route.path)) for route in world.client.app.routes
              if getattr(route, "methods", None) for method in route.methods if method != "HEAD"}
    assert declared == served


def test_unknown_route_uses_the_error_envelope(world):
    error(world.client.get("/v1/nothing"), 404, "NOT_FOUND")
    assert world.client.get("/health").headers["cache-control"] == "no-store"


def test_production_inbox_withholds_one_time_codes(world):
    import dataclasses
    tx, headers = world.begin("MHSTU0001")
    world.send_otp(headers, "revenue")
    world.core.settings = dataclasses.replace(world.core.settings, environment="production")
    body = world.client.get("/v1/messages", params={"transaction_reference": tx}, headers=world.dashboard).json()["messages"][0]["body"]
    assert body == "A one-time code was sent to this number." and not re.search(r"\d{6}", body)
