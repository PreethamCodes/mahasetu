"""In-process stand-ins for the three departments, speaking their real protocols over httpx.MockTransport.

They mirror the provider contracts (SOAP 1.2 with Basic auth, flat JSON with X-API-Key, nested JSON with an expiring
bearer token) and the synthetic fixtures MHSTU0001 to MHSTU0005. Faults can be injected per department.
"""
import base64
import json
import re

import httpx
from lxml import etree as xml

SOAP, REV = "http://www.w3.org/2003/05/soap-envelope", "urn:mahasetu:revenue:v1"
USER, PASSWORD, API_KEY, CLIENT, SECRET = "rev-user", "rev-pass", "exam-key", "uni-client", "uni-secret"
VALID_UNTIL, EXPIRED = "2030-12-31", "2026-01-01"

REVENUE = {
    "MHSTU0001": (150000, "VALID", VALID_UNTIL, "VALID", VALID_UNTIL),
    "MHSTU0002": (350000, "VALID", VALID_UNTIL, "VALID", VALID_UNTIL),
    "MHSTU0003": (150000, "EXPIRED", EXPIRED, "EXPIRED", EXPIRED),
    "MHSTU0004": (150000, "VALID", VALID_UNTIL, "VALID", VALID_UNTIL),
    "MHSTU0005": (150000, "VALID", VALID_UNTIL, "VALID", VALID_UNTIL),
}
EXAM = {n: dict(ssc_total_marks=480, ssc_maximum_marks=600, ssc_exam_year=2023, ssc_result_status="PASS",
                ssc_certificate_reference=f"CERT-SSC{n[-1]}", intermediate_total_marks=480,
                intermediate_maximum_marks=600, intermediate_exam_year=2025, intermediate_result_status="PASS",
                intermediate_certificate_reference=f"CERT-INTERMEDIATE{n[-1]}", record_reference=f"SSC{n[-1]},INTERMEDIATE{n[-1]}",
                student_reference=n, updated_at="2026-11-09T00:00:00Z") for n in
        ("MHSTU0001", "MHSTU0002", "MHSTU0003", "MHSTU0005")}
UNIVERSITY = {
    "MHSTU0001": ("ACTIVE", VALID_UNTIL), "MHSTU0002": ("ACTIVE", VALID_UNTIL), "MHSTU0003": ("ACTIVE", VALID_UNTIL),
    "MHSTU0004": ("ACTIVE", VALID_UNTIL), "MHSTU0005": ("PENDING", None),
}


def provider_error(status, code, category, message):
    return httpx.Response(status, json={"code": code, "category": category, "message": message,
                                        "transaction_reference": None, "provider_reference": "fake",
                                        "retryable": status == 503, "timestamp": "2026-11-09T00:00:00Z"})


def soap_fault(status, code, message):
    body = (f'<s:Envelope xmlns:s="{SOAP}" xmlns:r="{REV}"><s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code>'
            f'<s:Reason><s:Text xml:lang="en">{message}</s:Text></s:Reason><s:Detail><r:ProviderFault><r:Code>{code}</r:Code>'
            f'<r:Category>X</r:Category><r:Message>{message}</r:Message><r:RequestReference>x</r:RequestReference>'
            f'<r:Retryable>{str(status == 503).lower()}</r:Retryable><r:Timestamp>2026-11-09T00:00:00Z</r:Timestamp>'
            f'</r:ProviderFault></s:Detail></s:Fault></s:Body></s:Envelope>')
    return httpx.Response(status, content=body, headers={"content-type": "application/soap+xml"})


