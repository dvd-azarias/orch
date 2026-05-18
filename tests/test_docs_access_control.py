from __future__ import annotations

from dataclasses import dataclass

from app.main import (
    _ip_in_networks,
    _is_docs_protected_path,
    _parse_ip,
    _parse_networks,
    _resolve_request_origin_ip,
)


@dataclass
class _FakeClient:
    host: str


@dataclass
class _FakeUrl:
    path: str


@dataclass
class _FakeRequest:
    client: _FakeClient
    headers: dict[str, str]
    url: _FakeUrl


def test_is_docs_protected_path() -> None:
    assert _is_docs_protected_path("/docs")
    assert _is_docs_protected_path("/docs/oauth2-redirect")
    assert _is_docs_protected_path("/openapi.json")
    assert _is_docs_protected_path("/redoc")
    assert not _is_docs_protected_path("/health/live")


def test_resolve_origin_ip_direct_external() -> None:
    req = _FakeRequest(
        client=_FakeClient(host="8.8.8.8"),
        headers={},
        url=_FakeUrl(path="/docs"),
    )
    trusted_proxy_networks = _parse_networks(("10.1.20.0/24",))
    origin_ip = _resolve_request_origin_ip(req, trusted_proxy_networks)
    assert str(origin_ip) == "8.8.8.8"


def test_resolve_origin_ip_uses_forwarded_for_when_proxy_trusted() -> None:
    req = _FakeRequest(
        client=_FakeClient(host="10.1.20.11"),
        headers={"x-forwarded-for": "8.8.8.8, 10.1.20.11"},
        url=_FakeUrl(path="/docs"),
    )
    trusted_proxy_networks = _parse_networks(("10.1.20.0/24",))
    origin_ip = _resolve_request_origin_ip(req, trusted_proxy_networks)
    assert str(origin_ip) == "8.8.8.8"


def test_resolve_origin_ip_ignores_forwarded_for_from_untrusted_proxy() -> None:
    req = _FakeRequest(
        client=_FakeClient(host="8.8.8.8"),
        headers={"x-forwarded-for": "10.1.20.22"},
        url=_FakeUrl(path="/docs"),
    )
    trusted_proxy_networks = _parse_networks(("10.1.20.0/24",))
    origin_ip = _resolve_request_origin_ip(req, trusted_proxy_networks)
    assert str(origin_ip) == "8.8.8.8"


def test_resolve_origin_ip_fails_closed_when_trusted_proxy_without_forwarded_for() -> None:
    req = _FakeRequest(
        client=_FakeClient(host="10.1.20.130"),
        headers={},
        url=_FakeUrl(path="/docs"),
    )
    trusted_proxy_networks = _parse_networks(("10.1.20.130/32",))
    origin_ip = _resolve_request_origin_ip(req, trusted_proxy_networks)
    assert origin_ip is None


def test_external_origin_is_blocked_by_internal_network_rule() -> None:
    req = _FakeRequest(
        client=_FakeClient(host="10.1.20.11"),
        headers={"x-forwarded-for": "8.8.8.8"},
        url=_FakeUrl(path="/docs"),
    )
    trusted_proxy_networks = _parse_networks(("10.1.20.0/24",))
    internal_networks = _parse_networks(("10.1.20.0/24",))

    origin_ip = _resolve_request_origin_ip(req, trusted_proxy_networks)
    assert not _ip_in_networks(origin_ip, internal_networks)


def test_internal_origin_is_allowed_by_internal_network_rule() -> None:
    req = _FakeRequest(
        client=_FakeClient(host="10.1.20.11"),
        headers={"x-forwarded-for": "10.1.20.99"},
        url=_FakeUrl(path="/docs"),
    )
    trusted_proxy_networks = _parse_networks(("10.1.20.0/24",))
    internal_networks = _parse_networks(("10.1.20.0/24",))

    origin_ip = _resolve_request_origin_ip(req, trusted_proxy_networks)
    assert _ip_in_networks(origin_ip, internal_networks)


def test_vpn_origin_is_allowed_by_internal_network_rule() -> None:
    req = _FakeRequest(
        client=_FakeClient(host="10.1.20.130"),
        headers={"x-forwarded-for": "10.100.105.129"},
        url=_FakeUrl(path="/docs"),
    )
    trusted_proxy_networks = _parse_networks(("10.1.20.130/32",))
    internal_networks = _parse_networks(("10.1.20.0/24", "10.100.105.0/24"))

    origin_ip = _resolve_request_origin_ip(req, trusted_proxy_networks)
    assert _ip_in_networks(origin_ip, internal_networks)


def test_parse_ip_invalid_returns_none() -> None:
    assert _parse_ip("not-an-ip") is None
