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
        http_timeout_s=5.0,
        log_format='json',
        wiley_tdm_token=None,
        springer_api_key=None,
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
    """
    return Settings(
        contact_email='test@example.com',
        data_dir=tmp_path,
        http_timeout_s=5.0,
        log_format='json',
        wiley_tdm_token='dummy-wiley-token',
        springer_api_key='dummy-springer-key',
        elsevier_api_key='dummy-elsevier-key',
        elsevier_insttoken='dummy-elsevier-insttoken',
        rate_limit_wiley=3.0,
        rate_limit_springer=5.0,
        rate_limit_elsevier=6.0,
        expected_egress_cidrs=(),
    )
