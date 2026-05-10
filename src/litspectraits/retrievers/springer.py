"""Springer Nature TDM retriever (``docs/overview-v3.md`` §7.2).

Wraps ``springernature-api-client``'s ``TDMAPI.search`` for per-DOI
retrieval. The SDK is sync (``requests``-based) and is bridged via
:func:`asyncio.to_thread`.

Inconsistencies with §7.2 surfaced during implementation
--------------------------------------------------------

- **SDK prints to stdout.** ``TDMAPI.search`` and ``TDMAPI.save_xml`` use
  bare ``print()`` calls for progress / error reporting. To keep stdout
  clean (we honor the ``--json`` contract upstream) we redirect stdout
  during the SDK call into a discarded ``StringIO``.
- **``save_xml`` is unsuitable.** It pretty-prints, runs the body through
  ``minidom`` (which can mangle namespaced JATS), and swallows formatter
  exceptions silently. Pretty-printing also rewrites bytes, so the
  sha256 we hash would not match what Springer actually served. We
  therefore bypass ``save_xml`` and write the raw response body to
  ``tmp_dir`` ourselves.
- **Hardcoded ``timeout=10``** in the SDK's ``_make_request``; our
  ``Settings.http_timeout_s`` is ignored on this code path. Out of scope
  to monkeypatch here — the SDK's retry already covers transient
  network failures.
- **Errors are flattened to ``APIRequestError``.** ``requests.HTTPError``
  is caught and re-raised as a generic ``APIRequestError`` with no status
  code attribute. We chase ``__cause__`` to recover the HTTP status when
  available so 401 / 403 surface as :class:`AuthRejectedError` instead of
  the catch-all :class:`PublisherAPIError`.
- **Single-record assertion** uses a simple ``<article`` byte-count after
  the sniff window. lxml inspection of the full document is overkill
  here — the retriever's job is to detect "zero records returned" vs
  "exactly one"; deeper structural validation belongs to the JATS
  extractor.
"""

import asyncio
import contextlib
import hashlib
import io
import re
import secrets
from importlib import metadata
from pathlib import Path
from typing import Any, ClassVar, Final, NoReturn

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

_DIST_NAME: Final = 'springernature-api-client'
_TDM_BASE_URL: Final = 'https://spdi.public.springernature.app/xmldata/jats'

# ``<article`` followed by ``>`` or whitespace — i.e. a real opening tag,
# not ``<article-title>`` or ``<article-meta>``.
_ARTICLE_OPEN_RE: Final = re.compile(rb'<article[\s>]')

_logger: Final = structlog.get_logger('litspectraits.retrievers.springer')


