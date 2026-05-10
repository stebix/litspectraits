"""Magic-byte sniffing for fetched and sideloaded artifacts.

Both the streamed-acquire path (:mod:`litspectraits.acquisition.fetch`) and
the manual-sideload path (:mod:`litspectraits.acquisition.sideload`) need to
confirm that bytes-on-the-wire plausibly represent the format the resolver
asserted. They share this single sniffer so a paywall HTML page served as
``Content-Type: application/pdf`` is rejected the same way regardless of how
the artifact entered the store.

Semantic validity (e.g. 'is this JATS payload a real fulltext or just an
abstract stub?') is an extraction-stage concern; this module's bar is
'plausibly represents the format'.
"""

from typing import Final

from litspectraits.resolver.types import Format

SNIFF_BYTES: Final = 4096
"""Number of leading bytes the acquire path reads into memory before opening
a temp file. Large enough to clear typical XML BOM/comment preambles, small
enough to stay cheap on the heap."""

PREVIEW_BYTES: Final = 64
"""Slice of the leading bytes recorded on a magic-byte mismatch for debug."""

_MAGIC_PDF: Final = b'%PDF-'
_MAGIC_GZ: Final = b'\x1f\x8b'
_MAGIC_JATS_PREFIXES: Final = (b'<?xml', b'<article', b'<!DOCTYPE')
_MAGIC_LATEX_KEYWORDS: Final = (b'\\documentclass', b'\\begin{document}')


def magic_bytes_match(format_: Format, head: bytes) -> bool:
    """Return ``True`` iff ``head`` plausibly opens a ``format_`` payload.

    Parameters
    ----------
    format_ : Format
        The format the resolver advertised.
    head : bytes
        Leading bytes of the payload (typically :data:`SNIFF_BYTES` long).
        ``head`` may be shorter than :data:`SNIFF_BYTES` when the response
        body itself is shorter; that is fine — magic-byte presence is what
        matters.
    """
    if format_ is Format.PDF:
        return head.startswith(_MAGIC_PDF)
    if format_ is Format.JATS:
        stripped = head.lstrip()
        return any(stripped.startswith(p) for p in _MAGIC_JATS_PREFIXES)
    if format_ is Format.LATEX:
        if head.startswith(_MAGIC_GZ):
            return True
        return any(kw in head for kw in _MAGIC_LATEX_KEYWORDS)
    return False


__all__ = [
    'PREVIEW_BYTES',
    'SNIFF_BYTES',
    'magic_bytes_match',
]