class Departments:
    """Fake departments. Set fail[provider] = [status, ...] to answer with those statuses before serving normally."""

    def __init__(self):
        self.calls, self.fail, self.tokens, self.token_serial = [], {}, set(), 0
        self.expire_next_token_use = False
        self.revenue_student_override = None
        self.on_call = None  # optional hook run while a request is in flight (to simulate concurrent events)
        self.transport = httpx.MockTransport(self.handle)

    # ---- routing
    def handle(self, request):
        path = request.url.path
        provider = "revenue" if path.startswith("/soap") else "examination" if path.startswith("/api/v1") else "university"
        self.calls.append((provider, request.method, path))
        if self.on_call:
            hook, self.on_call = self.on_call, None
            hook()
        queue = self.fail.get(provider)
        if queue:
            status = queue.pop(0)
            if status == "timeout":
                raise httpx.ReadTimeout("simulated timeout")
            if status == "connect":
                raise httpx.ConnectError("simulated outage")
            return provider_error(status, "DATABASE_UNAVAILABLE", "PROVIDER", "down") if provider != "revenue" \
                else soap_fault(status, "DATABASE_UNAVAILABLE", "down")
        if provider == "revenue":
            return self.revenue(request)
        if provider == "examination":
            return self.examination(request)
        return self.university(request)

    # ---- revenue (SOAP 1.2 + Basic)
    def revenue(self, request):
        raw = request.headers.get("authorization", "")
        if raw != "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode():
            return soap_fault(401, "INVALID_CREDENTIALS", "Service authentication required")
        assert request.headers["content-type"].startswith("application/soap+xml")
        root = xml.fromstring(request.content)
        op = root.find(f"{{{SOAP}}}Body/{{{REV}}}GetStudentCertificates")
        assert op is not None and [c.tag.split("}")[1] for c in op][0] == "StudentID"
        student = op.findtext(f"{{{REV}}}StudentID")
        kind = op.findtext(f"{{{REV}}}CertificateType")
        reference = op.findtext(f"{{{REV}}}RequestReference")
        if student not in REVENUE:
            return soap_fault(404, "RECORD_MISSING", "Requested certificate unavailable")
        income, istatus, iexp, cstatus, cexp = REVENUE[student]
        answer = self.revenue_student_override or student
        parts = []
        if kind in (None, "INCOME"):
            parts.append(f"<r:IncomeCertificate><r:ProviderRecordReference>income-{student[-1]}</r:ProviderRecordReference>"
                         f"<r:CertificateStatus>{istatus}</r:CertificateStatus><r:IssueDate>2025-01-01</r:IssueDate>"
                         f"<r:ExpiryDate>{iexp}</r:ExpiryDate><r:AnnualIncomeINR>{income}</r:AnnualIncomeINR></r:IncomeCertificate>")
        if kind in (None, "CASTE"):
            parts.append(f"<r:CasteCertificate><r:ProviderRecordReference>caste-{student[-1]}</r:ProviderRecordReference>"
                         f"<r:CertificateStatus>{cstatus}</r:CertificateStatus><r:IssueDate>2025-01-01</r:IssueDate>"
                         f"<r:ExpiryDate>{cexp}</r:ExpiryDate><r:Category>DEMO-A</r:Category></r:CasteCertificate>")
        body = (f'<s:Envelope xmlns:s="{SOAP}" xmlns:r="{REV}"><s:Body><r:GetStudentCertificatesResponse>'
                f"<r:StudentID>{answer}</r:StudentID><r:RequestReference>{reference}</r:RequestReference>"
                f'{"".join(parts)}</r:GetStudentCertificatesResponse></s:Body></s:Envelope>')
        return httpx.Response(200, content=body, headers={"content-type": "application/soap+xml"})

    # ---- examination (flat JSON + X-API-Key)
    def examination(self, request):
        if request.headers.get("x-api-key") != API_KEY:
            return provider_error(401, "INVALID_API_KEY", "AUTHENTICATION", "Valid API key required")
        student = re.search(r"/students/([^/]+)/", request.url.path).group(1)
        if student not in EXAM:
            return provider_error(404, "NO_EXAMINATION_RECORDS" if re.fullmatch(r"MHSTU000\d", student) else "UNKNOWN_STUDENT",
                                  "MISSING", "Examination records unavailable")
        return httpx.Response(200, json=EXAM[student])

    # ---- university (nested JSON + expiring token)
    def university(self, request):
        if request.url.path == "/demo/token":
            expected = "Basic " + base64.b64encode(f"{CLIENT}:{SECRET}".encode()).decode()
            if request.headers.get("authorization") != expected:
                return provider_error(401, "INVALID_CLIENT", "AUTHENTICATION", "Valid client credentials required")
            self.token_serial += 1
            token = f"token-{self.token_serial}"
            self.tokens.add(token)
            return httpx.Response(200, json={"access_token": token, "token_type": "Bearer", "expires_in": 300})
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or token not in self.tokens:
            return provider_error(401, "INVALID_TOKEN", "AUTHENTICATION", "Invalid bearer token")
        if self.expire_next_token_use:
            self.expire_next_token_use = False
            self.tokens.discard(token)
            return provider_error(401, "TOKEN_EXPIRED", "AUTHENTICATION", "Demo token expired")
        student = re.search(r"/students/([^/]+)/", request.url.path).group(1)
        if student not in UNIVERSITY:
            return provider_error(404, "UNKNOWN_STUDENT", "MISSING", "Student reference not found")
        enrolment, bonafide = UNIVERSITY[student]
        return httpx.Response(200, json={
            "student": {"id": student},
            "academic": {"registration": {"reference": f"REG-{student[-1]}", "status": "REGISTERED"},
                         "programme": {"name": "Demo BSc", "department": {"name": "Science"}},
                         "enrolment_status": enrolment},
            "documents": {"bonafide": {"valid_until": bonafide} if bonafide else None},
            "record_reference": f"ENR-{student[-1]}", "updated_at": "2026-11-09T00:00:00Z"})