class SpringerRetriever:
    """Async wrapper around ``springernature_api_client.tdm.TDMAPI``.

    Parameters
    ----------
    rate_per_second : float, optional
        Token-bucket ceiling. Defaults to the §8 spec value (5.0).

    Notes
    -----
    Premium tier (``is_premium=True``) is mandatory: the non-premium
    endpoint returns metadata only, while we need the full JATS payload.
    """

    publisher: ClassVar[Publisher] = Publisher.SPRINGER_NATURE
    format: ClassVar[Format] = Format.JATS_XML

    def __init__(self, *, rate_per_second: float | None = None) -> None:
        rate = (
            rate_per_second
            if rate_per_second is not None
            else _default_rate(Publisher.SPRINGER_NATURE, 0.0)
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
        # ``client`` and ``meta`` are unused — the SDK manages its own
        # ``requests`` session and we don't need bibliographic data here.
        del client, meta
        if not settings.springer_api_key:
            raise MissingCredentialError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint='set SPRINGER_API_KEY (no IP-based fallback for Springer Nature)',
            )
        sdk = self._import_sdk(doi)
        await self._limiter.acquire()
        body = await self._search_one(
            doi=doi, api_key=settings.springer_api_key, sdk=sdk
        )
        staged_path = self._stage_body(tmp_dir=tmp_dir, body=body)
        verify(staged_path, expected=Format.JATS_XML, doi=doi)
        # The sniff guarantees the root element is ``<article>``. A
        # response carrying *no* ``<article>`` (zero hits for the DOI)
        # would have failed the sniff already; an extra count guards the
        # "two articles in one response" defensive case the spec calls out.
        n_articles = len(_ARTICLE_OPEN_RE.findall(body))
        if n_articles != 1:
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint='expected exactly one <article> element in response',
                articles_found=n_articles,
            )
        sha256, byte_size = _hash_and_size(staged_path)
        _logger.info(
            'springer fetch ok',
            doi=doi,
            sha256=sha256,
            byte_size=byte_size,
            sdk_version=sdk.version,
        )
        return RetrievePayload(
            sha256=sha256,
            byte_size=byte_size,
            tmp_path=staged_path,
            format=Format.JATS_XML,
            fetched_url=_TDM_BASE_URL,
            sdk_version=f'{_DIST_NAME} {sdk.version}',
        )

    @staticmethod
    def _import_sdk(doi: str) -> _SdkHandle:
        try:
            from springernature_api_client import tdm
            from springernature_api_client.exceptions import (
                APIRequestError,
                InvalidAPIKeyError,
                RateLimitExceededError,
            )
        except ImportError as exc:
            raise MissingCredentialError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint='install the [springer] extra (uv pip install litspectraits[springer])',
                import_error=str(exc),
            ) from exc
        try:
            version = metadata.version(_DIST_NAME)
        except metadata.PackageNotFoundError:  # pragma: no cover — defensive
            version = 'unknown'
        return _SdkHandle(
            tdm_module=tdm,
            invalid_key_exc=InvalidAPIKeyError,
            rate_limit_exc=RateLimitExceededError,
            request_exc=APIRequestError,
            version=version,
        )

    async def _search_one(
        self, *, doi: str, api_key: str, sdk: _SdkHandle
    ) -> bytes:
        """Run the SDK call, mapping its exceptions to ours.

        Returns the raw response body as bytes (the SDK returns ``str``).
        """

        def _do_search() -> str:
            client_obj = sdk.tdm_module.TDMAPI(api_key=api_key)
            # ``q='doi:<doi>'``, ``p=1``, ``s=1`` → at most one record.
            with contextlib.redirect_stdout(io.StringIO()):
                return client_obj.search(
                    q=f'doi:{doi}', p=1, s=1, fetch_all=False, is_premium=True
                )

        try:
            response = await asyncio.to_thread(_do_search)
        except sdk.invalid_key_exc as exc:
            raise MissingCredentialError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint='SPRINGER_API_KEY rejected by SDK constructor',
                sdk_error=str(exc),
            ) from exc
        except sdk.rate_limit_exc as exc:
            raise RateLimitExhaustedError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                attempts='sdk-internal',
                sdk_error=str(exc),
            ) from exc
        except sdk.request_exc as exc:
            self._raise_for_request_error(doi=doi, exc=exc)
        if not isinstance(response, str):
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint='SDK returned non-string response',
                response_type=type(response).__name__,
            )
        if not response.strip():
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint='SDK returned empty body — DOI likely absent from corpus',
            )
        return response.encode('utf-8')

    @staticmethod
    def _raise_for_request_error(*, doi: str, exc: Exception) -> NoReturn:
        """Translate the SDK's flattened ``APIRequestError`` to our taxonomy.

        The SDK catches ``requests.exceptions.RequestException`` and
        re-raises ``APIRequestError(str(e))``. We chase ``__cause__`` to
        recover the HTTP status code when available.
        """
        status_code = _http_status_from_cause(exc)
        if status_code in (401, 403):
            raise AuthRejectedError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                http_status=status_code,
                sdk_error=str(exc),
            ) from exc
        raise PublisherAPIError(
            doi=doi,
            publisher=Publisher.SPRINGER_NATURE.value,
            http_status=status_code,
            sdk_error=str(exc),
        ) from exc

    @staticmethod
    def _stage_body(*, tmp_dir: Path, body: bytes) -> Path:
        """Write ``body`` to a unique ``*.part`` file in ``tmp_dir``."""
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = tmp_dir / f'springer-{secrets.token_hex(8)}.xml.part'
        path.write_bytes(body)
        return path


def _http_status_from_cause(exc: Exception) -> int | None:
    """Walk ``__cause__`` looking for a ``requests`` HTTPError-like object."""
    cause = exc.__cause__
    while cause is not None:
        response = getattr(cause, 'response', None)
        status = getattr(response, 'status_code', None)
        if isinstance(status, int):
            return status
        cause = cause.__cause__
    return None


def _hash_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as fp:
        while chunk := fp.read(64 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class _SdkHandle:
    """Bundle of SDK symbols + version, populated once per fetch."""

    __slots__ = (
        'invalid_key_exc',
        'rate_limit_exc',
        'request_exc',
        'tdm_module',
        'version',
    )

    def __init__(
        self,
        *,
        tdm_module: Any,
        invalid_key_exc: type[Exception],
        rate_limit_exc: type[Exception],
        request_exc: type[Exception],
        version: str,
    ) -> None:
        self.tdm_module = tdm_module
        self.invalid_key_exc = invalid_key_exc
        self.rate_limit_exc = rate_limit_exc
        self.request_exc = request_exc
        self.version = version
