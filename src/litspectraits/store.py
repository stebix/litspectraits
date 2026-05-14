"""Filesystem-backed artifact store (``docs/overview-v3.md`` §3, §17.3).

Layout under :attr:`Settings.data_dir`::

    artifacts/{pdf,jats,elsevier}/sha256/<aa>/<sha>.<ext>
    manifests/sha256/<aa>/<sha>.manifest.json
    documents/<sha256>/{document.json,meta.json}
    normalized/<sha256>/{document.json,meta.json}
    index/by_doi.jsonl
    tmp/                   # cleared on init

The format directory under ``artifacts/`` doubles as the disambiguator
between JATS and Elsevier XML (both ``.xml``); ``<aa>`` is the first two
hex characters of the sha256 (one-level sharding).

``documents/`` is keyed on the *artifact* sha256 so an extraction always
co-locates with the bytes it describes. It is intentionally **not** sharded
— the population is bounded by the artifact population (one extraction per
artifact, append-only) and one-level-deeper directories already gives
filesystems an easy enumerate. ``normalized/`` mirrors the same un-sharded
shape one layer downstream (E0.5b persistence, ``docs/normalized-documents-discussion.md``
§3); when corpus size eventually motivates sharding, both layers move
together in one coordinated migration (see
``docs/dual-route-comparison-overview.md`` §9).

Atomic ``os.replace`` from ``tmp/`` to the canonical path is non-negotiable
(§3): a half-written ``.part`` file must never be visible at the canonical
location. Extract and normalize outputs follow the same discipline
(``extract-pdf-plan.md`` §3 stage 5; persistence module).
"""

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

from litspectraits.errors import IntegrityError
from litspectraits.manifest import AcquisitionRecord, Format, converter

_FORMAT_DIR: Final[dict[Format, str]] = {
    Format.PDF: 'pdf',
    Format.JATS_XML: 'jats',
    Format.ELSEVIER_XML: 'elsevier',
}

_FORMAT_EXT: Final[dict[Format, str]] = {
    Format.PDF: '.pdf',
    Format.JATS_XML: '.xml',
    Format.ELSEVIER_XML: '.xml',
}

_SHARD_PREFIX_LEN: Final = 2
_INDEX_FILENAME: Final = 'by_doi.jsonl'

_logger: Final = structlog.get_logger('litspectraits.store')


