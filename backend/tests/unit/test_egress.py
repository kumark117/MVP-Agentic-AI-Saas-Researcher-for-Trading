"""Egress policy: allowlist, HTTPS only, public addresses only, redirects re-validated."""

from __future__ import annotations

import httpx
import pytest

from alloyiq.security.egress import EgressBlocked, EgressPolicy, SafeHttpClient, is_public_address


def _resolver(mapping: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    async def resolve(host: str, port: int) -> list[str]:
        return mapping.get(host, ["93.184.216.34"])

    return resolve


@pytest.mark.parametrize(
    "addr",
    [
        "10.0.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:10.0.0.1",
        "0.0.0.0",
    ],
)
def test_private_and_metadata_addresses_blocked(addr: str) -> None:
    assert not is_public_address(addr)


def test_public_address_allowed() -> None:
    assert is_public_address("8.8.8.8")


@pytest.mark.parametrize(
    "url",
    [
        "http://www.alphavantage.co/query",  # not HTTPS
        "https://evil.example/collect",  # not allowlisted
        "https://user:pw@www.alphavantage.co/",  # credentials
        "https://10.0.0.5/",  # IP literal
        "file:///etc/passwd",
    ],
)
async def test_disallowed_urls(url: str) -> None:
    with pytest.raises(EgressBlocked):
        await EgressPolicy(resolver=_resolver({})).check(url)


async def test_dns_rebinding_to_private_address_blocked() -> None:
    policy = EgressPolicy(resolver=_resolver({"api.tavily.com": ["93.184.216.34", "10.1.1.1"]}))
    with pytest.raises(EgressBlocked, match="non-public"):
        await policy.check("https://api.tavily.com/search")


async def test_redirect_to_unlisted_host_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.alphavantage.co":
            return httpx.Response(302, headers={"location": "https://evil.example/x"})
        return httpx.Response(200, text="should never be reached")

    client = SafeHttpClient(
        EgressPolicy(resolver=_resolver({})), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(EgressBlocked, match="allowlist"):
        await client.request("GET", "https://www.alphavantage.co/query")
    await client.aclose()


async def test_oversized_response_rejected() -> None:
    client = SafeHttpClient(
        EgressPolicy(resolver=_resolver({})),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 100)),
    )
    with pytest.raises(EgressBlocked, match="larger"):
        await client.request("GET", "https://api.tavily.com/search", max_bytes=10)
    await client.aclose()


def test_no_payment_network_hosts_on_allowlist() -> None:
    from alloyiq.security.egress import DEFAULT_ALLOWED_HOSTS

    assert not any(
        p in h
        for h in DEFAULT_ALLOWED_HOSTS
        for p in ("stripe", "razorpay", "paypal", "payu", "braintree", "adyen")
    )
