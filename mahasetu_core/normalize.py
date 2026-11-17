"""Standardize each department's own format into per-category canonical records (contracts/v1/canonical.schema.json).

Only the categories requested for the transaction are populated (data minimization). A legitimate zero is preserved;
absent attributes stay null with a warning; the fetch time is never used as the source update time.
"""
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from jsonschema import Draft202012Validator

from .settings import CONTRACTS

TRANSFORMATION_VERSION = "1"
SCHEMA = Draft202012Validator(json.loads((CONTRACTS / "canonical.schema.json").read_text(encoding="utf-8")),
                              format_checker=Draft202012Validator.FORMAT_CHECKER)

BLANK = {
    "revenue": dict(annual_income=None, income_certificate_status=None, caste_certificate_status=None,
                    certificate_issue_date=None, certificate_expiry_date=None),
    "examination": dict(ssc_marks=None, ssc_maximum_marks=None, ssc_result_status=None, intermediate_marks=None,
                        intermediate_maximum_marks=None, intermediate_result_status=None),
    "university": dict(registration_status=None, programme=None, department=None, enrolment_status=None,
                       bonafide_validity=None),
}


class Draft:
    """Mutable builder for one canonical record."""

    def __init__(self, provider, category, student, retrieved_at, schema_version):
        self.category, self.provider = category, provider
        self.doc = {"student_id": student, "source_student_reference": None, "provider_id": provider,
                    "source_record_reference": None, "retrieved_at": retrieved_at.isoformat(),
                    "source_updated_at": None, "source_schema_version": schema_version,
                    "transformation_version": TRANSFORMATION_VERSION, "validation_warnings": [],
                    "evidence_status": "PRESENT", "evidence": dict(BLANK[provider])}

    def warn(self, text):
        self.doc["validation_warnings"].append(text)

    def status(self, value, warning=None):
        self.doc["evidence_status"] = value
        if warning:
            self.warn(warning)
        return self

    def finish(self):
        errors = sorted(SCHEMA.iter_errors(self.doc), key=lambda e: list(e.path))
        if errors:
            raise ValueError("Canonical record failed validation: " + errors[0].message[:120])
        return self.category, self.doc


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return False