class ArtifactStore:
    """Filesystem store for artifacts, manifests, and the DOI index.

    Parameters
    ----------
    data_dir : pathlib.Path
        Root for ``artifacts/``, ``manifests/``, ``index/`` and ``tmp/``.
        Subdirectories are created if missing; ``tmp/`` is cleared on
        construction so aborted ingests from a prior run do not pollute
        the staging area.

    Notes
    -----
    The store carries no per-DOI lock: concurrent commits on the same DOI
    would race on the index file. Single-process orchestration is the v3
    contract; the future ``--batch`` mode will add a semaphore at the
    ingest layer rather than locking inside the store.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._artifacts_dir = data_dir / 'artifacts'
        self._manifests_dir = data_dir / 'manifests'
        self._documents_dir = data_dir / 'documents'
        self._normalized_dir = data_dir / 'normalized'
        self._index_dir = data_dir / 'index'
        self._tmp_dir = data_dir / 'tmp'
        self._index_path = self._index_dir / _INDEX_FILENAME

        for path in (
            self._artifacts_dir,
            self._manifests_dir,
            self._documents_dir,
            self._normalized_dir,
            self._index_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

        self._reset_tmp()

    @property
    def tmp_dir(self) -> Path:
        """Staging directory for in-flight downloads."""
        return self._tmp_dir

    @property
    def index_path(self) -> Path:
        """Absolute path of the DOI index (``index/by_doi.jsonl``)."""
        return self._index_path

    def artifact_path(self, sha256: str, fmt: Format) -> Path:
        """Absolute path where an artifact for ``(sha256, fmt)`` lives.

        The path is computed; whether the file exists is a separate query.
        """
        shard = sha256[:_SHARD_PREFIX_LEN]
        filename = f'{sha256}{_FORMAT_EXT[fmt]}'
        return self._artifacts_dir / _FORMAT_DIR[fmt] / 'sha256' / shard / filename

    def artifact_relpath(self, sha256: str, fmt: Format) -> str:
        """Artifact path relative to :attr:`data_dir`.

        This is the value persisted into ``AcquisitionRecord.artifact_path``
        so manifests stay portable across ``data_dir`` relocations.
        """
        return self.artifact_path(sha256, fmt).relative_to(self.data_dir).as_posix()

    def manifest_path(self, sha256: str) -> Path:
        """Absolute path of the manifest JSON for ``sha256``."""
        shard = sha256[:_SHARD_PREFIX_LEN]
        return self._manifests_dir / 'sha256' / shard / f'{sha256}.manifest.json'

    def document_dir(self, sha256: str) -> Path:
        """Absolute path of the per-artifact extract output directory.

        The directory itself is not created here — extractors lazy-create
        it on commit so a query for an un-extracted artifact does not
        leave an empty dir behind.
        """
        return self._documents_dir / sha256

    def normalized_dir(self, sha256: str) -> Path:
        """Absolute path of the per-artifact normalised-output directory.

        Sibling of :meth:`document_dir`, with the same un-sharded layout
        — see this module's layout docstring for the rationale.
        Lazy-created by the normalize commit (
        :mod:`litspectraits.normalize.persistence`) so a query for an
        un-normalised artifact does not leave an empty directory behind.
        """
        return self._normalized_dir / sha256

    def commit(self, *, src: Path, record: AcquisitionRecord) -> Path:
        """Atomically install ``src`` and persist ``record``.

        Steps, in order:

        1. Move ``src`` (which must live under :attr:`tmp_dir`) into its
           shard via :func:`os.replace`.
        2. Write the manifest atomically (write to ``tmp/``, replace).
        3. Append a line to ``index/by_doi.jsonl``.

        Idempotent on ``record.sha256``: if an artifact already exists at
        the canonical path with the same byte size, ``src`` is removed and
        the existing artifact is reused. A size mismatch raises
        :class:`~litspectraits.errors.IntegrityError` rather than silently
        overwriting (§14).

        Parameters
        ----------
        src : pathlib.Path
            Staging file produced by a retriever or sideload. Must be an
            absolute path inside :attr:`tmp_dir`. Consumed on success.
        record : AcquisitionRecord
            The manifest to persist. ``record.artifact_path`` must match
            :meth:`artifact_relpath` for ``(sha256, format)`` — a mismatch
            indicates a programming error in the caller.

        Returns
        -------
        pathlib.Path
            Absolute path of the committed artifact.

        Raises
        ------
        ValueError
            If ``src`` is not absolute, not under :attr:`tmp_dir`, or
            ``record.artifact_path`` disagrees with the computed relpath.
        FileNotFoundError
            If ``src`` does not exist.
        IntegrityError
            If the canonical destination already exists with a different
            size than ``record.byte_size``.
        """
        self._validate_commit_inputs(src=src, record=record)

        dest = self.artifact_path(record.sha256, record.format)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists():
            existing_size = dest.stat().st_size
            if existing_size != record.byte_size:
                raise IntegrityError(
                    doi=record.doi,
                    sha256=record.sha256,
                    existing_size=existing_size,
                    incoming_size=record.byte_size,
                    artifact_path=str(dest),
                )
            src.unlink()
            _logger.info(
                'artifact already present; reusing existing bytes',
                doi=record.doi,
                sha256=record.sha256,
                artifact_path=record.artifact_path,
            )
        else:
            os.replace(src, dest)
            _logger.info(
                'artifact committed',
                doi=record.doi,
                sha256=record.sha256,
                artifact_path=record.artifact_path,
                byte_size=record.byte_size,
            )

        self._write_manifest(record)
        self._append_index(record)
        return dest

    def read_manifest(self, sha256: str) -> AcquisitionRecord:
        """Load the manifest at ``manifests/sha256/<aa>/<sha>.manifest.json``."""
        path = self.manifest_path(sha256)
        raw = json.loads(path.read_text())
        return converter.structure(raw, AcquisitionRecord)

    def find_by_doi(self, doi: str) -> AcquisitionRecord | None:
        """Return the most recent manifest ingested for ``doi``.

        Reads ``index/by_doi.jsonl`` (append-only), keeping the *last*
        matching entry — the index is written in commit order, so the last
        line for a DOI is its newest ingest.

        Returns ``None`` when the DOI has never been ingested.
        """
        if not self._index_path.exists():
            return None
        latest_sha: str | None = None
        with self._index_path.open(encoding='utf-8') as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get('doi') == doi:
                    latest_sha = entry['sha256']
        if latest_sha is None:
            return None
        return self.read_manifest(latest_sha)

    def _validate_commit_inputs(self, *, src: Path, record: AcquisitionRecord) -> None:
        if not src.is_absolute():
            raise ValueError(f'commit src must be an absolute path: {src!r}')
        try:
            src.relative_to(self._tmp_dir)
        except ValueError as exc:
            raise ValueError(f'commit src must live under {self._tmp_dir}; got {src!r}') from exc
        if not src.exists():
            raise FileNotFoundError(f'commit src does not exist: {src!r}')
        expected_relpath = self.artifact_relpath(record.sha256, record.format)
        if record.artifact_path != expected_relpath:
            raise ValueError(
                f'record.artifact_path={record.artifact_path!r} does not match '
                f'computed relpath {expected_relpath!r} for sha256={record.sha256!r} '
                f'format={record.format.value!r}'
            )

    def _write_manifest(self, record: AcquisitionRecord) -> None:
        path = self.manifest_path(record.sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = self._tmp_dir / f'{record.sha256}.manifest.json.part'
        unstructured = converter.unstructure(record)
        staging.write_text(json.dumps(unstructured, indent=2, sort_keys=True), encoding='utf-8')
        os.replace(staging, path)

    def _append_index(self, record: AcquisitionRecord) -> None:
        entry = {
            'doi': record.doi,
            'sha256': record.sha256,
            'format': record.format.value,
            'added_at': datetime.now(tz=UTC).isoformat(),
        }
        with self._index_path.open('a', encoding='utf-8') as fp:
            fp.write(json.dumps(entry) + '\n')

    def _reset_tmp(self) -> None:
        if self._tmp_dir.exists():
            shutil.rmtree(self._tmp_dir)
        self._tmp_dir.mkdir(parents=True)
