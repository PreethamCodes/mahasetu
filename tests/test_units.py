"""Pure-logic tests: state graph, rule engine, normalization, connectors, messages and settings. No database needed."""
import json
import re
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest

from mahasetu_core import outbox, rules
from mahasetu_core.connectors import Fetch, Revenue
from mahasetu_core.normalize import SCHEMA, normalize
from mahasetu_core.rules import Check, Record
from mahasetu_core.security import bearer, mask_mobile, otp_code, otp_hash
from mahasetu_core.settings import CONTRACTS, Settings, requester_env_name, return_url_env_name
from mahasetu_core.states import ACTIVE, GRAPH, TERMINAL, can_move

TODAY = date(2026, 11, 17)
NOW = datetime(2026, 11, 17, 6, 0, tzinfo=UTC)
NAMES = {"revenue": "Revenue Department", "examination": "Examination Department", "university": "University"}


# ---- states ---------------------------------------------------------------------------------------------

def test_state_graph():
    assert json.loads((CONTRACTS / "states.json").read_text()) == GRAPH
    assert ACTIVE == {"CREATED", "AUTHENTICATION_PENDING", "AUTHORIZED", "PROCESSING"}
    assert TERMINAL == set(GRAPH) - ACTIVE
    assert can_move("AUTHORIZED", "PROCESSING") and can_move("PROCESSING", "VERIFIED")
    assert not can_move("AUTHENTICATION_PENDING", "VERIFIED") and not can_move("AUTHORIZED", "PARTIAL")
    assert all(not can_move(state, other) for state in TERMINAL for other in GRAPH)


# ---- rule loading -----------------------------------------------------------------------------------------

def row(rule_id="R", version=1, field="revenue.annual_income", operator="LESS_THAN_OR_EQUAL", expected=250000,
        scheme="DEMO-PMS", status="APPROVED", start="2026-01-01", end="2030-12-31", severity="REQUIRED", **extra):
    definition = {"rule_id": rule_id, "rule_name": "Name " + rule_id, "description": "d", "scheme_id": scheme,
                  "academic_year": 2026, "evidence_field": field, "operator": operator, "expected_value": expected,
                  "severity": severity, "source_document": "doc", "source_page": 1, "source_section": "s",
                  "source_text": "t", "effective_from": start, "effective_until": end, "version": version,
                  "approval_status": "PENDING", "approved_by": None, "approved_at": None} | extra
    return {"rule_id": rule_id, "version": version, "scheme_id": scheme, "approval_status": status,
            "approved_by": "00000000-0000-4000-8000-0000000000a1" if status == "APPROVED" else None,
            "approved_at": NOW if status == "APPROVED" else None, "definition": definition}


def load(*rows):
    return rules.load_rules(list(rows), "DEMO-PMS", TODAY)


def test_only_approved_applicable_valid_rules_load():
    loaded, skipped = load(
        row("OK"), row("PENDING", status="PENDING"), row("REJECTED", status="REJECTED"),
        row("OTHER", scheme="OTHER-PMS"), row("OLD", end="2026-06-30"), row("FUTURE", start="2027-01-01"),
        row("INVERTED", start="2030-01-01", end="2029-01-01"), row("BADTYPE", operator="IN", expected=5),
        row("BADIN", operator="IN", expected=["a"]),
        row("TEXTCOMPARE", field="university.programme", operator="LESS_THAN", expected=3),
        row("DATECOMPARE", field="revenue.certificate_expiry_date", operator="EQUALS", expected="2030-12-31"),
        row("MISMATCH") | {"version": 7})
    assert [r.rule_id for r in loaded] == ["OK"]
    reasons = {(rid, why.split(":")[0]) for rid, _, why in skipped}
    assert {("PENDING", "not approved"), ("REJECTED", "not approved"), ("OLD", "not effective on the evaluation date"),
            ("FUTURE", "not effective on the evaluation date"), ("INVERTED", "not effective on the evaluation date"),
            ("BADTYPE", "invalid definition"), ("BADIN", "incompatible operands"), ("TEXTCOMPARE", "incompatible operands"),
            ("DATECOMPARE", "incompatible operands")} <= reasons
    assert any(rid == "MISMATCH" for rid, *_ in skipped) and not any(rid == "OTHER" for rid, *_ in skipped)


