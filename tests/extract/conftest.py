"""Shared fixtures for extract tests.

Two helpers worth highlighting:

- :func:`fake_docling` — monkeypatches
  ``litspectraits.extract.pdf._load_docling`` so tests never touch the real
  docling library. Identical pattern to the wiley / springer SDK fakes
  (``tests/retrievers/test_wiley.py``); keeps the unit tests fast and
  free of the 1-2 GB model download.
- :func:`synthetic_pdf` — writes minimal valid PDF bytes (``%PDF-`` magic
  + ``%%EOF`` trailer) to a per-test path. The bytes are sufficient for
  the ingest-side ``sniff.verify`` invariants; the fake docling converter
  ignores file content.

When the real-docling smoke story lands in step 10f, replace the
synthetic-bytes fixture with a generator that emits a structurally rich
PDF (one heading, one paragraph, one 2x2 table, per
``docs/extract-pdf-plan.md`` §10).
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any

import pytest

import litspectraits.extract.pdf as pdf_mod
from litspectraits.extract.pdf import _DoclingAdapter
from litspectraits.manifest import (
    AcquisitionRecord,
    CrossRefMetadata,
    Format,
    Publisher,
)
from litspectraits.store import ArtifactStore

# Minimal PDF body — magic prefix, one trivial object, the %%EOF trailer.
# Sized to satisfy the magic-byte sniff and any downstream byte-size
# assertions; not parseable by a real PDF reader and not intended to be.
_FAKE_PDF: bytes = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n'


class FakeStatus(Enum):
    """Stand-in for ``docling.datamodel.base_models.ConversionStatus``."""

    SUCCESS = auto()
    PARTIAL_SUCCESS = auto()
    FAILURE = auto()


@dataclass
class FakeTextItem:
    """Stand-in for ``docling`` text items.

    The extractor only reads ``.text`` and ``.label`` — anything else
    docling carries on these items (``prov``, ``self_ref``, …) is
    irrelevant for counting and is omitted here.
    """

    text: str
    label: str = 'text'


@dataclass
class FakeTableItem:
    """Stand-in for ``docling.TableItem`` — counted, never inspected."""


@dataclass
class FakePictureItem:
    """Stand-in for ``docling.PictureItem`` — counted, never inspected."""


@dataclass
class FakeDocument:
    """Stand-in for ``docling.DoclingDocument``.

    Carries only the four flat collections + the ``num_pages()`` method
    and ``export_to_dict()`` payload our extractor actually consumes.
    """

    texts: list[FakeTextItem] = field(default_factory=list)
    tables: list[FakeTableItem] = field(default_factory=list)
    pictures: list[FakePictureItem] = field(default_factory=list)
    n_pages: int = 1
    export_payload: dict[str, Any] | None = None
    export_exc: BaseException | None = None

    def num_pages(self) -> int:
        return self.n_pages

    def export_to_dict(self) -> dict[str, Any]:
        if self.export_exc is not None:
            raise self.export_exc
        if self.export_payload is not None:
            return self.export_payload
        return {
            'schema_name': 'fake-docling-document',
            'schema_version': '0.0',
            'n_text_items': len(self.texts),
            'n_table_items': len(self.tables),
            'n_picture_items': len(self.pictures),
        }


@dataclass
class FakeConvertResult:
    """Stand-in for ``DocumentConverter.convert()``'s return value."""

    status: FakeStatus
    document: FakeDocument | None = None
    errors: list[str] = field(default_factory=list)


class FakeConverter:
    """Mimics ``DocumentConverter.convert(path)``.

    Tests pre-register a result per artifact path. Calls record into
    ``calls`` so a test can assert ``convert`` was invoked exactly once
    against the expected file.
    """

    def __init__(self) -> None:
        self.results: dict[Path, FakeConvertResult] = {}
        self.calls: list[Path] = []

    def register(self, path: Path, result: FakeConvertResult) -> None:
        self.results[Path(path)] = result

    def convert(self, path: Any) -> FakeConvertResult:
        resolved = Path(path)
        self.calls.append(resolved)
        if resolved not in self.results:
            raise AssertionError(
                f'FakeConverter has no registered result for {resolved!r}; '
                f'known: {list(self.results)}'
            )
        return self.results[resolved]


