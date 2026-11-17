"""Simulated SMS: compose messages, write them to the outbox, and hand them to a sender interface.

The default sender only marks a message SENT (no carrier is involved). A real gateway implements the same
send() method. Result messages hold plain-language reasons only; never raw department data.
"""
import logging

from .db import many, one, run

logger = logging.getLogger("uvicorn.error")
MAX_BODY = 480
HEADLINES = {"FAILED_CHECK": "was not approved", "PARTIAL": "is incomplete", "INSUFFICIENT_DATA": "is incomplete",
             "PROVIDER_ERROR": "could not be completed", "MANUAL_REVIEW": "needs a manual check",
             "ACCESS_DENIED": "was not completed", "RULES_NOT_CONFIGURED": "was not completed"}


class SimulatedSender:
    """Accepts every message; nothing leaves the system."""

    def send(self, recipient, body):
        return None


def otp_body(code, service_name, dept, minutes):
    return (f"MahaSetu: {code} is your one-time code to let {service_name} check your {dept} records. "
            f"It is valid for {minutes} minutes. Do not share it with anyone.")[:MAX_BODY]


def reference(transaction):
    return str(transaction).replace("-", "")[:8].upper()


def result_body(service_name, transaction, state, checks):
    """checks: [(outcome, reason_code, message)] in report order. Lists failures first, then unresolved items."""
    head = f"MahaSetu: Your {service_name} verification (ref {reference(transaction)}) {HEADLINES.get(state, 'was not approved')}."
    reasons, codes, seen = [], [], set()
    for wanted in ("FAIL", "NEEDS_REVIEW"):
        for outcome, code, message in checks:
            if outcome == wanted and message not in seen:
                seen.add(message)
                reasons.append(message)
                codes.append(code)
    tail = " You can apply again once this is resolved."
    body, used = head + " Reasons:", 0
    for number, message in enumerate(reasons, 1):
        part = f" {number}) {message}"
        remaining = len(reasons) - number
        more = f" (+{remaining} more)" if remaining else ""
        if used and len(body) + len(part) + len(more) + len(tail) > MAX_BODY:
            break
        body += part
        used = number
    if used < len(reasons):
        body += f" (+{len(reasons) - used} more)"
    return (body + tail)[:MAX_BODY], list(dict.fromkeys(codes[:used]))


def enqueue(conn, request_id, kind, mobile, body, codes=()):
    return one(conn, """INSERT INTO mahasetu.message_outbox (request_id, kind, recipient_mobile, body, reason_codes)
        VALUES (:request, :kind, :mobile, :body, CAST(:codes AS text[])) RETURNING id""",
               request=request_id, kind=kind, mobile=mobile, body=body, codes=list(codes))["id"]


def dispatch(db, sender, now, limit=50):
    """Send queued messages and mark them. A sender failure marks the message FAILED and never affects a decision."""
    with db.begin() as conn:
        queued = many(conn, """SELECT id, recipient_mobile, body FROM mahasetu.message_outbox
            WHERE status = 'QUEUED' ORDER BY created_at LIMIT :limit FOR UPDATE SKIP LOCKED""", limit=limit)
        for message in queued:
            try:
                sender.send(message["recipient_mobile"], message["body"])
            except Exception:  # noqa: BLE001 - the sender is an external boundary
                logger.warning("message delivery failed")
                run(conn, "UPDATE mahasetu.message_outbox SET status = 'FAILED' WHERE id = :id", id=message["id"])
            else:
                run(conn, "UPDATE mahasetu.message_outbox SET status = 'SENT', sent_at = :now WHERE id = :id",
                    id=message["id"], now=now)