def test_effective_dates_are_inclusive():
    assert load(row("EDGE", start="2026-11-17", end="2026-11-17"))[0]
    assert load(row("OPEN", end=None))[0][0].rule_id == "OPEN"


def test_latest_applicable_version_wins():
    loaded, _ = load(row("R", 1, expected=100), row("R", 2, expected=200), row("R", 3, expected=300, status="PENDING"),
                     row("R", 4, expected=400, end="2026-01-31"))
    assert [(r.version, r.expected) for r in loaded] == [(2, 200)]


def test_invalid_definition_is_skipped():
    broken = row("BROKEN")
    broken["definition"].pop("source_text")
    assert load(broken)[0] == []


# ---- evaluation -------------------------------------------------------------------------------------------

def record(provider, category, status="PRESENT", warnings=(), **evidence):
    blank = {"revenue": dict(annual_income=None, income_certificate_status=None, caste_certificate_status=None,
                             certificate_issue_date=None, certificate_expiry_date=None),
             "examination": dict(ssc_marks=None, ssc_maximum_marks=None, ssc_result_status=None, intermediate_marks=None,
                                 intermediate_maximum_marks=None, intermediate_result_status=None),
             "university": dict(registration_status=None, programme=None, department=None, enrolment_status=None,
                                bonafide_validity=None)}[provider]
    doc = {"evidence_status": status, "validation_warnings": list(warnings), "evidence": blank | evidence}
    return {(provider, category): Record(f"id-{provider}-{category}", provider, category, doc)}


def rule(field, operator, expected=None, severity="REQUIRED"):
    return rules.Rule("R", 1, "Rule", field, operator, expected, severity)


def evaluate(r, records, state="COMPLETED", today=TODAY):
    provider = r.field.split(".")[0]
    return rules.evaluate(r, records, {provider: state}, NAMES, today)


@pytest.mark.parametrize("operator,expected,value,outcome", [
    ("LESS_THAN_OR_EQUAL", 250000, 250000, "PASS"), ("LESS_THAN_OR_EQUAL", 250000, 250001, "FAIL"),
    ("LESS_THAN", 250000, 250000, "FAIL"), ("LESS_THAN", 250000, 249999, "PASS"),
    ("GREATER_THAN_OR_EQUAL", 10, 10, "PASS"), ("GREATER_THAN_OR_EQUAL", 10, 9, "FAIL"),
    ("GREATER_THAN", 10, 10, "FAIL"), ("GREATER_THAN", 10, 11, "PASS"),
    ("EQUALS", 5, 5, "PASS"), ("NOT_EQUALS", 5, 5, "FAIL"), ("IN", [1, 5], 5, "PASS"), ("NOT_IN", [1, 5], 5, "FAIL"),
])
def test_numeric_operators(operator, expected, value, outcome):
    check = evaluate(rule("revenue.annual_income", operator, expected), record("revenue", "income", annual_income=value))
    assert check.outcome == outcome and check.evidence_refs == ["id-revenue-income"]
    if outcome == "FAIL" and operator.startswith(("LESS", "GREATER")):
        assert check.reason_code in ("VALUE_ABOVE_LIMIT", "VALUE_BELOW_MINIMUM")


def test_zero_is_a_real_value_not_missing():
    zero = record("revenue", "income", annual_income=0)
    assert evaluate(rule("revenue.annual_income", "LESS_THAN_OR_EQUAL", 250000), zero).outcome == "PASS"
    assert evaluate(rule("revenue.annual_income", "GREATER_THAN_OR_EQUAL", 1), zero).outcome == "FAIL"


def test_text_operators_ignore_case():
    rec = record("university", "enrolment", programme="Demo BSc")
    assert evaluate(rule("university.programme", "EQUALS", "DEMO BSC"), rec).outcome == "PASS"
    assert evaluate(rule("university.programme", "IN", ["x", "demo bsc"]), rec).outcome == "PASS"
    assert evaluate(rule("university.programme", "NOT_EQUALS", "demo bsc"), rec).reason_code == "VALUE_NOT_ACCEPTED"


