"""Shared pytest fixtures."""

from pathlib import Path

import pytest

from litspectraits.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Test ``Settings`` with all publisher credentials unset.

    Use this for tests that exercise behavior under the
    "no credentials configured" path — every retriever should raise
    :class:`~litspectraits.errors.MissingCredentialError` against this
    fixture.
    """
    return Settings(
        contact_email='test@example.com',
        data_dir=tmp_path,
        docling_model_cache_dir=None,
        mineru_model_cache_dir=None,
        http_timeout_s=5.0,
        log_format='json',
        log_level='warning',
        wiley_tdm_token=None,
        springer_oa_api_key=None,
        springer_tdm_api_key=None,
        elsevier_api_key=None,
        elsevier_insttoken=None,
        rate_limit_wiley=3.0,
        rate_limit_springer=5.0,
        rate_limit_elsevier=6.0,
        expected_egress_cidrs=(),
    )


@pytest.fixture
def settings_with_creds(tmp_path: Path) -> Settings:
    """Test ``Settings`` with dummy publisher credentials populated.

    Use this for tests that need credentials present (so the retriever
    proceeds to the network call) but should not actually hit a publisher
    — pair with ``respx`` mocks or SDK monkeypatches.

    Both Springer keys are populated so the retriever's tier dispatch
    picks the TDM path — that matches what the legacy Springer tests in
    ``tests/retrievers/test_springer.py`` exercise. Tests that want to
    cover the Open Access tier specifically should use the
    :func:`settings_with_oa_creds` fixture below (TDM key intentionally
    blank so the tier branch falls through to OA).
    """
    return Settings(
        contact_email='test@example.com',
        data_dir=tmp_path,
        docling_model_cache_dir=None,
        mineru_model_cache_dir=None,
        http_timeout_s=5.0,
        log_format='json',
        log_level='warning',
        wiley_tdm_token='dummy-wiley-token',
        springer_oa_api_key='dummy-springer-key',
        springer_tdm_api_key='dummy-springer-tdm-key',
        elsevier_api_key='dummy-elsevier-key',
        elsevier_insttoken='dummy-elsevier-insttoken',
        rate_limit_wiley=3.0,
        rate_limit_springer=5.0,
        rate_limit_elsevier=6.0,
        expected_egress_cidrs=(),
    )


@pytest.fixture
def settings_with_oa_creds(tmp_path: Path) -> Settings:
    """Test ``Settings`` with the Springer **Open Access tier only** configured.

    Mirrors :func:`settings_with_creds` but with
    ``springer_tdm_api_key=None`` so the retriever's tier dispatch picks
    the Open Access path (``api.springernature.com/openaccess/jats``).
    Used by the OA-tier slice of ``test_springer.py``.
    """
    return Settings(
        contact_email='test@example.com',
        data_dir=tmp_path,
        docling_model_cache_dir=None,
        mineru_model_cache_dir=None,
        http_timeout_s=5.0,
        log_format='json',
        log_level='warning',
        wiley_tdm_token='dummy-wiley-token',
        springer_oa_api_key='dummy-springer-oa-key',
        springer_tdm_api_key=None,
        elsevier_api_key='dummy-elsevier-key',
        elsevier_insttoken='dummy-elsevier-insttoken',
        rate_limit_wiley=3.0,
        rate_limit_springer=5.0,
        rate_limit_elsevier=6.0,
        expected_egress_cidrs=(),
    )
