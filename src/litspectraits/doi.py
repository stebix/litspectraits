"""DOI normalization.

DOIs are case-insensitive per the registry spec; we lowercase the entire
identifier and strip ``https://doi.org/`` / ``doi:`` prefixes so downstream
keys are stable.
"""

import re
from typing import Final

_DOI_URL_PREFIX_RE: Final = re.compile(r'^(?:https?://)?(?:dx\.)?doi\.org/', flags=re.IGNORECASE)
_DOI_PREFIX_RE: Final = re.compile(r'^doi:', flags=re.IGNORECASE)
_DOI_SHAPE_RE: Final = re.compile(r'^10\.\d{4,9}/\S+$')


class InvalidDOIError(ValueError):
    """Raised when a string cannot be normalized into a valid DOI."""


def normalize(raw: str) -> str:
    """Normalize a DOI string.

    Strips ``https://doi.org/`` and ``doi:`` prefixes, removes surrounding
    whitespace, and lowercases the entire identifier.

    Parameters
    ----------
    raw : str
        Raw input — full URL, ``doi:`` prefixed, or bare DOI.

    Returns
    -------
    str
        Normalized DOI.

    Raises
    ------
    InvalidDOIError
        If the input does not match the basic DOI shape ``10.NNNN/...``.
    """
    if not raw:
        raise InvalidDOIError('empty DOI')
    s = raw.strip()
    s = _DOI_URL_PREFIX_RE.sub('', s)
    s = _DOI_PREFIX_RE.sub('', s)
    s = s.lower()
    if not _DOI_SHAPE_RE.match(s):
        raise InvalidDOIError(f'not a valid DOI: {raw!r}')
    return s
