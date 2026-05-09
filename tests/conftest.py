"""Shared pytest fixtures."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from litspectraits.acquisition.store import ArtifactStore
from litspectraits.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        contact_email='test@example.com',
        data_dir=tmp_path,
        crossref_tdm_token=None,
        http_timeout_s=5.0,
        log_format='json',
    )


@pytest.fixture
def settings_with_tdm(tmp_path: Path) -> Settings:
    return Settings(
        contact_email='test@example.com',
        data_dir=tmp_path,
        crossref_tdm_token='dummy-token',
        http_timeout_s=5.0,
        log_format='json',
    )


@pytest.fixture
def store(settings: Settings) -> Iterator[ArtifactStore]:
    yield ArtifactStore(settings.data_dir)
