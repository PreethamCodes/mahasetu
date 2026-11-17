"""Opaque tokens, hashing and constant-time credential checks."""
import hashlib
import hmac
import secrets


def new_token(nbytes=32):
    """URL-safe, unguessable reference (43 characters for 32 bytes)."""
    return secrets.token_urlsafe(nbytes)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def same(left, right):
    return hmac.compare_digest(left.encode(), right.encode())


def otp_code():
    return f"{secrets.randbelow(1_000_000):06d}"


def otp_hash(key, challenge_id, code):
    return hmac.new(key.encode(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()


def bearer(header):
    scheme, _, token = (header or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else ""


def mask_mobile(mobile):
    """9000000001 -> 90•••••001 (matches the Mobile display pattern in the contract)."""
    return f"{mobile[:2]}{chr(0x2022) * 5}{mobile[-3:]}"
