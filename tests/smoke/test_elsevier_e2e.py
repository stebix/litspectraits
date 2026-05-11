"""Gated end-to-end smoke test for the Elsevier retriever (``overview-v3.md`` §22).

DOI → CrossRef metadata → publisher dispatch → Elsevier ``view=FULL``
retrieve → magic-byte sniff → store + manifest, against a ``tmp_path``-
backed store. The retriever always requests ``view=FULL`` and raises
``EntitlementDowngradeError`` on a META_ABS payload, so the extra
``<originalText>`` assertion below is belt-and-braces: a META_ABS that
somehow slipped through would have no full-text subtree.

Skipped unless ``ELSEVIER_API_KEY`` is set (``ELSEVIER_INSTTOKEN`` is
optional — without it only OA-tier titles are entitled, which is exactly
what the OA smoke DOI is); deselected from the default run — see
``tests/smoke/conftest.py``.
"""

import pytest

from litspectraits._smoke_dois import SMOKE_DOI
from litspectraits.config import Settings
from litspectraits.manifest import Format, Publisher
from tests.smoke.conftest import assert_e2e_invariants, run_e2e_ingest

pytestmark = [pytest.mark.smoke, pytest.mark.requires_elsevier_creds]


async def test_elsevier_e2e(smoke_settings: Settings) -> None:
    record, store = await run_e2e_ingest(SMOKE_DOI[Publisher.ELSEVIER], settings=smoke_settings)
    xml = assert_e2e_invariants(
        record, store, publisher=Publisher.ELSEVIER, fmt=Format.ELSEVIER_XML, byte_floor=10_000
    )
    head = xml[:4096]
    # The live ScienceDirect API serves the body with no XML declaration —
    # it starts straight at the envelope root.
    assert head.lstrip().startswith(b'<full-text-retrieval-response')
    # Full-text subtree present → not an abstract-only META_ABS downgrade.
    assert b'originalText' in xml
