"""Tests for the error taxonomies in :mod:`litspectraits.errors`.

The ingest tree is already exercised end-to-end by retriever / store /
ingest tests. This file focuses on the contracts that those higher-level
tests trust implicitly:

- Every error class is instantiable with the DOI + context keyword shape.
- ``str(exc)`` is useful even when no explicit message is passed (so a
  stray ``logger.exception`` produces a readable line before the CLI's
  Rich panel kicks in).
- The two trees (:class:`IngestError`, :class:`ExtractError`) are
  inheritance-disjoint, so a CLI ``except IngestError`` block cannot
  accidentally catch an extract failure.
"""

import pytest

from litspectraits.errors import (
    AuthRejectedError,
    DoclingConversionError,
    DoclingDegradedError,
    DoclingImportError,
    DOINotFoundError,
    EmptyDocumentError,
    EntitlementDowngradeError,
    ExtractError,
    ExtractIntegrityError,
    IngestError,
    IntegrityError,
    MalformedArtifactError,
    MissingArtifactError,
    MissingCredentialError,
    ParseDegradedError,
    PublisherAPIError,
    RateLimitExhaustedError,
    SerializationError,
    UnsupportedPublisherError,
    WrongFormatForExtractorError,
)

_DOI = '10.1002/mrm.27973'

_EXTRACT_CLASSES = [
    DoclingImportError,
    WrongFormatForExtractorError,
    MissingArtifactError,
    DoclingConversionError,
    DoclingDegradedError,
    EmptyDocumentError,
    ParseDegradedError,
    SerializationError,
    ExtractIntegrityError,
]

_INGEST_CLASSES = [
    DOINotFoundError,
    UnsupportedPublisherError,
    MissingCredentialError,
    AuthRejectedError,
    EntitlementDowngradeError,
    RateLimitExhaustedError,
    PublisherAPIError,
    MalformedArtifactError,
    IntegrityError,
]


@pytest.mark.parametrize('cls', _EXTRACT_CLASSES)
def test_extract_error_subclass_instantiates_with_doi(cls: type[ExtractError]) -> None:
    exc = cls(doi=_DOI)
    assert exc.doi == _DOI
    assert exc.context == {}
    assert cls.__name__ in str(exc)
    assert _DOI in str(exc)


@pytest.mark.parametrize('cls', _EXTRACT_CLASSES)
def test_extract_error_subclass_preserves_context(cls: type[ExtractError]) -> None:
    exc = cls(doi=_DOI, extractor='docling', stage='convert')
    assert exc.context == {'extractor': 'docling', 'stage': 'convert'}
    rendered = str(exc)
    assert "extractor='docling'" in rendered
    assert "stage='convert'" in rendered


@pytest.mark.parametrize('cls', _EXTRACT_CLASSES)
def test_extract_error_subclass_honors_explicit_message(cls: type[ExtractError]) -> None:
    exc = cls('explicit', doi=_DOI, extractor='docling')
    assert str(exc) == 'explicit'
    assert exc.context == {'extractor': 'docling'}


@pytest.mark.parametrize('cls', _EXTRACT_CLASSES)
def test_extract_error_subclass_inherits_from_extracterror(cls: type[ExtractError]) -> None:
    exc = cls(doi=_DOI)
    assert isinstance(exc, ExtractError)


@pytest.mark.parametrize('cls', _EXTRACT_CLASSES)
def test_extract_error_subclass_is_not_an_ingest_error(cls: type[ExtractError]) -> None:
    """The two trees must stay inheritance-disjoint.

    A CLI ``except IngestError`` block at the ingest pipeline boundary
    must not swallow an extract-side failure, and vice versa. This pins
    that contract so a future refactor that promotes a shared base would
    fail the test instead of silently widening the catch.
    """
    exc = cls(doi=_DOI)
    assert not isinstance(exc, IngestError)


@pytest.mark.parametrize('cls', _INGEST_CLASSES)
def test_ingest_error_subclass_is_not_an_extract_error(cls: type[IngestError]) -> None:
    exc = cls(doi=_DOI)
    assert not isinstance(exc, ExtractError)
