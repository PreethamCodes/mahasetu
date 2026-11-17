"""Deterministic rule engine. Only approved, applicable rule versions are executed.

AI may propose rules and humans approve them elsewhere; this module neither drafts nor approves. Technical
faults (an unavailable department) never become FAIL; they stay NEEDS_REVIEW with a PROVIDER_ reason code.
"""
import json
from dataclasses import dataclass, field
from datetime import date

from jsonschema import Draft202012Validator

from .settings import CONTRACTS
from .vocab import (FAIL_BY_OPERATOR, FIELDS, STATUS_MEANING, TRANSACTION_REASON_PROVIDER, pass_message,
                    reason_message)

RULE_SCHEMA = Draft202012Validator(json.loads((CONTRACTS / "rule.schema.json").read_text(encoding="utf-8")),
                                   format_checker=Draft202012Validator.FORMAT_CHECKER)
COMPARISONS = {"GREATER_THAN", "LESS_THAN", "GREATER_THAN_OR_EQUAL", "LESS_THAN_OR_EQUAL"}
MEMBERSHIP = {"EQUALS", "NOT_EQUALS", "IN", "NOT_IN"}
DATE_KINDS = {"issue_date", "expiry_date"}
STRING_KINDS = {"text", "certificate", "result", "registration", "enrolment"}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: int
    name: str
    field: str
    operator: str
    expected: object
    severity: str


@dataclass
class Record:
    id: str
    provider: str
    category: str
    doc: dict


@dataclass
class Check:
    rule_id: str | None
    version: int | None
    name: str | None
    provider_id: str
    severity: str
    outcome: str
    reason_code: str
    message: str
    evidence_refs: list = field(default_factory=list)


def _is_number(value):
    return isinstance(value, int | float) and not isinstance(value, bool)


def operands_compatible(rule_field, operator, expected):
    """Reject an incompatible field/operator/operand combination before any evaluation."""
    kind = FIELDS[rule_field][5]
    if operator in COMPARISONS:
        return kind == "number" and _is_number(expected)
    if operator in ("IS_VALID", "IS_PRESENT"):
        return expected is None
    if kind in DATE_KINDS:
        return False
    values = expected if operator in ("IN", "NOT_IN") else [expected]
    if not isinstance(values, list) or not values:
        return False
    return all(_is_number(v) for v in values) if kind == "number" else all(isinstance(v, str) for v in values)


def load_rules(rows, scheme_id, today):
    """rows: dicts with rule_id, version, scheme_id, approval_status, approved_by, approved_at, definition.

    Returns (rules, skipped) where skipped is a list of (rule_id, version, reason). The latest applicable approved
    version of each rule_id is used.
    """
    best, skipped = {}, []
    for row in rows:
        ident = (row["rule_id"], row["version"])
        if row["approval_status"] != "APPROVED":
            skipped.append((*ident, "not approved"))
            continue
        definition = dict(row["definition"], approval_status="APPROVED", approved_by=str(row["approved_by"]),
                          approved_at=row["approved_at"].isoformat() if row["approved_at"] else None)
        errors = list(RULE_SCHEMA.iter_errors(definition))
        if errors:
            skipped.append((*ident, "invalid definition: " + errors[0].message[:100]))
            continue
        if (definition["rule_id"], definition["version"]) != ident or definition["scheme_id"] != row["scheme_id"]:
            skipped.append((*ident, "metadata mismatch"))
            continue
        if row["scheme_id"] != scheme_id:
            continue
        start, end = date.fromisoformat(definition["effective_from"]), definition["effective_until"]
        end = date.fromisoformat(end) if end else None
        if (end and end < start) or today < start or (end and today > end):
            skipped.append((*ident, "not effective on the evaluation date"))
            continue
        if not operands_compatible(definition["evidence_field"], definition["operator"], definition["expected_value"]):
            skipped.append((*ident, "incompatible operands"))
            continue
        rule = Rule(definition["rule_id"], definition["version"], definition["rule_name"],
                    definition["evidence_field"], definition["operator"], definition["expected_value"],
                    definition["severity"])
        if rule.rule_id not in best or rule.version > best[rule.rule_id].version:
            best[rule.rule_id] = rule
    # Report order follows the field catalogue (revenue, examination, university), then the rule id.
    order = list(FIELDS)
    return sorted(best.values(), key=lambda r: (order.index(r.field), r.rule_id)), skipped


def _result(rule, outcome, code, message, refs=()):
    provider = FIELDS[rule.field][0]
    return Check(rule.rule_id, rule.version, rule.name, provider, rule.severity, outcome, code, message, list(refs))


