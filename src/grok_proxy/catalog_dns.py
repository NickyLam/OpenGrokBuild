"""Opt-in HTTPS tunnel for a VPN's broken synthetic Grok DNS route.

TLS stays end-to-end: only CONNECT is handled here, never HTTP credentials
or model payloads. The catalog hostname uses public DNS; other hosts keep
the system resolver. No system DNS, hosts file, or Grok config is changed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import select
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, proxy_bypass_environment, urlopen

CATALOG_HOST = "cli-chat-proxy.grok.com"
logger = logging.getLogger(__name__)


class CatalogResolver:
    def __init__(self) -> None:
        self._addresses: list[str] = []
        self._expires = 0.0
        self._lock = threading.Lock()

    def addresses(self) -> list[str]:
        with self._lock:
            if self._addresses and time.monotonic() < self._expires:
                return list(self._addresses)
            request = Request(
                f"https://dns.google/resolve?name={CATALOG_HOST}&type=A",
                headers={"Accept": "application/dns-json"},
            )
            with urlopen(request, timeout=10) as response:
                data = json.load(response)
            addresses: list[str] = []
            ttl = 300
            if data.get("Status") == 0:
                for answer in data.get("Answer", []):
                    if answer.get("type") != 1:
                        continue
                    address = ipaddress.ip_address(answer["data"])
                    if address.version == 4 and address.is_global:
                        addresses.append(str(address))
                        ttl = min(ttl, max(int(answer.get("TTL", 60)), 1))
            if not addresses:
                raise RuntimeError("Grok catalog DNS returned no public IPv4 addresses")
            self._addresses = addresses
            self._expires = time.monotonic() + ttl
            return list(addresses)


class _CatalogTunnel(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, resolver: CatalogResolver) -> None:
        self.resolver = resolver
        self.stopping = threading.Event()
        super().__init__(("127.0.0.1", 0), _ConnectHandler)


class _ConnectHandler(BaseHTTPRequestHandler):
    server: _CatalogTunnel

    def log_message(self, format: str, *args: object) -> None:
        # CONNECT logs are unnecessary; never log HTTP headers.
        return

    def do_CONNECT(self) -> None:
        try:
            host, raw_port = self.path.rsplit(":", 1)
            port = int(raw_port)
            if port != 443:
                self.send_error(403, "Only HTTPS CONNECT on port 443 is supported")
                return
        except ValueError:
            self.send_error(400, "Invalid CONNECT target")
            return
        upstream: socket.socket | None = None
        try:
            addresses = self.server.resolver.addresses() if host.lower() == CATALOG_HOST else [host]
            for address in addresses:
                try:
                    upstream = socket.create_connection((address, port), timeout=10)
                    break
                except OSError:
                    if address == addresses[-1]:
                        raise
        except (OSError, RuntimeError, ValueError):
            logger.exception("HTTPS tunnel connection failed for %s", host)
            self.send_error(502, "HTTPS tunnel connection failed")
            return
        if upstream is None:
            self.send_error(502, "No HTTPS tunnel address")
            return
        with upstream:
            self.send_response(200, "Connection Established")
            self.end_headers()
            self.wfile.flush()
            upstream.settimeout(None)
            self.connection.settimeout(None)
            try:
                while not self.server.stopping.is_set():
                    ready, _, _ = select.select([self.connection, upstream], [], [], 1)
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        target = upstream if source is self.connection else self.connection
                        target.sendall(data)
            except OSError:
                # A disconnected client/upstream closes this one tunnel.
                return


@contextmanager
def catalog_dns_proxy(*, enabled: bool):
    if not enabled:
        yield
        return
    resolver = CatalogResolver()
    resolver.addresses()  # Fail visibly before starting a gateway with a broken route.
    server = _CatalogTunnel(resolver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    proxy = f"http://127.0.0.1:{server.server_port}"
    keys = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "GROK_PROXY_CATALOG_DNS_PROXY")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        thread.start()
        os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = proxy
        os.environ["GROK_PROXY_CATALOG_DNS_PROXY"] = proxy
        for key in ("NO_PROXY", "no_proxy"):
            entries = (os.environ.get(key) or "").split(",")
            os.environ[key] = ",".join(
                entry for entry in entries
                if entry.strip() and not proxy_bypass_environment(CATALOG_HOST, {"no": entry})
            )
        logger.info("Grok catalog DNS repair enabled; TLS verification remains with Grok CLI")
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server.stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
