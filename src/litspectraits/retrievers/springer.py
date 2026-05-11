"""Springer Nature retriever (``docs/overview-v3.md`` §7.2).

Two tiers, picked by which credential is configured:

* **Premium TDM** (``SPRINGER_TDM_API_KEY``) — wraps
  ``springernature-api-client``'s ``TDMAPI.search`` for the full Springer
  Nature corpus (open and subscription content alike). The SDK is sync
  (``requests``-based) and is bridged via :func:`asyncio.to_thread`.
* **Open Access** (``SPRINGER_OA_API_KEY``) — direct ``httpx`` call to
  ``api.springernature.com/openaccess/jats``. Uses a standard developer-
  portal key; covers open-access content only.

When both keys are present the TDM path wins (strictly larger corpus).
The OA path is the fallback for operators who only hold a free
dev-portal key; it raises :class:`NotOpenAccessError` for DOIs the
endpoint cannot serve (real Springer DOI, not open-access). That class
exists exactly so the failure carries *recourse* — "acquire a TDM
licence or sideload" — rather than masquerading as a generic publisher
error.

Inconsistencies with §7.2 surfaced during implementation
--------------------------------------------------------

- **TDM SDK prints to stdout.** ``TDMAPI.search`` and ``TDMAPI.save_xml``
  use bare ``print()`` calls. To keep stdout clean (we honor the
  ``--json`` contract upstream) we redirect stdout during the SDK call
  into a discarded ``StringIO``.
- **``save_xml`` is unsuitable.** It pretty-prints, runs the body through
  ``minidom`` (which can mangle namespaced JATS), and swallows formatter
  exceptions silently. Pretty-printing also rewrites bytes, so the
  sha256 we hash would not match what Springer actually served. We
  bypass ``save_xml`` and write the raw response body to ``tmp_dir``
  ourselves.
- **Hardcoded ``timeout=10``** in the SDK's ``_make_request``; our
  ``Settings.http_timeout_s`` is ignored on the TDM path. Out of scope
  to monkeypatch — the SDK's retry already covers transient failures.
- **SDK errors are flattened to ``APIRequestError``.** ``requests.HTTPError``
  is caught and re-raised as a generic ``APIRequestError`` with no status
  code. We chase ``__cause__`` to recover the HTTP status when available
  so 401 / 403 surface as :class:`AuthRejectedError` instead of the
  catch-all :class:`PublisherAPIError`.
- **OA endpoint envelopes the article.** The TDM endpoint returns raw
  JATS rooted at ``<article>``; the Open Access endpoint wraps records
  in a ``<response>``/``<records>`` envelope. The sniff (§3) requires
  ``<article>`` at the root, so the OA path unwraps the single article
  via ``lxml`` before staging — the artifact on disk ends up
  structurally identical to a TDM-fetched one, and the JATS extractor
  (Step 10c) does not need to know which tier produced it.
- **Single-record assertion** on the TDM path uses an ``<article``
  byte-count after the sniff. The OA path uses ``lxml`` (already in
  hand for the unwrap). Both refuse zero or 2+ articles.
"""

import asyncio
import contextlib
import hashlib
import io
import re
import secrets
from importlib import metadata
from pathlib import Path
from typing import Any, ClassVar, Final, NoReturn, cast

import httpx
import structlog
from lxml import etree

from litspectraits.config import Settings
from litspectraits.errors import (
    AuthRejectedError,
    MissingCredentialError,
    NotOpenAccessError,
    PublisherAPIError,
    RateLimitExhaustedError,
)
from litspectraits.manifest import CrossRefMetadata, Format, Publisher, RetrievePayload
from litspectraits.retrievers._ratelimit import RateLimiter, _default_rate
from litspectraits.sniff import verify

_DIST_NAME: Final = 'springernature-api-client'
_TDM_BASE_URL: Final = 'https://spdi.public.springernature.app/xmldata/jats'
_OA_BASE_URL: Final = 'https://api.springernature.com/openaccess/jats'

# ``<article`` followed by ``>`` or whitespace — i.e. a real opening tag,
# not ``<article-title>`` or ``<article-meta>``.
_ARTICLE_OPEN_RE: Final = re.compile(rb'<article[\s>]')

# ``local-name()`` query: tolerates any namespace (or the default
# namespace) the Open Access endpoint chooses for ``<article>``.
_ARTICLE_XPATH: Final = './/*[local-name()="article"]'

_logger: Final = structlog.get_logger('litspectraits.retrievers.springer')