def evaluate(rule, records, dept_state, dept_name, today):
    """records: {(provider, category): Record}; dept_state: provider -> COMPLETED | UNAVAILABLE | NOT_PROVIDED."""
    provider, categories, key, label, noun, kind = FIELDS[rule.field]
    dept = dept_name.get(provider, provider)
    text = dict(label=label, noun=noun, dept=dept)

    def review(code, refs=()):
        return _result(rule, "NEEDS_REVIEW", code, reason_message(code, **text), refs)

    state = dept_state.get(provider, "NOT_PROVIDED")
    if state == "UNAVAILABLE":
        return review("PROVIDER_UNAVAILABLE")
    candidates = [records[(provider, c)] for c in categories if (provider, c) in records]
    if state != "COMPLETED" or not candidates:
        return review("EVIDENCE_NOT_PROVIDED")
    chosen = next((r for r in candidates if r.doc["evidence"].get(key) is not None), candidates[0])
    refs, status = [chosen.id], chosen.doc["evidence_status"]
    if status == "INVALID":
        return review("IDENTITY_MISMATCH" if "IDENTITY_MISMATCH" in chosen.doc["validation_warnings"]
                      else "EVIDENCE_INVALID", refs)
    if status == "UNAVAILABLE":
        return review("PROVIDER_UNAVAILABLE", refs)
    value = chosen.doc["evidence"].get(key)
    if status == "MISSING" or value is None:
        return review("EVIDENCE_MISSING", refs)

    def done(ok, code=None):
        if ok:
            return _result(rule, "PASS", "RULE_PASSED", pass_message(rule.operator, label), refs)
        return _result(rule, "FAIL", code, reason_message(code, **text), refs)

    operator, expected = rule.operator, rule.expected
    if operator == "IS_PRESENT":
        return done(status == "PRESENT", "EVIDENCE_EXPIRED")
    if operator == "IS_VALID":
        return _is_valid(rule, kind, value, status, done, review, refs, today)
    if operator in COMPARISONS:
        ok = {"GREATER_THAN": value > expected, "LESS_THAN": value < expected,
              "GREATER_THAN_OR_EQUAL": value >= expected, "LESS_THAN_OR_EQUAL": value <= expected}[operator]
        return done(ok, FAIL_BY_OPERATOR[operator])
    pool = expected if operator in ("IN", "NOT_IN") else [expected]
    norm = (lambda v: v.upper() if isinstance(v, str) else v)
    inside = norm(value) in [norm(v) for v in pool]
    return done(inside if operator in ("EQUALS", "IN") else not inside, "VALUE_NOT_ACCEPTED")


def _is_valid(rule, kind, value, status, done, review, refs, today):
    if kind in DATE_KINDS:
        try:
            moment = date.fromisoformat(value)
        except (TypeError, ValueError):
            return review("EVIDENCE_INVALID", refs)
        return done(moment >= today, "EVIDENCE_EXPIRED") if kind == "expiry_date" else done(
            moment <= today, "EVIDENCE_NOT_VALID")
    if kind in STATUS_MEANING:
        if status == "EXPIRED":
            return done(False, "EVIDENCE_EXPIRED")
        verdict, code = STATUS_MEANING[kind].get(str(value).upper(), ("REVIEW", "EVIDENCE_UNRECOGNISED"))
        if verdict == "PASS":
            return done(True)
        return done(False, code) if verdict == "FAIL" else review(code, refs)
    return done(status == "PRESENT", "EVIDENCE_EXPIRED")


def transaction_check(code, state_text=None):
    """A transaction-level reason (provider_id 'mahasetu') for outcomes that no rule explains."""
    return Check(None, None, None, TRANSACTION_REASON_PROVIDER, "REQUIRED", "NEEDS_REVIEW", code,
                 state_text or reason_message(code))


def decide(checks, dept_state, records, requested):
    """Aggregate rule checks into a terminal state. Only REQUIRED checks drive the decision.

    FAILED_CHECK: valid evidence definitively failed a required rule.
    MANUAL_REVIEW: a department's record did not match the applicant's identifier.
    PROVIDER_ERROR: a department could not be reached and nothing failed.
    INSUFFICIENT_DATA: required evidence is wholly missing from a department.
    PARTIAL: some evidence is usable but required checks remain unresolved.
    VERIFIED: every required check passed.
    """
    if not checks:
        return "RULES_NOT_CONFIGURED"
    required = [c for c in checks if c.severity == "REQUIRED"]
    if any(c.outcome == "FAIL" for c in required):
        return "FAILED_CHECK"
    if any(c.reason_code == "IDENTITY_MISMATCH" for c in required):
        return "MANUAL_REVIEW"
    if any(c.reason_code == "PROVIDER_UNAVAILABLE" for c in required):
        return "PROVIDER_ERROR"
    unresolved = [c for c in required if c.outcome == "NEEDS_REVIEW"]
    if not unresolved:
        return "VERIFIED"

    def usable(provider):
        return any(r.doc["evidence_status"] in ("PRESENT", "EXPIRED") for (p, _), r in records.items()
                   if p == provider and dept_state.get(provider) == "COMPLETED")

    if any(not usable(c.provider_id) for c in unresolved if c.provider_id in requested):
        return "INSUFFICIENT_DATA"
    return "PARTIAL"


RECOMMENDATION = {"VERIFIED": "APPROVE", "FAILED_CHECK": "REJECT", "ACCESS_DENIED": "REJECT", "CANCELLED": "REJECT"}


def recommendation(state):
    return RECOMMENDATION.get(state, "MANUAL_REVIEW")
