"""Shared fixtures. Database tests need a migrated and seeded mahasetu schema:

    CORE_TEST_DATABASE_URL=postgresql+psycopg://<runtime login>@host/db  pytest

Use the deployment bootstrap (scripts/bootstrap_local_db.py) or run `alembic upgrade head` and db/seed.sql yourself.
The tests use only the runtime role, so they also prove the scoped grants and row-level security are sufficient.
"""
import dataclasses
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes  # noqa: E402

CONTRACT = json.loads((Path(__file__).resolve().parent.parent / "contracts/v1/openapi.json").read_text(encoding="utf-8"))
DATABASE = os.environ.get("CORE_TEST_DATABASE_URL")
REQUESTER_TOKEN = "requester-token-for-tests-0123456789"
OTHER_TOKEN = "other-requester-token-for-tests-0123"
DASHBOARD_TOKEN = "dashboard-token-for-tests-0123456789"
OTP_KEY = "otp-hash-key-for-tests-0123456789abcdef"
PURPOSE = "SCHOLARSHIP_VERIFICATION"
EVIDENCE = [{"provider_id": "revenue", "evidence_categories": ["income", "caste"]},
            {"provider_id": "examination", "evidence_categories": ["marks"]},
            {"provider_id": "university", "evidence_categories": ["enrolment", "bonafide"]}]


def validator(name):
    return Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": CONTRACT["components"]})


def conforms(name, payload):
    errors = sorted(validator(name).iter_errors(payload), key=lambda e: list(e.path))
    assert not errors, f"{name}: {errors[0].message} at {list(errors[0].path)} in {json.dumps(payload)[:300]}"
    return payload


class Clock:
    def __init__(self):
        self.offset = timedelta()

    def __call__(self):
        return datetime.now(UTC) + self.offset

    def advance(self, **kwargs):
        self.offset += timedelta(**kwargs)


@pytest.fixture(scope="session")
def database_url():
    if not DATABASE:
        pytest.skip("CORE_TEST_DATABASE_URL is not set")
    return DATABASE


@pytest.fixture()
def env(monkeypatch, database_url):
    values = {"DATABASE_URL": database_url, "DASHBOARD_CLIENT_TOKEN": DASHBOARD_TOKEN, "OTP_HASH_KEY": OTP_KEY,
              "REQUESTER_TOKEN_SCHOLARSHIP": REQUESTER_TOKEN, "REVENUE_USER": fakes.USER,
              "REVENUE_PASSWORD": fakes.PASSWORD, "EXAMINATION_API_KEY": fakes.API_KEY,
              "UNIVERSITY_CLIENT_ID": fakes.CLIENT, "UNIVERSITY_CLIENT_SECRET": fakes.SECRET,
              "OTP_RESEND_SECONDS": "30"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in ("REVENUE_URL", "EXAMINATION_URL", "UNIVERSITY_URL", "OTP_DEMO_DISCLOSURE", "CORE_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture()
def world(env):
    from fastapi.testclient import TestClient

    from mahasetu_core.api import create_app
    from mahasetu_core.connectors import Departments
    from mahasetu_core.service import Core
    from mahasetu_core.settings import Settings

    settings = dataclasses.replace(Settings.from_env(), provider_backoff_seconds=0)
    departments_fake, clock = fakes.Departments(), Clock()
    core = Core(settings, departments=Departments(settings, transport=departments_fake.transport, sleep=lambda s: None),
                clock=clock)
    with TestClient(create_app(core)) as client:
        yield World(client, core, departments_fake, clock, env)


class World:
    def __init__(self, client, core, departments, clock, env):
        self.client, self.core, self.departments, self.clock, self.env = client, core, departments, clock, env

    # ---- requesting service
    def requester(self, token=REQUESTER_TOKEN):
        return {"Authorization": f"Bearer {token}"}

    dashboard = {"Authorization": f"Bearer {DASHBOARD_TOKEN}"}

    def body(self, student="MHSTU0001", **changes):
        body = {"service_id": "DEMO-PMS", "application_reference": "APP-" + uuid.uuid4().hex[:8],
                "applicant_reference": student, "applicant_mobile": "900000000" + student[-1], "purpose": PURPOSE,
                "requested_evidence": EVIDENCE}
        return body | changes

    def create(self, student="MHSTU0001", key=None, token=REQUESTER_TOKEN, **changes):
        return self.client.post("/v1/verification-requests", json=self.body(student, **changes),
                                headers=self.requester(token) | {"Idempotency-Key": key or uuid.uuid4().hex})

    def begin(self, student="MHSTU0001"):
        """Create a request and exchange its launch reference. Returns (transaction, applicant headers)."""
        created = self.create(student)
        assert created.status_code == 201, created.text
        data = conforms("CreatedVerification", created.json())
        grant = self.client.post("/v1/applicant/sessions", json={"launch_reference": data["launch_reference"]},
                                 headers=self.dashboard)
        assert grant.status_code == 201, grant.text
        conforms("ApplicantSessionGrant", grant.json())
        return data["transaction_reference"], {"Authorization": "Bearer " + grant.json()["session_token"]}

    # ---- applicant
    def overview(self, headers):
        response = self.client.get("/v1/applicant/session", headers=headers)
        assert response.status_code == 200, response.text
        return conforms("ApplicantOverview", response.json())

    def card(self, headers, provider):
        return next(c for c in self.overview(headers)["departments"] if c["provider_id"] == provider)

    def code_from_inbox(self, transaction):
        listing = self.client.get("/v1/messages", params={"transaction_reference": transaction, "kind": "OTP"},
                                  headers=self.dashboard)
        assert listing.status_code == 200
        return re.search(r"\b(\d{6})\b", listing.json()["messages"][0]["body"]).group(1)

    def send_otp(self, headers, provider):
        return self.client.post(f"/v1/applicant/session/departments/{provider}/otp", headers=headers)

    def verify_otp(self, headers, provider, challenge, code):
        return self.client.post(f"/v1/applicant/session/departments/{provider}/otp/verify", headers=headers,
                                json={"challenge_reference": challenge, "code": code})

    def authorize(self, transaction, headers, provider):
        sent = self.send_otp(headers, provider)
        assert sent.status_code == 202, sent.text
        challenge = conforms("OtpChallenge", sent.json())["challenge_reference"]
        verified = self.verify_otp(headers, provider, challenge, self.code_from_inbox(transaction))
        assert verified.status_code == 200, verified.text
        return conforms("OtpVerified", verified.json())

    def check(self, headers, provider):
        response = self.client.post(f"/v1/applicant/session/departments/{provider}/verify", headers=headers)
        assert response.status_code == 202, response.text
        card = conforms("DepartmentCard", response.json())
        assert card["status"] == "RETRIEVING"
        return card

    def complete(self, headers):
        return self.client.post("/v1/applicant/session/complete", headers=headers)

    def run_all(self, student, providers=("revenue", "examination", "university")):
        transaction, headers = self.begin(student)
        for provider in providers:
            self.authorize(transaction, headers, provider)
            self.check(headers, provider)
        done = self.complete(headers)
        assert done.status_code == 200, done.text
        return transaction, headers, conforms("CompletionResponse", done.json())

    def result_messages(self, transaction):
        listing = self.client.get("/v1/messages", params={"transaction_reference": transaction,
                                                          "kind": "VERIFICATION_RESULT"}, headers=self.dashboard)
        assert listing.status_code == 200
        return conforms("MessageList", listing.json())["messages"]

    def report(self, headers):
        response = self.client.get("/v1/applicant/session/result", headers=headers)
        assert response.status_code == 200, response.text
        return conforms("ResultReport", response.json())
