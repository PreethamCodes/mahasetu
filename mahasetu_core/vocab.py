"""Field catalogue, status vocabularies and plain-language reason text.

Messages never contain raw department values (they feed the applicant SMS), only what is wrong or missing.
"""

# evidence_field -> (provider, candidate categories, key, label, noun, kind)
FIELDS = {
    "revenue.annual_income": ("revenue", ("income",), "annual_income", "Annual income", "annual income", "number"),
    "revenue.income_certificate_status": ("revenue", ("income",), "income_certificate_status",
                                          "Income certificate", "income certificate", "certificate"),
    "revenue.caste_certificate_status": ("revenue", ("caste",), "caste_certificate_status",
                                         "Caste certificate", "caste certificate", "certificate"),
    "revenue.certificate_issue_date": ("revenue", ("income", "caste"), "certificate_issue_date",
                                       "Certificate issue date", "certificate", "issue_date"),
    "revenue.certificate_expiry_date": ("revenue", ("income", "caste"), "certificate_expiry_date",
                                        "Certificate validity", "certificate", "expiry_date"),
    "examination.ssc_marks": ("examination", ("marks",), "ssc_marks", "SSC marks", "SSC marks", "number"),
    "examination.ssc_maximum_marks": ("examination", ("marks",), "ssc_maximum_marks", "SSC maximum marks",
                                      "SSC marks", "number"),
    "examination.ssc_result_status": ("examination", ("marks",), "ssc_result_status", "SSC result", "SSC result",
                                      "result"),
    "examination.intermediate_marks": ("examination", ("marks",), "intermediate_marks", "Intermediate marks",
                                       "Intermediate marks", "number"),
    "examination.intermediate_maximum_marks": ("examination", ("marks",), "intermediate_maximum_marks",
                                               "Intermediate maximum marks", "Intermediate marks", "number"),
    "examination.intermediate_result_status": ("examination", ("marks",), "intermediate_result_status",
                                               "Intermediate result", "Intermediate result", "result"),
    "university.registration_status": ("university", ("enrolment",), "registration_status",
                                       "University registration", "university registration", "registration"),
    "university.programme": ("university", ("enrolment",), "programme", "Programme", "programme", "text"),
    "university.department": ("university", ("enrolment",), "department", "Academic department",
                              "academic department", "text"),
    "university.enrolment_status": ("university", ("enrolment",), "enrolment_status", "Enrolment", "enrolment",
                                    "enrolment"),
    "university.bonafide_validity": ("university", ("bonafide",), "bonafide_validity", "Bonafide certificate",
                                     "bonafide certificate", "expiry_date"),
}

# Provider status -> ("PASS" | "FAIL" | "REVIEW", reason code) for IS_VALID.
STATUS_MEANING = {
    "certificate": {"VALID": ("PASS", None), "EXPIRED": ("FAIL", "EVIDENCE_EXPIRED"),
                    "REVOKED": ("FAIL", "EVIDENCE_NOT_VALID")},
    "result": {"PASS": ("PASS", None), "FAIL": ("FAIL", "EVIDENCE_NOT_VALID"), "WITHHELD": ("REVIEW", "EVIDENCE_PENDING")},
    "registration": {"REGISTERED": ("PASS", None), "PENDING": ("REVIEW", "EVIDENCE_PENDING")},
    "enrolment": {"ACTIVE": ("PASS", None), "PENDING": ("REVIEW", "EVIDENCE_PENDING"),
                  "INACTIVE": ("FAIL", "EVIDENCE_NOT_VALID")},
}
# A status that is not listed is unrecognised and goes to review; it is never counted as a failed check.

REASONS = {
    "EVIDENCE_EXPIRED": "{label} has expired.",
    "EVIDENCE_NOT_VALID": "{label} is not valid.",
    "VALUE_ABOVE_LIMIT": "{label} is above the limit for this scheme.",
    "VALUE_BELOW_MINIMUM": "{label} is below the minimum for this scheme.",
    "VALUE_NOT_ACCEPTED": "{label} does not meet what this scheme requires.",
    "EVIDENCE_MISSING": "No {noun} was found at the {dept}.",
    "EVIDENCE_NOT_PROVIDED": "{dept} records were not checked.",
    "PROVIDER_UNAVAILABLE": "The {dept} could not be reached. Please try again later.",
    "EVIDENCE_PENDING": "{label} is still pending.",
    "EVIDENCE_INVALID": "The {noun} received could not be used.",
    "EVIDENCE_UNRECOGNISED": "The {noun} has a status that needs a manual check.",
    "IDENTITY_MISMATCH": "The {dept} record did not match your student ID, so it needs a manual check.",
    "RULE_UNSUPPORTED": "A requirement about {noun} could not be applied.",
    "NO_AUTHORIZATION": "No department was authorized, so nothing could be verified.",
    "RULES_NOT_CONFIGURED": "No approved rules are available for this scheme yet.",
    "REQUEST_CANCELLED": "The request was cancelled before it was completed.",
}
PASS_MESSAGES = {
    "LESS_THAN_OR_EQUAL": "{label} is within the limit.", "LESS_THAN": "{label} is within the limit.",
    "GREATER_THAN_OR_EQUAL": "{label} meets the minimum.", "GREATER_THAN": "{label} meets the minimum.",
    "EQUALS": "{label} matches the requirement.", "IN": "{label} matches the requirement.",
    "NOT_EQUALS": "{label} is acceptable.", "NOT_IN": "{label} is acceptable.",
    "IS_VALID": "{label} is valid.", "IS_PRESENT": "{label} is available.",
}
FAIL_BY_OPERATOR = {
    "LESS_THAN_OR_EQUAL": "VALUE_ABOVE_LIMIT", "LESS_THAN": "VALUE_ABOVE_LIMIT",
    "GREATER_THAN_OR_EQUAL": "VALUE_BELOW_MINIMUM", "GREATER_THAN": "VALUE_BELOW_MINIMUM",
}

TRANSACTION_REASON_PROVIDER = "mahasetu"


def reason_message(code, *, label="", noun="", dept="", **_):
    return REASONS[code].format(label=label, noun=noun, dept=dept)[:240]


def pass_message(operator, label):
    return PASS_MESSAGES[operator].format(label=label)[:240]
