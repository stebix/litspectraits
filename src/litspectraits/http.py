"""Shared HTTP client factory.

A single :class:`httpx.AsyncClient` is created per resolve / acquire call
and shared across all probes via :class:`~litspectraits.resolver.probes.base.ProbeContext`.
The client is configured for the polite pool (User-Agent with mailto, gzip,
HTTP/2) and a uniform timeout.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import httpx

from litspectraits import __version__
from litspectraits.config import Settings

_USER_AGENT_TEMPLATE: Final = 'litspectraits/{version} (+mailto:{email})'


def user_agent(settings: Settings) -> str:
    """Build the polite-pool ``User-Agent`` string for the current process."""
    return _USER_AGENT_TEMPLATE.format(version=__version__, email=settings.contact_email)


@asynccontextmanager
async def http_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a configured :class:`httpx.AsyncClient`.

    The client follows redirects, negotiates HTTP/2, advertises gzip, and
    uses a shared timeout from ``settings.http_timeout_s``.
    """
    headers = {
        'User-Agent': user_agent(settings),
        'Accept-Encoding': 'gzip, deflate',
    }
    timeout = httpx.Timeout(settings.http_timeout_s)
    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        http2=True,
    ) as client:
        yield client
