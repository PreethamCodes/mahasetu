"""Applicant flow end to end through the HTTP API, against a real PostgreSQL and fake departments."""
import dataclasses
import re
import uuid

import pytest
from conftest import PURPOSE, conforms

EXPECTED = {
    "MHSTU0001": ("VERIFIED", False, ()),
    "MHSTU0002": ("FAILED_CHECK", True, ("Annual income is above the limit",)),
    "MHSTU0003": ("FAILED_CHECK", True, ("Income certificate has expired", "Caste certificate has expired")),
    "MHSTU0004": ("INSUFFICIENT_DATA", True, ("No SSC result was found at the Examination Department",)),
    "MHSTU0005": ("PARTIAL", True, ("Enrolment is still pending", "No bonafide certificate was found")),
}


@pytest.mark.parametrize("student", sorted(EXPECTED))
def test_five_demo_students(world, student):
    state, message_expected, reasons = EXPECTED[student]
    transaction, headers, done = world.run_all(student)
    assert done["state"] == state
    assert done["message_queued"] is message_expected
    assert done["return_url"].endswith("?ref=" + transaction)
    report = world.report(headers)
    assert report["state"] == state
    text = " ".join(c["message"] for c in report["checks"])
    for reason in reasons:
        assert reason in text, (reason, text)
    required = world.client.get(f"/v1/verification-requests/{transaction}/results", headers=world.requester())
    assert required.status_code == 200 and conforms("ResultReport", required.json()) == report
    sms = world.result_messages(transaction)
    if state == "VERIFIED":
        assert sms == [] and report["failed"] == 0 and report["needs_review"] == 0 and report["recommendation"] == "APPROVE"
        return
    assert len(sms) == 1  # exactly one result message
    body = sms[0]["body"]
    assert len(body) <= 480 and sms[0]["status"] == "SENT" and sms[0]["recipient_masked"] == "90•••••00" + student[-1]
    for reason in reasons:
        assert reason in body
    assert "350000" not in body and "150000" not in body and student not in body  # no raw department data
    assert transaction.replace("-", "")[:8].upper() in body
    assert sms[0]["reason_codes"] and set(sms[0]["reason_codes"]) <= {c["reason_code"] for c in report["checks"]}
    assert report["recommendation"] == ("REJECT" if state == "FAILED_CHECK" else "MANUAL_REVIEW")


def test_state_progression_and_requester_view(world):
    created = world.create()
    data = created.json()
    tx = data["transaction_reference"]
    assert data["state"] == "CREATED"
    view = lambda: world.client.get(f"/v1/verification-requests/{tx}", headers=world.requester()).json()  # noqa: E731
    assert conforms("TransactionState", view()["state"]) == "CREATED"
    grant = world.client.post("/v1/applicant/sessions", json={"launch_reference": data["launch_reference"]}, headers=world.dashboard)
    headers = {"Authorization": "Bearer " + grant.json()["session_token"]}
    assert view()["state"] == "AUTHENTICATION_PENDING"
    assert {a["status"] for a in view()["authorizations"]} == {"PENDING"}
    world.authorize(tx, headers, "revenue")
    assert view()["state"] == "AUTHORIZED"
    world.check(headers, "revenue")  # background retrieval already finished in the test client
    assert view()["state"] == "PROCESSING"
    card = world.card(headers, "revenue")
    assert card["status"] == "COMPLETED" and card["detail"]
    statuses = {a["provider_id"]: a["status"] for a in view()["authorizations"]}
    assert statuses == {"revenue": "GRANTED", "examination": "PENDING", "university": "PENDING"}
    assert world.client.get(f"/v1/verification-requests/{tx}/results", headers=world.requester()).status_code == 409
    for a in view()["authorizations"]:
        conforms("Authorization", a)


def test_complete_is_idempotent_and_sends_one_message(world):
    transaction, headers, first = world.run_all("MHSTU0002")
    second = world.complete(headers)
    assert second.status_code == 200 and second.json() == first
    assert len(world.result_messages(transaction)) == 1


