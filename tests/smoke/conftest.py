"""Fixtures, credential gating, and shared assertions for the smoke suite.

The smoke tests (``overview-v3.md`` §22) drive the full v3 happy path —
``fetch_metadata`` → publisher dispatch → real-network retrieve →
magic-byte sniff → store + manifest — against an ephemeral, ``tmp_path``-
backed store. They are the only test layer that catches publisher-side
surprises (API drift, response-shape changes, expired tokens, IP
de-allowlisting), and the only one that touches the network.

They are *deselected* from the default ``uv run pytest`` via the
``addopts = -m "not smoke"`` line in ``pyproject.toml``; run them on
demand with::

    uv run pytest -m smoke

Each test is additionally gated on its publisher's credential: a test
marked ``requires_<publisher>_creds`` is *skipped* (not failed) when none
of that publisher's credential env vars is set, so an operator who holds
only some publishers' tokens still gets signal for the ones they can
reach. The project ``.env`` is loaded on demand (the same file the CLI
reads — see :func:`litspectraits.cli._startup`) so ``uv run pytest -m
smoke`` works without exporting anything first; ``dotenv.load_dotenv``
never overwrites a variable already present in the environment.
"""

import json
import os
from pathlib import Path

import attrs
import dotenv
import pytest

from litspectraits.config import Settings
from litspectraits.http import http_client
from litspectraits.ingest import ingest
from litspectraits.manifest import AcquisitionRecord, Format, Publisher
from litspectraits.store import ArtifactStore

# ``requires_<publisher>_creds`` marker → the credential env vars that satisfy
# it. Any one present is enough; Springer has two tiers (OA dev-portal key vs.
# premium TDM licence) and the retriever picks whichever is configured.
_CREDENTIAL_MARKERS: dict[str, tuple[str, ...]] = {
    'requires_wiley_creds': ('WILEY_TDM_TOKEN',),
    'requires_springer_creds': ('SPRINGER_OA_API_KEY', 'SPRINGER_TDM_API_KEY'),
    'requires_elsevier_creds': ('ELSEVIER_API_KEY',),
}

# ``Format`` → the ``artifacts/`` subtree the store writes that format under
# (``overview-v3.md`` §3, §22 invariant table). Asserted against
# ``record.artifact_path`` so a misrouted format fails loudly.
_ARTIFACT_PREFIX: dict[Format, str] = {
    Format.PDF: 'artifacts/pdf/sha256/',
    Format.JATS_XML: 'artifacts/jats/sha256/',
    Format.ELSEVIER_XML: 'artifacts/elsevier/sha256/',
}


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Load the project ``.env`` and skip smoke tests lacking their credential.

    Scoped to ``tests/smoke/`` — deselected items never reach this hook,
    so a default ``uv run pytest`` mutates nothing. For each
    ``requires_<publisher>_creds`` marker on the item, skip when none of
    that publisher's credential env vars resolves to a non-empty value.
    """
    dotenv.load_dotenv()
    for marker_name, env_vars in _CREDENTIAL_MARKERS.items():
        if item.get_closest_marker(marker_name) is None:
            continue
        if not any(os.environ.get(name) for name in env_vars):
            pytest.skip(f'{marker_name}: set one of {", ".join(env_vars)} to run')


@pytest.fixture
def smoke_settings(tmp_path: Path) -> Settings:
    """Real, env-derived :class:`Settings` with ``data_dir`` pinned to ``tmp_path``.

    Everything except ``data_dir`` comes from the operator's environment
    (and the ``.env`` loaded in :func:`pytest_runtest_setup`): contact
    email, publisher tokens, rate-limit overrides. ``data_dir`` is
    redirected to pytest's per-test tempdir so the smoke run never
    touches the operator's real corpus; pytest's normal tempdir cleanup
    handles teardown.
    """
    return attrs.evolve(Settings.from_env(), data_dir=tmp_path)


async def run_e2e_ingest(
    doi: str, *, settings: Settings
) -> tuple[AcquisitionRecord, ArtifactStore]:
    """Ingest ``doi`` end-to-end against a fresh store under ``settings.data_dir``.

    Mirrors :func:`litspectraits.cli._run_ingest`'s core: build the
    store, open a polite-pool HTTP client, run :func:`litspectraits.ingest.ingest`.
    Returns both so the caller can assert on the store as well as the
    record.
    """
    store = ArtifactStore(settings.data_dir)
    async with http_client(settings) as client:
        record = await ingest(doi, settings=settings, store=store, client=client)
    return record, store


def assert_e2e_invariants(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    publisher: Publisher,
    fmt: Format,
    byte_floor: int,
) -> bytes:
    """Assert the §22 cross-publisher invariants; return the artifact bytes.

    Covers everything the per-publisher invariant table shares: the
    record's publisher matches the dispatch table, the format and a
    plausible byte floor, the on-disk artifact lives under the right
    ``artifacts/`` subtree and is exactly ``byte_size`` bytes, the
    manifest is present and re-readable, and ``index/by_doi.jsonl``
    carries a row keyed to this ``(doi, sha256)`` with the right
    ``format`` column. The caller adds the format-specific
    structural-marker checks (PDF magic, JATS / Elsevier root element) on
    the returned bytes.
    """
    assert record.publisher is publisher
    assert record.format is fmt
    assert record.byte_size >= byte_floor

    assert record.artifact_path.startswith(_ARTIFACT_PREFIX[fmt])
    artifact = store.data_dir / record.artifact_path
    assert artifact.is_file()
    data = artifact.read_bytes()
    assert len(data) == record.byte_size

    assert store.manifest_path(record.sha256).is_file()
    assert store.read_manifest(record.sha256).sha256 == record.sha256

    index_rows = [
        json.loads(line)
        for line in store.index_path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]
    assert any(
        row['doi'] == record.doi and row['sha256'] == record.sha256 and row['format'] == fmt.value
        for row in index_rows
    )
    return data
