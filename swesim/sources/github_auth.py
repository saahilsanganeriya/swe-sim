"""GitHub credentials for repository discovery: several quota buckets behind one `TokenPool`.

GitHub's core REST quota is 5,000 calls per hour *per identity*. A personal access token is one identity; a GitHub App
installation is another with its own 5,000/h; more personal tokens are more buckets. The pool holds every configured
source, keeps a per-source view of the quota (`remaining`, the window's `reset_at`, and any pause a `RateLimited` refusal
named) and hands out the source with the most calls left; when every source is out it names the one that frees up
soonest and how long that is. `swesim.sources.threads` owns the gh calls and drives the pool; nothing here calls gh.

Sources
- `PatSource(name)`: a personal token read on demand through `swesim.settings.secret` (`GH_TOKEN`, `GH_TOKEN_2`, ...).
- `AppSource(app_id, installation_id, private_key)`: an installation token minted from an RS256 JWT (`iss` = app id,
  `iat` 60 s back, `exp` 9 min ahead) signed with `google.auth.crypt.RSASigner`, cached and refreshed
  `REFRESH_MARGIN_SEC` before it expires; thread-safe. The private key is `GH_APP_PRIVATE_KEY_PATH` (a file, laptop)
  or the `GH_APP_PRIVATE_KEY` secret (its content, hosted pools).
- `StaticSource(name, token)` for callers that already hold a token (tests, one-off CLI runs); `AmbientSource` when
  nothing is configured: gh's own credential store, so a laptop with `gh auth login` keeps working.

No token, JWT or key ever appears in a repr, a log line or an exception message; `TokenError.kind` (auth | network |
server-error) is all a caller learns about a mint that failed.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from swesim.settings import MissingSecret, Settings, secret

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 5000  # GitHub's core quota per identity per hour; the estimate for a source not yet measured
DEFAULT_WINDOW_SEC = 3600.0
APP_TOKEN_URL = "https://api.github.com/app/installations/{installation_id}/access_tokens"
JWT_BACKDATE_SEC = 60  # GitHub tolerates iat up to 60 s in the past (clock drift)
JWT_LIFETIME_SEC = 540  # exp at most 10 min ahead; 9 keeps a margin
REFRESH_MARGIN_SEC = 300.0  # mint a new installation token this long before the current one expires
MINT_TIMEOUT_SEC = 30.0

Poster = Callable[[str, dict[str, str]], dict]


class TokenError(RuntimeError):
    """A source could not produce a token. `kind` is auth (GitHub refused the mint or the response was malformed),
    network (no answer) or server-error (5xx). The message never carries a JWT, a token or key material."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


class TokenSource(Protocol):
    name: str

    def token(self) -> str | None:
        """The credential to put in the subprocess's `GH_TOKEN`; None means gh's own credential store."""


class StaticSource:
    def __init__(self, name: str, token: str):
        self.name, self._token = name, token

    def token(self) -> str:
        return self._token

    def __repr__(self) -> str:
        return f"StaticSource({self.name!r})"


class AmbientSource:
    """gh's own credential (`gh auth login`); the only source when no GH_TOKEN and no App are configured."""

    name = "gh"

    def token(self) -> None:
        return None

    def __repr__(self) -> str:
        return "AmbientSource()"


class PatSource:
    """A personal access token, read on demand (never held): `GH_TOKEN`, `GH_TOKEN_2`, ... via `settings.secret`."""

    def __init__(self, name: str, settings: Settings | None = None, environ: Mapping[str, str] | None = None):
        self.name, self._settings, self._environ = name, settings, environ

    def token(self) -> str:
        return secret(self.name, self._settings, self._environ)

    def __repr__(self) -> str:
        return f"PatSource({self.name!r})"


def post_json(url: str, headers: dict[str, str]) -> dict:
    """POST with no body, expecting GitHub's 201 + JSON; every other outcome is a `TokenError` naming only the status."""
    import httpx

    try:
        response = httpx.post(url, headers=headers, timeout=MINT_TIMEOUT_SEC)
    except httpx.HTTPError as error:
        raise TokenError("network", f"installation token request failed: {type(error).__name__}") from error
    if response.status_code != 201:
        kind = "auth" if response.status_code in (401, 403, 404) else "server-error" if response.status_code >= 500 else "network"
        raise TokenError(kind, f"installation token request refused: HTTP {response.status_code}")
    return response.json()


