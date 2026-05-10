"""Acquisition: fetch raw artifacts and persist them content-addressed.

Submodules :mod:`fetch`, :mod:`sideload`, :mod:`store`, and :mod:`manifest`
are imported explicitly by callers — the package ``__init__`` deliberately
re-exports only the lightweight types to avoid evaluation cycles with the
resolver package.
"""

from litspectraits.acquisition.attempt import AcquisitionAttempt, AttemptOutcome
from litspectraits.acquisition.manifest import (
    AcquisitionRecord,
    ManualProvenance,
    Origin,
)
from litspectraits.acquisition.store import (
    AcquisitionExhaustedError,
    ArtifactStore,
    IntegrityError,
    MalformedArtifactError,
    NoSourceAvailableError,
)

__all__ = [
    'AcquisitionAttempt',
    'AcquisitionExhaustedError',
    'AcquisitionRecord',
    'ArtifactStore',
    'AttemptOutcome',
    'IntegrityError',
    'MalformedArtifactError',
    'ManualProvenance',
    'NoSourceAvailableError',
    'Origin',
]
