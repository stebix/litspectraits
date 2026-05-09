"""Manual artifact registration.

Operators sideload a file they have retrieved out-of-band (publisher PDF
via institutional proxy, …) into the same content-addressed store used by
auto-fetched artifacts. The manifest carries a :class:`ManualProvenance`
record so the legal trail (operator, source URL, license assertion) is
durable.
"""

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

from litspectraits import __version__
from litspectraits.acquisition.manifest import (
    AcquisitionRecord,
    ManualProvenance,
    Origin,
)
from litspectraits.acquisition.store import ArtifactStore, MalformedArtifactError
from litspectraits.config import Settings
from litspectraits.doi import normalize as normalize_doi
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    SourceKind,
    Version,
    make_extra,
)

_CHUNK: Final = 1 << 16

_FORMAT_MEDIA_TYPE: Final = {
    Format.JATS: 'application/xml',
    Format.LATEX: 'application/x-eprint-tar',
    Format.PDF: 'application/pdf',
}

# Magic-byte sniffers — small enough to inline.
_MAGIC_PDF: Final = b'%PDF-'
_MAGIC_GZ: Final = b'\x1f\x8b'
_MAGIC_JATS_PREFIXES: Final = (b'<?xml', b'<article', b'<!DOCTYPE')
_MAGIC_LATEX_KEYWORDS: Final = (b'\\documentclass', b'\\begin{document}')

log = structlog.get_logger('litspectraits.acquisition.sideload')


def sideload(
    *,
    doi: str,
    path: Path,
    version: Version,
    format: Format,
    license_assertion: str,
    note: str,
    source_url: str | None,
    settings: Settings,
    store: ArtifactStore,
) -> AcquisitionRecord:
    """Register a manually-retrieved artifact in the store.

    Parameters
    ----------
    doi : str
        DOI of the paper. Normalized internally.
    path : pathlib.Path
        Path to the file on disk to ingest.
    version, format : enums
        Operator-asserted axes for the artifact.
    license_assertion : str
        Required license string (SPDX or free-text).
    note : str
        Free-text justification — recorded verbatim in the manifest.
    source_url : str | None
        URL the operator hit, if known.
    settings : Settings
        Runtime configuration (the operator email is taken from here).
    store : ArtifactStore
        Target store.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    MalformedArtifactError
        If the file's magic bytes do not match ``format``, or it is empty.
    """
    normalized = normalize_doi(doi)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.stat().st_size == 0:
        raise MalformedArtifactError(f'{path} is empty')
    _check_magic(path, format)

    sha256, byte_size = _hash_file(path)

    for existing in store.find_by_doi(normalized):
        if existing.sha256 == sha256:
            log.info('sideload.cache_hit', doi=normalized, sha256=sha256)
            return existing

    final_path = store.shard_path(format, sha256)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, final_path)

    availability = Availability(
        source_kind=SourceKind.MANUAL,
        version=version,
        format=format,
        access=Access.OPEN,
        url=f'file://{final_path}',
        media_type=_FORMAT_MEDIA_TYPE[format],
        license=license_assertion,
        extra=make_extra(source_url=source_url),
    )
    record = AcquisitionRecord(
        doi=normalized,
        sha256=sha256,
        artifact_path=str(final_path.relative_to(store.data_dir).as_posix()),
        source=availability,
        resolve_result=None,
        fetched_at=datetime.now(UTC),
        fetcher_version=__version__,
        byte_size=byte_size,
        origin=Origin.MANUAL,
        manual_provenance=ManualProvenance(
            operator=settings.contact_email,
            retrieved_at=datetime.now(UTC),
            source_url=source_url,
            note=note,
            license_assertion=license_assertion,
        ),
    )
    store.write_manifest(record)
    log.info(
        'sideload.stored',
        doi=normalized,
        sha256=sha256,
        format=format,
        version=version,
        bytes=byte_size,
    )
    return record


def _hash_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    total = 0
    with path.open('rb') as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
    return hasher.hexdigest(), total


def _check_magic(path: Path, format_: Format) -> None:
    """Sniff the file head and raise :class:`MalformedArtifactError` on mismatch."""
    with path.open('rb') as fh:
        head = fh.read(2048)
    if format_ is Format.PDF:
        if not head.startswith(_MAGIC_PDF):
            raise MalformedArtifactError(f'{path}: not a PDF (missing %PDF- header)')
        return
    if format_ is Format.JATS:
        stripped = head.lstrip()
        if not any(stripped.startswith(p) for p in _MAGIC_JATS_PREFIXES):
            raise MalformedArtifactError(f'{path}: not JATS XML (missing XML/article header)')
        return
    if format_ is Format.LATEX:
        if head.startswith(_MAGIC_GZ):
            return  # tarball — accept
        if any(kw in head for kw in _MAGIC_LATEX_KEYWORDS):
            return
        raise MalformedArtifactError(f'{path}: not LaTeX (no \\documentclass or gzip magic)')