@pytest.mark.parametrize("field,key,value,outcome,code", [
    ("revenue.income_certificate_status", "income_certificate_status", "VALID", "PASS", "RULE_PASSED"),
    ("revenue.income_certificate_status", "income_certificate_status", "EXPIRED", "FAIL", "EVIDENCE_EXPIRED"),
    ("revenue.income_certificate_status", "income_certificate_status", "REVOKED", "FAIL", "EVIDENCE_NOT_VALID"),
    ("revenue.income_certificate_status", "income_certificate_status", "SURPRISE", "NEEDS_REVIEW", "EVIDENCE_UNRECOGNISED"),
    ("examination.ssc_result_status", "ssc_result_status", "PASS", "PASS", "RULE_PASSED"),
    ("examination.ssc_result_status", "ssc_result_status", "FAIL", "FAIL", "EVIDENCE_NOT_VALID"),
    ("examination.ssc_result_status", "ssc_result_status", "WITHHELD", "NEEDS_REVIEW", "EVIDENCE_PENDING"),
    ("university.enrolment_status", "enrolment_status", "ACTIVE", "PASS", "RULE_PASSED"),
    ("university.enrolment_status", "enrolment_status", "PENDING", "NEEDS_REVIEW", "EVIDENCE_PENDING"),
    ("university.enrolment_status", "enrolment_status", "INACTIVE", "FAIL", "EVIDENCE_NOT_VALID"),
    ("university.registration_status", "registration_status", "REGISTERED", "PASS", "RULE_PASSED"),
    ("university.registration_status", "registration_status", "PENDING", "NEEDS_REVIEW", "EVIDENCE_PENDING"),
])
def test_is_valid_status_meanings(field, key, value, outcome, code):
    provider = field.split(".")[0]
    category = {"revenue": "income", "examination": "marks", "university": "enrolment"}[provider]
    check = evaluate(rule(field, "IS_VALID"), record(provider, category, **{key: value}))
    assert (check.outcome, check.reason_code) == (outcome, code)


def test_dates_are_inclusive_and_use_the_evaluation_date():
    field = "university.bonafide_validity"
    assert evaluate(rule(field, "IS_VALID"), record("university", "bonafide", bonafide_validity="2026-11-17")).outcome == "PASS"
    late = evaluate(rule(field, "IS_VALID"), record("university", "bonafide", bonafide_validity="2026-11-16"))
    assert (late.outcome, late.reason_code) == ("FAIL", "EVIDENCE_EXPIRED")
    assert evaluate(rule(field, "IS_VALID"), record("university", "bonafide", bonafide_validity="2026-11-16"),
                    today=date(2026, 11, 1)).outcome == "PASS"


def test_missing_and_unavailable_evidence_stays_unresolved():
    r = rule("revenue.annual_income", "LESS_THAN_OR_EQUAL", 250000)
    income = record("revenue", "income", annual_income=1)
    cases = [(evaluate(r, income, "UNAVAILABLE"), "PROVIDER_UNAVAILABLE"),
             (evaluate(r, income, "NOT_PROVIDED"), "EVIDENCE_NOT_PROVIDED"),
             (evaluate(r, {}, "COMPLETED"), "EVIDENCE_NOT_PROVIDED"),
             (evaluate(r, record("revenue", "income", "MISSING")), "EVIDENCE_MISSING"),
             (evaluate(r, record("revenue", "income", annual_income=None)), "EVIDENCE_MISSING"),
             (evaluate(r, record("revenue", "income", "INVALID")), "EVIDENCE_INVALID"),
             (evaluate(r, record("revenue", "income", "INVALID", ["IDENTITY_MISMATCH"])), "IDENTITY_MISMATCH")]
    for check, code in cases:
        assert (check.outcome, check.reason_code) == ("NEEDS_REVIEW", code), check
    assert cases[0][0].reason_code.startswith("PROVIDER_")  # a technical fault is never a FAIL


def test_expired_record_fails_is_valid_and_is_not_present():
    expired = record("revenue", "income", "EXPIRED", income_certificate_status="EXPIRED", annual_income=1)
    assert evaluate(rule("revenue.income_certificate_status", "IS_VALID"), expired).reason_code == "EVIDENCE_EXPIRED"
    assert evaluate(rule("revenue.annual_income", "IS_PRESENT"), expired).outcome == "FAIL"
    assert evaluate(rule("revenue.annual_income", "LESS_THAN_OR_EQUAL", 250000), expired).outcome == "PASS"