def test_complete_with_nothing_authorized_is_access_denied(world):
    transaction, headers = world.begin("MHSTU0001")
    done = world.complete(headers)
    assert done.status_code == 200 and done.json()["state"] == "ACCESS_DENIED" and done.json()["message_queued"]
    report = world.report(headers)
    assert report["checks"][0]["provider_id"] == "mahasetu" and report["checks"][0]["reason_code"] == "NO_AUTHORIZATION"
    assert "No department was authorized" in world.result_messages(transaction)[0]["body"]


def test_skipping_departments_counts_as_missing_evidence(world):
    _, headers, done = world.run_all("MHSTU0001", providers=("revenue",))
    assert done["state"] == "INSUFFICIENT_DATA"
    codes = {c["reason_code"] for c in world.report(headers)["checks"] if c["outcome"] == "NEEDS_REVIEW"}
    assert codes == {"EVIDENCE_NOT_PROVIDED"}


def test_requires_authorization_before_a_check(world):
    _, headers = world.begin()
    response = world.client.post("/v1/applicant/session/departments/revenue/verify", headers=headers)
    assert response.status_code == 403 and response.json()["code"] == "AUTHORIZATION_REQUIRED"
    conforms("Error", response.json())


def test_technical_failure_is_retryable_and_never_a_failed_check(world):
    transaction, headers = world.begin("MHSTU0001")
    world.departments.fail["revenue"] = [503, 503, 503]  # exceeds the three-attempt budget
    world.authorize(transaction, headers, "revenue")
    world.check(headers, "revenue")
    card = world.card(headers, "revenue")
    assert card["status"] == "UNAVAILABLE" and "try again" in card["detail"]
    assert sum(1 for c in world.departments.calls if c[0] == "revenue") == 3
    world.check(headers, "revenue")  # retry succeeds
    assert world.card(headers, "revenue")["status"] == "COMPLETED"
    # leave a different department unavailable and finish: PROVIDER_ERROR, no FAIL
    world.departments.fail["examination"] = [503, 503, 503]
    for provider in ("examination", "university"):
        world.authorize(transaction, headers, provider)
        world.check(headers, provider)
    done = world.complete(headers).json()
    assert done["state"] == "PROVIDER_ERROR"
    report = world.report(headers)
    assert report["failed"] == 0
    unavailable = [c for c in report["checks"] if c["reason_code"] == "PROVIDER_UNAVAILABLE"]
    assert unavailable and all(c["outcome"] == "NEEDS_REVIEW" for c in unavailable)
    assert "could not be reached" in " ".join(c["message"] for c in unavailable)


def test_transient_fault_is_retried_within_the_budget(world):
    transaction, headers = world.begin("MHSTU0001")
    world.departments.fail["examination"] = [503, "timeout"]
    world.authorize(transaction, headers, "examination")
    world.check(headers, "examination")
    assert world.card(headers, "examination")["status"] == "COMPLETED"
    assert sum(1 for c in world.departments.calls if c[0] == "examination") == 3


def test_non_retryable_provider_error_is_not_retried(world):
    transaction, headers = world.begin("MHSTU0001")
    world.departments.fail["university"] = [400]
    world.authorize(transaction, headers, "university")
    world.check(headers, "university")
    assert world.card(headers, "university")["status"] == "UNAVAILABLE"
    # the injected 400 answers the token request; a non-retryable fault is attempted exactly once
    assert sum(1 for c in world.departments.calls if c[0] == "university") == 1


def test_university_token_refresh(world):
    transaction, headers = world.begin("MHSTU0001")
    world.authorize(transaction, headers, "university")
    world.check(headers, "university")
    world.departments.expire_next_token_use = True
    world.check(headers, "university")  # token expired early: Core fetches a fresh one and retries once
    assert world.card(headers, "university")["status"] == "COMPLETED"
    assert world.departments.token_serial == 2