class SpringerRetriever:
    """Async retriever with TDM-then-OpenAccess credential dispatch.

    Parameters
    ----------
    rate_per_second : float, optional
        Token-bucket ceiling. Defaults to the §8 spec value (5.0).

    Notes
    -----
    Tier selection is purely a function of which key is set; the
    retriever does **not** fall back from TDM to OA on a 401/403. A loud
    failure is preferable to silently degrading the corpus.
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
        # ``meta`` is unused — we don't need bibliographic data on either tier.
        del meta
        tier = _select_tier(settings)
        if tier == _Tier.NONE:
            raise MissingCredentialError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint=(
                    'set SPRINGER_OA_API_KEY (Open Access tier) or '
                    'SPRINGER_TDM_API_KEY (premium TDM tier)'
                ),
            )
        await self._limiter.acquire()
        if tier == _Tier.TDM:
            return await self._fetch_tdm(
                doi=doi, tmp_dir=tmp_dir, api_key=_unwrap(settings.springer_tdm_api_key)
            )
        return await self._fetch_oa(
            doi=doi,
            tmp_dir=tmp_dir,
            client=client,
            api_key=_unwrap(settings.springer_oa_api_key),
        )

    # ------------------------------------------------------------------
    # TDM tier (premium Full-Text licence)
    # ------------------------------------------------------------------

    async def _fetch_tdm(self, *, doi: str, tmp_dir: Path, api_key: str) -> RetrievePayload:
        sdk = self._import_sdk(doi)
        body = await self._tdm_search(doi=doi, api_key=api_key, sdk=sdk)
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
                tier=_Tier.TDM,
            )
        sha256, byte_size = _hash_and_size(staged_path)
        _logger.info(
            'springer fetch ok',
            doi=doi,
            tier=_Tier.TDM,
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

    async def _tdm_search(self, *, doi: str, api_key: str, sdk: _SdkHandle) -> bytes:
        """Run the TDM SDK call, mapping its exceptions to ours.

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
                hint='SPRINGER_TDM_API_KEY rejected by SDK constructor',
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
                tier=_Tier.TDM,
            )
        if not response.strip():
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint='SDK returned empty body — DOI likely absent from corpus',
                tier=_Tier.TDM,
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
                tier=_Tier.TDM,
                hint=(
                    'SPRINGER_TDM_API_KEY rejected — a standard developer-portal '
                    'key does NOT work against the TDM endpoint; the TDM tier '
                    'needs a separate Full-Text licence from Springer Nature. '
                    'If you only hold a dev-portal key, unset SPRINGER_TDM_API_KEY '
                    'and rely on the SPRINGER_OA_API_KEY (Open Access) fallback.'
                ),
            ) from exc
        raise PublisherAPIError(
            doi=doi,
            publisher=Publisher.SPRINGER_NATURE.value,
            http_status=status_code,
            sdk_error=str(exc),
            tier=_Tier.TDM,
        ) from exc

    # ------------------------------------------------------------------
    # Open Access tier (dev-portal key)
    # ------------------------------------------------------------------

    async def _fetch_oa(
        self,
        *,
        doi: str,
        tmp_dir: Path,
        client: httpx.AsyncClient,
        api_key: str,
    ) -> RetrievePayload:
        envelope = await self._oa_get(doi=doi, client=client, api_key=api_key)
        article_bytes = self._unwrap_oa_article(doi=doi, envelope=envelope)
        staged_path = self._stage_body(tmp_dir=tmp_dir, body=article_bytes)
        verify(staged_path, expected=Format.JATS_XML, doi=doi)
        sha256, byte_size = _hash_and_size(staged_path)
        _logger.info(
            'springer fetch ok',
            doi=doi,
            tier=_Tier.OA,
            sha256=sha256,
            byte_size=byte_size,
        )
        return RetrievePayload(
            sha256=sha256,
            byte_size=byte_size,
            tmp_path=staged_path,
            format=Format.JATS_XML,
            fetched_url=f'{_OA_BASE_URL}?q=doi:{doi}',
            sdk_version=f'litspectraits-springer-oa {_resolve_self_version()}',
        )

    @staticmethod
    async def _oa_get(*, doi: str, client: httpx.AsyncClient, api_key: str) -> bytes:
        """``GET`` the Open Access JATS endpoint; translate errors to our taxonomy.

        Returns the raw envelope body as bytes. The endpoint wraps the
        record set in a ``<response>``/``<records>`` element, so the caller
        is responsible for extracting the single ``<article>`` subtree
        before staging (see :meth:`_unwrap_oa_article`).
        """
        # Query is a bare ``doi:<doi>`` term — same shape as the TDM tier.
        # NB: do *not* AND in an ``openaccess:true`` filter. Despite the
        # endpoint name, the ``openaccess:`` *filter operator* is itself a
        # premium feature: ``q=doi:<doi> openaccess:true`` is rejected with
        # 403 "Access to this resource is restricted. This is a premium
        # feature." The ``/openaccess/jats`` endpoint is already OA-scoped,
        # so the filter would be redundant even if it were allowed — a
        # non-OA DOI just comes back with zero records (→ NotOpenAccessError).
        params = {
            'q': f'doi:{doi}',
            'p': '1',
            's': '1',
            'api_key': api_key,
        }
        headers = {'Accept': 'application/xml'}
        try:
            response = await client.get(_OA_BASE_URL, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint='transport error against Springer Open Access API',
                tier=_Tier.OA,
                error=str(exc),
            ) from exc
        status = response.status_code
        if status in (401, 403):
            raise AuthRejectedError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                http_status=status,
                tier=_Tier.OA,
                hint='SPRINGER_OA_API_KEY rejected by the Open Access endpoint',
            )
        if status == 429:
            raise RateLimitExhaustedError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                http_status=status,
                tier=_Tier.OA,
                retry_after=response.headers.get('Retry-After'),
            )
        if status >= 400:
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                http_status=status,
                tier=_Tier.OA,
                body_prefix=response.text[:200],
            )
        return response.content

    @staticmethod
    def _unwrap_oa_article(*, doi: str, envelope: bytes) -> bytes:
        """Extract the single ``<article>`` subtree from an OA envelope.

        Parameters
        ----------
        doi : str
            DOI being fetched; attached to raised errors.
        envelope : bytes
            Raw body returned by the Open Access endpoint.

        Returns
        -------
        bytes
            A standalone JATS document rooted at ``<article>`` (with an
            XML declaration prepended) — structurally interchangeable
            with what the TDM endpoint serves, so the sniff and the
            downstream JATS extractor see one shape across both tiers.

        Raises
        ------
        NotOpenAccessError
            Envelope contains zero ``<article>`` elements — the DOI
            exists but is not open-access.
        PublisherAPIError
            Envelope failed to parse, or contains 2+ ``<article>``
            elements (defensive — a ``p=1, s=1`` query shouldn't).
        """
        try:
            root = etree.fromstring(envelope)
        except etree.XMLSyntaxError as exc:
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint='Open Access response did not parse as XML',
                tier=_Tier.OA,
                lxml_error=str(exc),
                body_prefix=envelope[:200].decode('utf-8', errors='replace'),
            ) from exc
        # ``.//*[local-name()='article']`` always yields a node-set, never a
        # scalar, so the cast is sound; lxml's union return type is the
        # conservative one for arbitrary xpath expressions. Mirrors the
        # convention in ``extract/_lxml_helpers.local_findall``.
        articles = cast(list[etree._Element], root.xpath(_ARTICLE_XPATH))
        if len(articles) == 0:
            raise NotOpenAccessError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                tier=_Tier.OA,
                hint=(
                    'Springer Open Access API returned zero records — the '
                    'DOI exists in the Springer Nature catalogue but is not '
                    'open-access. Recourse: set SPRINGER_TDM_API_KEY (premium '
                    'TDM licence required), or `litspectraits sideload <doi> '
                    '<pdf>` an institutionally-licensed copy.'
                ),
            )
        if len(articles) > 1:
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.SPRINGER_NATURE.value,
                hint='expected exactly one <article> element in OA response',
                articles_found=len(articles),
                tier=_Tier.OA,
            )
        # ``etree.tostring`` of a sub-element re-emits in-scope namespace
        # declarations on the serialized root, so the resulting document
        # is parseable on its own. We prepend an XML declaration so the
        # 4 KiB sniff window (`sniff.py` §3) sees ``<?xml`` at the head.
        article_xml = etree.tostring(articles[0], encoding='utf-8')
        return b'<?xml version="1.0" encoding="UTF-8"?>\n' + article_xml

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stage_body(*, tmp_dir: Path, body: bytes) -> Path:
        """Write ``body`` to a unique ``*.part`` file in ``tmp_dir``."""
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = tmp_dir / f'springer-{secrets.token_hex(8)}.xml.part'
        path.write_bytes(body)
        return path


