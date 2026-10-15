"""Environment for grok child processes.

Kept separate from grok_runner so model discovery can use it during startup
without importing the runner (that import cycle breaks `python -m grok_proxy`).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Hosts the CLI must reach to load grok-4.7. A loopback forwarder that
# blackholes these hosts leaves models() on the built-in pair and `-m grok-4.7`
# exits with "unknown model id".
_GROK_CATALOG_HOSTS = (
    "cli-chat-proxy.grok.com",
    "grok.com",
    ".grok.com",
    "x.ai",
    ".x.ai",
)
_PROXY_ENV_KEYS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_logged_catalog_proxy_bypass = False


def _proxy_hostname(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        return (urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""


def _uses_loopback_proxy(env: dict[str, str]) -> bool:
    for key in _PROXY_ENV_KEYS:
        host = _proxy_hostname(env.get(key) or "")
        if host in _LOOPBACK_HOSTS:
            return True
    return False


def _merge_no_proxy(existing: str | None) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for chunk in (existing or "").split(","):
        item = chunk.strip()
        if not item or item.lower() in seen:
            continue
        seen.add(item.lower())
        parts.append(item)
    for host in _GROK_CATALOG_HOSTS:
        if host.lower() in seen:
            continue
        seen.add(host.lower())
        parts.append(host)
    return ",".join(parts)


def apply_grok_cli_env(env: dict[str, str]) -> dict[str, str]:
    """Env for a grok child.

    Keep a loopback HTTP proxy for everything else, but do not send the model
    catalog through it. A forwarder on 127.0.0.1:7890 times out for
    cli-chat-proxy.grok.com while a direct connection answers; grok then
    starts with only grok-4.6 and grok-4.5 and rejects grok-4.7.
    """
    global _logged_catalog_proxy_bypass
    env.setdefault("GROK_DISABLE_AUTOUPDATER", "1")
    # The opt-in catalog tunnel deliberately uses public DNS for this host.
    # Adding NO_PROXY here would silently send it back to the broken VPN DNS.
    catalog_proxy = env.get("GROK_PROXY_CATALOG_DNS_PROXY")
    if catalog_proxy and env.get("HTTPS_PROXY") == catalog_proxy:
        return env
    if not _uses_loopback_proxy(env):
        return env
    merged = _merge_no_proxy(env.get("NO_PROXY") or env.get("no_proxy"))
    env["NO_PROXY"] = merged
    env["no_proxy"] = merged
    if not _logged_catalog_proxy_bypass:
        logger.info("bypassing loopback HTTP proxy for the Grok catalog hosts")
        _logged_catalog_proxy_bypass = True
    return env
