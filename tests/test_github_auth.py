"""GitHub credentials for discovery (`swesim.sources.github_auth`): a GitHub App installation token minted from an RS256
JWT and refreshed before it expires, personal tokens read through `settings.secret`, and a `TokenPool` that hands out the
token with the most quota left and names the soonest reset when every bucket is empty. No network: the mint is a fake
callable, the RSA key is generated here, and every clock is a number."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import jwt as google_jwt

from swesim import settings as S
from swesim.sources import github_auth as GA

APP_ID, INSTALLATION_ID = 1234567, 987654321


@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private_pem, public_pem


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeMint:
    """The installation-token endpoint: records every request, answers a fresh token valid for `lifetime` seconds."""

    def __init__(self, clock, lifetime: float = 3600.0, delay: float = 0.0):
        self.clock, self.lifetime, self.delay, self.calls = clock, lifetime, delay, []

    def __call__(self, url: str, headers: dict) -> dict:
        self.calls.append((url, dict(headers)))
        if self.delay:
            time.sleep(self.delay)
        return {"token": f"ghs_fake_{len(self.calls)}", "expires_at": _iso(self.clock() + self.lifetime)}


# ---------------------------------------------------------------- AppSource: JWT shape, minting, refresh, thread safety, no leaks

def test_app_jwt_is_rs256_signed_by_the_private_key_with_github_s_claims(rsa_key):
    private_pem, public_pem = rsa_key
    now = time.time()
    source = GA.AppSource(APP_ID, INSTALLATION_ID, private_pem, post=FakeMint(lambda: now), clock=lambda: now)
    token = source.jwt()
    assert google_jwt.decode_header(token) == {"typ": "JWT", "alg": "RS256"}
    payload = google_jwt.decode(token, certs=public_pem, verify=True)  # the signature verifies against the key's public half
    assert payload == {"iss": str(APP_ID), "iat": int(now) - GA.JWT_BACKDATE_SEC, "exp": int(now) + GA.JWT_LIFETIME_SEC}
    assert GA.JWT_BACKDATE_SEC == 60 and GA.JWT_LIFETIME_SEC == 540  # GitHub: iat up to 60 s in the past, exp at most 10 min ahead


def test_app_source_mints_once_and_refreshes_five_minutes_before_expiry(rsa_key):
    private_pem, _ = rsa_key
    clock = {"now": 1_800_000_000.0}
    mint = FakeMint(lambda: clock["now"], lifetime=3600.0)
    source = GA.AppSource(APP_ID, INSTALLATION_ID, private_pem, post=mint, clock=lambda: clock["now"])
    assert source.name == f"app:{INSTALLATION_ID}"
    assert source.token() == "ghs_fake_1" and source.token() == "ghs_fake_1" and len(mint.calls) == 1  # cached while valid
    url, headers = mint.calls[0]
    assert url == f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    assert headers["Accept"] == "application/vnd.github+json" and headers["X-GitHub-Api-Version"] and headers["Authorization"].startswith("Bearer ey")
    assert source.expires_at == clock["now"] + 3600
    clock["now"] += 3600 - GA.REFRESH_MARGIN_SEC - 1
    assert source.token() == "ghs_fake_1" and len(mint.calls) == 1  # more than five minutes left: still cached
    clock["now"] += 1
    assert source.token() == "ghs_fake_2" and len(mint.calls) == 2  # five minutes before expiry: minted again
    assert GA.REFRESH_MARGIN_SEC == 300.0


def test_concurrent_callers_share_one_mint(rsa_key):
    private_pem, _ = rsa_key
    now = 1_800_000_000.0
    mint = FakeMint(lambda: now, delay=0.02)
    source = GA.AppSource(APP_ID, INSTALLATION_ID, private_pem, post=mint, clock=lambda: now)
    seen = []
    workers = [threading.Thread(target=lambda: seen.append(source.token())) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert seen == ["ghs_fake_1"] * 8 and len(mint.calls) == 1


def test_app_source_never_exposes_key_material_or_tokens(rsa_key):
    private_pem, _ = rsa_key
    now = 1_800_000_000.0
    source = GA.AppSource(APP_ID, INSTALLATION_ID, private_pem, post=FakeMint(lambda: now), clock=lambda: now)
    source.token()
    text = repr(source) + str(source)
    assert "PRIVATE KEY" not in text and "ghs_fake" not in text and str(INSTALLATION_ID) in text

    def refused(url, headers):
        raise GA.TokenError("auth", "installation token request refused: HTTP 401")
    broken = GA.AppSource(APP_ID, INSTALLATION_ID, private_pem, post=refused, clock=lambda: now)
    with pytest.raises(GA.TokenError) as exc:
        broken.token()
    assert exc.value.kind == "auth" and "Bearer" not in str(exc.value) and "ghs_" not in str(exc.value)
    malformed = GA.AppSource(APP_ID, INSTALLATION_ID, private_pem, post=lambda u, h: {"message": "nope"}, clock=lambda: now)
    with pytest.raises(GA.TokenError) as exc:
        malformed.token()
    assert exc.value.kind == "auth" and "token" in str(exc.value)


def test_post_json_maps_http_outcomes_to_token_error_kinds(monkeypatch):
    import httpx

    class Response:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body
    answers = {}
    monkeypatch.setattr(httpx, "post", lambda url, headers, timeout: answers[url])
    answers["u"] = Response(201, {"token": "ghs_x", "expires_at": "2026-01-01T00:00:00Z"})
    assert GA.post_json("u", {}) == {"token": "ghs_x", "expires_at": "2026-01-01T00:00:00Z"}
    for status, kind in ((401, "auth"), (403, "auth"), (404, "auth"), (500, "server-error"), (429, "network")):
        answers["u"] = Response(status, {"message": "secret-free"})
        with pytest.raises(GA.TokenError) as exc:
            GA.post_json("u", {"Authorization": "Bearer ey.secret.jwt"})
        assert exc.value.kind == kind and str(status) in str(exc.value) and "ey.secret" not in str(exc.value)

    def down(url, headers, timeout):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(GA.TokenError) as exc:
        GA.post_json("u", {})
    assert exc.value.kind == "network"


# ---------------------------------------------------------------- PAT and static sources

def test_pat_source_reads_the_named_secret_on_demand(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GH_TOKEN_2=ghp_from_file\n")
    settings = S.Settings.from_env({"SWESIM_ENV_FILE": str(env_file)})
    assert GA.PatSource("GH_TOKEN_2", settings, environ={}).token() == "ghp_from_file"
    assert GA.PatSource("GH_TOKEN_2", settings, environ={"GH_TOKEN_2": "ghp_from_env"}).token() == "ghp_from_env"
    with pytest.raises(S.MissingSecret):
        GA.PatSource("GH_TOKEN_3", settings, environ={}).token()
    assert "ghp_" not in repr(GA.PatSource("GH_TOKEN_2", settings, environ={}))
    assert GA.StaticSource("x", "tok").token() == "tok" and "tok" not in repr(GA.StaticSource("x", "tok"))
    assert GA.AmbientSource().token() is None and GA.AmbientSource().name == "gh"


# ---------------------------------------------------------------- TokenPool: selection, bookkeeping, exhaustion

def _pool(*names):
    return GA.TokenPool([GA.StaticSource(n, f"tok-{n}") for n in names])


def test_pool_hands_out_the_source_with_the_most_remaining_quota():
    pool, now = _pool("GH_TOKEN", "GH_TOKEN_2", "app:1"), 1000.0
    assert pool.names == ("GH_TOKEN", "GH_TOKEN_2", "app:1") and len(pool) == 3
    assert pool.select(now).name == "GH_TOKEN" and pool.select(now).available  # unknown quotas count as full; ties keep the configured order
    pool.record_status("GH_TOKEN", 100, 5000, now + 1800, now)
    pool.record_status("GH_TOKEN_2", 2500, 5000, now + 1200, now)
    pool.record_status("app:1", 900, 5000, now + 600, now)
    assert pool.select(now).name == "GH_TOKEN_2"
    for _ in range(1601):
        pool.record_call("GH_TOKEN_2")  # every call lowers the estimate until the next measurement
    assert pool.select(now).name == "app:1" and pool.token("app:1") == "tok-app:1"
    assert pool.status(now) == (900, 5000, now + 600)
    pool.record_status("GH_TOKEN_2", 4000, 5000, now + 1200, now + 10)  # a fresh measurement replaces the estimate
    assert pool.select(now + 10).name == "GH_TOKEN_2"
    assert pool.stale("GH_TOKEN_2", now + 39, ttl=30) is False and pool.stale("GH_TOKEN_2", now + 40, ttl=30) is True and pool.stale("GH_TOKEN", now, ttl=30) is False


def test_pool_sets_a_limited_source_aside_until_its_reset_and_names_the_soonest_when_all_are_out():
    pool, now = _pool("a", "b"), 1000.0
    pool.record_status("a", 3000, 5000, now + 900, now)
    pool.record_status("b", 10, 5000, now + 300, now)
    pool.record_limited("a", now + 900, remaining=0)
    sel = pool.select(now)
    assert sel.name == "b" and sel.available and sel.wait == 0
    pool.record_limited("b", now + 120, remaining=10, secondary=True)
    sel = pool.select(now)
    assert not sel.available and sel.name == "b" and sel.wait == 120 and sel.reset_at == now + 120 and sel.secondary and sel.remaining == 10
    assert pool.status(now) == (0, 5000, now + 120)
    summary = pool.summary(now)
    assert [s["name"] for s in summary] == ["a", "b"] and all("tok-" not in str(s) for s in summary)
    assert summary[0]["blocked_until"] == now + 900 and summary[1]["secondary"] is True and not any(s["available"] for s in summary)
    assert pool.select(now + 120).name == "b" and pool.select(now + 120).available  # the secondary pause passed
    assert pool.select(now + 300).name == "b" and pool.select(now + 300).remaining is None  # b's window reset: full again, exact figure unknown
    assert pool.select(now + 900).name == "a"  # both renewed and unknown: configured order again


def test_pool_status_readings_that_show_an_empty_quota_block_the_source_until_the_reset():
    pool, now = _pool("a", "b"), 1000.0
    pool.record_status("a", 0, 5000, now + 500, now)
    pool.record_status("b", 0, 5000, now + 200, now)
    sel = pool.select(now)
    assert not sel.available and sel.name == "b" and sel.wait == 200 and sel.remaining == 0 and not sel.secondary
    assert pool.status(now) == (0, 5000, now + 200)
    pool.set_aside("b", now + 250)  # a source whose token could not be produced is skipped for a while
    sel = pool.select(now + 201)  # b's window has reset but it is set aside: still the soonest (49 s), a is out until now + 500
    assert sel.name == "b" and not sel.available and sel.wait == 49
    assert pool.select(now + 250).name == "b" and pool.select(now + 250).available


def test_pool_refuses_duplicate_or_missing_sources():
    with pytest.raises(ValueError):
        GA.TokenPool([])
    with pytest.raises(ValueError):
        _pool("a", "a")
    with pytest.raises(KeyError):
        _pool("a").record_call("zzz")


# ---------------------------------------------------------------- pool_from_settings

def test_pool_from_settings_collects_every_configured_pat_and_the_app(tmp_path, rsa_key):
    private_pem, _ = rsa_key
    key_path = tmp_path / "app.pem"
    key_path.write_bytes(private_pem)
    env_file = tmp_path / ".env"
    env_file.write_text(f"GH_TOKEN=ghp_1\nGH_TOKEN_2=ghp_2\nGH_TOKEN_4=ghp_4\nGH_TOKEN_5=ghp_5\nSWESIM_GH_EXTRA_TOKENS=3\n"
                        f"GH_APP_ID={APP_ID}\nGH_APP_INSTALLATION_ID={INSTALLATION_ID}\nGH_APP_PRIVATE_KEY_PATH={key_path}\n")
    settings = S.Settings.from_env({"SWESIM_ENV_FILE": str(env_file)})
    pool = GA.pool_from_settings(settings, environ={})
    assert pool.names == ("GH_TOKEN", "GH_TOKEN_2", "GH_TOKEN_4", f"app:{INSTALLATION_ID}")  # GH_TOKEN_3 absent, GH_TOKEN_5 beyond the count
    assert pool.token("GH_TOKEN_4") == "ghp_4" and isinstance(pool.source(f"app:{INSTALLATION_ID}"), GA.AppSource)
    assert GA.pat_names(0) == ("GH_TOKEN",) and GA.pat_names(2) == ("GH_TOKEN", "GH_TOKEN_2", "GH_TOKEN_3")


def test_pool_from_settings_reads_the_app_key_from_the_secret_when_there_is_no_file(tmp_path, rsa_key):
    private_pem, _ = rsa_key
    env_file = tmp_path / ".env"
    env_file.write_text(f"GH_APP_ID={APP_ID}\nGH_APP_INSTALLATION_ID={INSTALLATION_ID}\n")
    settings = S.Settings.from_env({"SWESIM_ENV_FILE": str(env_file)})
    pool = GA.pool_from_settings(settings, environ={"GH_TOKEN": "ghp_env", "GH_APP_PRIVATE_KEY": private_pem.decode()})
    assert pool.names == ("GH_TOKEN", f"app:{INSTALLATION_ID}")
    escaped = private_pem.decode().replace("\n", "\\n")  # a PEM pasted as one line with escaped newlines still loads
    assert GA.private_key_from_settings(settings, environ={"GH_APP_PRIVATE_KEY": escaped}) == private_pem
    assert isinstance(GA.pool_from_settings(settings, environ={"GH_APP_PRIVATE_KEY": escaped}).source(f"app:{INSTALLATION_ID}"), GA.AppSource)
    with pytest.raises(S.MissingSecret) as exc:
        GA.pool_from_settings(settings, environ={"GH_TOKEN": "ghp_env"})
    assert "GH_APP_PRIVATE_KEY" in str(exc.value) and "ghp_env" not in str(exc.value)
    missing_file = S.Settings.from_env({"SWESIM_ENV_FILE": str(env_file), "GH_APP_PRIVATE_KEY_PATH": str(tmp_path / "absent.pem")})
    with pytest.raises(S.MissingSecret) as exc:
        GA.pool_from_settings(missing_file, environ={})
    assert "absent.pem" in str(exc.value)
    half = S.Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "GH_APP_ID": str(APP_ID)})
    with pytest.raises(ValueError, match="GH_APP_INSTALLATION_ID"):
        GA.pool_from_settings(half, environ={})


def test_pool_from_settings_without_any_token_falls_back_to_gh_s_own_credential(tmp_path):
    settings = S.Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env")})
    pool = GA.pool_from_settings(settings, environ={})
    assert pool.names == ("gh",) and pool.token("gh") is None
    assert not any("Documents" in str(v) for v in settings.model_dump().values() if isinstance(v, (str, Path)))
