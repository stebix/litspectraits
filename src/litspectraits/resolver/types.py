"""Core data types for the resolver.

The three orthogonal axes — :class:`Version`, :class:`Format`,
:class:`Access` — tag every :class:`Availability` so the
:class:`~litspectraits.resolver.policy.RankingPolicy` can rank them
lexicographically without collapsing semantically distinct trade-offs.
"""

from datetime import datetime
from enum import StrEnum

from attrs import frozen


class Version(StrEnum):
    """Provenance of the bytes — version of record vs author manuscript vs preprint."""

    PUBLISHED = 'published'
    ACCEPTED_MANUSCRIPT = 'accepted_manuscript'
    PREPRINT = 'preprint'


class Format(StrEnum):
    """Container format, ordered by extraction fidelity."""

    JATS = 'jats'
    LATEX = 'latex'
    PDF = 'pdf'


class Access(StrEnum):
    """How the artifact can be retrieved from this environment."""

    OPEN = 'open'
    TDM_TOKEN = 'tdm_token'
    SUBSCRIPTION = 'subscription'


class SourceKind(StrEnum):
    """Which probe surfaced the availability — provenance, not ranking signal."""

    JATS_PMC = 'jats_pmc'
    JATS_EUROPEPMC = 'jats_europepmc'
    JATS_BIORXIV = 'jats_biorxiv'
    JATS_CROSSREF_TDM = 'jats_crossref_tdm'
    LATEX_ARXIV = 'latex_arxiv'
    PDF_UNPAYWALL = 'pdf_unpaywall'
    PDF_CROSSREF_TDM = 'pdf_crossref_tdm'
    MANUAL = 'manual'
    LOCAL_CACHE = 'local_cache'


@frozen
class Availability:
    """A single fetchable (or known-to-exist) source for a DOI.

    Attributes
    ----------
    source_kind : SourceKind
        Which probe surfaced this entry. Provenance only — ranking is done
        on the three semantic axes below.
    version, format, access : enums
        See module docstring.
    url : str
        Fetch URL (network or ``file://``).
    media_type : str
        IANA media type for the served body.
    license : str | None
        License string as advertised by the source. May be SPDX-ish or raw.
    extra : tuple[tuple[str, str], ...]
        Sorted, immutable mapping-as-tuples of probe-specific provenance
        hints (PMCID, arXiv ID, OA-location index, …).
    """

    source_kind: SourceKind
    version: Version
    format: Format
    access: Access
    url: str
    media_type: str
    license: str | None = None
    extra: tuple[tuple[str, str], ...] = ()


@frozen
class ProbeOutcome:
    """Result of running a single probe.

    Each probe yields zero or more :class:`Availability` instances, plus
    optional error context and timing.
    """

    probe: str
    availabilities: tuple[Availability, ...] = ()
    error: str | None = None
    duration_ms: int = 0


@frozen
class ResolveResult:
    """The full audit-trailed output of a single resolve call.

    Attributes
    ----------
    doi : str
        Normalized DOI.
    chosen : Availability | None
        Top-ranked fetchable availability, or ``None`` if nothing was usable.
    candidates : tuple[Availability, ...]
        All availabilities surfaced by probes, ordered by the active policy.
        Includes both permitted and excluded entries — exclusion is reported
        via the ``excluded`` field.
    excluded : tuple[tuple[Availability, str], ...]
        ``(availability, reason)`` pairs for entries excluded from selection
        (auth-blocked, version-excluded, …).
    probes : tuple[ProbeOutcome, ...]
        Full audit trail of every probe, including negatives and errors.
    policy_name : str
        Name of the :class:`~litspectraits.resolver.policy.RankingPolicy`
        used for selection.
    resolved_at : datetime
        UTC timestamp of when resolution completed.
    """

    doi: str
    chosen: Availability | None
    candidates: tuple[Availability, ...]
    excluded: tuple[tuple[Availability, str], ...]
    probes: tuple[ProbeOutcome, ...]
    policy_name: str
    resolved_at: datetime

    @property
    def permitted_candidates(self) -> tuple[Availability, ...]:
        """Candidates not excluded by policy, in policy-rank order.

        Derived from ``candidates`` minus the URLs found in ``excluded``;
        the resolver places permitted entries first in ``candidates``, so
        rank order is preserved.
        """
        excluded_urls = {a.url for a, _ in self.excluded}
        return tuple(c for c in self.candidates if c.url not in excluded_urls)


def make_extra(**kwargs: str | None) -> tuple[tuple[str, str], ...]:
    """Build a sorted, hashable ``extra`` tuple from keyword arguments.

    ``None`` values are dropped; remaining keys are sorted lexicographically.
    """
    return tuple(sorted((k, v) for k, v in kwargs.items() if v is not None))


__all__ = [
    'Access',
    'Availability',
    'Format',
    'ProbeOutcome',
    'ResolveResult',
    'SourceKind',
    'Version',
    'make_extra',
]
