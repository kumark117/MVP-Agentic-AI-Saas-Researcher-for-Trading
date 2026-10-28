"""Identity flows end to end through the HTTP API (R008, R010-R013; D012-D017)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from .apiclient import (
    PASSWORD,
    ApiClient,
    latest_link,
    login,
    new_email,
    register_verified,
    token_from,
)

pytestmark = pytest.mark.integration


def _strip_meta(body: dict[str, object]) -> object:
    return body.get("data")


# ------------------------------------------------------------------ enumeration resistance
async def test_register_is_generic_for_new_and_existing_emails(
    app: FastAPI, migrator: Engine
) -> None:
    async with ApiClient(app) as c:
        existing = await register_verified(c, migrator)
        new = new_email()
        payload = {
            "password": PASSWORD,
            "display_name": "X",
            "terms_version": "t1",
            "privacy_version": "p1",
        }
        r_new = await c.post("/auth/register", {"email": new, **payload})
        r_old = await c.post("/auth/register", {"email": existing, **payload})
    assert r_new.status_code == r_old.status_code == 202
    assert _strip_meta(r_new.json()) == _strip_meta(r_old.json())
    # The difference is only in which email is queued, which an attacker cannot see.
    assert latest_link(migrator, new, "verify_email")
    with migrator.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM email_outbox WHERE to_email = :e AND"
                    " template = 'account_exists'"
                ),
                {"e": existing},
            ).scalar()
            == 1
        )


async def test_forgot_and_resend_are_generic(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as c:
        existing = await register_verified(c, migrator)
        for path in ("/auth/forgot-password", "/auth/resend-verification"):
            a = await c.post(path, {"email": existing})
            b = await c.post(path, {"email": new_email()})
            assert a.status_code == b.status_code == 202
            assert _strip_meta(a.json()) == _strip_meta(b.json())


async def test_bad_password_and_unknown_email_look_identical(
    app: FastAPI, migrator: Engine
) -> None:
    async with ApiClient(app) as c:
        email = await register_verified(c, migrator)
        wrong = await c.post("/auth/login", {"email": email, "password": "nope-nope-nope"})
        unknown = await c.post("/auth/login", {"email": new_email(), "password": "nope-nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert {k: v for k, v in wrong.json().items() if k != "request_id"} == {
        k: v for k, v in unknown.json().items() if k != "request_id"
    }


# ------------------------------------------------------------------ verification and login
async def test_verify_then_login_and_me(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as c:
        email = new_email()
        await c.post(
            "/auth/register",
            {
                "email": email,
                "password": PASSWORD,
                "display_name": "Asha",
                "terms_version": "t1",
                "privacy_version": "p1",
            },
        )
        r = await c.post("/auth/login", {"email": email, "password": PASSWORD})
        assert r.status_code == 403 and r.json()["type"].endswith("email-unverified")
        token = token_from(latest_link(migrator, email, "verify_email"))
        assert (await c.post("/auth/verify-email", {"token": token})).status_code == 200
        # Single use.
        assert (await c.post("/auth/verify-email", {"token": token})).status_code == 422
        # Verification does not create a session.
        assert (await c.get("/me")).status_code == 401
        r = await login(c, email)
        assert "alloyiq_sid" in r.cookies and "HttpOnly" in r.headers["set-cookie"]
        assert "samesite=lax" in r.headers["set-cookie"].lower()
        me = (await c.get("/me")).json()["data"]
    assert me["user"]["email"] == email and me["user"]["email_verified"] is True
    assert me["account"]["currency"] == "INR" and me["account"]["plan"] == "demo"


async def test_expired_verification_token_rejected(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as c:
        email = new_email()
        await c.post(
            "/auth/register",
            {
                "email": email,
                "password": PASSWORD,
                "display_name": "X",
                "terms_version": "t1",
                "privacy_version": "p1",
            },
        )
        with migrator.begin() as conn:
            conn.execute(
                text(
                    "UPDATE auth_tokens SET expires_at = now() - interval '1 minute'"
                    " WHERE user_id = (SELECT id FROM users WHERE email = :e)"
                ),
                {"e": email},
            )
        token = token_from(latest_link(migrator, email, "verify_email"))
        assert (await c.post("/auth/verify-email", {"token": token})).status_code == 422


async def test_tokens_are_stored_hashed_only(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as c:
        email = await register_verified(c, migrator)
        r = await login(c, email)
        cookie = r.cookies["alloyiq_sid"]
    with migrator.connect() as conn:
        stored = (
            conn.execute(
                text(
                    "SELECT encode(token_hash, 'escape') FROM sessions s JOIN users u ON u.id = s.user_id"
                    " WHERE u.email = :e"
                ),
                {"e": email},
            )
            .scalars()
            .all()
        )
    assert stored and all(cookie not in s for s in stored)


# ------------------------------------------------------------------ CSRF
async def test_csrf_required_on_mutations(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as c:
        email = await register_verified(c, migrator)
        anon_token = c.csrf
        await login(c, email)
        # No header.
        r = await c.http.post("/api/v1/auth/logout")
        assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"
        # Wrong origin.
        r = await c.post("/auth/logout", headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        # A pre-login token is bound to "anon", not to this session.
        c.http.cookies.delete("alloyiq_csrf")
        c.http.cookies.set("alloyiq_csrf", anon_token or "")
        r = await c.send("POST", "/auth/logout", headers={"X-CSRF-Token": anon_token or ""})
        assert r.status_code == 403
        c.http.cookies.delete("alloyiq_csrf")
        await c.refresh_csrf()
        assert (await c.post("/auth/logout")).status_code == 200


# ------------------------------------------------------------------ multiple devices
async def test_two_devices_and_per_session_revocation(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as laptop, ApiClient(app, ip="10.1.2.3") as phone:
        email = await register_verified(laptop, migrator)
        await login(laptop, email)
        await login(phone, email)
        sessions = (await laptop.get("/me/sessions")).json()["data"]["items"]
        assert len(sessions) == 2
        assert sum(s["current"] for s in sessions) == 1
        assert {s["device"] for s in sessions} == {"Chrome on Windows"}
        assert "10.1.2.0/24" in {s["ip_prefix"] for s in sessions}
        phone_id = next(s["session_id"] for s in sessions if not s["current"])
        assert (await laptop.send("DELETE", f"/me/sessions/{phone_id}")).status_code == 200
        assert (await phone.get("/me")).status_code == 401
        assert (await laptop.get("/me")).status_code == 200
        # Idempotent repeat.
        assert (await laptop.send("DELETE", f"/me/sessions/{phone_id}")).status_code == 200


async def test_cannot_touch_another_accounts_session(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as a, ApiClient(app) as b:
        await login(a, await register_verified(a, migrator))
        await login(b, await register_verified(b, migrator))
        b_session = (await b.get("/me")).json()["data"]["current_session_id"]
        r = await a.send("DELETE", f"/me/sessions/{b_session}")
        assert r.status_code == 404 and r.json()["code"] == "RESOURCE_NOT_FOUND"
        assert (await b.get("/me")).status_code == 200


async def test_expired_session_is_rejected(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as c:
        email = await register_verified(c, migrator)
        await login(c, email)
        with migrator.begin() as conn:
            conn.execute(
                text(
                    "UPDATE sessions SET idle_expires_at = now() - interval '1 second'"
                    " WHERE user_id = (SELECT id FROM users WHERE email = :e)"
                ),
                {"e": email},
            )
        assert (await c.get("/me")).status_code == 401


# ------------------------------------------------------------------ freshness-gated actions
def _make_stale(engine: Engine, email: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE sessions SET fresh_until = now() - interval '1 second'"
                " WHERE user_id = (SELECT id FROM users WHERE email = :e)"
            ),
            {"e": email},
        )


async def test_logout_all_requires_recent_reauthentication(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as a, ApiClient(app) as b:
        email = await register_verified(a, migrator)
        await login(a, email)
        await login(b, email)
        _make_stale(migrator, email)
        r = await a.post("/auth/logout-all")
        assert r.status_code == 403 and r.json()["type"].endswith("reauthentication-required")
        assert (
            await a.post("/auth/reauthenticate", {"password": "wrong-password"})
        ).status_code == 401
        r = await a.post("/auth/reauthenticate", {"password": PASSWORD})
        assert r.status_code == 200 and r.json()["data"]["result_type"] == "fresh"
        r = await a.post("/auth/logout-all")
        assert r.status_code == 200 and r.json()["data"]["revoked_count"] == 2
        assert (await a.get("/me")).status_code == 401
        assert (await b.get("/me")).status_code == 401


async def test_password_reset_revokes_every_session(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as a, ApiClient(app) as b:
        email = await register_verified(a, migrator)
        await login(a, email)
        await login(b, email)
        await b.post("/auth/forgot-password", {"email": email})
        token = token_from(latest_link(migrator, email, "reset_password"))
        new_pw = "a brand new long password"
        r = await b.post("/auth/reset-password", {"token": token, "new_password": new_pw})
        assert r.status_code == 200
        assert (await a.get("/me")).status_code == 401
        assert (await b.get("/me")).status_code == 401
        await b.refresh_csrf()  # the revoked session's CSRF binding is gone too
        assert (
            await b.post("/auth/reset-password", {"token": token, "new_password": new_pw})
        ).status_code == 422
        await a.refresh_csrf()
        assert (
            await a.post("/auth/login", {"email": email, "password": PASSWORD})
        ).status_code == 401
        await login(a, email, new_pw)


async def test_change_password_rotates_this_session_and_ends_others(
    app: FastAPI, migrator: Engine
) -> None:
    async with ApiClient(app) as a, ApiClient(app) as b:
        email = await register_verified(a, migrator)
        old_cookie = (await login(a, email)).cookies["alloyiq_sid"]
        await login(b, email)
        r = await a.post("/me/password", {"new_password": "another long password!"})
        assert r.status_code == 200
        assert a.http.cookies.get("alloyiq_sid") != old_cookie
        assert (await a.get("/me")).status_code == 200
        assert (await b.get("/me")).status_code == 401


async def test_deletion_request_requires_fresh_auth_and_is_idempotent(
    app: FastAPI, migrator: Engine
) -> None:
    async with ApiClient(app) as c:
        email = await register_verified(c, migrator)
        await login(c, email)
        _make_stale(migrator, email)
        assert (await c.post("/me/deletion-requests", {"confirmation": True})).status_code == 403
        await c.post("/auth/reauthenticate", {"password": PASSWORD})
        r1 = await c.post("/me/deletion-requests", {"confirmation": True})
        r2 = await c.post("/me/deletion-requests", {"confirmation": True})
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["data"]["request_id"] == r2.json()["data"]["request_id"]


# ------------------------------------------------------------------ profile and limits
async def test_patch_me_validates_input(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as c:
        await login(c, await register_verified(c, migrator))
        assert (await c.send("PATCH", "/me", {"timezone": "Mars/Base"})).status_code == 422
        assert (await c.send("PATCH", "/me", {"account_id": "x"})).status_code == 422
        r = await c.send("PATCH", "/me", {"timezone": "Asia/Kolkata", "display_name": "Ravi"})
    assert r.status_code == 200
    assert r.json()["data"]["preferences"]["timezone"] == "Asia/Kolkata"


async def test_login_is_rate_limited_per_email(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as c:
        email = await register_verified(c, migrator)
        codes = [
            (await c.post("/auth/login", {"email": email, "password": "wrong-pass-x"}))
            for _ in range(6)
        ]
    assert [r.status_code for r in codes[:5]] == [401] * 5
    assert codes[5].status_code == 429 and int(codes[5].headers["retry-after"]) >= 1


async def test_validation_errors_never_echo_passwords(app: FastAPI) -> None:
    async with ApiClient(app) as c:
        r = await c.post(
            "/auth/register",
            {
                "email": "not-an-email",
                "password": "s3cr3t-pw",
                "display_name": "",
                "terms_version": "t",
                "privacy_version": "p",
            },
        )
    assert r.status_code == 422 and "s3cr3t-pw" not in r.text
