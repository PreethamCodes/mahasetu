"""Core orchestration: requests, applicant sessions, one-time codes and consent, department checks, completion.

Rules of the design:
- Every state change is validated against states.json and guarded by a state precondition inside one transaction.
- Technical failures never become eligibility failures; an unreachable department stays UNAVAILABLE and is retryable.
- Failures that must be reported to the caller are raised only after the transaction that recorded them commits.
"""
import base64
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import outbox, rules
from .connectors import Departments
from .db import Database, many, one, run
from .errors import ApiError, conflict, not_found, unauthorized
from .normalize import normalize
from .security import digest, mask_mobile, new_token, otp_code, otp_hash, same
from .settings import Settings
from .states import ACTIVE, can_move, is_terminal

logger = logging.getLogger("uvicorn.error")
IST = timezone(timedelta(hours=5, minutes=30))
UNAVAILABLE_TEXT = "This department is temporarily unavailable. You can try again."
STOPPED_TEXT = "The check was stopped."
RESULT_STATES_WITHOUT_MESSAGE = frozenset({"VERIFIED", "CANCELLED"})
REQUEST_SQL = """SELECT r.*, s.display_name AS service_name, s.return_url
    FROM mahasetu.verification_requests r JOIN mahasetu.registered_services s ON s.service_id = r.service_id
    WHERE r.id = :id"""


def iso(value):
    return value.isoformat() if value else None


def return_url(base, transaction):
    parts = urlsplit(base)
    query = parse_qsl(parts.query, keep_blank_values=True) + [("ref", str(transaction))]
    return urlunsplit(parts._replace(query=urlencode(query)))


class Session:
    def __init__(self, request_id, expires_at):
        self.request_id, self.expires_at = request_id, expires_at