def test_identity_mismatch_goes_to_manual_review(world):
    world.departments.revenue_student_override = "MHSTU0002"
    _, headers, done = world.run_all("MHSTU0001")
    assert done["state"] == "MANUAL_REVIEW"
    codes = {c["reason_code"] for c in world.report(headers)["checks"]}
    assert "IDENTITY_MISMATCH" in codes


def test_decline_revoke_and_reauthorize(world):
    transaction, headers = world.begin("MHSTU0001")
    world.authorize(transaction, headers, "revenue")
    world.check(headers, "revenue")
    declined = world.client.post("/v1/applicant/session/departments/revenue/decline", headers=headers)
    assert declined.status_code == 200 and conforms("DepartmentCard", declined.json())["status"] == "DECLINED"
    again = world.client.post("/v1/applicant/session/departments/revenue/decline", headers=headers)
    assert again.status_code == 200 and again.json()["status"] == "DECLINED"
    status = world.client.get(f"/v1/verification-requests/{transaction}", headers=world.requester()).json()
    revoked = next(a for a in status["authorizations"] if a["provider_id"] == "revenue")
    assert revoked["status"] == "REVOKED" and revoked["revoked_at"] and revoked["authorized_at"]
    forbidden = world.client.post("/v1/applicant/session/departments/revenue/verify", headers=headers)
    assert forbidden.status_code == 403
    done = world.complete(headers).json()  # everything withdrawn and nothing else granted
    assert done["state"] == "ACCESS_DENIED"


def test_change_of_mind_after_decline(world):
    transaction, headers = world.begin("MHSTU0001")
    assert world.client.post("/v1/applicant/session/departments/revenue/decline", headers=headers).json()["status"] == "DECLINED"
    world.authorize(transaction, headers, "revenue")
    world.check(headers, "revenue")
    assert world.card(headers, "revenue")["status"] == "COMPLETED"


def test_withdrawn_evidence_is_not_used(world):
    transaction, headers = world.begin("MHSTU0001")
    for provider in ("revenue", "examination", "university"):
        world.authorize(transaction, headers, provider)
        world.check(headers, provider)
    world.client.post("/v1/applicant/session/departments/university/decline", headers=headers)
    done = world.complete(headers).json()
    assert done["state"] == "INSUFFICIENT_DATA"
    report = world.report(headers)
    assert {c["reason_code"] for c in report["checks"] if c["provider_id"] == "university"} == {"EVIDENCE_NOT_PROVIDED"}


def test_requester_cancellation(world):
    transaction, headers = world.begin("MHSTU0001")
    world.authorize(transaction, headers, "revenue")
    cancelled = world.client.post(f"/v1/verification-requests/{transaction}/cancel", headers=world.requester())
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"
    assert world.client.post(f"/v1/verification-requests/{transaction}/cancel", headers=world.requester()).json()["state"] == "CANCELLED"
    for path in ("/v1/applicant/session/departments/revenue/verify", "/v1/applicant/session/departments/revenue/otp"):
        assert world.client.post(path, headers=headers).status_code == 409
    done = world.complete(headers)
    assert done.json()["state"] == "CANCELLED" and done.json()["message_queued"] is False
    assert world.result_messages(transaction) == []
    report = world.client.get(f"/v1/verification-requests/{transaction}/results", headers=world.requester()).json()
    assert report["state"] == "CANCELLED" and report["recommendation"] == "REJECT"


def test_cancel_after_completion_conflicts(world):
    transaction, _, _ = world.run_all("MHSTU0001")
    response = world.client.post(f"/v1/verification-requests/{transaction}/cancel", headers=world.requester())
    assert response.status_code == 409 and response.json()["code"] == "TRANSACTION_CLOSED"


