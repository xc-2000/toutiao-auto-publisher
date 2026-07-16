"""Network helpers for IPv4-only servers (common on Chinese cloud VMs)."""

from __future__ import annotations

import socket
from typing import Any

import httpx

_PATCHED = False


def prefer_ipv4() -> None:
    """Force dual-stack hostname resolution to prefer IPv4 addresses."""
    global _PATCHED
    if _PATCHED:
        return
    original = socket.getaddrinfo

    def patched(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        if family == 0:
            try:
                return original(host, port, socket.AF_INET, type, proto, flags)
            except socket.gaierror:
                return original(host, port, family, type, proto, flags)
        return original(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched  # type: ignore[assignment]
    _PATCHED = True


def httpx_client(**kwargs: Any) -> httpx.Client:
    """httpx client forced onto IPv4 where possible.

    Supports optional proxy=... for overseas CDN downloads.
    """
    prefer_ipv4()
    headers = kwargs.pop("headers", None)
    timeout = kwargs.pop("timeout", 60)
    follow_redirects = kwargs.pop("follow_redirects", True)
    transport = kwargs.pop("transport", None)
    proxy = kwargs.pop("proxy", None)
    # older/newer httpx compatibility: proxy vs proxies
    if transport is None and proxy is None:
        # local_address 0.0.0.0 forces IPv4 sockets on Linux
        transport = httpx.HTTPTransport(local_address="0.0.0.0")
    client_kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": timeout,
        "follow_redirects": follow_redirects,
        **kwargs,
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    if proxy:
        # httpx>=0.28 uses proxy=; some builds still accept proxies=
        client_kwargs["proxy"] = proxy
    return httpx.Client(**client_kwargs)