def _parse_expiry(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


class AppSource:
    """A GitHub App installation: mints `ghs_` tokens (valid one hour) from an RS256 JWT and refreshes them early."""

    def __init__(self, app_id: int, installation_id: int, private_key: bytes, *, post: Poster | None = None,
                 clock: Callable[[], float] = time.time):
        from google.auth import crypt

        self.app_id, self.installation_id = int(app_id), int(installation_id)
        self.name = f"app:{self.installation_id}"
        self._signer = crypt.RSASigner.from_string(private_key)
        self._post = post or post_json
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def __repr__(self) -> str:
        return f"AppSource(app_id={self.app_id}, installation_id={self.installation_id})"

    @property
    def expires_at(self) -> float:
        """Epoch second the cached installation token expires (0.0 before the first mint)."""
        return self._expires_at

    def jwt(self, now: float | None = None) -> str:
        """The App's RS256 JWT for GitHub's App endpoints: `iss` = app id, `iat` backdated, `exp` nine minutes ahead."""
        from google.auth import jwt as google_jwt

        at = int(self._clock() if now is None else now)
        payload = {"iat": at - JWT_BACKDATE_SEC, "exp": at + JWT_LIFETIME_SEC, "iss": str(self.app_id)}
        return google_jwt.encode(self._signer, payload).decode()

    def token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token is None or now >= self._expires_at - REFRESH_MARGIN_SEC:
                self._mint(now)
            assert self._token is not None
            return self._token

    def _mint(self, now: float) -> None:
        headers = {"Authorization": f"Bearer {self.jwt(now)}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        document = self._post(APP_TOKEN_URL.format(installation_id=self.installation_id), headers)
        try:
            token, expires_at = document["token"], _parse_expiry(document["expires_at"])
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise TokenError("auth", f"installation token response for {self.name} lacks token/expires_at") from error
        if not isinstance(token, str) or not token:
            raise TokenError("auth", f"installation token response for {self.name} carries no token")
        self._token, self._expires_at = token, expires_at
        log.info("github-auth: minted an installation token for %s; it expires in %.0fs", self.name, expires_at - now)


# ---------------------------------------------------------------- the pool

@dataclass
class TokenState:
    """One source's quota as last seen. `remaining`/`reset_at` come from `rate_limit` readings (then fall by one per call
    until the next reading); `blocked_until` is a pause a refusal named or a set-aside; None means unknown."""

    source: TokenSource
    remaining: int | None = None
    limit: int | None = None
    reset_at: float | None = None
    blocked_until: float | None = None
    secondary: bool = False
    checked_at: float | None = None

    def refresh(self, now: float) -> None:
        if self.blocked_until is not None and now >= self.blocked_until:
            self.blocked_until, self.secondary = None, False
        if self.reset_at is not None and now >= self.reset_at:  # a new window: the quota is full again, exact figure unknown
            self.remaining, self.reset_at, self.checked_at = None, None, None

    def available(self, now: float) -> bool:
        self.refresh(now)
        return self.blocked_until is None and self.remaining != 0

    def estimate(self) -> int:
        return self.remaining if self.remaining is not None else (self.limit or DEFAULT_LIMIT)

    def horizon(self, now: float) -> float:
        """The epoch second this source can be used again (`now` when it can be used now)."""
        if self.available(now):
            return now
        horizons = [t for t in (self.blocked_until, self.reset_at if self.remaining == 0 else None) if t is not None]
        return max(horizons) if horizons else now + DEFAULT_WINDOW_SEC


@dataclass(frozen=True)
class Selection:
    """The pool's answer: `name` of the source to use (or, when `wait` > 0, the one that frees up soonest), the
    `wait` in seconds until it is usable, the epoch second `reset_at` that happens, and what is known of its quota."""

    name: str
    wait: float
    reset_at: float
    remaining: int | None
    secondary: bool

    @property
    def available(self) -> bool:
        return self.wait <= 0


class TokenPool:
    """Every configured source with its quota bookkeeping; thread-safe. `select` never produces a token (that is
    `token(name)`, which may mint over the network); a `TokenError` there is the caller's cue for `set_aside`."""

    def __init__(self, sources: Sequence[TokenSource]):
        names = [s.name for s in sources]
        if not names:
            raise ValueError("a TokenPool needs at least one source")
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate token source names: {names}")
        self._states = {s.name: TokenState(s) for s in sources}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._states)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._states)

    def source(self, name: str) -> TokenSource:
        return self._states[name].source

    def token(self, name: str) -> str | None:
        return self._states[name].source.token()

    def select(self, now: float) -> Selection:
        with self._lock:
            live = [s for s in self._states.values() if s.available(now)]
            if live:
                best = max(live, key=lambda s: s.estimate())
                return Selection(best.source.name, 0.0, now, best.remaining, False)
            soonest = min(self._states.values(), key=lambda s: s.horizon(now))
            horizon = soonest.horizon(now)
            return Selection(soonest.source.name, max(0.0, horizon - now), horizon, soonest.remaining, soonest.secondary)

    def record_status(self, name: str, remaining: int, limit: int, reset_at: float, now: float) -> None:
        """A `rate_limit` reading for one source (GitHub does not count that call)."""
        with self._lock:
            state = self._states[name]
            state.remaining, state.limit, state.reset_at, state.checked_at = int(remaining), int(limit), float(reset_at), now
            if state.remaining > 0 and not state.secondary:
                state.blocked_until = None  # a measured quota beats an inferred block

    def record_call(self, name: str) -> None:
        """One counted call went out under `name`: the estimate falls until the next reading."""
        with self._lock:
            state = self._states[name]
            if state.remaining is not None and state.remaining > 0:
                state.remaining -= 1

    def record_limited(self, name: str, reset_at: float, *, remaining: int | None = None, secondary: bool = False) -> None:
        """GitHub refused a call under `name`: the source is out until `reset_at` (the quota's reset, or the pause a
        secondary limit named)."""
        with self._lock:
            state = self._states[name]
            state.blocked_until = max(float(reset_at), state.blocked_until or 0.0)
            state.secondary = secondary
            if remaining is not None:
                state.remaining = int(remaining)
            if not secondary:
                state.reset_at = max(float(reset_at), state.reset_at or 0.0)

    def set_aside(self, name: str, until: float) -> None:
        """Skip `name` until `until` (its token could not be produced)."""
        with self._lock:
            self._states[name].blocked_until = max(float(until), self._states[name].blocked_until or 0.0)

    def stale(self, name: str, now: float, *, ttl: float) -> bool:
        """True when `name` has never been measured in its current window or its reading is older than `ttl`."""
        with self._lock:
            state = self._states[name]
            state.refresh(now)
            return state.checked_at is None or not (0 <= now - state.checked_at < ttl)

    def reading(self, name: str, now: float) -> tuple[int, int, float]:
        """(remaining, limit, reset_at) of one source as last seen (estimates when unmeasured)."""
        with self._lock:
            state = self._states[name]
            state.refresh(now)
            return state.estimate(), state.limit or DEFAULT_LIMIT, state.reset_at if state.reset_at is not None else now + DEFAULT_WINDOW_SEC

    def status(self, now: float) -> tuple[int, int, float]:
        """(remaining, limit, reset_at) of the best available source; (0, limit, soonest reset) when none is."""
        selection = self.select(now)
        with self._lock:
            state = self._states[selection.name]
            limit = state.limit or DEFAULT_LIMIT
            if selection.available:
                return state.estimate(), limit, state.reset_at if state.reset_at is not None else now + DEFAULT_WINDOW_SEC
            return 0, limit, selection.reset_at

    def summary(self, now: float) -> list[dict]:
        """Per-source quota view for logs and heartbeats; never a token."""
        with self._lock:
            out = []
            for state in self._states.values():
                state.refresh(now)
                out.append({"name": state.source.name, "remaining": state.remaining, "limit": state.limit, "reset_at": state.reset_at,
                            "blocked_until": state.blocked_until, "secondary": state.secondary, "available": state.available(now)})
            return out


