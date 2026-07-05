"""Gated end-to-end smoke for the MinerU-primary default path (``overview-v3.md`` §22).

The real validation the ``mineru-primary-promotion`` handoff §6 calls for: the
first time the actual ``vlm-engine`` ``middle.json`` / ``.md`` / ``images``
shape flows through our extractor + adapter end-to-end.

DOI → CrossRef → Wiley TDM retrieve → magic-byte sniff → store → **bare
``extract`` (defaults to ``mineru`` / ``vlm-engine``)** → **normalize**, against
a ``tmp_path``-backed store. Asserts the promotion invariants, not byte
equality — the VLM head is autoregressive, so exact formula/table strings
vary between runs (spot-checks stay structural / substring).

Heavy: needs the ``vlm`` weight family + likely a GPU, plus Wiley TDM access.
Skipped unless ``WILEY_TDM_TOKEN`` is set (or the host is on the Würzburg
egress allow-list); deselected from the default run — see
``tests/smoke/conftest.py``.
"""

import json

import pytest

from litspectraits._smoke_dois import SMOKE_DOI
from litspectraits.config import Settings
from litspectraits.extract import extract as run_extract
from litspectraits.manifest import Format, Publisher
from litspectraits.normalize import normalize_mineru_document
from litspectraits.normalize.models import EquationBlock, TableBlock
from tests.smoke.conftest import assert_e2e_invariants, run_e2e_ingest

pytestmark = [pytest.mark.smoke, pytest.mark.requires_wiley_creds]

# The reviewed reference is the Wiley MR-fingerprinting paper whose title the
# VLM parse recovers verbatim; a stable substring is a cheap "we parsed the
# right document" anchor that survives VLM run-to-run variance.
_TITLE_ANCHOR = 'fingerprinting'


async def test_mineru_vlm_engine_e2e(smoke_settings: Settings) -> None:
    doi = SMOKE_DOI[Publisher.WILEY]

    # 1. Ingest (real Wiley TDM PDF) + the shared §22 envelope invariants.
    record, store = await run_e2e_ingest(doi, settings=smoke_settings)
    pdf = assert_e2e_invariants(
        record, store, publisher=Publisher.WILEY, fmt=Format.PDF, byte_floor=50_000
    )
    assert pdf.startswith(b'%PDF-')

    # 2. Bare extract → the default backend is MinerU / vlm-engine.
    await run_extract(record, store, model_cache_dir=smoke_settings.docling_model_cache_dir)

    doc_dir = store.document_dir(record.sha256)
    meta = json.loads((doc_dir / 'meta.json').read_text(encoding='utf-8'))
    assert meta['backend_id'] == 'mineru'
    assert meta['pipeline']['engine'] == 'vlm-engine'
    # vlm-engine ignores effort, so the config view records it as null.
    assert meta['pipeline']['effort'] is None

    # Auxiliary human-QA outputs (promotion §5): document.md must land beside
    # the canonical document.json and be named in meta.aux_outputs.
    markdown_path = doc_dir / 'document.md'
    assert markdown_path.is_file()
    assert markdown_path.stat().st_size > 0
    assert meta['aux_outputs'].get('markdown') == 'document.md'
    # A 14-page relaxometry paper has figures; if any images were emitted the
    # meta must point at the dir and the dir must exist.
    if 'images_dir' in meta['aux_outputs']:
        assert (doc_dir / meta['aux_outputs']['images_dir']).is_dir()

    # 3. Normalize the verbatim vlm middle.json through the MinerU adapter.
    payload = json.loads((doc_dir / 'document.json').read_text(encoding='utf-8'))
    # The real vlm-engine stamps ``_backend='vlm'`` (≠ 'pipeline'); this is the
    # tag the adapter grades geometry off — assert it so a future MinerU
    # rename surfaces here rather than silently downgrading every fidelity.
    assert payload.get('_backend') == 'vlm'

    document = normalize_mineru_document(payload)
    assert document.route == 'mineru'
    assert document.title is not None
    assert _TITLE_ANCHOR in document.title.lower()

    # vlm-engine predicts geometry → every placed block is 'approximate' (or
    # honestly 'absent' when a bbox was unusable); crucially never 'exact',
    # which would mean _engine_fidelity mis-mapped the vlm tag.
    fidelities = {b.provenance.geometry_fidelity for b in document.blocks}
    assert 'exact' not in fidelities
    assert 'approximate' in fidelities

    # Formula + table recovery is the whole point of the promotion: the paper
    # carries display equations and relaxometry tables, so both block kinds
    # must survive the parse with non-empty content.
    assert document.completeness.has_equations
    equations = [b for b in document.blocks if isinstance(b, EquationBlock)]
    assert any(b.latex for b in equations)
    tables = [b for b in document.blocks if isinstance(b, TableBlock)]
    assert tables
    assert any(cell.text for table in tables for row in table.cells for cell in row)
