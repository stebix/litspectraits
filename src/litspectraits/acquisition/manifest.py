"""Acquisition records and the cattrs converter that (de)serializes them.

Manual sideloads carry a non-optional :class:`ManualProvenance` entry so the
operator, source URL, and license assertion are preserved on disk — these
are typically institutional-access artifacts and the manifest is the only
durable record of how they were obtained.
"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import cattrs
from attrs import frozen
from cattrs.preconf.json import make_converter

from litspectraits.resolver.types import Availability, ResolveResult


class Origin(StrEnum):
    """How the artifact entered the store."""

    AUTO = 'auto'
    MANUAL = 'manual'


@frozen
class ManualProvenance:
    """Operator-attested context for a manual sideload.

    Attributes
    ----------
    operator : str
        Email of the operator who retrieved the artifact (taken from
        ``LITSPECTRAITS_CONTACT_EMAIL`` at sideload time).
    retrieved_at : datetime
        When the artifact was retrieved (UTC, ISO).
    source_url : str | None
        URL the operator hit, when known.
    note : str
        Free-text justification — e.g. "via uni-wuerzburg library proxy".
    license_assertion : str
        Operator-declared license. Required because auto-detection cannot
        fire on manual artifacts.
    """

    operator: str
    retrieved_at: datetime
    source_url: str | None
    note: str
    license_assertion: str


@frozen
class AcquisitionRecord:
    """The on-disk manifest paired with each stored artifact.

    Attributes
    ----------
    doi : str
        Normalized DOI.
    sha256 : str
        Hex digest of the artifact bytes; also the filename stem.
    artifact_path : str
        Path relative to ``data_dir``; POSIX-style so the manifest is
        portable across filesystems.
    source : Availability
        The selected availability that produced this artifact.
    resolve_result : ResolveResult | None
        Full resolver audit trail; ``None`` for manual sideloads.
    fetched_at : datetime
        UTC timestamp of acquisition.
    fetcher_version : str
        ``litspectraits.__version__`` at acquisition time.
    byte_size : int
        Size of the stored artifact in bytes.
    origin : Origin
        Whether the artifact was auto-fetched or manually sideloaded.
    manual_provenance : ManualProvenance | None
        Populated iff ``origin == MANUAL``.
    """

    doi: str
    sha256: str
    artifact_path: str
    source: Availability
    resolve_result: ResolveResult | None
    fetched_at: datetime
    fetcher_version: str
    byte_size: int
    origin: Origin
    manual_provenance: ManualProvenance | None


def _build_converter() -> cattrs.Converter:
    conv = make_converter()
    conv.register_structure_hook(Path, lambda v, _: Path(v))
    conv.register_unstructure_hook(Path, str)
    conv.register_structure_hook(datetime, lambda v, _: datetime.fromisoformat(v))
    conv.register_unstructure_hook(datetime, lambda d: d.isoformat())
    return conv


converter: cattrs.Converter = _build_converter()


def serialize(record: AcquisitionRecord) -> Mapping[str, Any]:
    """Convert ``record`` into a JSON-serializable mapping."""
    return converter.unstructure(record)


def deserialize(payload: Mapping[str, Any]) -> AcquisitionRecord:
    """Inverse of :func:`serialize`."""
    return converter.structure(payload, AcquisitionRecord)