def test_messages_hold_no_raw_values():
    check = evaluate(rule("revenue.annual_income", "LESS_THAN_OR_EQUAL", 250000), record("revenue", "income", annual_income=987654))
    assert check.outcome == "FAIL" and "987654" not in check.message and "250000" not in check.message
    assert 1 <= len(check.message) <= 240


def check(provider, outcome, code="X", severity="REQUIRED"):
    return Check("R", 1, "n", provider, severity, outcome, code, "m")


def decide(checks, states=None, records=None):
    states = states or {"revenue": "COMPLETED", "examination": "COMPLETED", "university": "COMPLETED"}
    records = records or (record("revenue", "income") | record("examination", "marks") | record("university", "enrolment"))
    return rules.decide(checks, states, records, {"revenue", "examination", "university"})


def test_decisions():
    assert rules.decide([], {}, {}, set()) == "RULES_NOT_CONFIGURED"
    assert decide([check("revenue", "PASS"), check("university", "PASS")]) == "VERIFIED"
    assert decide([check("revenue", "FAIL"), check("university", "PASS")]) == "FAILED_CHECK"
    assert decide([check("revenue", "FAIL"), check("university", "NEEDS_REVIEW", "PROVIDER_UNAVAILABLE")]) == "FAILED_CHECK"
    assert decide([check("revenue", "PASS"), check("university", "NEEDS_REVIEW", "IDENTITY_MISMATCH")]) == "MANUAL_REVIEW"
    assert decide([check("revenue", "PASS"), check("university", "NEEDS_REVIEW", "PROVIDER_UNAVAILABLE")]) == "PROVIDER_ERROR"
    assert decide([check("revenue", "PASS"), check("university", "NEEDS_REVIEW", "EVIDENCE_PENDING")]) == "PARTIAL"
    missing = record("revenue", "income") | record("examination", "marks", "MISSING") | record("university", "enrolment")
    assert decide([check("revenue", "PASS"), check("examination", "NEEDS_REVIEW", "EVIDENCE_MISSING")], records=missing) == "INSUFFICIENT_DATA"
    assert decide([check("revenue", "PASS"), check("examination", "NEEDS_REVIEW", "EVIDENCE_NOT_PROVIDED")],
                  states={"revenue": "COMPLETED", "examination": "NOT_PROVIDED", "university": "COMPLETED"}) == "INSUFFICIENT_DATA"
    assert decide([check("revenue", "PASS"), check("university", "FAIL", severity="ADVISORY")]) == "VERIFIED"
    assert decide([check("revenue", "PASS"), check("university", "NEEDS_REVIEW", severity="ADVISORY")]) == "VERIFIED"


def test_recommendations():
    assert [rules.recommendation(s) for s in ("VERIFIED", "FAILED_CHECK", "ACCESS_DENIED", "CANCELLED", "PARTIAL",
                                              "INSUFFICIENT_DATA", "PROVIDER_ERROR", "MANUAL_REVIEW")] == [
        "APPROVE", "REJECT", "REJECT", "REJECT", "MANUAL_REVIEW", "MANUAL_REVIEW", "MANUAL_REVIEW", "MANUAL_REVIEW"]


# ---- normalization ----------------------------------------------------------------------------------------

def norm(provider, data, categories, student="MHSTU0001", outcome="OK"):
    return dict(normalize(provider, Fetch(outcome, data=data), categories, student, NOW, TODAY, "1"))


def cert(status="VALID", expires="2030-12-31", amount="150000"):
    return {"reference": "income-1", "status": status, "issued": "2025-01-01", "expires": expires, "amount": amount}


def test_revenue_records_are_separate_and_minimized():
    docs = norm("revenue", {"student": "MHSTU0001", "income": cert(), "caste": dict(cert(), reference="caste-1")}, ["income"])
    assert list(docs) == ["income"]  # caste was not requested, so it is not stored
    doc = docs["income"]
    assert doc["evidence"]["annual_income"] == 150000 and doc["evidence"]["caste_certificate_status"] is None
    assert doc["source_record_reference"] == "income-1" and doc["source_updated_at"] is None  # fetch time is never substituted
    assert not list(SCHEMA.iter_errors(doc))


