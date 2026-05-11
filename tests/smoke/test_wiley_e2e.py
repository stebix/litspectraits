"""Gated end-to-end smoke test for the Wiley TDM retriever (``overview-v3.md`` §22).

DOI → CrossRef metadata → publisher dispatch → ``wiley-tdm`` retrieve →
magic-byte sniff → store + manifest, against a ``tmp_path``-backed store.
Asserts envelope + plausible size + structural markers, not byte
equality — Wiley TDM rotates PDF metadata between fetches.

Skipped unless ``WILEY_TDM_TOKEN`` is set (or the host is on the
Würzburg egress allow-list and IP auth carries the request); deselected
from the default run — see ``tests/smoke/conftest.py``.
"""

import pytest

from litspectraits._smoke_dois import SMOKE_DOI
from litspectraits.config import Settings
from litspectraits.manifest import Format, Publisher
from tests.smoke.conftest import assert_e2e_invariants, run_e2e_ingest

pytestmark = [pytest.mark.smoke, pytest.mark.requires_wiley_creds]


async def test_wiley_e2e(smoke_settings: Settings) -> None:
    record, store = await run_e2e_ingest(SMOKE_DOI[Publisher.WILEY], settings=smoke_settings)
    pdf = assert_e2e_invariants(
        record, store, publisher=Publisher.WILEY, fmt=Format.PDF, byte_floor=50_000
    )
    assert pdf.startswith(b'%PDF-')
    assert b'%%EOF' in pdf[-1024:]
