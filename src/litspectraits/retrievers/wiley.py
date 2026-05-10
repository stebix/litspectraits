"""Wiley TDM retriever (``docs/overview-v3.md`` §7.1).

Wraps the ``wiley-tdm`` SDK (``TDMClient.download_pdf``). The SDK is sync
and writes a PDF to disk under its ``download_dir``; we point that at the
caller-supplied ``tmp_dir`` and bridge the call through
:func:`asyncio.to_thread`. The atomic move into the artifact shard is the
orchestrator's job.

Inconsistencies with §7.1 surfaced during implementation
--------------------------------------------------------

- **No real IP-only fallback at the SDK boundary.** §7.1 says "Würzburg-IP
  requests succeed even without a token for entitled titles" — but
  ``TDMClient.__init__`` raises ``ValueError`` when ``TDM_API_TOKEN`` is
  unset, before any network call. IP-based entitlement only applies on
  *top* of a valid token. We therefore raise
  :class:`~litspectraits.errors.MissingCredentialError` whenever
  ``settings.wiley_tdm_token`` is unset, full stop.
- **SDK enforces a 5 s floor between requests.** ``TDMClient.api_rate_limit``
  rejects values below ``5.0``. Our 3 req/s spec ceiling is moot in
  practice — the SDK's internal pacing dominates. Our token bucket still
  runs (one acquire per fetch) so the contract is uniform across
  retrievers, but the effective Wiley rate is never higher than the SDK
  allows.
- **``wiley_tdm.__version__`` is stale.** It reports ``'0.1.0'`` while the
  installed package is ``1.0.0``. We source ``sdk_version`` from
  :func:`importlib.metadata.version` instead.

Failure translation
-------------------

``DownloadResult.status`` →

- ``SUCCESS`` → :class:`~litspectraits.manifest.RetrievePayload`
- ``ACCESS_DENIED`` → :class:`~litspectraits.errors.AuthRejectedError`
- ``api_status == 429`` (any status) → bounded retry, then
  :class:`~litspectraits.errors.RateLimitExhaustedError`
- everything else → :class:`~litspectraits.errors.PublisherAPIError`

The post-download magic-byte sniff (defence in depth — Wiley has been
known to serve paywall HTML with a ``.pdf`` suffix on edge cases)
runs unconditionally on success.
"""

import asyncio
import contextlib
import hashlib
import os
import secrets
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from typing import Any, ClassVar, Final

import httpx
import structlog

from litspectraits.config import Settings
from litspectraits.errors import (
    AuthRejectedError,
    MissingCredentialError,
    PublisherAPIError,
    RateLimitExhaustedError,
)
from litspectraits.manifest import CrossRefMetadata, Format, Publisher, RetrievePayload
from litspectraits.retrievers._ratelimit import RateLimiter, _default_rate
from litspectraits.sniff import verify

# ``wiley_tdm`` lives behind the ``[wiley]`` extra and is imported lazily
# inside ``fetch``. Annotations referring to its ``DownloadResult`` /
# ``DownloadStatus`` types use ``Any`` to avoid pulling the SDK at import
# time and to stay compatible with our no-``from __future__ import
# annotations`` rule (quoted forward refs would trip ruff UP037).

_TOKEN_ENV: Final = 'TDM_API_TOKEN'
_MAX_429_ATTEMPTS: Final = 3
_DIST_NAME: Final = 'wiley-tdm'

_logger: Final = structlog.get_logger('litspectraits.retrievers.wiley')


@contextlib.contextmanager
def _patched_env(key: str, value: str) -> Iterator[None]:
    """Temporarily set ``os.environ[key]``; restore on exit.

    The SDK reads the token from ``TDM_API_TOKEN`` once at
    ``TDMClient.__init__`` and copies it into a private attribute. The
    env override window only needs to cover that constructor call — we
    do not leave the token in the ambient env, which would leak it into
    concurrent tasks and child processes.
    """
    sentinel = object()
    previous: object = os.environ.get(key, sentinel)
    os.environ[key] = value
    try:
        yield
    finally:
        if previous is sentinel:
            os.environ.pop(key, None)
        else:
            assert isinstance(previous, str)
            os.environ[key] = previous


