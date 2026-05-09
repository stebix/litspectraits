"""Resolver: DOI -> structured availability across the supported source kinds.

The orchestrator entry point lives at :mod:`litspectraits.resolver.resolver`;
import :func:`resolve` from there directly to avoid evaluation cycles
between the resolver and acquisition packages.
"""

from litspectraits.resolver.policy import FIDELITY_FIRST, PRESETS, PUBLISHED_FIRST, RankingPolicy
from litspectraits.resolver.types import (
    Access,
    Availability,
    Format,
    ProbeOutcome,
    ResolveResult,
    SourceKind,
    Version,
)

__all__ = [
    'FIDELITY_FIRST',
    'PRESETS',
    'PUBLISHED_FIRST',
    'Access',
    'Availability',
    'Format',
    'ProbeOutcome',
    'RankingPolicy',
    'ResolveResult',
    'SourceKind',
    'Version',
]
