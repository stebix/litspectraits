"""Elsevier full-text retriever (``docs/overview-v3.md`` §7.3).

No SDK — ``elsapy`` is archived (last release 2019; read-only since
2025-01-13). We talk to the API directly with our shared
:class:`httpx.AsyncClient`. The full-text endpoint serves Elsevier's own
XML schema (``<full-text-retrieval-response>``); JATS is not an option
on this route.

The headline failure mode is silent abstract-only downgrade. Elsevier
returns the same envelope shape regardless of entitlement — what differs
is whether the ``<originalText>`` / ``<xocs:doc>`` subtree is present
inside ``<originalText>``. We always pass ``view=FULL`` and raise
:class:`~litspectraits.errors.EntitlementDowngradeError` when the
response lacks that subtree. Abstracts are corpus poison; never silently
accept them (§5).

The ``lxml`` entitlement check lives here, not in the orchestrator —
it's a publisher-specific concern.
"""

import hashlib
import secrets
from importlib import metadata
from pathlib import Path
from typing import ClassVar, Final

import httpx
import structlog
from lxml import etree

from litspectraits.config import Settings
from litspectraits.errors import (
    AuthRejectedError,
    EntitlementDowngradeError,
    MissingCredentialError,
    PublisherAPIError,
    RateLimitExhaustedError,
)
from litspectraits.manifest import CrossRefMetadata, Format, Publisher, RetrievePayload
from litspectraits.retrievers._ratelimit import RateLimiter, _default_rate
from litspectraits.sniff import verify

_BASE: Final = 'https://api.elsevier.com/content/article/doi'
_DIST_NAME: Final = 'litspectraits'  # we own this code path; tag with our version

# The full-text envelope's root has no namespace prefix in practice
# (Elsevier's docs document it as plain ``<full-text-retrieval-response>``);
# the ``originalText`` and ``xocs:doc`` subtrees live under the
# ``http://www.elsevier.com/xml/svapi/article/dtd`` and ``xocs`` namespaces
# respectively. ``local-name()`` queries side-step the namespace zoo.
_ORIGINAL_TEXT_XPATH: Final = './/*[local-name()="originalText"]'
_XOCS_DOC_XPATH: Final = './/*[local-name()="doc" and namespace-uri()="http://www.elsevier.com/xml/xocs/dtd"]'

_logger: Final = structlog.get_logger('litspectraits.retrievers.elsevier')


class ElsevierRetriever:
    """Direct ``httpx`` retriever for Elsevier full-text XML.

    Parameters
    ----------
    rate_per_second : float, optional
        Token-bucket ceiling. Defaults to the §8 spec value (6.0).
    """

    publisher: ClassVar[Publisher] = Publisher.ELSEVIER
    format: ClassVar[Format] = Format.ELSEVIER_XML

    def __init__(self, *, rate_per_second: float | None = None) -> None:
        rate = (
            rate_per_second
            if rate_per_second is not None
            else _default_rate(Publisher.ELSEVIER, 0.0)
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
        del meta
        if not settings.elsevier_api_key:
            raise MissingCredentialError(
                doi=doi,
                publisher=Publisher.ELSEVIER.value,
                hint='set ELSEVIER_API_KEY (no IP-only fallback for Elsevier)',
            )
        await self._limiter.acquire()
        body = await self._fetch_body(doi=doi, client=client, settings=settings)
        # Sniff first — catches the obvious "publisher served HTML / 5xx
        # body" cases before lxml is asked to parse them.
        staged_path = self._stage_body(tmp_dir=tmp_dir, body=body)
        verify(staged_path, expected=Format.ELSEVIER_XML, doi=doi)
        self._assert_full_text(doi=doi, body=body)
        sha256, byte_size = _hash_and_size(staged_path)
        sdk_version = _resolve_version()
        _logger.info(
            'elsevier fetch ok',
            doi=doi,
            sha256=sha256,
            byte_size=byte_size,
        )
        return RetrievePayload(
            sha256=sha256,
            byte_size=byte_size,
            tmp_path=staged_path,
            format=Format.ELSEVIER_XML,
            fetched_url=f'{_BASE}/{doi}?view=FULL',
            sdk_version=f'litspectraits-elsevier {sdk_version}',
        )

    async def _fetch_body(
        self, *, doi: str, client: httpx.AsyncClient, settings: Settings
    ) -> bytes:
        headers = {
            'X-ELS-APIKey': settings.elsevier_api_key or '',
            'Accept': 'text/xml',
        }
        if settings.elsevier_insttoken:
            headers['X-ELS-Insttoken'] = settings.elsevier_insttoken
        url = f'{_BASE}/{doi}'
        try:
            response = await client.get(url, params={'view': 'FULL'}, headers=headers)
        except httpx.HTTPError as exc:
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.ELSEVIER.value,
                hint='transport error',
                error=str(exc),
            ) from exc
        status = response.status_code
        if status == 401:
            raise AuthRejectedError(
                doi=doi,
                publisher=Publisher.ELSEVIER.value,
                http_status=status,
                hint='ELSEVIER_API_KEY rejected',
            )
        if status == 403:
            raise AuthRejectedError(
                doi=doi,
                publisher=Publisher.ELSEVIER.value,
                http_status=status,
                hint='forbidden — IP not allowlisted or insttoken missing',
            )
        if status == 429:
            raise RateLimitExhaustedError(
                doi=doi,
                publisher=Publisher.ELSEVIER.value,
                http_status=status,
                retry_after=response.headers.get('Retry-After'),
            )
        if status >= 400:
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.ELSEVIER.value,
                http_status=status,
                body_prefix=response.text[:200],
            )
        return response.content

    @staticmethod
    def _stage_body(*, tmp_dir: Path, body: bytes) -> Path:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = tmp_dir / f'elsevier-{secrets.token_hex(8)}.xml.part'
        path.write_bytes(body)
        return path

    @staticmethod
    def _assert_full_text(*, doi: str, body: bytes) -> None:
        """Raise if the response is the abstract-only envelope.

        The full-text envelope carries an ``<originalText>`` element and
        usually an ``<xocs:doc>`` subtree under it. The abstract-only
        downgrade returns the same root element (``<full-text-retrieval-
        response>``) without that subtree — only ``<coredata>`` plus
        ``<dc:description>``. We treat absence of *either* full-text
        marker as a downgrade.
        """
        try:
            root = etree.fromstring(body)
        except etree.XMLSyntaxError as exc:
            raise PublisherAPIError(
                doi=doi,
                publisher=Publisher.ELSEVIER.value,
                hint='response did not parse as XML',
                lxml_error=str(exc),
            ) from exc
        original_text = root.xpath(_ORIGINAL_TEXT_XPATH)
        xocs_doc = root.xpath(_XOCS_DOC_XPATH)
        if not original_text and not xocs_doc:
            raise EntitlementDowngradeError(
                doi=doi,
                publisher=Publisher.ELSEVIER.value,
                hint=(
                    'response lacks <originalText> and <xocs:doc> — Elsevier '
                    'silently downgraded to the META_ABS abstract envelope'
                ),
            )


def _hash_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as fp:
        while chunk := fp.read(64 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _resolve_version() -> str:
    try:
        return metadata.version(_DIST_NAME)
    except metadata.PackageNotFoundError:  # pragma: no cover — defensive
        return 'unknown'
