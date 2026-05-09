"""Resolver orchestration tests.

Use stub probes to drive the orchestrator deterministically — the live
probes are exercised in their own respx-backed test files.
"""

from collections.abc import Iterator

import pytest
import respx

from litspectraits.acquisition.store import ArtifactStore
from litspectraits.config import Settings
from litspectraits.resolver.policy import (
    FIDELITY_FIRST,
    PUBLISHED_FIRST,
    credentials_from_settings,
)
from litspectraits.resolver.probes.base import ProbeContext
from litspectraits.resolver.resolver import resolve
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    ProbeOutcome,
    SourceKind,
    Version,
)


def _avail(
    *,
    source_kind: SourceKind,
    version: Version,
    fmt: Format,
    access: Access = Access.OPEN,
    url: str = 'https://example.com',
) -> Availability:
    return Availability(
        source_kind=source_kind,
        version=version,
        format=fmt,
        access=access,
        url=url,
        media_type='',
    )


class _StubProbe:
    def __init__(self, name: str, availabilities: tuple[Availability, ...]) -> None:
        self.name = name
        self._availabilities = availabilities

    async def run(self, ctx: ProbeContext) -> ProbeOutcome:
        return ProbeOutcome(probe=self.name, availabilities=self._availabilities)


@pytest.fixture(autouse=True)
def _silence_crossref() -> Iterator[None]:
    """Make sure crossref calls in the resolver never escape to the network."""
    with respx.mock(assert_all_called=False):
        respx.get(url__startswith='https://api.crossref.org/works/').mock(
            return_value=respx.MockResponse(404),
        )
        yield


async def test_published_first_picks_published_pdf_over_preprint_jats(
    settings: Settings, store: ArtifactStore
) -> None:
    pub_pdf = _avail(
        source_kind=SourceKind.PDF_UNPAYWALL,
        version=Version.PUBLISHED,
        fmt=Format.PDF,
        url='https://pub.example.com/paper.pdf',
    )
    pre_jats = _avail(
        source_kind=SourceKind.JATS_BIORXIV,
        version=Version.PREPRINT,
        fmt=Format.JATS,
        url='https://biorxiv.example.com/paper.xml',
    )
    probes = (
        _StubProbe('unpaywall', (pub_pdf,)),
        _StubProbe('biorxiv', (pre_jats,)),
    )

    result = await resolve(
        '10.1002/mrm.27973',
        settings=settings,
        store=store,
        policy=PUBLISHED_FIRST,
        probes=probes,
    )

    assert result.chosen is not None
    assert result.chosen.source_kind is SourceKind.PDF_UNPAYWALL
    assert result.chosen.version is Version.PUBLISHED
    assert result.candidates[0] is result.chosen


async def test_fidelity_first_inverts_preference(settings: Settings, store: ArtifactStore) -> None:
    pub_pdf = _avail(
        source_kind=SourceKind.PDF_UNPAYWALL,
        version=Version.PUBLISHED,
        fmt=Format.PDF,
    )
    pre_jats = _avail(
        source_kind=SourceKind.JATS_BIORXIV,
        version=Version.PREPRINT,
        fmt=Format.JATS,
    )
    probes = (
        _StubProbe('unpaywall', (pub_pdf,)),
        _StubProbe('biorxiv', (pre_jats,)),
    )

    result = await resolve(
        '10.1002/mrm.27973',
        settings=settings,
        store=store,
        policy=FIDELITY_FIRST,
        probes=probes,
    )

    assert result.chosen is not None
    assert result.chosen.source_kind is SourceKind.JATS_BIORXIV
    assert result.chosen.format is Format.JATS


async def test_tdm_token_blocked_falls_through_to_open_pdf(
    settings: Settings, store: ArtifactStore
) -> None:
    creds = credentials_from_settings(settings.crossref_tdm_token)
    assert Access.TDM_TOKEN not in creds  # sanity: fixture has no token

    tdm_jats = _avail(
        source_kind=SourceKind.JATS_CROSSREF_TDM,
        version=Version.PUBLISHED,
        fmt=Format.JATS,
        access=Access.TDM_TOKEN,
    )
    open_pdf = _avail(
        source_kind=SourceKind.PDF_UNPAYWALL,
        version=Version.PREPRINT,
        fmt=Format.PDF,
    )
    probes = (
        _StubProbe('crossref_tdm', (tdm_jats,)),
        _StubProbe('unpaywall', (open_pdf,)),
    )

    result = await resolve(
        '10.1002/mrm.27973',
        settings=settings,
        store=store,
        policy=PUBLISHED_FIRST,
        probes=probes,
    )

    assert result.chosen is not None
    assert result.chosen.source_kind is SourceKind.PDF_UNPAYWALL
    excluded_kinds = {a.source_kind for a, _ in result.excluded}
    assert SourceKind.JATS_CROSSREF_TDM in excluded_kinds


async def test_no_sources_yields_chosen_none(settings: Settings, store: ArtifactStore) -> None:
    result = await resolve(
        '10.1002/mrm.27973',
        settings=settings,
        store=store,
        policy=PUBLISHED_FIRST,
        probes=(_StubProbe('empty', ()),),
    )
    assert result.chosen is None
    assert result.candidates == ()
