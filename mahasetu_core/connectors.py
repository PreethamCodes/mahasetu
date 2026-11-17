"""Department connectors: one per protocol. Each returns a Fetch and never raises for provider faults.

Provider response bodies are parsed here and then discarded; only technical metadata is logged or stored.
"""
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx
from lxml import etree as xml

SOAP = "http://www.w3.org/2003/05/soap-envelope"
REV = "urn:mahasetu:revenue:v1"
S, R = "{" + SOAP + "}", "{" + REV + "}"
MAX_BODY = 262_144


@dataclass
class Fetch:
    outcome: str  # OK | MISSING | ERROR
    data: object = None
    code: str = ""
    retryable: bool = False
    http_status: int | None = None
    attempts: int = 1
    duration_ms: int = 0


def _failure(code, retryable, status=None):
    return Fetch("ERROR", code=code, retryable=retryable, http_status=status)


def _http_error(status, code=""):
    """Map an unexpected provider HTTP status. 5xx and 429 are transient; 4xx (other than 404) are not."""
    if status in (429, 500, 502, 503, 504):
        return _failure(code or f"HTTP_{status}", True, status)
    if status in (401, 403):
        return _failure(code or "PROVIDER_AUTH_FAILED", False, status)
    return _failure(code or f"HTTP_{status}", False, status)