class WileyRetriever:
    """Async wrapper around ``wiley_tdm.TDMClient``.

    Parameters
    ----------
    rate_per_second : float, optional
        Token-bucket ceiling. Defaults to the §8 spec value (3.0). The
        SDK also enforces an internal 5 s floor, so this is largely a
        no-op for Wiley; the parameter exists for symmetry with the
        other retrievers and for tests.
    """

    publisher: ClassVar[Publisher] = Publisher.WILEY
    format: ClassVar[Format] = Format.PDF

    def __init__(self, *, rate_per_second: float | None = None) -> None:
        rate = (
            rate_per_second
            if rate_per_second is not None
            else _default_rate(Publisher.WILEY, 0.0)
        )
        self.rate_per_second = rate
        self._limiter = RateLimiter(rate)

    async def fetch(
        self,
        doi: str,
        meta: CrossRefMetadata,
        *,
        client: httpx.AsyncClient,
        tmp_dir: Path,
        settings: Settings,
    ) -> RetrievePayload:
        # ``client`` is unused — the SDK manages its own ``requests`` session.
        # ``meta`` is unused here but accepted for protocol uniformity.
        del client, meta
        if not settings.wiley_tdm_token:
            raise MissingCredentialError(
                doi=doi,
                publisher=Publisher.WILEY.value,
                hint='set WILEY_TDM_TOKEN (or install the [wiley] extra and a token)',
            )
        sdk = self._import_sdk(doi)
        await self._limiter.acquire()

        result = await self._download_with_429_retry(
            doi=doi,
            token=settings.wiley_tdm_token,
            tmp_dir=tmp_dir,
            sdk=sdk,
        )
        return self._payload_from_result(doi=doi, result=result, sdk_version=sdk.version)

    @staticmethod
    def _import_sdk(doi: str) -> _SdkHandle:
        """Lazy-import wiley-tdm; raise a clean error if the extra is absent."""
        try:
            from wiley_tdm import DownloadStatus, TDMClient
        except ImportError as exc:
            raise MissingCredentialError(
                doi=doi,
                publisher=Publisher.WILEY.value,
                hint='install the [wiley] extra (uv pip install litspectraits[wiley])',
                import_error=str(exc),
            ) from exc
        # ``__version__`` in the installed wheel is stale; trust the
        # distribution metadata instead.
        try:
            version = metadata.version(_DIST_NAME)
        except metadata.PackageNotFoundError:  # pragma: no cover — defensive
            version = 'unknown'
        return _SdkHandle(client_cls=TDMClient, status_cls=DownloadStatus, version=version)

    async def _download_with_429_retry(
        self,
        *,
        doi: str,
        token: str,
        tmp_dir: Path,
        sdk: _SdkHandle,
    ) -> Any:
        """Run the SDK call with bounded 429 retry.

        Three total attempts; exponential backoff (1s, 2s) between
        attempts. Anything that's not a 429 returns or raises immediately
        — we do not retry transport / 5xx here because the SDK already
        does its own modest retry internally, and the spec only mandates
        retry for the rate-limit case (§8).
        """
        last_429: Any = None
        for attempt in range(1, _MAX_429_ATTEMPTS + 1):
            with _patched_env(_TOKEN_ENV, token):
                tdm = sdk.client_cls(download_dir=tmp_dir)
            tdm.skip_existing_files = False  # we cleared tmp/, never reuse
            result = await asyncio.to_thread(tdm.download_pdf, doi)
            if result.status is sdk.status_cls.SUCCESS:
                return result
            if result.status is sdk.status_cls.ACCESS_DENIED:
                raise AuthRejectedError(
                    doi=doi,
                    publisher=Publisher.WILEY.value,
                    api_status=_api_status_int(result),
                    comment=result.comment,
                )
            if _api_status_int(result) == 429:
                last_429 = result
                if attempt < _MAX_429_ATTEMPTS:
                    await asyncio.sleep(2 ** (attempt - 1))
                    continue
                break
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.WILEY.value,
                sdk_status=result.status.name,
                api_status=_api_status_int(result),
                comment=result.comment,
            )
        raise RateLimitExhaustedError(
            doi=doi,
            publisher=Publisher.WILEY.value,
            attempts=_MAX_429_ATTEMPTS,
            api_status=_api_status_int(last_429) if last_429 is not None else None,
        )

    def _payload_from_result(
        self, *, doi: str, result: Any, sdk_version: str
    ) -> RetrievePayload:
        if result.path is None:
            # SUCCESS without a path is a SDK contract violation; treat
            # as PublisherAPIError rather than crashing on AttributeError.
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.WILEY.value,
                sdk_status=result.status.name,
                comment='SDK reported SUCCESS but did not populate result.path',
            )
        # Move into a unique ``*.part`` filename inside the same tmp_dir
        # so the orchestrator's commit step always operates on a name it
        # can safely ``os.replace``. The SDK derives the filename from the
        # DOI and would otherwise collide on a re-fetch within the same
        # session.
        sdk_path = Path(result.path)
        unique_name = f'wiley-{secrets.token_hex(8)}.pdf.part'
        staged_path = sdk_path.with_name(unique_name)
        sdk_path.replace(staged_path)
        verify(staged_path, expected=Format.PDF, doi=doi)
        sha256, byte_size = _hash_and_size(staged_path)
        _logger.info(
            'wiley fetch ok',
            doi=doi,
            sha256=sha256,
            byte_size=byte_size,
            sdk_version=sdk_version,
        )
        return RetrievePayload(
            sha256=sha256,
            byte_size=byte_size,
            tmp_path=staged_path,
            format=Format.PDF,
            fetched_url=_wiley_tdm_url(doi),
            sdk_version=f'{_DIST_NAME} {sdk_version}',
        )


def _api_status_int(result: Any) -> int | None:
    if result is None or result.api_status is None:
        return None
    return int(result.api_status)


def _hash_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as fp:
        while chunk := fp.read(64 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _wiley_tdm_url(doi: str) -> str:
    return f'https://api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}'


class _SdkHandle:
    """Thin tuple-like for the lazily-imported SDK symbols.

    Carries the concrete ``TDMClient`` class, the ``DownloadStatus`` enum
    we compare against, and the resolved package version. Bundled together
    so the import-and-version dance happens exactly once per fetch.
    """

    __slots__ = ('client_cls', 'status_cls', 'version')

    def __init__(self, client_cls: type, status_cls: type[Any], version: str) -> None:
        self.client_cls = client_cls
        self.status_cls = status_cls
        self.version = version
