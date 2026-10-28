"""Opaque tokens, hashing and signed double-submit CSRF tokens (D013, D015, D016)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets


def new_token(nbytes: int = 32) -> str:
    """URL-safe random token. Only its SHA-256 is ever stored."""
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class CsrfTokens:
    """Signed double-submit tokens: "<nonce>.<hmac(secret, nonce|binding)>".

    The token lives in an HttpOnly cookie and must be echoed in X-CSRF-Token. The binding is
    the session ID (or "anon" before login), so a token minted for one session is useless
    for another, and login always issues a fresh one.
    """

    def __init__(self, secret: str) -> None:
        if len(secret) < 16:
            raise ValueError("SESSION_SECRET must be at least 16 characters")
        self._key = hashlib.sha256(("csrf:" + secret).encode()).digest()

    def issue(self, binding: str) -> str:
        nonce = _b64(secrets.token_bytes(18))
        return f"{nonce}.{self._sign(nonce, binding)}"

    def verify(self, token: str | None, binding: str) -> bool:
        if not token or token.count(".") != 1:
            return False
        nonce, sig = token.split(".")
        return hmac.compare_digest(sig, self._sign(nonce, binding))

    def _sign(self, nonce: str, binding: str) -> str:
        return _b64(hmac.new(self._key, f"{nonce}|{binding}".encode(), hashlib.sha256).digest())


def pseudonym(secret: str, value: str) -> str:
    """Stable HMAC pseudonym for logs and traces (account_pseudonym; build prompt section 17)."""
    key = hashlib.sha256(("pseudonym:" + secret).encode()).digest()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:20]


def ip_prefix(ip: str | None) -> str | None:
    """Coarse network prefix for the session list: /24 for IPv4, /48 for IPv6."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    bits = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{bits}", strict=False))


_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_OSES = (
    ("Windows", "Windows"),
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Mac OS X", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)


def device_summary(user_agent: str | None) -> str:
    """Approximate, display-only device label (D015: not an authoritative fingerprint)."""
    ua = user_agent or ""
    browser = next((name for key, name in _BROWSERS if key in ua), None)
    os_name = next((name for key, name in _OSES if key in ua), None)
    if browser and os_name:
        return f"{browser} on {os_name}"
    if browser or os_name:
        return browser or os_name or "Unknown device"
    short = re.sub(r"[^\w./ -]", "", ua)[:40]
    return short or "Unknown device"