def _safe_json(response):
    if len(response.content) > MAX_BODY:
        return None
    try:
        value = response.json()
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class Revenue:
    provider_id = "revenue"

    def __init__(self, client, settings):
        self.client, self.user, self.password = client, settings.revenue_user, settings.revenue_password

    @staticmethod
    def request_xml(student, categories, reference):
        root = xml.Element(S + "Envelope", nsmap={"s": SOAP, "r": REV})
        operation = xml.SubElement(xml.SubElement(root, S + "Body"), R + "GetStudentCertificates")
        xml.SubElement(operation, R + "StudentID").text = student
        wanted = {c for c in categories if c in ("income", "caste")}
        if len(wanted) == 1:  # both categories: omit the filter and receive both certificates
            xml.SubElement(operation, R + "CertificateType").text = next(iter(wanted)).upper()
        xml.SubElement(operation, R + "RequestReference").text = reference
        return xml.tostring(root, encoding="utf-8", xml_declaration=True)

    def once(self, url, student, categories, reference):
        if not self.user or not self.password:
            return _failure("PROVIDER_NOT_CONFIGURED", False)
        try:
            response = self.client.post(url, content=self.request_xml(student, categories, reference), auth=(
                self.user, self.password), headers={
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{REV}/GetStudentCertificates"'})
        except httpx.HTTPError as exc:
            return _failure("TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "CONNECTION_FAILED", True)
        parsed = self.parse(response)
        if parsed is not None:
            return parsed
        return _http_error(response.status_code)

    @staticmethod
    def parse(response):
        """Return a Fetch for a SOAP payload, or None when the body is not usable SOAP."""
        if len(response.content) > MAX_BODY:
            return None
        try:
            root = xml.fromstring(response.content, xml.XMLParser(
                resolve_entities=False, load_dtd=False, no_network=True, remove_comments=True, remove_pis=True))
        except xml.XMLSyntaxError:
            return None
        if root.getroottree().docinfo.doctype or root.tag != S + "Envelope":
            return None
        body = root.find(S + "Body")
        if body is None or len(body) != 1:
            return None
        node = body[0]
        if node.tag == S + "Fault":
            code = (node.findtext(f"{S}Detail/{R}ProviderFault/{R}Code") or "").strip()
            if code == "RECORD_MISSING":
                return Fetch("MISSING", code=code, http_status=response.status_code)
            retryable = (node.findtext(f"{S}Detail/{R}ProviderFault/{R}Retryable") or "").lower() == "true"
            if response.status_code == 401:
                return _failure("PROVIDER_AUTH_FAILED", False, 401)
            return _failure(code or "SOAP_FAULT", retryable or response.status_code in (502, 503, 504),
                            response.status_code)
        if node.tag != R + "GetStudentCertificatesResponse" or response.status_code != 200:
            return None

        def certificate(tag, extra):
            item = node.find(R + tag)
            if item is None:
                return None
            value = {"reference": item.findtext(R + "ProviderRecordReference"),
                     "status": item.findtext(R + "CertificateStatus"),
                     "issued": item.findtext(R + "IssueDate"), "expires": item.findtext(R + "ExpiryDate")}
            value[extra[0]] = item.findtext(R + extra[1])
            return value

        return Fetch("OK", data={"student": node.findtext(R + "StudentID"),
                                 "income": certificate("IncomeCertificate", ("amount", "AnnualIncomeINR")),
                                 "caste": certificate("CasteCertificate", ("category", "Category"))},
                     http_status=200)


class Examination:
    provider_id = "examination"

    def __init__(self, client, settings):
        self.client, self.key = client, settings.examination_api_key

    def once(self, url, student, categories, reference):
        if not self.key:
            return _failure("PROVIDER_NOT_CONFIGURED", False)
        try:
            response = self.client.get(f"{url}/students/{quote(student, safe='')}/examination-records",
                                       headers={"X-API-Key": self.key})
        except httpx.HTTPError as exc:
            return _failure("TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "CONNECTION_FAILED", True)
        body = _safe_json(response)
        code = str((body or {}).get("code", ""))
        if response.status_code == 200 and body is not None:
            return Fetch("OK", data=body, http_status=200)
        if response.status_code == 404 and code in ("UNKNOWN_STUDENT", "NO_EXAMINATION_RECORDS"):
            return Fetch("MISSING", code=code, http_status=404)
        return _http_error(response.status_code, code if code and response.status_code != 401 else "")


class University:
    provider_id = "university"

    def __init__(self, client, settings, clock=time.monotonic):
        self.client, self.client_id, self.secret = client, settings.university_client_id, settings.university_client_secret
        self.clock, self.lock, self.token, self.valid_until = clock, threading.Lock(), "", 0.0

    def _token(self, origin, force=False):
        with self.lock:
            if force or not self.token or self.clock() >= self.valid_until:
                response = self.client.post(f"{origin}/demo/token", auth=(self.client_id, self.secret))
                body = _safe_json(response)
                if response.status_code != 200 or not body or not body.get("access_token"):
                    self.token = ""
                    raise PermissionError(response.status_code)
                self.token = body["access_token"]
                self.valid_until = self.clock() + max(int(body.get("expires_in", 60)) - 20, 1)
            return self.token

    def once(self, url, student, categories, reference):
        if not self.client_id or not self.secret:
            return _failure("PROVIDER_NOT_CONFIGURED", False)
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        refreshed = False
        while True:
            try:
                token = self._token(origin, force=refreshed)
                response = self.client.get(f"{url}/students/{quote(student, safe='')}/academic-records",
                                           headers={"Authorization": f"Bearer {token}"})
            except PermissionError as exc:
                status = exc.args[0]
                return _http_error(status, "PROVIDER_AUTH_FAILED" if status in (401, 403) else "")
            except httpx.HTTPError as exc:
                return _failure("TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "CONNECTION_FAILED", True)
            body = _safe_json(response)
            code = str((body or {}).get("code", ""))
            if response.status_code == 401 and code in ("TOKEN_EXPIRED", "INVALID_TOKEN") and not refreshed:
                refreshed = True  # the cached token expired early: fetch one fresh token and retry once
                continue
            if response.status_code == 200 and body is not None:
                return Fetch("OK", data=body, http_status=200)
            if response.status_code == 404 and code in ("UNKNOWN_STUDENT", "NO_ACADEMIC_RECORDS"):
                return Fetch("MISSING", code=code, http_status=404)
            return _http_error(response.status_code, "PROVIDER_AUTH_FAILED" if response.status_code == 401 else code)


class Departments:
    """Routes a request to the right connector and applies the bounded retry budget."""

    def __init__(self, settings, transport=None, sleep=time.sleep):
        self.settings, self.sleep = settings, sleep
        self.client = httpx.Client(timeout=httpx.Timeout(settings.provider_timeout_seconds), follow_redirects=False,
                                   transport=transport)
        self.connectors = {c.provider_id: c for c in (
            Revenue(self.client, settings), Examination(self.client, settings), University(self.client, settings))}

    def close(self):
        self.client.close()

    def fetch(self, provider_id, url, student, categories, reference):
        connector = self.connectors.get(provider_id)
        if connector is None:
            return Fetch("ERROR", code="PROVIDER_UNSUPPORTED")
        url = self.settings.provider_urls.get(provider_id, url).rstrip("/")
        if provider_id == "revenue" and not url.endswith("/soap/v1"):
            url += "/soap/v1"
        elif provider_id == "examination" and not url.endswith("/api/v1"):
            url += "/api/v1"
        elif provider_id == "university" and not url.endswith("/api/v2"):
            url += "/api/v2"
        started = time.monotonic()
        result = Fetch("ERROR", code="NOT_ATTEMPTED")
        for attempt in range(1, self.settings.provider_attempts + 1):
            result = connector.once(url, student, categories, reference)
            result.attempts = attempt
            if result.outcome != "ERROR" or not result.retryable or attempt == self.settings.provider_attempts:
                break
            self.sleep(self.settings.provider_backoff_seconds * 2 ** (attempt - 1))
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result