def test_revenue_edge_cases():
    zero = norm("revenue", {"student": "MHSTU0001", "income": cert(amount="0"), "caste": None}, ["income", "caste"])
    assert zero["income"]["evidence"]["annual_income"] == 0 and zero["caste"]["evidence_status"] == "MISSING"
    assert norm("revenue", {"student": "MHSTU0001", "income": cert(amount="-5"), "caste": None}, ["income"])["income"]["evidence_status"] == "INVALID"
    assert norm("revenue", {"student": "MHSTU0001", "income": cert(amount="abc"), "caste": None}, ["income"])["income"]["evidence_status"] == "INVALID"
    assert norm("revenue", {"student": "MHSTU0001", "income": cert(expires="not-a-date"), "caste": None}, ["income"])["income"]["evidence_status"] == "INVALID"
    stale = norm("revenue", {"student": "MHSTU0001", "income": cert(expires="2026-01-01"), "caste": None}, ["income"])["income"]
    assert stale["evidence_status"] == "EXPIRED" and stale["evidence"]["income_certificate_status"] == "EXPIRED"
    float_income = norm("revenue", {"student": "MHSTU0001", "income": cert(amount="150000.50"), "caste": None}, ["income"])["income"]
    assert float_income["evidence"]["annual_income"] == 150000.5


def test_identity_mismatch_drops_the_evidence():
    docs = norm("revenue", {"student": "MHSTU0002", "income": cert(), "caste": None}, ["income"])
    assert docs["income"]["evidence_status"] == "INVALID" and "IDENTITY_MISMATCH" in docs["income"]["validation_warnings"]
    assert all(v is None for v in docs["income"]["evidence"].values())  # another person's data is not kept
    exam = norm("examination", {"student_reference": "MHSTU0002", "ssc_total_marks": 1}, ["marks"])["marks"]
    assert exam["evidence_status"] == "INVALID" and exam["evidence"]["ssc_marks"] is None


def exam_row(**changes):
    return {"student_reference": "MHSTU0001", "record_reference": "SSC1,INTERMEDIATE1", "updated_at": "2026-11-09T00:00:00Z",
            "ssc_total_marks": 480, "ssc_maximum_marks": 600, "ssc_result_status": "PASS",
            "intermediate_total_marks": 0, "intermediate_maximum_marks": 600, "intermediate_result_status": "FAIL"} | changes


def test_examination_levels_map_independently():
    doc = norm("examination", exam_row(), ["marks"])["marks"]
    assert doc["evidence"] == {"ssc_marks": 480, "ssc_maximum_marks": 600, "ssc_result_status": "PASS",
                               "intermediate_marks": 0, "intermediate_maximum_marks": 600, "intermediate_result_status": "FAIL"}
    assert doc["source_updated_at"].startswith("2026-11-09T00:00:00")
    only_ssc = norm("examination", exam_row(intermediate_total_marks=None, intermediate_maximum_marks=None,
                                            intermediate_result_status=None), ["marks"])["marks"]
    assert only_ssc["evidence_status"] == "PRESENT" and only_ssc["evidence"]["intermediate_marks"] is None
    assert "Intermediate result is not available." in only_ssc["validation_warnings"]
    over = norm("examination", exam_row(ssc_total_marks=700, intermediate_total_marks=None, intermediate_maximum_marks=None,
                                        intermediate_result_status=None), ["marks"])["marks"]
    assert over["evidence_status"] == "INVALID"
    bad_time = norm("examination", exam_row(updated_at="yesterday"), ["marks"])["marks"]
    assert bad_time["source_updated_at"] is None and any("update time" in w for w in bad_time["validation_warnings"])


def university_row(**changes):
    return {"student": {"id": "MHSTU0001"}, "academic": {"registration": {"reference": "REG-1", "status": "registered"},
            "programme": {"name": "Demo BSc", "department": {"name": "Science"}}, "enrolment_status": "active"},
            "documents": {"bonafide": {"valid_until": "2030-12-31"}}, "record_reference": "ENR-1",
            "updated_at": "2026-11-09T00:00:00Z"} | changes


