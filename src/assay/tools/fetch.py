"""Fetch a web page and extract its main text, under a byte cap and a time cap."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
import trafilatura

from assay.tools import SourceDoc, Tool

log = logging.getLogger(__name__)

USER_AGENT = "assay-research/0.1 (+https://github.com/zaidwhy)"


class FetchRefused(ValueError):
    """The URL is not something this service will fetch."""


async def _ensure_public_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchRefused(f"url scheme must be http or https, got {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise FetchRefused("url has no host")
    # Page text can steer the model toward internal addresses; refuse anything that is not public.
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise FetchRefused(f"host {host} resolves to non-public address {address}")


async def fetch_page(url: str, *, max_bytes: int, timeout_s: float) -> SourceDoc:
    await _ensure_public_host(url)
    async with httpx.AsyncClient(
        timeout=timeout_s, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            body = bytearray()
            async for piece in response.aiter_bytes():
                body.extend(piece)
                if len(body) > max_bytes:
                    log.info("fetch of %s stopped at the %d byte cap", url, max_bytes)
                    del body[max_bytes:]
                    break
            content_type = response.headers.get("content-type", "")
            final_url = str(response.url)
    html = bytes(body).decode("utf-8", errors="replace")
    if "html" in content_type or html.lstrip()[:1] == "<":
        text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
        metadata = trafilatura.extract_metadata(html)
        title = (metadata.title if metadata and metadata.title else "") or final_url
    else:
        text, title = html, final_url
    return SourceDoc(url=final_url, title=title, text=text[:max_bytes], origin="web")


def make_fetch_tool(*, max_bytes: int, timeout_s: float) -> Tool:
    async def run(args: dict[str, Any]) -> list[SourceDoc]:
        url = str(args.get("url", "")).strip()
        if not url or len(url) > 2048:
            raise ValueError("fetch.url must be 1-2048 characters")
        page = await fetch_page(url, max_bytes=max_bytes, timeout_s=timeout_s)
        return [page] if page.text.strip() else []

    return Tool(
        name="fetch",
        description="Fetch one web page by URL and return its main text.",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "http or https URL"}},
            "required": ["url"],
        },
        run=run,
    )