def test_cancellation_before_the_call_stops_it(world):
    from mahasetu_core.db import many
    transaction, headers = world.begin("MHSTU0001")
    world.authorize(transaction, headers, "revenue")
    session = world.core.session(headers["Authorization"].split()[1])
    _, run_id = world.core.start_check(session, "revenue")
    world.client.post(f"/v1/verification-requests/{transaction}/cancel", headers=world.requester())
    world.core.run_retrieval(session.request_id, "revenue", run_id)
    assert world.departments.calls == []  # no new call is made once the request is cancelled
    with world.core.db.begin() as conn:
        assert many(conn, "SELECT 1 FROM mahasetu.canonical_records WHERE request_id = :r", r=transaction) == []


def test_response_arriving_after_cancellation_is_ignored_and_audited(world):
    from mahasetu_core.db import many
    transaction, headers = world.begin("MHSTU0001")
    world.authorize(transaction, headers, "revenue")
    cancel = lambda: world.client.post(f"/v1/verification-requests/{transaction}/cancel", headers=world.requester())  # noqa: E731
    world.departments.on_call = cancel  # the requester cancels while the department call is in flight
    world.check(headers, "revenue")
    assert [c[0] for c in world.departments.calls] == ["revenue"]
    with world.core.db.begin() as conn:
        assert many(conn, "SELECT 1 FROM mahasetu.canonical_records WHERE request_id = :r", r=transaction) == []
        events = [e["event_type"] for e in many(conn, "SELECT event_type FROM mahasetu.audit_events WHERE request_id = :r", r=transaction)]
    assert "LATE_RESPONSE_IGNORED" in events and "REQUEST_CREATED" in events
    assert world.client.get(f"/v1/verification-requests/{transaction}", headers=world.requester()).json()["state"] == "CANCELLED"


def test_retrieval_in_progress_blocks_a_second_check_and_completion(world):
    transaction, headers = world.begin("MHSTU0001")
    world.authorize(transaction, headers, "revenue")
    session = world.core.session(headers["Authorization"].split()[1])
    world.core.start_check(session, "revenue")  # leave RETRIEVING: the worker has not run
    assert world.card(headers, "revenue")["status"] == "RETRIEVING"
    assert world.overview(headers)["can_complete"] is False
    assert world.client.post("/v1/applicant/session/departments/revenue/verify", headers=headers).status_code == 409
    blocked = world.complete(headers)
    assert blocked.status_code == 409 and blocked.json()["code"] == "RETRIEVAL_IN_PROGRESS"
    world.clock.advance(seconds=world.core.settings.retrieval_stale_seconds + 5)  # a stuck run is released
    assert world.card(headers, "revenue")["status"] == "UNAVAILABLE"


def test_result_before_completion_is_pending(world):
    _, headers = world.begin()
    response = world.client.get("/v1/applicant/session/result", headers=headers)
    assert response.status_code == 409 and response.json()["code"] == "REPORT_PENDING"


def test_overview_content_and_privacy(world):
    transaction, headers = world.begin("MHSTU0003")
    overview = world.overview(headers)
    assert overview["state"] == "AUTHENTICATION_PENDING" and overview["mobile_masked"] == "90•••••003"
    assert [d["provider_id"] for d in overview["departments"]] == ["revenue", "examination", "university"]
    revenue = overview["departments"][0]
    assert revenue["display_name"] == "Revenue Department" and revenue["status"] == "NOT_STARTED"
    assert [d["label"] for d in revenue["documents"]] == ["Income certificate", "Caste certificate"]
    assert "9000000003" not in str(overview) and overview["service_name"]


def test_return_address_can_be_set_for_the_environment(world):
    world.core.settings = dataclasses.replace(
        world.core.settings, return_urls={"RETURN_URL_DEMO_PMS": "https://portal.example.test/verification/return"})
    transaction, headers, done = world.run_all("MHSTU0001")
    assert done["return_url"] == "https://portal.example.test/verification/return?ref=" + transaction
    again = world.client.post("/v1/applicant/session/complete", headers=headers)
    assert again.status_code == 200 and again.json()["return_url"] == done["return_url"]
