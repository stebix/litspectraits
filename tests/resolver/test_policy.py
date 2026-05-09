"""Ranking policy tests."""

import pytest

from litspectraits.resolver.policy import (
    FIDELITY_FIRST,
    PUBLISHED_FIRST,
    credentials_from_settings,
)
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    SourceKind,
    Version,
)


def _avail(version: Version, format_: Format, access: Access = Access.OPEN) -> Availability:
    return Availability(
        source_kind=SourceKind.JATS_PMC,
        version=version,
        format=format_,
        access=access,
        url='http://example.com',
        media_type='',
    )


def test_published_first_prefers_published_pdf_over_preprint_jats() -> None:
    pub_pdf = _avail(Version.PUBLISHED, Format.PDF)
    pre_jats = _avail(Version.PREPRINT, Format.JATS)
    creds = credentials_from_settings(None)
    assert PUBLISHED_FIRST.permits(pub_pdf, available_credentials=creds)
    assert PUBLISHED_FIRST.permits(pre_jats, available_credentials=creds)
    assert PUBLISHED_FIRST.sort_key(pub_pdf) < PUBLISHED_FIRST.sort_key(pre_jats)


def test_fidelity_first_prefers_preprint_jats_over_published_pdf() -> None:
    pub_pdf = _avail(Version.PUBLISHED, Format.PDF)
    pre_jats = _avail(Version.PREPRINT, Format.JATS)
    assert FIDELITY_FIRST.sort_key(pre_jats) < FIDELITY_FIRST.sort_key(pub_pdf)


def test_published_first_orders_jats_above_pdf_within_same_version() -> None:
    pub_jats = _avail(Version.PUBLISHED, Format.JATS)
    pub_pdf = _avail(Version.PUBLISHED, Format.PDF)
    assert PUBLISHED_FIRST.sort_key(pub_jats) < PUBLISHED_FIRST.sort_key(pub_pdf)


def test_tdm_token_blocked_when_no_token_configured() -> None:
    tdm = _avail(Version.PUBLISHED, Format.JATS, access=Access.TDM_TOKEN)
    creds = credentials_from_settings(None)
    assert not PUBLISHED_FIRST.permits(tdm, available_credentials=creds)
    reason = PUBLISHED_FIRST.reason_excluded(tdm, available_credentials=creds)
    assert reason is not None
    assert 'auth_required' in reason


def test_tdm_token_unlocked_when_token_configured() -> None:
    tdm = _avail(Version.PUBLISHED, Format.JATS, access=Access.TDM_TOKEN)
    creds = credentials_from_settings('a-token')
    assert PUBLISHED_FIRST.permits(tdm, available_credentials=creds)


def test_excluded_versions_drops_preprints() -> None:
    from attrs import evolve

    no_preprints = evolve(PUBLISHED_FIRST, excluded_versions=frozenset({Version.PREPRINT}))
    pre = _avail(Version.PREPRINT, Format.JATS)
    creds = credentials_from_settings(None)
    assert not no_preprints.permits(pre, available_credentials=creds)
    reason = no_preprints.reason_excluded(pre, available_credentials=creds)
    assert reason is not None
    assert 'version_excluded' in reason


@pytest.mark.parametrize('policy', [PUBLISHED_FIRST, FIDELITY_FIRST])
def test_subscription_excluded_by_minimum_access(policy) -> None:
    sub = _avail(Version.PUBLISHED, Format.PDF, access=Access.SUBSCRIPTION)
    creds = credentials_from_settings('any-token')
    assert not policy.permits(sub, available_credentials=creds)