@dataclass
class FakeDocling:
    """Bundle of test handles for one fake-docling installation.

    Returned by :func:`fake_docling`. Tests interact with ``converter``
    to register results; ``adapter`` is what ``_load_docling`` returns
    while the patch is active.
    """

    converter: FakeConverter
    adapter: _DoclingAdapter
    load_calls: list[str]


@pytest.fixture
def fake_docling(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDocling]:
    """Patch ``litspectraits.extract.pdf._load_docling`` for the test.

    Returns a :class:`FakeDocling` bundle; tests use
    ``fake_docling.converter.register(path, result)`` to script per-PDF
    behavior, then call ``extract_pdf`` (or the dispatch layer) and
    assert on the outcome.
    """
    converter = FakeConverter()
    adapter = _DoclingAdapter(
        converter=converter,
        status_cls=FakeStatus,
        version='2.0.0-fake',
        pipeline_view={
            'do_ocr': False,
            'do_table_structure': True,
            'table_mode': 'accurate',
            'table_structure_kind': 'docling_tableformer',
            'do_cell_matching': True,
            'do_formula_enrichment': True,
            'layout_model': 'docling_layout_egret_large',
            'document_timeout': 120.0,
            'force_backend_text': False,
            'device': 'cpu',
        },
    )
    load_calls: list[str] = []

    def _fake_load(*, doi: str, model_cache_dir: Path | None = None) -> _DoclingAdapter:
        del model_cache_dir  # the fake adapter ignores the weights location
        load_calls.append(doi)
        return adapter

    monkeypatch.setattr(pdf_mod, '_load_docling', _fake_load)
    yield FakeDocling(converter=converter, adapter=adapter, load_calls=load_calls)


@pytest.fixture
def synthetic_pdf_bytes() -> bytes:
    """Minimal valid PDF body (magic prefix + trivial object + ``%%EOF``).

    Sized to satisfy the magic-byte sniff. The fake docling converter
    ignores file content; replace with a structurally-rich generated PDF
    when the real-docling smoke tests land in step 10f.
    """
    return _FAKE_PDF


@pytest.fixture
def make_acquisition_record() -> Callable[..., AcquisitionRecord]:
    """Factory for a stored-PDF :class:`AcquisitionRecord`.

    Stages the PDF bytes inside the requested store and returns a record
    whose ``artifact_path`` points at the staged file. Defaults are tuned
    for the common happy-path test; override per-arg as needed.
    """

    def _make(
        *,
        store: ArtifactStore,
        body: bytes = _FAKE_PDF,
        doi: str = '10.1002/mrm.27973',
        sha256: str = 'a' * 64,
        fmt: Format = Format.PDF,
        publisher: Publisher = Publisher.WILEY,
        write_artifact: bool = True,
    ) -> AcquisitionRecord:
        relpath = store.artifact_relpath(sha256, fmt)
        if write_artifact:
            artifact_path = store.data_dir / relpath
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(body)
        meta = CrossRefMetadata(
            doi=doi,
            publisher_str='Wiley',
            title='Some Quantitative MRI Paper',
            authors=('Doe, Jane',),
            year=2024,
            type='journal-article',
            license=None,
        )
        return AcquisitionRecord(
            doi=doi,
            sha256=sha256,
            artifact_path=relpath,
            format=fmt,
            publisher=publisher,
            metadata=meta,
            fetched_url='https://api.wiley.com/onlinelibrary/tdm/v1/articles/' + doi,
            fetched_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
            fetcher_version='0.1.0',
            sdk_version='wiley-tdm 1.0.0',
            byte_size=len(body),
            origin='auto',
            manual_provenance=None,
        )

    return _make


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(data_dir=tmp_path)


def make_paragraph_text(*, length: int) -> str:
    """Build deterministic block text of approximately ``length`` chars.

    Used to push char_count above / below :data:`FLOOR_CHARS` in
    structural-sanity tests without committing a paragraph blob.
    """
    base = 'Quantitative magnetic resonance measurement of T1, T2, and PD. '
    repeats = (length // len(base)) + 1
    return (base * repeats)[:length]