class Core:
    def __init__(self, settings: Settings, db=None, departments=None, sender=None, clock=None):
        self.settings = settings
        self.db = db or Database(settings.database_url)
        self.departments = departments or Departments(settings)
        self.sender = sender or outbox.SimulatedSender()
        self.clock = clock or (lambda: datetime.now(UTC))

    def close(self):
        self.departments.close()
        self.db.dispose()

    def now(self):
        return self.clock()

    def today(self):
        return self.now().astimezone(IST).date()

    # ------------------------------------------------------------------ authentication

    def authenticate_requester(self, token):
        with self.db.begin() as conn:
            services = many(conn, "SELECT service_id, credential_ref FROM mahasetu.registered_services")
        match = None
        for service in services:
            try:
                expected = self.settings.requester_token(service["credential_ref"])
            except RuntimeError:  # a configured token that is too short is never accepted
                logger.warning("requester credential for %s is not usable", service["service_id"])
                continue
            if expected and token and same(token, expected):
                match = service["service_id"]
        if match is None:
            raise unauthorized()
        return match

    def authenticate_dashboard(self, token):
        if not token or not same(token, self.settings.dashboard_token):
            raise unauthorized()

    def session(self, token):
        if not token:
            raise unauthorized("SESSION_REQUIRED", "An applicant session is required.")
        with self.db.begin() as conn:
            found = one(conn, "SELECT request_id, expires_at FROM mahasetu.applicant_sessions WHERE token_hash = :h",
                        h=digest(token))
        if not found:
            raise unauthorized("SESSION_INVALID", "The session is not valid.")
        if found["expires_at"] <= self.now():
            raise unauthorized("SESSION_EXPIRED", "The session has expired. Start again from the requesting service.")
        return Session(found["request_id"], found["expires_at"])

    # ------------------------------------------------------------------ helpers

    def audit(self, conn, request_id, actor, event, detail=None):
        run(conn, """INSERT INTO mahasetu.audit_events (request_id, actor_ref, event_type, detail)
            VALUES (:r, :a, :e, CAST(:d AS jsonb))""", r=request_id, a=actor, e=event, d=json.dumps(detail or {}))

    def load(self, conn, request_id, lock=False):
        found = one(conn, REQUEST_SQL + (" FOR UPDATE OF r" if lock else ""), id=request_id)
        if not found:
            raise not_found("REQUEST_NOT_FOUND", "The verification request was not found.")
        return found

    def move(self, conn, req, target, actor):
        source = one(conn, "SELECT state FROM mahasetu.verification_requests WHERE id = :id", id=req["id"])["state"]
        if source == target:
            return target
        if not can_move(source, target):
            raise RuntimeError(f"Invalid transition {source} -> {target}")
        changed = one(conn, """UPDATE mahasetu.verification_requests SET state = :t, updated_at = :now
            WHERE id = :id AND state = :s RETURNING id""", t=target, now=self.now(), id=req["id"], s=source)
        if not changed:
            raise conflict("STATE_CHANGED", "The request changed while this was processed. Try again.")
        self.audit(conn, req["id"], actor, "STATE_CHANGED", {"from": source, "to": target})
        return target

    def state_of(self, conn, request_id):
        return one(conn, "SELECT state FROM mahasetu.verification_requests WHERE id = :id", id=request_id)["state"]

    def plan(self, conn, req):
        """Departments requested for this transaction with display data and whether authorization is needed."""
        out = []
        for item in req["requested_evidence"]:
            provider, categories = item["provider_id"], list(item["evidence_categories"])
            info = one(conn, "SELECT display_name FROM mahasetu.registered_providers WHERE provider_id = :p", p=provider)
            policy = one(conn, """SELECT bool_or(authorization_required) AS needs FROM mahasetu.access_policies
                WHERE service_id = :s AND provider_id = :p AND purpose = :pu AND category = ANY(:c)""",
                         s=req["service_id"], p=provider, pu=req["purpose"], c=categories)
            documents = many(conn, """SELECT category, label, description FROM mahasetu.evidence_categories
                WHERE provider_id = :p AND category = ANY(:c)""", p=provider, c=categories)
            labels = {d["category"]: d for d in documents}
            out.append({"provider_id": provider, "display_name": info["display_name"] if info else provider,
                        "categories": categories, "authorization_required": bool(policy and policy["needs"]),
                        "documents": [{"category": c, "label": labels[c]["label"] if c in labels else c.title(),
                                       "description": labels[c]["description"] if c in labels
                                       else f"{c.title()} information."} for c in categories]})
        return out

    def department(self, plan, provider_id):
        found = next((d for d in plan if d["provider_id"] == provider_id), None)
        if found is None:
            raise not_found("UNKNOWN_DEPARTMENT", "That department is not part of this verification.")
        return found

    def reap(self, conn, request_id):
        """Lazily close out anything left open by time: stale retrievals, expired consents and expired codes."""
        now = self.now()
        run(conn, """UPDATE mahasetu.department_checks SET status = 'UNAVAILABLE', run_id = NULL, detail = :d,
            updated_at = :now WHERE request_id = :id AND status = 'RETRIEVING' AND started_at < :cutoff""",
            d=UNAVAILABLE_TEXT, now=now, id=request_id,
            cutoff=now - timedelta(seconds=self.settings.retrieval_stale_seconds))
        run(conn, """UPDATE mahasetu.authorization_records SET status = 'EXPIRED'
            WHERE request_id = :id AND status = 'GRANTED' AND expires_at <= :now""", id=request_id, now=now)
        run(conn, """UPDATE mahasetu.otp_challenges SET status = 'EXPIRED'
            WHERE request_id = :id AND status = 'ACTIVE' AND expires_at <= :now""", id=request_id, now=now)

    def authorizations(self, conn, request_id):
        return {a["provider_id"]: a for a in many(
            conn, "SELECT * FROM mahasetu.authorization_records WHERE request_id = :id", id=request_id)}

    def authorization_view(self, auth, req):
        status = auth["status"]
        return {"authorization_reference": str(auth["id"]), "applicant_reference": req["applicant_ref"],
                "service_id": req["service_id"], "provider_id": auth["provider_id"],
                "evidence_categories": list(auth["categories"]), "purpose": req["purpose"], "status": status,
                "authorized_at": iso(auth["decided_at"]) if status in ("GRANTED", "EXPIRED", "REVOKED") else None,
                "expires_at": iso(auth["expires_at"]), "revoked_at": iso(auth["revoked_at"]),
                "transaction_reference": str(req["id"])}

    def cards(self, conn, req, plan):
        request_id = req["id"]
        auths = self.authorizations(conn, request_id)
        checks = {c["provider_id"]: c for c in many(
            conn, "SELECT * FROM mahasetu.department_checks WHERE request_id = :id", id=request_id)}
        challenges = {c["provider_id"]: c for c in many(conn, """SELECT DISTINCT ON (provider_id) * FROM
            mahasetu.otp_challenges WHERE request_id = :id ORDER BY provider_id, created_at DESC""", id=request_id)}
        statuses = {}
        for r in many(conn, """SELECT provider_id, evidence_status FROM mahasetu.canonical_records
                WHERE request_id = :id AND superseded_at IS NULL""", id=request_id):
            statuses.setdefault(r["provider_id"], []).append(r["evidence_status"])
        masked, now, cards = mask_mobile(req["applicant_mobile"]), self.now(), []
        for dept in plan:
            p, a, c, ch = dept["provider_id"], auths.get(dept["provider_id"]), checks.get(dept["provider_id"]), \
                challenges.get(dept["provider_id"])
            detail, stamp = None, None
            if a and a["status"] in ("DENIED", "REVOKED"):
                status = "DECLINED"
                detail = "You withdrew your permission." if a["status"] == "REVOKED" else "You declined access."
                stamp = a["revoked_at"] or a["decided_at"]
            elif c and c["status"] == "RETRIEVING":
                status, detail, stamp = "RETRIEVING", "Fetching the records.", c["updated_at"]
            elif c and c["status"] == "COMPLETED":
                status, stamp, found = "COMPLETED", c["updated_at"], statuses.get(p, [])
                usable = [s for s in found if s in ("PRESENT", "EXPIRED")]
                if usable and len(usable) < len(found):
                    detail = "Records retrieved; some documents were not found."
                elif usable:
                    detail = "Records retrieved and standardized."
                elif "INVALID" in found:
                    detail = "A record was received but needs a manual check."
                else:
                    detail = "No matching record was found at this department."
            elif c and c["status"] == "UNAVAILABLE":
                status, detail, stamp = "UNAVAILABLE", c["detail"] or UNAVAILABLE_TEXT, c["updated_at"]
            elif a and a["status"] == "GRANTED":
                status, detail, stamp = "AUTHORIZED", "Permission given. Ready to fetch the records.", a["decided_at"]
            elif ch and ch["status"] == "ACTIVE" and ch["expires_at"] > now:
                status, detail, stamp = "OTP_SENT", f"A code was sent to {masked}.", ch["created_at"]
            else:
                status = "NOT_STARTED"
            cards.append({"provider_id": p, "display_name": dept["display_name"], "status": status,
                          "authorization_required": dept["authorization_required"], "documents": dept["documents"],
                          "detail": detail, "updated_at": iso(stamp)})
        return cards

    # ------------------------------------------------------------------ requesting service

    def create_request(self, service_id, body, idempotency_key):
        requested = {}
        for item in body["requested_evidence"]:
            if item["provider_id"] in requested:
                raise ApiError(400, "DUPLICATE_PROVIDER", "List each department once in requested_evidence.")
            requested[item["provider_id"]] = list(dict.fromkeys(item["evidence_categories"]))  # keep the caller's order
        if body["service_id"] != service_id:
            raise ApiError(403, "SERVICE_MISMATCH", "The request is for a different service than the credential.",
                           category="PERMISSION")
        normalized = {"application_reference": body["application_reference"],
                      "applicant_reference": body["applicant_reference"], "applicant_mobile": body["applicant_mobile"],
                      "purpose": body["purpose"],
                      "requested_evidence": sorted((p, sorted(c)) for p, c in requested.items())}
        payload_digest = digest(json.dumps(normalized, sort_keys=True, separators=(",", ":")))
        launch, now = new_token(), self.now()
        launch_expires = now + timedelta(seconds=self.settings.launch_ttl_seconds)
        with self.db.begin() as conn:
            for provider, categories in requested.items():
                if not one(conn, "SELECT 1 FROM mahasetu.registered_providers WHERE provider_id = :p", p=provider):
                    raise not_found("UNKNOWN_PROVIDER", f"Department {provider} is not registered.", provider=provider)
                for category in categories:
                    if not one(conn, """SELECT 1 FROM mahasetu.access_policies WHERE service_id = :s AND provider_id = :p
                            AND category = :c AND purpose = :pu""", s=service_id, p=provider, c=category,
                               pu=body["purpose"]):
                        raise ApiError(403, "ACCESS_POLICY_DENIED",
                                       f"No access policy allows {category} from {provider} for this purpose.",
                                       provider=provider)
            evidence = [{"provider_id": p, "evidence_categories": c} for p, c in requested.items()]
            created = one(conn, """INSERT INTO mahasetu.verification_requests (applicant_ref, application_ref, service_id,
                    purpose, requested_evidence, idempotency_key, payload_digest, state, applicant_mobile, launch_hash,
                    launch_expires_at) VALUES (:ap, :app, :s, :pu, CAST(:ev AS jsonb), :k, :d, 'CREATED', :m, :lh, :le)
                ON CONFLICT (service_id, idempotency_key) DO NOTHING RETURNING id, state""",
                          ap=body["applicant_reference"], app=body["application_reference"], s=service_id,
                          pu=body["purpose"], ev=json.dumps(evidence), k=idempotency_key, d=payload_digest,
                          m=body["applicant_mobile"], lh=digest(launch), le=launch_expires)
            if created:
                for provider, categories in requested.items():
                    needs = one(conn, """SELECT bool_or(authorization_required) AS needs FROM mahasetu.access_policies
                        WHERE service_id = :s AND provider_id = :p AND purpose = :pu AND category = ANY(:c)""",
                                s=service_id, p=provider, pu=body["purpose"], c=categories)["needs"]
                    if needs:
                        run(conn, """INSERT INTO mahasetu.authorization_records (request_id, provider_id, categories,
                            status) VALUES (:r, :p, :c, 'PENDING')""", r=created["id"], p=provider, c=categories)
                self.audit(conn, created["id"], service_id, "REQUEST_CREATED",
                           {"departments": sorted(requested), "application": body["application_reference"]})
                return {"transaction_reference": str(created["id"]), "state": created["state"],
                        "launch_reference": launch, "launch_expires_at": iso(launch_expires)}
            existing = one(conn, """SELECT id, state, payload_digest FROM mahasetu.verification_requests
                WHERE service_id = :s AND idempotency_key = :k FOR UPDATE""", s=service_id, k=idempotency_key)
            if existing["payload_digest"] != payload_digest:
                raise conflict("IDEMPOTENCY_CONFLICT", "This Idempotency-Key was already used with a different request.")
            # A replay means the caller lost the first response: issue a fresh launch reference and retire the old one.
            run(conn, """UPDATE mahasetu.verification_requests SET launch_hash = :lh, launch_expires_at = :le
                WHERE id = :id""", lh=digest(launch), le=launch_expires, id=existing["id"])
            self.audit(conn, existing["id"], service_id, "REQUEST_REPLAYED")
            return {"transaction_reference": str(existing["id"]), "state": existing["state"],
                    "launch_reference": launch, "launch_expires_at": iso(launch_expires)}

    def owned(self, conn, service_id, request_id, lock=False):
        req = self.load(conn, request_id, lock)
        if req["service_id"] != service_id:  # another service's transaction is indistinguishable from a missing one
            raise not_found("REQUEST_NOT_FOUND", "The verification request was not found.")
        return req

    def get_request(self, service_id, request_id):
        with self.db.begin() as conn:
            req = self.owned(conn, service_id, request_id)
            self.reap(conn, request_id)
            req = self.load(conn, request_id)
            auths = [self.authorization_view(a, req) for a in sorted(
                self.authorizations(conn, request_id).values(), key=lambda a: a["provider_id"])]
        return {"transaction_reference": str(req["id"]), "state": req["state"], "authorizations": auths}

    def cancel(self, service_id, request_id):
        with self.db.begin() as conn:
            req = self.owned(conn, service_id, request_id, lock=True)
            state = req["state"]
            if state == "CANCELLED":
                return {"transaction_reference": str(req["id"]), "state": state}
            if is_terminal(state):
                raise conflict("TRANSACTION_CLOSED", "The request already finished and cannot be cancelled.",
                               transaction=req["id"])
            self.move(conn, req, "CANCELLED", service_id)
            now = self.now()
            run(conn, "UPDATE mahasetu.verification_requests SET completed_at = :now WHERE id = :id", now=now, id=req["id"])
            run(conn, """UPDATE mahasetu.department_checks SET status = 'STOPPED', run_id = NULL, updated_at = :now
                WHERE request_id = :id AND status IN ('RETRIEVING', 'COMPLETED', 'UNAVAILABLE')""", now=now, id=req["id"])
            run(conn, "UPDATE mahasetu.otp_challenges SET status = 'SUPERSEDED' WHERE request_id = :id AND status = 'ACTIVE'",
                id=req["id"])
        return {"transaction_reference": str(request_id), "state": "CANCELLED"}

    def requirements(self, service_id):
        with self.db.begin() as conn:
            purposes = [r["purpose"] for r in many(conn, """SELECT DISTINCT purpose FROM mahasetu.access_policies
                WHERE service_id = :s ORDER BY purpose""", s=service_id)]
            if not purposes:
                raise not_found("SERVICE_NOT_FOUND", "No requirements are registered for that service.")
            rows = many(conn, """SELECT provider_id, array_agg(category ORDER BY category) AS categories,
                bool_or(authorization_required) AS needs FROM mahasetu.access_policies
                WHERE service_id = :s AND purpose = :p GROUP BY provider_id ORDER BY provider_id""",
                        s=service_id, p=purposes[0])
        return {"service_id": service_id, "purpose": purposes[0],
                "requirements": [{"provider_id": r["provider_id"], "evidence_categories": list(r["categories"]),
                                  "authorization_required": bool(r["needs"])} for r in rows]}

    def results(self, service_id, request_id):
        with self.db.begin() as conn:
            req = self.owned(conn, service_id, request_id)
            return self.report(conn, req)

    # ------------------------------------------------------------------ applicant session

    def exchange(self, launch_reference):
        with self.db.begin() as conn:
            found = one(conn, "SELECT id, launch_expires_at FROM mahasetu.verification_requests WHERE launch_hash = :h",
                        h=digest(launch_reference))
            if not found:
                raise not_found("LAUNCH_NOT_FOUND", "That link is not valid. Start again from the requesting service.")
            if found["launch_expires_at"] <= self.now():
                raise ApiError(410, "LAUNCH_EXPIRED", "That link has expired. Start again from the requesting service.")
            req = self.load(conn, found["id"], lock=True)
            if req["state"] == "CREATED":
                self.move(conn, req, "AUTHENTICATION_PENDING", "applicant")
            token = new_token()
            expires = self.now() + timedelta(seconds=self.settings.session_ttl_seconds)
            run(conn, "INSERT INTO mahasetu.applicant_sessions (request_id, token_hash, expires_at) VALUES (:r, :h, :e)",
                r=req["id"], h=digest(token), e=expires)
            self.audit(conn, req["id"], "applicant", "SESSION_ISSUED")
        return {"session_token": token, "session_expires_at": iso(expires), "transaction_reference": str(req["id"])}

    def overview(self, session):
        with self.db.begin() as conn:
            self.reap(conn, session.request_id)
            req = self.load(conn, session.request_id)
            plan = self.plan(conn, req)
            cards = self.cards(conn, req, plan)
        return {"transaction_reference": str(req["id"]), "state": req["state"], "service_id": req["service_id"],
                "service_name": req["service_name"], "purpose": req["purpose"],
                "mobile_masked": mask_mobile(req["applicant_mobile"]), "session_expires_at": iso(session.expires_at),
                "can_complete": all(c["status"] != "RETRIEVING" for c in cards), "departments": cards}

    def card_for(self, conn, req, provider_id):
        plan = self.plan(conn, req)
        self.department(plan, provider_id)
        return next(c for c in self.cards(conn, req, plan) if c["provider_id"] == provider_id)

    def active(self, req):
        if not (req["state"] in ACTIVE):
            raise conflict("TRANSACTION_CLOSED", "This verification has finished.", transaction=req["id"])

    # ------------------------------------------------------------------ one-time code and consent

    def send_otp(self, session, provider_id):
        failure = None
        with self.db.begin() as conn:
            req = self.load(conn, session.request_id, lock=True)
            self.active(req)
            self.reap(conn, req["id"])
            dept = self.department(self.plan(conn, req), provider_id)
            if not dept["authorization_required"]:
                raise conflict("AUTHORIZATION_NOT_REQUIRED", "This department needs no one-time code.")
            auth = self.authorizations(conn, req["id"]).get(provider_id)
            now = self.now()
            if auth and auth["status"] == "GRANTED":
                raise conflict("ALREADY_AUTHORIZED", "Permission was already given for this department.")
            history = many(conn, """SELECT status, expires_at, resend_after FROM mahasetu.otp_challenges
                WHERE request_id = :r AND provider_id = :p ORDER BY created_at DESC""", r=req["id"], p=provider_id)
            if len(history) >= self.settings.otp_max_challenges:
                failure = ApiError(429, "OTP_LIMIT_REACHED", "Too many codes were requested for this department.")
            elif history and history[0]["status"] == "ACTIVE" and history[0]["resend_after"] > now:
                seconds = max(int((history[0]["resend_after"] - now).total_seconds()) + 1, 1)
                failure = ApiError(429, "OTP_RESEND_TOO_SOON", f"Wait {seconds} seconds before asking for another code.",
                                   retryable=True, headers={"Retry-After": str(seconds)})
            if failure is None:
                code, challenge = otp_code(), uuid.uuid4()
                expires = now + timedelta(seconds=self.settings.otp_ttl_seconds)
                resend = now + timedelta(seconds=self.settings.otp_resend_seconds)
                run(conn, "UPDATE mahasetu.otp_challenges SET status = 'SUPERSEDED' WHERE request_id = :r AND provider_id = :p AND status = 'ACTIVE'",
                    r=req["id"], p=provider_id)
                run(conn, """INSERT INTO mahasetu.otp_challenges (id, request_id, provider_id, code_hash, created_at,
                    expires_at, resend_after) VALUES (:id, :r, :p, :h, :now, :e, :rs)""", id=challenge, r=req["id"],
                    p=provider_id, h=otp_hash(self.settings.otp_hash_key, challenge, code), now=now, e=expires, rs=resend)
                if auth and auth["status"] in ("DENIED", "REVOKED", "EXPIRED"):  # the applicant changed their mind
                    run(conn, """UPDATE mahasetu.authorization_records SET status = 'PENDING', decided_at = NULL,
                        expires_at = NULL, revoked_at = NULL WHERE id = :id""", id=auth["id"])
                outbox.enqueue(conn, req["id"], "OTP", req["applicant_mobile"], outbox.otp_body(
                    code, req["service_name"], dept["display_name"], self.settings.otp_ttl_seconds // 60))
                self.audit(conn, req["id"], "applicant", "OTP_SENT", {"provider": provider_id})
                result = {"challenge_reference": str(challenge), "provider_id": provider_id,
                          "mobile_masked": mask_mobile(req["applicant_mobile"]), "expires_at": iso(expires),
                          "resend_after": iso(resend), "attempts_allowed": self.settings.otp_max_attempts}
                if self.settings.demo_otp_disclosure:
                    result["demo_code"] = code
        if failure:
            raise failure
        outbox.dispatch(self.db, self.sender, self.now())
        return result

    def verify_otp(self, session, provider_id, challenge_reference, code):
        failure = None
        with self.db.begin() as conn:
            req = self.load(conn, session.request_id, lock=True)
            self.active(req)
            self.reap(conn, req["id"])
            dept = self.department(self.plan(conn, req), provider_id)
            if not dept["authorization_required"]:
                raise conflict("AUTHORIZATION_NOT_REQUIRED", "This department needs no one-time code.")
            auth = self.authorizations(conn, req["id"]).get(provider_id)
            if auth and auth["status"] == "GRANTED":
                raise conflict("ALREADY_AUTHORIZED", "Permission was already given for this department.")
            challenge = one(conn, """SELECT * FROM mahasetu.otp_challenges WHERE id = :id AND request_id = :r
                AND provider_id = :p FOR UPDATE""", id=challenge_reference, r=req["id"], p=provider_id)
            now = self.now()
            if not challenge:
                raise conflict("NO_ACTIVE_CHALLENGE", "Ask for a new code first.")
            if challenge["status"] != "ACTIVE":
                failure = {"EXHAUSTED": ApiError(429, "OTP_ATTEMPTS_EXHAUSTED", "Too many wrong codes. Ask for a new code."),
                           "EXPIRED": ApiError(410, "OTP_EXPIRED", "The code has expired. Ask for a new code.")}.get(
                    challenge["status"], conflict("NO_ACTIVE_CHALLENGE", "Ask for a new code first."))
            elif challenge["expires_at"] <= now:
                run(conn, "UPDATE mahasetu.otp_challenges SET status = 'EXPIRED' WHERE id = :id", id=challenge["id"])
                failure = ApiError(410, "OTP_EXPIRED", "The code has expired. Ask for a new code.")
            else:
                attempts = challenge["attempts"] + 1
                expected = otp_hash(self.settings.otp_hash_key, challenge["id"], code)
                if same(expected, challenge["code_hash"]):
                    run(conn, """UPDATE mahasetu.otp_challenges SET status = 'VERIFIED', verified_at = :now,
                        attempts = :a WHERE id = :id""", now=now, a=attempts, id=challenge["id"])
                    expires = now + timedelta(seconds=self.settings.authorization_ttl_seconds)
                    run(conn, """UPDATE mahasetu.authorization_records SET status = 'GRANTED', decided_at = :now,
                        expires_at = :e, revoked_at = NULL WHERE request_id = :r AND provider_id = :p""",
                        now=now, e=expires, r=req["id"], p=provider_id)
                    if req["state"] == "AUTHENTICATION_PENDING":
                        self.move(conn, req, "AUTHORIZED", "applicant")
                    self.audit(conn, req["id"], "applicant", "CONSENT_GRANTED", {"provider": provider_id})
                    auth = self.authorizations(conn, req["id"])[provider_id]
                    req = self.load(conn, req["id"])
                    result = {"department": self.card_for(conn, req, provider_id),
                              "authorization": self.authorization_view(auth, req)}
                elif attempts >= self.settings.otp_max_attempts:
                    run(conn, "UPDATE mahasetu.otp_challenges SET status = 'EXHAUSTED', attempts = :a WHERE id = :id",
                        a=attempts, id=challenge["id"])
                    failure = ApiError(429, "OTP_ATTEMPTS_EXHAUSTED", "Too many wrong codes. Ask for a new code.")
                else:
                    run(conn, "UPDATE mahasetu.otp_challenges SET attempts = :a WHERE id = :id", a=attempts, id=challenge["id"])
                    left = self.settings.otp_max_attempts - attempts
                    failure = ApiError(400, "INVALID_CODE", f"That code is not correct. {left} attempt(s) left.")
        if failure:
            raise failure
        return result

    def decline(self, session, provider_id):
        with self.db.begin() as conn:
            req = self.load(conn, session.request_id, lock=True)
            self.active(req)
            self.reap(conn, req["id"])
            dept = self.department(self.plan(conn, req), provider_id)
            if not dept["authorization_required"]:
                raise conflict("AUTHORIZATION_NOT_REQUIRED", "This department needs no permission to withdraw.")
            auth = self.authorizations(conn, req["id"]).get(provider_id)
            now = self.now()
            if auth and auth["status"] in ("DENIED", "REVOKED"):
                return self.card_for(conn, req, provider_id)
            granted = auth and auth["status"] == "GRANTED"
            run(conn, """UPDATE mahasetu.authorization_records SET status = :s, decided_at = coalesce(decided_at, :now),
                revoked_at = :rv WHERE request_id = :r AND provider_id = :p""", s="REVOKED" if granted else "DENIED",
                now=now, rv=now if granted else None, r=req["id"], p=provider_id)
            run(conn, "UPDATE mahasetu.otp_challenges SET status = 'SUPERSEDED' WHERE request_id = :r AND provider_id = :p AND status = 'ACTIVE'",
                r=req["id"], p=provider_id)
            # Consent withdrawn: stop any call in flight, and redact evidence already retrieved from this department.
            run(conn, """UPDATE mahasetu.department_checks SET status = 'STOPPED', run_id = NULL, detail = :d,
                updated_at = :now WHERE request_id = :r AND provider_id = :p AND status <> 'STOPPED'""",
                d=STOPPED_TEXT, now=now, r=req["id"], p=provider_id)
            run(conn, """UPDATE mahasetu.canonical_records SET superseded_at = :now, evidence = '{}'::jsonb
                WHERE request_id = :r AND provider_id = :p AND superseded_at IS NULL""", now=now, r=req["id"], p=provider_id)
            self.audit(conn, req["id"], "applicant", "CONSENT_WITHDRAWN" if granted else "CONSENT_DECLINED",
                       {"provider": provider_id})
            return self.card_for(conn, req, provider_id)

    # ------------------------------------------------------------------ department checks

    def start_check(self, session, provider_id):
        with self.db.begin() as conn:
            req = self.load(conn, session.request_id, lock=True)
            self.active(req)
            self.reap(conn, req["id"])
            dept = self.department(self.plan(conn, req), provider_id)
            if dept["authorization_required"]:
                auth = self.authorizations(conn, req["id"]).get(provider_id)
                if not auth or auth["status"] != "GRANTED" or auth["expires_at"] <= self.now():
                    raise ApiError(403, "AUTHORIZATION_REQUIRED",
                                   "Give permission with a one-time code before this department is checked.",
                                   category="PERMISSION", transaction=req["id"], provider=provider_id)
            current = one(conn, "SELECT status FROM mahasetu.department_checks WHERE request_id = :r AND provider_id = :p",
                          r=req["id"], p=provider_id)
            if current and current["status"] == "RETRIEVING":
                raise conflict("RETRIEVAL_IN_PROGRESS", "This department is already being checked.", provider=provider_id)
            if req["state"] == "AUTHENTICATION_PENDING":
                self.move(conn, req, "AUTHORIZED", "applicant")
            if req["state"] != "PROCESSING":
                self.move(conn, req, "PROCESSING", "applicant")
            run_id, now = uuid.uuid4(), self.now()
            run(conn, """INSERT INTO mahasetu.department_checks (request_id, provider_id, status, run_id, detail,
                    attempts, started_at, updated_at) VALUES (:r, :p, 'RETRIEVING', :run, NULL, 0, :now, :now)
                ON CONFLICT (request_id, provider_id) DO UPDATE SET status = 'RETRIEVING', run_id = :run,
                    detail = NULL, attempts = 0, started_at = :now, updated_at = :now""",
                r=req["id"], p=provider_id, run=run_id, now=now)
            self.audit(conn, req["id"], "applicant", "RETRIEVAL_STARTED", {"provider": provider_id})
            req = self.load(conn, req["id"])
            return self.card_for(conn, req, provider_id), run_id

    def run_retrieval(self, request_id, provider_id, run_id):
        """Background worker: call the department, standardize the answer, persist it if the run is still current."""
        try:
            with self.db.begin() as conn:
                req = self.load(conn, request_id)
                endpoint = one(conn, """SELECT url, schema_version FROM mahasetu.provider_endpoints
                    WHERE provider_id = :p ORDER BY id DESC LIMIT 1""", p=provider_id)
                categories = next(i["evidence_categories"] for i in req["requested_evidence"]
                                  if i["provider_id"] == provider_id)
                current = one(conn, "SELECT run_id FROM mahasetu.department_checks WHERE request_id = :r AND provider_id = :p AND status = 'RETRIEVING'",
                              r=request_id, p=provider_id)
            if not current or str(current["run_id"]) != str(run_id):
                return
            if not endpoint:
                fetch, records = None, None
            else:
                fetch = self.departments.fetch(provider_id, endpoint["url"], req["applicant_ref"], categories, str(request_id))
                records = None
                if fetch.outcome in ("OK", "MISSING"):
                    try:
                        records = normalize(provider_id, fetch, categories, req["applicant_ref"], self.now(), self.today(),
                                            endpoint["schema_version"])
                    except (ValueError, KeyError, TypeError, AttributeError):
                        logger.warning("normalization failed for %s", provider_id)
                        fetch.outcome, fetch.code, fetch.retryable = "ERROR", "NORMALIZATION_FAILED", False
            self.finish_retrieval(request_id, provider_id, run_id, fetch, records)
        except Exception:  # noqa: BLE001 - never leave a department stuck in RETRIEVING
            logger.exception("retrieval failed unexpectedly")
            self.fail_retrieval(request_id, provider_id, run_id)

    def fail_retrieval(self, request_id, provider_id, run_id):
        with self.db.begin() as conn:
            run(conn, """UPDATE mahasetu.department_checks SET status = 'UNAVAILABLE', run_id = NULL, detail = :d,
                updated_at = :now WHERE request_id = :r AND provider_id = :p AND run_id = :run""",
                d=UNAVAILABLE_TEXT, now=self.now(), r=request_id, p=provider_id, run=run_id)

    def finish_retrieval(self, request_id, provider_id, run_id, fetch, records):
        with self.db.begin() as conn:
            req = self.load(conn, request_id, lock=True)
            now = self.now()
            check = one(conn, "SELECT run_id, status FROM mahasetu.department_checks WHERE request_id = :r AND provider_id = :p",
                        r=request_id, p=provider_id)
            auth = self.authorizations(conn, request_id).get(provider_id)
            outcome = fetch.code or fetch.outcome if fetch else "NO_ENDPOINT"
            if fetch:
                run(conn, """INSERT INTO mahasetu.provider_call_logs (request_id, provider_id, outcome, attempts, duration_ms,
                    detail) VALUES (:r, :p, :o, :a, :d, :det)""", r=request_id, p=provider_id,
                    o="OK" if fetch.outcome == "OK" else outcome, a=fetch.attempts, d=fetch.duration_ms,
                    det=(f"HTTP {fetch.http_status}" if fetch.http_status else None))
            current = (check and check["status"] == "RETRIEVING" and str(check["run_id"]) == str(run_id)
                       and req["state"] == "PROCESSING" and (auth is None or auth["status"] == "GRANTED"))
            if not current:  # cancelled, revoked, superseded or timed out meanwhile: audit and ignore the answer
                self.audit(conn, request_id, "core", "LATE_RESPONSE_IGNORED", {"provider": provider_id})
                return
            if records is None:
                text = UNAVAILABLE_TEXT
                run(conn, """UPDATE mahasetu.department_checks SET status = 'UNAVAILABLE', run_id = NULL, detail = :d,
                    attempts = :a, updated_at = :now WHERE request_id = :r AND provider_id = :p""",
                    d=text, a=fetch.attempts if fetch else 0, now=now, r=request_id, p=provider_id)
                self.audit(conn, request_id, "core", "RETRIEVAL_UNAVAILABLE",
                           {"provider": provider_id, "code": fetch.code if fetch else "NO_ENDPOINT"})
                return
            run(conn, """UPDATE mahasetu.canonical_records SET superseded_at = :now WHERE request_id = :r
                AND provider_id = :p AND superseded_at IS NULL""", now=now, r=request_id, p=provider_id)
            for category, doc in records:
                run(conn, """INSERT INTO mahasetu.canonical_records (request_id, provider_id, source_record_reference,
                        retrieved_at, evidence, category, evidence_status) VALUES (:r, :p, :ref, :at, CAST(:ev AS jsonb),
                        :c, :s)""", r=request_id, p=provider_id, ref=doc["source_record_reference"],
                    at=doc["retrieved_at"], ev=json.dumps(doc), c=category, s=doc["evidence_status"])
            run(conn, """UPDATE mahasetu.department_checks SET status = 'COMPLETED', run_id = NULL, detail = NULL,
                attempts = :a, updated_at = :now WHERE request_id = :r AND provider_id = :p""",
                a=fetch.attempts, now=now, r=request_id, p=provider_id)
            self.audit(conn, request_id, "core", "RETRIEVAL_COMPLETED",
                       {"provider": provider_id, "records": [r[0] for r in records]})

    # ------------------------------------------------------------------ completion and reports

    def complete(self, session):
        queued = False
        with self.db.begin() as conn:
            req = self.load(conn, session.request_id, lock=True)
            self.reap(conn, req["id"])
            if is_terminal(req["state"]):
                return self.completion(conn, req)
            if one(conn, "SELECT 1 FROM mahasetu.department_checks WHERE request_id = :r AND status = 'RETRIEVING'", r=req["id"]):
                raise conflict("RETRIEVAL_IN_PROGRESS", "A department is still being checked.", transaction=req["id"])
            plan = self.plan(conn, req)
            auths = self.authorizations(conn, req["id"])
            checks_by = {c["provider_id"]: c for c in many(
                conn, "SELECT * FROM mahasetu.department_checks WHERE request_id = :r", r=req["id"])}
            dept_state = {}
            for dept in plan:
                p, auth, check = dept["provider_id"], auths.get(dept["provider_id"]), checks_by.get(dept["provider_id"])
                if dept["authorization_required"] and (not auth or auth["status"] in ("DENIED", "REVOKED", "PENDING")):
                    dept_state[p] = "NOT_PROVIDED"
                elif check and check["status"] == "COMPLETED":
                    dept_state[p] = "COMPLETED"
                elif check and check["status"] == "UNAVAILABLE":
                    dept_state[p] = "UNAVAILABLE"
                else:
                    dept_state[p] = "NOT_PROVIDED"
            granted = any(a["status"] == "GRANTED" for a in auths.values())
            names = {d["provider_id"]: d["display_name"] for d in plan}
            if not granted and "COMPLETED" not in dept_state.values():
                results, state = [rules.transaction_check("NO_AUTHORIZATION")], "ACCESS_DENIED"
                self.move(conn, req, state, "core")
            else:
                if req["state"] == "AUTHENTICATION_PENDING":
                    self.move(conn, req, "AUTHORIZED", "core")
                self.move(conn, req, "PROCESSING", "core")
                records = {(r["provider_id"], r["category"]): rules.Record(str(r["id"]), r["provider_id"], r["category"],
                                                                            r["evidence"])
                           for r in many(conn, """SELECT id, provider_id, category, evidence FROM mahasetu.canonical_records
                               WHERE request_id = :r AND superseded_at IS NULL""", r=req["id"])
                           if dept_state.get(r["provider_id"]) == "COMPLETED"}
                rows = many(conn, """SELECT rule_id, version, scheme_id, approval_status, approved_by, approved_at, definition
                    FROM mahasetu.rule_versions WHERE scheme_id = :s AND approval_status = 'APPROVED'""", s=req["service_id"])
                loaded, skipped = rules.load_rules(rows, req["service_id"], self.today())
                if skipped:
                    self.audit(conn, req["id"], "core", "RULES_SKIPPED", {"skipped": [list(s) for s in skipped]})
                results = [rules.evaluate(rule, records, dept_state, names, self.today()) for rule in loaded]
                state = rules.decide(results, dept_state, records, {d["provider_id"] for d in plan})
                if state == "RULES_NOT_CONFIGURED":
                    results = [rules.transaction_check("RULES_NOT_CONFIGURED")]
                self.move(conn, req, state, "core")
            now = self.now()
            for position, check in enumerate(results):
                run(conn, """INSERT INTO mahasetu.verification_results (request_id, rule_id, version, outcome, reason_code,
                        evidence_refs, provider_id, message, position, severity, recorded_at) VALUES (:r, :ru, :v, :o,
                        :rc, CAST(:refs AS uuid[]), :p, :m, :pos, :sev, :now)""", r=req["id"], ru=check.rule_id,
                    v=check.version, o=check.outcome, rc=check.reason_code, refs=check.evidence_refs, p=check.provider_id,
                    m=check.message, pos=position, sev=check.severity, now=now)
            run(conn, "UPDATE mahasetu.verification_requests SET completed_at = :now WHERE id = :id", now=now, id=req["id"])
            if state not in RESULT_STATES_WITHOUT_MESSAGE:
                body, codes = outbox.result_body(req["service_name"], req["id"], state,
                                                 [(c.outcome, c.reason_code, c.message) for c in results])
                outbox.enqueue(conn, req["id"], "VERIFICATION_RESULT", req["applicant_mobile"], body, codes)
                queued = True
            self.audit(conn, req["id"], "core", "VERIFICATION_COMPLETED", {"state": state})
            response = {"transaction_reference": str(req["id"]), "state": state,
                        "return_url": return_url(req["return_url"], req["id"]), "message_queued": queued}
        if queued:
            outbox.dispatch(self.db, self.sender, self.now())
        return response

    def completion(self, conn, req):
        queued = bool(one(conn, "SELECT 1 FROM mahasetu.message_outbox WHERE request_id = :r AND kind = 'VERIFICATION_RESULT'",
                          r=req["id"]))
        return {"transaction_reference": str(req["id"]), "state": req["state"],
                "return_url": return_url(req["return_url"], req["id"]), "message_queued": queued}

    def report(self, conn, req):
        if not is_terminal(req["state"]):
            raise conflict("REPORT_PENDING", "The verification has not finished yet.", transaction=req["id"])
        rows = many(conn, """SELECT vr.rule_id, vr.version, vr.provider_id, vr.outcome, vr.reason_code, vr.message,
                rv.definition ->> 'rule_name' AS rule_name,
                (SELECT c.source_record_reference FROM mahasetu.canonical_records c WHERE c.id = vr.evidence_refs[1])
                    AS evidence_reference
            FROM mahasetu.verification_results vr
            LEFT JOIN mahasetu.rule_versions rv ON rv.rule_id = vr.rule_id AND rv.version = vr.version
            WHERE vr.request_id = :r ORDER BY vr.position""", r=req["id"])
        checks = [{"rule_id": r["rule_id"], "rule_version": r["version"], "rule_name": r["rule_name"],
                   "provider_id": r["provider_id"], "outcome": r["outcome"], "reason_code": r["reason_code"],
                   "message": r["message"], "evidence_reference": r["evidence_reference"]} for r in rows]
        count = lambda outcome: sum(1 for c in checks if c["outcome"] == outcome)  # noqa: E731
        return {"transaction_reference": str(req["id"]), "state": req["state"], "passed": count("PASS"),
                "failed": count("FAIL"), "needs_review": count("NEEDS_REVIEW"),
                "recommendation": rules.recommendation(req["state"]),
                "completed_at": iso(req["completed_at"] or req["updated_at"]), "checks": checks}

    def applicant_result(self, session):
        with self.db.begin() as conn:
            return self.report(conn, self.load(conn, session.request_id))

    # ------------------------------------------------------------------ message inbox and health

    def messages(self, transaction=None, kind=None, limit=25, cursor=None):
        after = None
        if cursor:
            try:
                stamp, ident = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                after = (datetime.fromisoformat(stamp), str(uuid.UUID(ident)))
            except (ValueError, TypeError):
                raise ApiError(400, "INVALID_CURSOR", "The cursor is not valid.") from None
        with self.db.begin() as conn:
            rows = many(conn, """SELECT id, request_id, kind, recipient_mobile, body, reason_codes, status, created_at, sent_at
                FROM mahasetu.message_outbox
                WHERE (CAST(:tx AS uuid) IS NULL OR request_id = CAST(:tx AS uuid))
                  AND (CAST(:kind AS text) IS NULL OR kind = CAST(:kind AS text))
                  AND (CAST(:ts AS timestamptz) IS NULL OR (created_at, id) < (CAST(:ts AS timestamptz), CAST(:id AS uuid)))
                ORDER BY created_at DESC, id DESC LIMIT :limit""", tx=str(transaction) if transaction else None,
                        kind=kind, ts=after[0] if after else None, id=after[1] if after else None, limit=limit + 1)
        page, more = rows[:limit], len(rows) > limit
        hide = self.settings.environment == "production"  # one-time codes are shown only in the demonstration inbox
        items = [{"message_reference": str(m["id"]),
                  "transaction_reference": str(m["request_id"]) if m["request_id"] else None, "kind": m["kind"],
                  "recipient_masked": mask_mobile(m["recipient_mobile"]),
                  "body": "A one-time code was sent to this number." if hide and m["kind"] == "OTP" else m["body"],
                  "reason_codes": list(m["reason_codes"]), "status": m["status"], "created_at": iso(m["created_at"]),
                  "sent_at": iso(m["sent_at"])} for m in page]
        token = None
        if more:
            last = page[-1]
            token = base64.urlsafe_b64encode(json.dumps([last["created_at"].isoformat(), str(last["id"])]).encode()).decode()
        return {"messages": items, "next_cursor": token}

    def health(self):
        try:
            with self.db.begin() as conn:
                one(conn, "SELECT 1 FROM mahasetu.registered_services LIMIT 1")
            return True
        except Exception:  # noqa: BLE001
            return False
