"""Environment-driven settings. Secrets come only from the environment, never from files in the repository."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts" / "v1"
MIN_SECRET_LENGTH = 24


def _int(name, default, low, high):
    raw = os.environ.get(name)
    value = default if raw in (None, "") else int(raw)
    if not low <= value <= high:
        raise RuntimeError(f"{name} must be between {low} and {high}")
    return value


def _secret(name, required=True):
    value = os.environ.get(name, "")
    if not value:
        if required:
            raise RuntimeError(f"Missing required setting: {name}")
        return ""
    if len(value) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"{name} must be at least {MIN_SECRET_LENGTH} characters")
    return value


def requester_env_name(credential_ref):
    """secret-ref/scholarship -> REQUESTER_TOKEN_SCHOLARSHIP."""
    tail = credential_ref.rsplit("/", 1)[-1]
    return "REQUESTER_TOKEN_" + re.sub(r"[^A-Za-z0-9]+", "_", tail).strip("_").upper()


@dataclass(frozen=True)
class Settings:
    database_url: str
    dashboard_token: str
    otp_hash_key: str
    environment: str = "development"
    demo_otp_disclosure: bool = False
    revenue_user: str = ""
    revenue_password: str = ""
    examination_api_key: str = ""
    university_client_id: str = ""
    university_client_secret: str = ""
    launch_ttl_seconds: int = 1800
    session_ttl_seconds: int = 3600
    authorization_ttl_seconds: int = 1800
    otp_ttl_seconds: int = 300
    otp_resend_seconds: int = 30
    otp_max_attempts: int = 3
    otp_max_challenges: int = 5
    provider_timeout_seconds: float = 5.0
    provider_attempts: int = 3
    provider_backoff_seconds: float = 0.3
    retrieval_stale_seconds: int = 90
    provider_urls: dict = field(default_factory=dict)  # optional overrides keyed by provider id

    @classmethod
    def from_env(cls):
        environment = os.environ.get("CORE_ENVIRONMENT", "development").lower()
        demo = os.environ.get("OTP_DEMO_DISCLOSURE", "disabled").lower() == "enabled"
        if demo and environment == "production":
            raise RuntimeError("OTP_DEMO_DISCLOSURE must not be enabled when CORE_ENVIRONMENT=production")
        urls = {}
        for provider in ("revenue", "examination", "university"):
            value = os.environ.get(provider.upper() + "_URL")
            if value:
                urls[provider] = value.rstrip("/")
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            raise RuntimeError("Missing required setting: DATABASE_URL")
        return cls(
            database_url=database_url,
            dashboard_token=_secret("DASHBOARD_CLIENT_TOKEN"),
            otp_hash_key=_secret("OTP_HASH_KEY"),
            environment=environment,
            demo_otp_disclosure=demo,
            revenue_user=os.environ.get("REVENUE_USER", ""),
            revenue_password=os.environ.get("REVENUE_PASSWORD", ""),
            examination_api_key=os.environ.get("EXAMINATION_API_KEY", ""),
            university_client_id=os.environ.get("UNIVERSITY_CLIENT_ID", ""),
            university_client_secret=os.environ.get("UNIVERSITY_CLIENT_SECRET", ""),
            launch_ttl_seconds=_int("LAUNCH_TTL_SECONDS", 1800, 60, 86400),
            session_ttl_seconds=_int("SESSION_TTL_SECONDS", 3600, 60, 86400),
            authorization_ttl_seconds=_int("AUTHORIZATION_TTL_SECONDS", 1800, 60, 86400),
            otp_ttl_seconds=_int("OTP_TTL_SECONDS", 300, 30, 900),
            otp_resend_seconds=_int("OTP_RESEND_SECONDS", 30, 0, 600),
            provider_timeout_seconds=float(os.environ.get("PROVIDER_TIMEOUT_SECONDS") or 5),
            provider_attempts=_int("PROVIDER_ATTEMPTS", 3, 1, 5),
            provider_urls=urls,
        )

    def requester_token(self, credential_ref):
        """Token for a registered requester, or empty when it is not configured on this host."""
        return _secret(requester_env_name(credential_ref), required=False)
