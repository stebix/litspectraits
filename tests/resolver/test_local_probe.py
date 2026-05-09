"""Local-store probe — surfaces sideloaded artifacts back into the resolver."""

from pathlib import Path

import httpx

from litspectraits.acquisition.sideload import sideload
from litspectraits.acquisition.store import ArtifactStore
from litspectraits.config import Settings
from litspectraits.resolver.probes.base import ProbeContext
from litspectraits.resolver.probes.local import LocalStoreProbe
from litspectraits.resolver.types import Format, SourceKind, Version


async def test_local_probe_surfaces_sideloaded_artifact(
    settings: Settings, store: ArtifactStore, tmp_path: Path
) -> None:
    src = tmp_path / 'paper.pdf'
    src.write_bytes(b'%PDF-1.4\n' + b'X' * 1024 + b'\n%%EOF\n')
    sideload(
        doi='10.1002/mrm.27973',
        path=src,
        version=Version.PUBLISHED,
        format=Format.PDF,
        license_assertion='cc-by',
        note='test',
        source_url=None,
        settings=settings,
        store=store,
    )

    async with httpx.AsyncClient() as client:
        ctx = ProbeContext(
            doi='10.1002/mrm.27973',
            settings=settings,
            client=client,
            crossref_metadata=None,
            store=store,
        )
        outcome = await LocalStoreProbe().run(ctx)

    assert outcome.error is None
    assert len(outcome.availabilities) == 1
    avail = outcome.availabilities[0]
    assert avail.source_kind is SourceKind.LOCAL_CACHE
    assert avail.version is Version.PUBLISHED
    assert avail.format is Format.PDF
    assert avail.url.startswith('file://')


async def test_local_probe_returns_empty_when_store_is_none(settings: Settings) -> None:
    async with httpx.AsyncClient() as client:
        ctx = ProbeContext(
            doi='10.1002/mrm.27973',
            settings=settings,
            client=client,
            crossref_metadata=None,
            store=None,
        )
        outcome = await LocalStoreProbe().run(ctx)
    assert outcome.availabilities == ()
