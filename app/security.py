"""Authentication and authorization.

Two principals exist:

* **end users** – identified either by the ``X-User-Id`` header (``AUTH_MODE=dev``,
  local development only) or by a signed Bearer token (``AUTH_MODE=jwt``).
* **administrators** – the back office logs in with a username/password and
  receives a short-lived signed token carrying the ``admin`` role.

Tokens are compact HS256 JWTs implemented with the standard library so the
project has no extra crypto dependency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Depends, Header, HTTPException, status

from .config import get_settings

_ITERATIONS = 120_000


# --------------------------------------------------------------------------- #
# Password hashing (PBKDF2-HMAC-SHA256, standard library only)
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Compact JWT (HS256)
# --------------------------------------------------------------------------- #
def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(subject: str, *, roles: list[str] | None = None, ttl_minutes: int | None = None) -> str:
    settings = get_settings()
    ttl = ttl_minutes if ttl_minutes is not None else settings.jwt_expire_minutes
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": subject,
        "roles": roles or [],
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + ttl * 60,
    }
    signing_input = f"{_b64url_encode(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64url_encode(json.dumps(payload, separators=(',', ':')).encode())}"
    signature = hmac.new(settings.jwt_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        expected = hmac.new(
            settings.jwt_secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(signature_b64)):
            raise ValueError("bad signature")
        if json.loads(_b64url_decode(header_b64)).get("alg") != "HS256":
            raise ValueError("unexpected algorithm")
        payload = json.loads(_b64url_decode(payload_b64))
        if payload.get("iss") != settings.jwt_issuer:
            raise ValueError("bad issuer")
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("token expired")
        return payload
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - normalized to a single 401
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired token") from exc


# --------------------------------------------------------------------------- #
# Principals
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Principal:
    user_id: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def has_role(self, role: str) -> bool:
        return role in self.roles


def _roles_from_claim(value) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple, set)):
        return frozenset(str(part).strip() for part in value if str(part).strip())
    return frozenset()


def current_principal(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> Principal:
    """Resolve the calling end user."""
    settings = get_settings()

    if settings.auth_mode == "dev":
        return Principal(user_id=(x_user_id or "user001").strip())

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    payload = decode_token(authorization.split(" ", 1)[1].strip())
    subject = payload.get("sub")
    if not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token missing subject")
    return Principal(user_id=str(subject), roles=_roles_from_claim(payload.get("roles")))


def current_admin(
    authorization: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> Principal:
    """Resolve the calling administrator."""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    elif x_admin_token:
        token = x_admin_token.strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="administrator token required")

    payload = decode_token(token)
    principal = Principal(user_id=str(payload.get("sub") or ""), roles=_roles_from_claim(payload.get("roles")))
    if not principal.has_role("admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="administrator role required")
    return principal


def authenticate_admin(username: str, password: str) -> bool:
    settings = get_settings()
    user_ok = hmac.compare_digest(username.strip(), settings.admin_username)
    pass_ok = hmac.compare_digest(password, settings.admin_password)
    return user_ok and pass_ok


AdminDep = Depends(current_admin)
UserDep = Depends(current_principal)