def _timestamp(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return moment.isoformat() if moment.tzinfo else False


def _number(value):
    """Integral values stay integers; negative or non-numeric values are rejected (False)."""
    if value is None or isinstance(value, bool):
        return False if value is not None else None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return False
    if not number.is_finite() or number < 0:
        return False
    return int(number) if number == number.to_integral_value() else float(number)


def _text(value):
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _mismatch(draft, source_student):
    draft.doc["source_student_reference"] = source_student
    return draft.status("INVALID", "IDENTITY_MISMATCH")


def _missing(provider, category, student, now, version, note):
    return Draft(provider, category, student, now, version).status("MISSING", note).finish()


def revenue(data, categories, student, now, today, version):
    out = []
    for category in ("income", "caste"):
        if category not in categories:
            continue
        draft = Draft("revenue", category, student, now, version)
        if data["student"] != student:
            out.append(_mismatch(draft, data["student"]).finish())
            continue
        draft.doc["source_student_reference"] = data["student"]
        cert = data[category]
        if cert is None:
            out.append(draft.status("MISSING", f"No {category} certificate is on record.").finish())
            continue
        status, issued, expires = _text(cert["status"]), _date(cert["issued"]), _date(cert["expires"])
        draft.doc["source_record_reference"] = cert["reference"]
        if status is None or issued is False or expires is False:
            out.append(draft.status("INVALID", "Certificate fields could not be read.").finish())
            continue
        evidence = draft.doc["evidence"]
        evidence[category + "_certificate_status"] = status
        evidence["certificate_issue_date"] = issued.isoformat() if issued else None
        evidence["certificate_expiry_date"] = expires.isoformat() if expires else None
        if category == "income":
            amount = _number(cert.get("amount"))
            if amount is False:
                out.append(draft.status("INVALID", "Annual income is not a valid amount.").finish())
                continue
            evidence["annual_income"] = amount
            if amount is None:
                draft.warn("Annual income was not supplied.")
        if status == "VALID" and expires and expires < today:
            evidence[category + "_certificate_status"] = "EXPIRED"
            draft.warn("The expiry date has passed.")
            status = "EXPIRED"
        if status == "EXPIRED":
            draft.status("EXPIRED")
        out.append(draft.finish())
    return out


def examination(data, categories, student, now, today, version):
    if "marks" not in categories:
        return []
    draft = Draft("examination", "marks", student, now, version)
    if data.get("student_reference") != student:
        return [_mismatch(draft, data.get("student_reference")).finish()]
    draft.doc["source_student_reference"] = data["student_reference"]
    draft.doc["source_record_reference"] = data.get("record_reference") or None
    updated = _timestamp(data.get("updated_at"))
    if updated is False:
        draft.warn("The source update time could not be read.")
    draft.doc["source_updated_at"] = updated or None
    evidence, usable = draft.doc["evidence"], 0
    for level, label in (("ssc", "SSC"), ("intermediate", "Intermediate")):
        marks, maximum = _number(data.get(f"{level}_total_marks")), _number(data.get(f"{level}_maximum_marks"))
        status = _text(data.get(f"{level}_result_status"))
        if marks is None and maximum is None and status is None:
            draft.warn(f"{label} result is not available.")
            continue
        if marks is False or maximum is False or not isinstance(marks, int | type(None)) or not isinstance(
                maximum, int | type(None)) or (marks is not None and maximum is not None and marks > maximum):
            draft.warn(f"{label} marks could not be used.")
            continue
        evidence[f"{level}_marks"], evidence[f"{level}_maximum_marks"] = marks, maximum
        evidence[f"{level}_result_status"] = status
        usable += 1
    if not usable:
        draft.status("INVALID" if any("could not be used" in w for w in draft.doc["validation_warnings"])
                     else "MISSING")
    return [draft.finish()]


def university(data, categories, student, now, today, version):
    out = []
    source = (data.get("student") or {}).get("id")
    academic, documents = data.get("academic") or {}, data.get("documents") or {}
    updated = _timestamp(data.get("updated_at"))
    for category in ("enrolment", "bonafide"):
        if category not in categories:
            continue
        draft = Draft("university", category, student, now, version)
        if source != student:
            out.append(_mismatch(draft, source).finish())
            continue
        draft.doc["source_student_reference"] = source
        draft.doc["source_record_reference"] = data.get("record_reference") or None
        draft.doc["source_updated_at"] = updated or None
        if updated is False:
            draft.warn("The source update time could not be read.")
        evidence = draft.doc["evidence"]
        if category == "enrolment":
            registration = academic.get("registration") or {}
            programme = academic.get("programme") or {}
            evidence["registration_status"] = _text(registration.get("status"))
            evidence["programme"] = programme.get("name") or None
            evidence["department"] = (programme.get("department") or {}).get("name") or None
            evidence["enrolment_status"] = _text(academic.get("enrolment_status"))
            if evidence["enrolment_status"] is None and evidence["registration_status"] is None:
                draft.status("MISSING", "No enrolment details are on record.")
        else:
            bonafide = documents.get("bonafide")
            if not bonafide:
                draft.status("MISSING", "No bonafide certificate is on record.")
            else:
                valid_until = _date(bonafide.get("valid_until"))
                if not valid_until:
                    draft.status("INVALID", "Bonafide validity could not be read.")
                else:
                    evidence["bonafide_validity"] = valid_until.isoformat()
                    if valid_until < today:
                        draft.status("EXPIRED", "The bonafide validity date has passed.")
        out.append(draft.finish())
    return out


NORMALIZERS = {"revenue": revenue, "examination": examination, "university": university}


def normalize(provider, fetch, categories, student, now, today, version):
    """Return [(category, canonical_document)] for one department fetch."""
    if fetch.outcome == "MISSING":
        return [_missing(provider, category, student, now, version, "The department has no record for this student.")
                for category in categories]
    return NORMALIZERS[provider](fetch.data, set(categories), student, now, today, version)
