from __future__ import annotations

import json
import os
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from grok_proxy.catalog_dns import CatalogResolver, catalog_dns_proxy


def test_catalog_resolver_uses_public_dns_and_caches_until_ttl():
    answer = {"Status": 0, "Answer": [
        {"type": 1, "TTL": 60, "data": "104.18.28.234"},
        {"type": 1, "TTL": 60, "data": "104.18.29.234"},
    ]}
    with patch("grok_proxy.catalog_dns.urlopen", return_value=BytesIO(json.dumps(answer).encode())) as fetch:
        resolver = CatalogResolver()
        assert resolver.addresses() == ["104.18.28.234", "104.18.29.234"]
        assert resolver.addresses() == ["104.18.28.234", "104.18.29.234"]
        assert fetch.call_count == 1
        assert fetch.call_args.args[0].full_url == (
            "https://dns.google/resolve?name=cli-chat-proxy.grok.com&type=A"
        )


def test_catalog_resolver_rejects_synthetic_or_private_dns_answers():
    answer = {"Status": 0, "Answer": [
        {"type": 1, "TTL": 60, "data": "198.18.0.47"},
        {"type": 1, "TTL": 60, "data": "127.0.0.1"},
    ]}
    with patch("grok_proxy.catalog_dns.urlopen", return_value=BytesIO(json.dumps(answer).encode())):
        with pytest.raises(RuntimeError, match="public IPv4"):
            CatalogResolver().addresses()


def test_disabled_catalog_proxy_does_not_change_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://existing.example:7890")
    with catalog_dns_proxy(enabled=False):
        assert os.environ["HTTPS_PROXY"] == "http://existing.example:7890"


def test_catalog_resolver_refreshes_expired_dns():
    first = {"Status": 0, "Answer": [{"type": 1, "TTL": 1, "data": "104.18.28.234"}]}
    second = {"Status": 0, "Answer": [{"type": 1, "TTL": 60, "data": "104.18.29.234"}]}
    with (
        patch("grok_proxy.catalog_dns.urlopen", side_effect=[
            BytesIO(json.dumps(first).encode()), BytesIO(json.dumps(second).encode()),
        ]),
        patch("grok_proxy.catalog_dns.time.monotonic", side_effect=[100, 102, 102]),
    ):
        resolver = CatalogResolver()
        assert resolver.addresses() == ["104.18.28.234"]
        assert resolver.addresses() == ["104.18.29.234"]


def test_catalog_proxy_restores_environment_and_stops_on_error(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://existing.example:7890")
    monkeypatch.setenv("NO_PROXY", "localhost,.grok.com,127.0.0.1")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("GROK_PROXY_CATALOG_DNS_PROXY", raising=False)
    server = MagicMock(server_port=12345)
    with (
        patch("grok_proxy.catalog_dns.CatalogResolver.addresses", return_value=["104.18.28.234"]),
        patch("grok_proxy.catalog_dns._CatalogTunnel", return_value=server),
        pytest.raises(RuntimeError, match="gateway stopped"),
        catalog_dns_proxy(enabled=True),
    ):
        assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:12345"
        assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"
        assert os.environ["no_proxy"] == ""
        raise RuntimeError("gateway stopped")
    assert os.environ["HTTPS_PROXY"] == "http://existing.example:7890"
    assert os.environ["NO_PROXY"] == "localhost,.grok.com,127.0.0.1"
    assert os.environ["no_proxy"] == "*"
    assert "https_proxy" not in os.environ
    assert "GROK_PROXY_CATALOG_DNS_PROXY" not in os.environ
    server.stopping.set.assert_called_once()
    server.shutdown.assert_called_once()
    server.server_close.assert_called_once()