def test_university_categories_are_separate():
    docs = norm("university", university_row(), ["enrolment", "bonafide"])
    assert docs["enrolment"]["evidence"] == {"registration_status": "REGISTERED", "programme": "Demo BSc", "department": "Science",
                                             "enrolment_status": "ACTIVE", "bonafide_validity": None}
    assert docs["bonafide"]["evidence"]["bonafide_validity"] == "2030-12-31" and docs["bonafide"]["evidence"]["enrolment_status"] is None
    none = norm("university", university_row(documents={"bonafide": None}), ["bonafide"])["bonafide"]
    assert none["evidence_status"] == "MISSING"
    old = norm("university", university_row(documents={"bonafide": {"valid_until": "2026-11-16"}}), ["bonafide"])["bonafide"]
    assert old["evidence_status"] == "EXPIRED"
    today_ok = norm("university", university_row(documents={"bonafide": {"valid_until": "2026-11-17"}}), ["bonafide"])["bonafide"]
    assert today_ok["evidence_status"] == "PRESENT"  # an inclusive valid-until date


def test_missing_department_record():
    docs = norm("examination", None, ["marks"], outcome="MISSING")
    assert docs["marks"]["evidence_status"] == "MISSING" and all(v is None for v in docs["marks"]["evidence"].values())
    both = norm("revenue", None, ["income", "caste"], outcome="MISSING")
    assert {d["evidence_status"] for d in both.values()} == {"MISSING"}


# ---- connectors -------------------------------------------------------------------------------------------

def test_soap_request_matches_the_contract_order():
    xml_text = Revenue.request_xml("MHSTU0001", ["income"], "tx-1").decode()
    assert xml_text.index("StudentID") < xml_text.index("CertificateType") < xml_text.index("RequestReference")
    assert "<r:CertificateType>INCOME</r:CertificateType>" in xml_text
    assert "CertificateType" not in Revenue.request_xml("MHSTU0001", ["income", "caste"], "tx-1").decode()
    assert "http://www.w3.org/2003/05/soap-envelope" in xml_text


def soap(body, status=200):
    return httpx.Response(status, content=body)


def test_soap_parser_is_defensive():
    xxe = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body/></s:Envelope>'
    assert Revenue.parse(soap(xxe)) is None
    assert Revenue.parse(soap("not xml")) is None
    assert Revenue.parse(soap("<a/>")) is None
    assert Revenue.parse(httpx.Response(200, content=b"x" * 300_000)) is None


# ---- outbox ---------------------------------------------------------------------------------------------------

def test_result_message_is_short_ordered_and_deduplicated():
    tx = "12345678-0000-4000-8000-000000000000"
    checks = [("NEEDS_REVIEW", "EVIDENCE_PENDING", "Enrolment is still pending."), ("FAIL", "EVIDENCE_EXPIRED", "Income certificate has expired."),
              ("PASS", "RULE_PASSED", "Fine."), ("FAIL", "EVIDENCE_EXPIRED", "Income certificate has expired.")]
    body, codes = outbox.result_body("Demo scholarship", tx, "FAILED_CHECK", checks)
    assert "ref 12345678" in body and body.index("Income certificate") < body.index("Enrolment") and "Fine" not in body
    assert body.count("Income certificate has expired") == 1 and codes == ["EVIDENCE_EXPIRED", "EVIDENCE_PENDING"]
    many = [("FAIL", f"CODE_{i}", f"Reason number {i} is described with some extra words to take space.") for i in range(30)]
    long_body, long_codes = outbox.result_body("Demo scholarship", tx, "PARTIAL", many)
    assert len(long_body) <= 480 and re.search(r"\(\+\d+ more\)", long_body) and set(long_codes) <= {c[1] for c in many}
    assert len(outbox.otp_body("123456", "Demo scholarship", "Revenue Department", 5)) <= 480


def test_sender_failure_marks_failed_without_raising():
    class Failing:
        def send(self, recipient, body):
            raise RuntimeError("gateway down")

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    statements = []
    db = SimpleNamespace(begin=lambda: Conn())
    original = (outbox.many, outbox.run)
    outbox.many = lambda conn, sql, **p: [{"id": 1, "recipient_mobile": "9000000001", "body": "b"}]
    outbox.run = lambda conn, sql, **p: statements.append(sql)
    try:
        outbox.dispatch(db, Failing(), NOW)  # must not raise
    finally:
        outbox.many, outbox.run = original
    assert len(statements) == 1 and "'FAILED'" in statements[0]


# ---- security and settings ---------------------------------------------------------------------------------------

