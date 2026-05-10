"""Content-addressed local artifact store.

Layout (under ``data_dir``):

::

    artifacts/<jats|latex|pdf>/sha256/<aa>/<bb>/<aabb…>.<ext>
    manifests/sha256/<aa>/<bb>/<aabb…>.manifest.json
    index/by_doi.jsonl
    tmp/<random>

Two-level prefix sharding bounds directory fanout (256x256 leaves) so
ordinary tooling (``ls``, ``rsync``) stays fast even with millions of
artifacts. The DOI index is an append-log; reads tolerate duplicates and
deduplicate by sha256.
"""

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Final

from litspectraits.acquisition.attempt import AcquisitionAttempt
from litspectraits.acquisition.manifest import (
    AcquisitionRecord,
    deserialize,
    serialize,
)
from litspectraits.resolver.types import Format

_FORMAT_DIR: Final = {
    Format.JATS: 'jats',
    Format.LATEX: 'latex',
    Format.PDF: 'pdf',
}

_FORMAT_EXT: Final = {
    Format.JATS: '.xml',
    Format.LATEX: '.tar.gz',
    Format.PDF: '.pdf',
}


class NoSourceAvailableError(RuntimeError):
    """Raised when acquisition is asked to fetch a resolve result with no chosen source."""


class IntegrityError(RuntimeError):
    """Raised when downloaded bytes fail an integrity check (e.g. hash mismatch on retry)."""


class MalformedArtifactError(RuntimeError):
    """Raised when a sideloaded artifact fails magic-byte validation."""


class AcquisitionExhaustedError(RuntimeError):
    """Raised when every permitted candidate failed to fetch.

    Carries the full :class:`~litspectraits.acquisition.attempt.AcquisitionAttempt`
    log so the CLI can render a 'we tried N routes, here's what each said'
    panel without re-resolving.
    """

    def __init__(self, doi: str, attempts: tuple[AcquisitionAttempt, ...]) -> None:
        super().__init__(
            f'no permitted candidate for {doi} succeeded after {len(attempts)} attempt(s)'
        )
        self.doi = doi
        self.attempts = attempts


class ArtifactStore:
    """Layout helper + DOI index for the on-disk artifact store.

    Construct once per process and pass through :class:`ProbeContext`.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._artifacts = data_dir / 'artifacts'
        self._manifests = data_dir / 'manifests' / 'sha256'
        self._index = data_dir / 'index'
        self._tmp = data_dir / 'tmp'
        self._index_file = self._index / 'by_doi.jsonl'
        for d in (self._artifacts, self._manifests, self._index, self._tmp):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def tmp_dir(self) -> Path:
        return self._tmp

    def shard_path(self, format_: Format, sha256: str) -> Path:
        """Compute the canonical artifact path for ``(format, sha256)``."""
        if len(sha256) < 4:
            raise ValueError(f'sha256 hex too short: {sha256!r}')
        ext = _FORMAT_EXT[format_]
        sub = _FORMAT_DIR[format_]
        return self._artifacts / sub / 'sha256' / sha256[:2] / sha256[2:4] / f'{sha256}{ext}'

    def manifest_path(self, sha256: str) -> Path:
        """Compute the manifest path for ``sha256`` (mirrors :meth:`shard_path`)."""
        if len(sha256) < 4:
            raise ValueError(f'sha256 hex too short: {sha256!r}')
        return self._manifests / sha256[:2] / sha256[2:4] / f'{sha256}.manifest.json'

    def absolute_path(self, record: AcquisitionRecord) -> Path:
        return self._data_dir / record.artifact_path

    def write_manifest(self, record: AcquisitionRecord) -> None:
        """Write the manifest and append a DOI index entry."""
        path = self.manifest_path(record.sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(serialize(record), indent=2, sort_keys=True)
        path.write_text(payload, encoding='utf-8')
        self._append_index(record)

    def find_by_doi(self, doi: str) -> tuple[AcquisitionRecord, ...]:
        """Return all manifests recorded for ``doi`` (deduplicated by sha256)."""
        seen: set[str] = set()
        records: list[AcquisitionRecord] = []
        for entry in self._iter_index_entries():
            if entry.get('doi') != doi:
                continue
            sha256 = entry.get('sha256')
            if not sha256 or sha256 in seen:
                continue
            seen.add(sha256)
            manifest_path = self.manifest_path(sha256)
            if not manifest_path.exists():
                continue
            payload = json.loads(manifest_path.read_text(encoding='utf-8'))
            records.append(deserialize(payload))
        return tuple(records)

    def _append_index(self, record: AcquisitionRecord) -> None:
        line = json.dumps(
            {
                'doi': record.doi,
                'sha256': record.sha256,
                'added_at': datetime.now(record.fetched_at.tzinfo).isoformat(),
                'origin': record.origin.value,
            },
            sort_keys=True,
        )
        with self._index_file.open('a', encoding='utf-8') as fh:
            fh.write(line + '\n')

    def _iter_index_entries(self) -> Iterator[dict[str, str]]:
        if not self._index_file.exists():
            return
        with self._index_file.open('r', encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                yield json.loads(line)
