"""Magic-byte format sniffing (``docs/overview-v3.md`` §3, §17.4).

Defence in depth between retrievers / sideload and the artifact store:
nothing reaches :meth:`litspectraits.store.ArtifactStore.commit` without
passing through :func:`verify`. The window is 4 KiB — wide enough to
span any conceivable XML preamble (declaration + comments + DOCTYPE)
without slurping a full document.

Recognized formats
------------------

- :attr:`~litspectraits.manifest.Format.PDF` — strict ``%PDF-`` prefix
  at byte 0.
- :attr:`~litspectraits.manifest.Format.JATS_XML` — XML declaration
  followed by an ``<article>`` root element (after stripping XML PIs,
  comments, and DOCTYPE).
- :attr:`~litspectraits.manifest.Format.ELSEVIER_XML` — XML declaration
  followed by a ``<full-text-retrieval-response>`` root element, same
  preamble treatment.

The classifier is intentionally root-element-based, not text-match.
``<?xml`` alone is not sufficient (XHTML uses one) and a bare
``<article`` substring would misclassify HTML pages that happen to
contain that token. The first opening tag, after preamble stripping,
is the strong signal — namespaced roots (``<jats:article>``) collapse
to their local name so they still match.

Notes
-----
The sniffer is the *first* gate, not the only one. Downstream parsers
(``lxml`` in the Elsevier retriever, the JATS extractor) impose stricter
structural checks; sniff only has to reject the obvious adversaries
(paywall HTML served with a ``.pdf`` extension, error pages with an
XML prelude, empty bodies).

PDFs are required to start *exactly* at byte 0. The PDF spec lets a
reader skip up to 1 KiB of leading garbage, but every TDM endpoint
and library-proxy sideload in scope returns clean bytes; relaxing this
would weaken the paywall-HTML guard for no real-world gain.
"""

import re
from pathlib import Path
from typing import Final

from litspectraits.errors import MalformedArtifactError
from litspectraits.manifest import Format

_SNIFF_WINDOW: Final = 4096
_PDF_MAGIC: Final = b'%PDF-'

# Order matters: PIs first (they share the ``<?`` prefix with nothing else
# we strip), then comments, then DOCTYPE — DOCTYPE may immediately follow
# either of the prior two and we want it gone before the first-tag scan.
_PI_RE: Final = re.compile(rb'<\?[^?]*\?>', re.DOTALL)
_COMMENT_RE: Final = re.compile(rb'<!--.*?-->', re.DOTALL)
_DOCTYPE_RE: Final = re.compile(rb'<!DOCTYPE[^>]*>', re.IGNORECASE | re.DOTALL)
_FIRST_TAG_RE: Final = re.compile(rb'<([A-Za-z_][A-Za-z0-9_:.-]*)')

_XML_ROOT_TO_FORMAT: Final[dict[str, Format]] = {
    'article': Format.JATS_XML,
    'full-text-retrieval-response': Format.ELSEVIER_XML,
}


def classify(head: bytes) -> Format | None:
    """Classify the leading bytes of an artifact.

    Pure function; no I/O. Returns ``None`` when no rule matches —
    callers that want a hard guarantee should use :func:`verify`.

    Parameters
    ----------
    head : bytes
        First :data:`_SNIFF_WINDOW` bytes (or fewer) of a candidate
        artifact. Longer buffers are accepted; only the prefix is
        inspected.

    Returns
    -------
    Format or None
        The matched format, or ``None`` if neither PDF nor either XML
        envelope is recognized.
    """
    window = head[:_SNIFF_WINDOW]
    if window.startswith(_PDF_MAGIC):
        return Format.PDF
    if not _has_xml_declaration(window):
        return None
    root = _xml_root_local_name(window)
    if root is None:
        return None
    return _XML_ROOT_TO_FORMAT.get(root)


def verify(path: Path, *, expected: Format, doi: str) -> None:
    """Read ``path`` and raise unless its sniffed format matches ``expected``.

    Parameters
    ----------
    path : pathlib.Path
        File to inspect. Up to :data:`_SNIFF_WINDOW` bytes are read.
    expected : Format
        Format the caller asserted the artifact would be.
    doi : str
        DOI under which the artifact was retrieved or sideloaded;
        attached to the error context for log correlation.

    Raises
    ------
    MalformedArtifactError
        The file could not be classified, or was classified as a
        different format. The exception's ``context`` carries
        ``expected`` (the asked-for ``Format`` value), ``detected``
        (the sniffed ``Format`` value, or ``'unrecognized'`` when no
        rule matched), the file path, and the file size — enough for
        an operator to triage paywall-HTML-as-PDF, abstract-only XML,
        or empty-body cases without re-opening the file.
    """
    head = _read_head(path)
    detected = classify(head)
    if detected is expected:
        return
    detected_label = detected.value if detected is not None else 'unrecognized'
    raise MalformedArtifactError(
        doi=doi,
        expected=expected.value,
        detected=detected_label,
        path=str(path),
        byte_size=path.stat().st_size,
    )


def _read_head(path: Path) -> bytes:
    with path.open('rb') as fp:
        return fp.read(_SNIFF_WINDOW)


def _has_xml_declaration(window: bytes) -> bool:
    # Strip a UTF-8 BOM and any leading whitespace before checking for the
    # XML decl. Real-world JATS/Elsevier responses do not include leading
    # whitespace, but tolerating it costs nothing and removes a class of
    # false negatives if a publisher proxy ever rewraps the body.
    stripped = window.lstrip(b'\xef\xbb\xbf').lstrip()
    return stripped.startswith(b'<?xml')


def _xml_root_local_name(window: bytes) -> str | None:
    cleaned = _PI_RE.sub(b'', window)
    cleaned = _COMMENT_RE.sub(b'', cleaned)
    cleaned = _DOCTYPE_RE.sub(b'', cleaned)
    match = _FIRST_TAG_RE.search(cleaned)
    if match is None:
        return None
    qname = match.group(1).decode('ascii', errors='replace').lower()
    _, _, local = qname.rpartition(':')
    return local
