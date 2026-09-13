"""Who is asking: ``Identity(email, evaluator)`` from Google IAP headers, anonymous otherwise.

Contract (interfaces §8, brief §2.5)
- ``X-Goog-Authenticated-User-Email`` (``accounts.google.com:<email>``) is trusted only when the accompanying
  ``X-Goog-IAP-JWT-Assertion`` verifies with ``google.oauth2.id_token.verify_token`` against IAP's public keys
  (``certs_url="https://www.gstatic.com/iap/verify/public_key"``) for ``audience = settings.iap_audience`` and its
  ``email`` claim equals the header's email. Without both headers the identity is anonymous and ``evaluator=False``.
- ``evaluator = email in Settings.evaluators`` (lower-cased). ``?evaluator=1`` does not exist anywhere.
- No audience configured means no header can be verified: identity stays anonymous (fail closed), and the reason is
  kept on the identity for the operator to see.
- Verification failures never raise into a route; they produce an anonymous identity with ``reason``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from swesim.settings import Settings

EMAIL_HEADER = "x-goog-authenticated-user-email"
JWT_HEADER = "x-goog-iap-jwt-assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"


@dataclass(frozen=True)
class Identity:
    email: str | None
    evaluator: bool
    reason: str = ""  # why the identity is anonymous when headers were present

    @property
    def anonymous(self) -> bool:
        return self.email is None

    @property
    def actor(self) -> str:
        return self.email or "anonymous"


ANONYMOUS = Identity(None, False)


def _default_verify(token: str, audience: str) -> Mapping:
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token
    claims = id_token.verify_token(token, g_requests.Request(), audience=audience, certs_url=IAP_CERTS_URL)
    if claims.get("iss") != "https://cloud.google.com/iap":
        raise ValueError("JWT issuer is not IAP")
    return claims


def _audience(settings: Settings) -> str | None:
    # Settings.iap_audience is added on main by the integrator; the getattr shim covers the window before the merge.
    return getattr(settings, "iap_audience", None) or None


def identity_from_headers(headers: Mapping[str, str], settings: Settings, verify: Callable[[str, str], Mapping] | None = None) -> Identity:
    """Identity for one request. ``verify(token, audience) -> claims`` is injectable for tests."""
    lower = {str(k).lower(): v for k, v in headers.items()}
    raw_email, token = lower.get(EMAIL_HEADER), lower.get(JWT_HEADER)
    if not raw_email or not token:
        return ANONYMOUS
    audience = _audience(settings)
    if not audience:
        return Identity(None, False, "IAP headers present but SWESIM_IAP_AUDIENCE is not configured; identity not trusted")
    email = raw_email.split(":", 1)[-1].strip().lower()
    try:
        claims = (verify or _default_verify)(token, audience)
    except Exception as e:  # noqa: BLE001 - google.auth raises ValueError subclasses and transport errors; all mean "not verified"
        return Identity(None, False, f"IAP JWT did not verify: {type(e).__name__}")
    claim_email = str(claims.get("email") or "").strip().lower()
    if not claim_email or claim_email != email:
        return Identity(None, False, "IAP JWT email claim does not match the identity header")
    return Identity(email, email in settings.evaluators)