# ---------------------------------------------------------------------------
# Tier selection
# ---------------------------------------------------------------------------


class _Tier:
    """Sentinel namespace for tier identity.

    A small string-constant bag rather than a real :mod:`enum`: the only
    consumers are this module's tier branch, the structured-logging
    fields, and the error-context dicts. All three want a stable string
    value (``'tdm'`` / ``'openaccess'``) without paying for ``StrEnum``
    membership machinery. The attributes *are* the values; there's no
    instance to wrap them.
    """

    TDM: Final = 'tdm'
    OA: Final = 'openaccess'
    NONE: Final = 'none'


def _select_tier(settings: Settings) -> str:
    """Return ``'tdm'``, ``'openaccess'``, or ``'none'`` based on which keys are set.

    TDM wins when both are present (strictly larger corpus). The OA
    tier is the fallback for operators who only hold a standard
    dev-portal key.
    """
    if settings.springer_tdm_api_key:
        return _Tier.TDM
    if settings.springer_oa_api_key:
        return _Tier.OA
    return _Tier.NONE


def _unwrap(value: str | None) -> str:
    """Narrow ``Optional[str]`` to ``str`` after a presence check has already passed.

    The tier-selection branch guarantees the relevant key is non-empty
    before we reach the per-tier fetcher; this helper preserves that
    invariant in the type system without spreading ``assert``s.
    """
    if value is None:  # pragma: no cover — defensive
        raise AssertionError('credential was None despite tier selection')
    return value


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


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


def _resolve_self_version() -> str:
    """Resolve our own package version for the OA tier's ``sdk_version`` slot.

    The OA path is direct-``httpx`` rather than SDK-mediated, so the
    natural identifier is our own version (mirroring the Elsevier
    retriever's choice).
    """
    try:
        return metadata.version('litspectraits')
    except metadata.PackageNotFoundError:  # pragma: no cover — defensive
        return 'unknown'


class _SdkHandle:
    """Bundle of TDM SDK symbols + version, populated once per fetch."""

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