# ---------------------------------------------------------------- from settings

def pat_names(extra: int) -> tuple[str, ...]:
    """`GH_TOKEN` plus `GH_TOKEN_2` .. `GH_TOKEN_{extra + 1}`."""
    return ("GH_TOKEN",) + tuple(f"GH_TOKEN_{n}" for n in range(2, extra + 2))


def private_key_from_settings(settings: Settings, environ: Mapping[str, str] | None = None) -> bytes:
    """The App's private key: the file `GH_APP_PRIVATE_KEY_PATH` names when it exists, else the `GH_APP_PRIVATE_KEY`
    secret's content (a PEM pasted as one line with escaped newlines is unescaped). `MissingSecret` names both places."""
    path = settings.gh_app_private_key_path
    if path is not None and path.is_file():
        return path.read_bytes()
    try:
        content = secret("GH_APP_PRIVATE_KEY", settings, environ)
    except MissingSecret:
        where = f"GH_APP_PRIVATE_KEY_PATH={path} is not a file and" if path is not None else "neither GH_APP_PRIVATE_KEY_PATH nor"
        raise MissingSecret(f"GH_APP_ID is set but {where} the GH_APP_PRIVATE_KEY secret is absent") from None
    if "\n" not in content and "\\n" in content:
        content = content.replace("\\n", "\n")
    return content.encode()


def pool_from_settings(settings: Settings, environ: Mapping[str, str] | None = None) -> TokenPool:
    """Every configured bucket: each present `GH_TOKEN[_n]` (n up to `gh_extra_tokens` + 1) and the App when
    `GH_APP_ID`/`GH_APP_INSTALLATION_ID` are set (a missing key is an error, not a silently smaller pool); gh's own
    credential when nothing is configured."""
    sources: list[TokenSource] = []
    for name in pat_names(settings.gh_extra_tokens):
        try:
            secret(name, settings, environ)
        except MissingSecret:
            continue
        sources.append(PatSource(name, settings, environ))
    if settings.gh_app_id is not None or settings.gh_app_installation_id is not None:
        if settings.gh_app_id is None or settings.gh_app_installation_id is None:
            raise ValueError("GH_APP_ID and GH_APP_INSTALLATION_ID must be set together")
        sources.append(AppSource(settings.gh_app_id, settings.gh_app_installation_id, private_key_from_settings(settings, environ)))
    if not sources:
        log.info("github-auth: no GH_TOKEN or GitHub App configured; gh uses its own credential store")
        sources.append(AmbientSource())
    log.info("github-auth: %d GitHub quota bucket(s): %s", len(sources), ", ".join(s.name for s in sources))
    return TokenPool(sources)
