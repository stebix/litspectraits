"""Tests for :mod:`litspectraits.retrievers.dispatch`."""

import pytest

from litspectraits.manifest import Format, Publisher
from litspectraits.retrievers.base import Retriever
from litspectraits.retrievers.dispatch import _BUILDERS, reset_cache, retriever_for
from litspectraits.retrievers.elsevier import ElsevierRetriever
from litspectraits.retrievers.springer import SpringerRetriever
from litspectraits.retrievers.wiley import WileyRetriever


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    reset_cache()


@pytest.mark.parametrize(
    ('publisher', 'cls', 'fmt'),
    [
        (Publisher.WILEY, WileyRetriever, Format.PDF),
        (Publisher.SPRINGER_NATURE, SpringerRetriever, Format.JATS_XML),
        (Publisher.ELSEVIER, ElsevierRetriever, Format.ELSEVIER_XML),
    ],
)
def test_dispatch_returns_expected_retriever(
    publisher: Publisher, cls: type, fmt: Format
) -> None:
    instance = retriever_for(publisher)
    assert isinstance(instance, cls)
    assert instance.publisher is publisher
    assert instance.format is fmt


def test_dispatch_caches_instances() -> None:
    first = retriever_for(Publisher.WILEY)
    second = retriever_for(Publisher.WILEY)
    assert first is second


def test_table_covers_every_publisher() -> None:
    # If a future Publisher value lands without a builder, this fires
    # before runtime would surface UnsupportedPublisherError downstream.
    assert set(_BUILDERS.keys()) == set(Publisher)


def test_returned_retriever_satisfies_protocol() -> None:
    instance = retriever_for(Publisher.ELSEVIER)
    # ``Retriever`` is ``runtime_checkable``; this guards against future
    # protocol drift (a method renamed without updating the impls).
    assert isinstance(instance, Retriever)
