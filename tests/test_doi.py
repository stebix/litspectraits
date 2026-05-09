"""DOI normalization tests."""

import pytest

from litspectraits.doi import InvalidDOIError, normalize


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('10.1002/MRM.27973', '10.1002/mrm.27973'),
        ('https://doi.org/10.1002/mrm.27973', '10.1002/mrm.27973'),
        ('https://dx.doi.org/10.1002/MRM.27973', '10.1002/mrm.27973'),
        ('doi:10.1101/2023.05.01.539123', '10.1101/2023.05.01.539123'),
        ('  10.48550/arXiv.2401.12345  ', '10.48550/arxiv.2401.12345'),
    ],
)
def test_normalize_strips_prefixes_and_lowercases(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize('raw', ['', 'not a doi', 'https://example.com/foo', '10.1234'])
def test_normalize_rejects_garbage(raw: str) -> None:
    with pytest.raises(InvalidDOIError):
        normalize(raw)