def test_security_helpers():
    assert re.fullmatch(r"[6-9][0-9]•{5}[0-9]{3}", mask_mobile("9000000001")) and "00000" not in mask_mobile("9000000001")
    assert re.fullmatch(r"\d{6}", otp_code())
    assert otp_hash("k" * 24, "a", "123456") == otp_hash("k" * 24, "a", "123456") != otp_hash("k" * 24, "b", "123456")
    assert bearer("Bearer abc") == "abc" and bearer("bearer  abc ") == "abc" and bearer("Basic abc") == "" and bearer(None) == ""


def test_settings(monkeypatch):
    assert requester_env_name("secret-ref/scholarship") == "REQUESTER_TOKEN_SCHOLARSHIP"
    assert requester_env_name("secret-ref/my-portal.v2") == "REQUESTER_TOKEN_MY_PORTAL_V2"
    for key in ("DATABASE_URL", "DASHBOARD_CLIENT_TOKEN", "OTP_HASH_KEY", "OTP_DEMO_DISCLOSURE", "CORE_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        Settings.from_env()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x")
    with pytest.raises(RuntimeError, match="DASHBOARD_CLIENT_TOKEN"):
        Settings.from_env()
    monkeypatch.setenv("DASHBOARD_CLIENT_TOKEN", "short")
    monkeypatch.setenv("OTP_HASH_KEY", "k" * 32)
    with pytest.raises(RuntimeError, match="at least"):
        Settings.from_env()
    monkeypatch.setenv("DASHBOARD_CLIENT_TOKEN", "d" * 32)
    settings = Settings.from_env()
    assert settings.demo_otp_disclosure is False and settings.otp_ttl_seconds == 300 and settings.otp_max_attempts == 3
    monkeypatch.setenv("REQUESTER_TOKEN_SCHOLARSHIP", "tiny")
    with pytest.raises(RuntimeError):
        settings.requester_token("secret-ref/scholarship")
    monkeypatch.setenv("OTP_TTL_SECONDS", "5")
    with pytest.raises(RuntimeError, match="OTP_TTL_SECONDS"):
        Settings.from_env()


def test_return_address_override(monkeypatch):
    assert return_url_env_name("DEMO-PMS") == "RETURN_URL_DEMO_PMS"
    for key, value in {"DATABASE_URL": "postgresql+psycopg://x", "DASHBOARD_CLIENT_TOKEN": "d" * 32, "OTP_HASH_KEY": "k" * 32}.items():
        monkeypatch.setenv(key, value)
    for key in ("OTP_DEMO_DISCLOSURE", "CORE_ENVIRONMENT", "RETURN_URL_DEMO_PMS"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert settings.return_base("DEMO-PMS", "http://localhost:3000/r") == "http://localhost:3000/r"
    monkeypatch.setenv("RETURN_URL_DEMO_PMS", "https://portal.example.test/verification/return")
    settings = Settings.from_env()
    assert settings.return_base("DEMO-PMS", "http://localhost:3000/r") == "https://portal.example.test/verification/return"
    assert settings.return_base("OTHER", "http://other/r") == "http://other/r"
    for bad in ("ftp://portal.example.test/r", "javascript:alert(1)", "https://user:pw@portal.example.test/r", "https://portal.example.test/r#x", "/relative"):
        monkeypatch.setenv("RETURN_URL_DEMO_PMS", bad)
        with pytest.raises(RuntimeError, match="RETURN_URL_DEMO_PMS"):
            Settings.from_env()
    monkeypatch.setenv("CORE_ENVIRONMENT", "production")
    monkeypatch.setenv("RETURN_URL_DEMO_PMS", "http://portal.example.test/r")
    with pytest.raises(RuntimeError, match="https"):
        Settings.from_env()
    monkeypatch.setenv("RETURN_URL_DEMO_PMS", "https://portal.example.test/r")
    assert Settings.from_env().return_urls


def test_student_reference_cannot_alter_the_request_path():
    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(404, json={"code": "UNKNOWN_STUDENT"})

    settings = SimpleNamespace(examination_api_key="k" * 24, university_client_id="", university_client_secret="")
    from mahasetu_core.connectors import Examination
    connector = Examination(httpx.Client(transport=httpx.MockTransport(handler)), settings)
    connector.once("http://exam/api/v1", "../../admin?x=1#", ["marks"], "tx")
    assert seen == ["/api/v1/students/..%2F..%2Fadmin%3Fx%3D1%23/examination-records"]
