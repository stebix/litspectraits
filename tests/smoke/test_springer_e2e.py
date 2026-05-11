"""Gated end-to-end smoke test for the Springer Nature retriever (``overview-v3.md`` §22).

DOI → CrossRef metadata → publisher dispatch → Springer retrieve (TDM
tier when ``SPRINGER_TDM_API_KEY`` is set, otherwise the Open Access
tier) → magic-byte sniff → store + manifest, against a ``tmp_path``-
backed store. The artifact lands rooted at ``<article>`` regardless of
tier — the OA path unwraps the ``<response>``/``<records>`` envelope
before staging — so the assertions below do not branch on which tier ran.

Skipped unless one of ``SPRINGER_OA_API_KEY`` / ``SPRINGER_TDM_API_KEY``
is set; deselected from the default run — see ``tests/smoke/conftest.py``.
A ``NotOpenAccessError`` here (OA tier, non-OA DOI) means the smoke DOI
has drifted out of open access — refresh the constant in
``litspectraits._smoke_dois`` before suspecting the code.
"""

import pytest

from litspectraits._smoke_dois import SMOKE_DOI
from litspectraits.config import Settings
from litspectraits.manifest import Format, Publisher
from tests.smoke.conftest import assert_e2e_invariants, run_e2e_ingest

pytestmark = [pytest.mark.smoke, pytest.mark.requires_springer_creds]


async def test_springer_e2e(smoke_settings: Settings) -> None:
    record, store = await run_e2e_ingest(
        SMOKE_DOI[Publisher.SPRINGER_NATURE], settings=smoke_settings
    )
    xml = assert_e2e_invariants(
        record, store, publisher=Publisher.SPRINGER_NATURE, fmt=Format.JATS_XML, byte_floor=5_000
    )
    head = xml[:4096]
    assert head.lstrip().startswith(b'<')  # markup, not JSON / an error blob / empty body
    # JATS root element, bare or namespaced — the XML declaration is optional
    # (and the TDM tier happens to include one while the OA-unwrap path adds it).
    assert b'<article' in head or b'<jats:article' in head
