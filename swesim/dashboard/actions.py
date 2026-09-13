"""Operator actions: two-step confirmation tokens, the strict Origin check and the audit row.

Contract (brief §2.5, Codex B-P2-7; interfaces §8)
- ``preview(action, params, identity)`` canonicalises the params, renders the exact CLI equivalent and returns a signed
  token bound to (action, params hash, actor, expiry). ``confirm(action, token, params, identity)`` verifies signature,
  expiry (``TOKEN_TTL_S``), action, actor and that the params are byte-identical to the previewed ones; any mismatch is a
  ``TokenError`` (non-retryable, 4xx).
- Signing key: ``SWESIM_WORKER_SECRET`` via ``swesim.settings.secret`` when configured (stable across instances), else a
  per-process random key (tokens then survive only this process; the preview says so).
- ``check_origin(headers, settings)`` compares the request ``Origin`` scheme+host+port with ``Settings.console_url`` when
  set, else with the request's own origin (``X-Forwarded-Proto``/``Host``). No ``Origin`` header (curl, tests) passes;
  ``null`` and mismatches are ``OriginError``.
- ``audit_row(...)`` builds the ``OperatorActionRow`` (ULID id, actor, action, target, params, confirmed, origin header,
  result, workflow_id, at) that every attempt — accepted, rejected or failed — is written with.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
import time
from datetime import UTC, datetime
from typing import Mapping
from urllib.parse import urlsplit

from swesim.dashboard import rows as R
from swesim.dashboard.identity import Identity
from swesim.settings import MissingSecret, Settings, secret

TOKEN_TTL_S = 600
ACTIONS = ("create", "run", "cancel", "review", "label", "retry", "rebuild")
_PROCESS_KEY = _secrets.token_bytes(32)


class TokenError(ValueError):
    """The confirmation token is missing, expired, forged or bound to different params/actor/action."""


class OriginError(ValueError):
    """Cross-site request: Origin scheme+host+port differs from the console's."""


def canonical(params: Mapping) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)


def params_hash(params: Mapping) -> str:
    return hashlib.sha256(canonical(params).encode()).hexdigest()


def signing_key(settings: Settings) -> tuple[bytes, bool]:
    """(key, shared): shared=True when the key comes from SWESIM_WORKER_SECRET (valid across console instances)."""
    try:
        return secret("SWESIM_WORKER_SECRET", settings).encode(), True
    except MissingSecret:
        return _PROCESS_KEY, False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(action: str, params: Mapping, identity: Identity, settings: Settings, now: float | None = None) -> tuple[str, datetime]:
    if action not in ACTIONS:
        raise TokenError(f"unknown action {action!r}")
    exp = int((time.time() if now is None else now) + TOKEN_TTL_S)
    payload = {"a": action, "p": params_hash(params), "u": identity.actor, "e": exp, "n": _secrets.token_hex(8)}
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    key, _shared = signing_key(settings)
    sig = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}", datetime.fromtimestamp(exp, UTC)


def verify_token(token: str, action: str, params: Mapping, identity: Identity, settings: Settings, now: float | None = None) -> None:
    """Raises TokenError unless the token was issued for exactly this action, these params and this actor and is unexpired."""
    if not isinstance(token, str) or token.count(".") != 1:
        raise TokenError("confirmation token missing or malformed")
    body, sig = token.split(".", 1)
    key, _shared = signing_key(settings)
    expect = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        raise TokenError("confirmation token signature invalid (issued by another process or tampered)")
    try:
        payload = json.loads(_unb64(body))
    except ValueError as e:
        raise TokenError("confirmation token payload unreadable") from e
    if payload.get("a") != action:
        raise TokenError("confirmation token was issued for a different action")
    if payload.get("u") != identity.actor:
        raise TokenError("confirmation token was issued to a different identity")
    if payload.get("p") != params_hash(params):
        raise TokenError("parameters changed since the preview; preview again")
    if int(payload.get("e", 0)) < (time.time() if now is None else now):
        raise TokenError("confirmation token expired; preview again")


# ----------------------------------------------------------------------------- origin

def _normalise(scheme: str, host: str, port: str | int | None) -> tuple[str, str, int]:
    scheme = (scheme or "").lower()
    host = (host or "").lower().strip("[]")
    p = int(port) if port not in (None, "") else (443 if scheme == "https" else 80)
    return scheme, host, p


def expected_origin(headers: Mapping[str, str], settings: Settings, request_url: str | None = None) -> tuple[str, str, int] | None:
    lower = {str(k).lower(): v for k, v in headers.items()}
    if settings.console_url:
        u = urlsplit(settings.console_url)
        if u.scheme and u.hostname:
            return _normalise(u.scheme, u.hostname, u.port)
    host = lower.get("x-forwarded-host") or lower.get("host")
    scheme = lower.get("x-forwarded-proto") or (urlsplit(request_url).scheme if request_url else "http")
    if not host:
        return None
    u = urlsplit(f"{scheme}://{host}")
    if not u.hostname:
        return None
    return _normalise(scheme, u.hostname, u.port)


def check_origin(headers: Mapping[str, str], settings: Settings, request_url: str | None = None) -> None:
    """Strict same-origin: scheme, host and port must all match. Absent Origin passes (non-browser clients)."""
    lower = {str(k).lower(): v for k, v in headers.items()}
    origin = lower.get("origin")
    if not origin:
        return
    if origin == "null":
        raise OriginError("cross-site request rejected (null origin)")
    try:
        u = urlsplit(origin)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:
            raise ValueError("not an origin")
        got = _normalise(u.scheme, u.hostname, u.port)
        want = expected_origin(headers, settings, request_url)
    except ValueError as e:
        raise OriginError("cross-site request rejected (origin is not valid)") from e
    if want is None or got != want:
        raise OriginError("cross-site request rejected (origin scheme/host/port does not match the console)")


# ----------------------------------------------------------------------------- audit

def _ulid() -> str:
    try:
        from ulid import ULID
        return str(ULID())
    except ImportError:  # python-ulid is a project dependency; keep the module importable without it
        return _secrets.token_hex(13)


def audit_row(*, identity: Identity, action: str, target: str | None, params: Mapping, confirmed: bool, origin_header: str | None,
              result: str, workflow_id: str | None, settings: Settings) -> R.OperatorActionRow:
    now = datetime.now(UTC)
    return R.OperatorActionRow(action_id=_ulid(), actor=identity.actor, action=action, target=target, params=dict(params), confirmed=confirmed,
                               origin_header=origin_header, result=result, workflow_id=workflow_id, at=now, created_at=now, updated_at=now,
                               run_id=None, worker_image_digest=settings.worker_image_digest, origin=settings.origin)
