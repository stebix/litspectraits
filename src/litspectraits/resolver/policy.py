"""Ranking policies for ordering :class:`~litspectraits.resolver.types.Availability`.

A policy ranks lexicographically over the three axes — version, format,
access — using a per-axis order. Two presets cover the common trade-offs:

- :data:`PUBLISHED_FIRST` (default): published-version-of-record beats
  preprint regardless of format. The result is that a published PDF
  outranks a preprint LaTeX even though LaTeX is a richer container.
- :data:`FIDELITY_FIRST`: format wins over version. A preprint JATS
  outranks a published PDF.
"""

from collections.abc import Mapping
from enum import StrEnum

from attrs import evolve, frozen

from litspectraits.resolver.types import Access, Availability, Format, Version


class Axis(StrEnum):
    """Ranking axis identifier."""

    VERSION = 'version'
    FORMAT = 'format'
    ACCESS = 'access'


@frozen
class RankingPolicy:
    """Lexicographic ranking of :class:`Availability` instances.

    Attributes
    ----------
    name : str
        Stable identifier; recorded in :class:`ResolveResult.policy_name`.
    axes : tuple[Axis, ...]
        Axis order, most-significant first.
    version_order, format_order, access_order : tuple[..., ...]
        Per-axis ordering; index 0 is best.
    minimum_access : Access
        Worst access tier the user is willing to consider, *given suitable
        credentials*. Whether credentials actually exist is decided by the
        resolver via :meth:`permits` / :meth:`reason_excluded`, which take
        an explicit ``available_credentials`` set.
    excluded_versions : frozenset[Version]
        Versions to drop entirely (e.g. forbid preprints).
    """

    name: str
    axes: tuple[Axis, ...]
    version_order: tuple[Version, ...]
    format_order: tuple[Format, ...]
    access_order: tuple[Access, ...]
    minimum_access: Access
    excluded_versions: frozenset[Version] = frozenset()

    def permits(
        self, availability: Availability, *, available_credentials: frozenset[Access]
    ) -> bool:
        """Return ``True`` iff this availability is selectable.

        An availability is selectable when its version is not in the
        excluded set, its access tier is no worse than ``minimum_access``,
        AND we hold a credential for that tier.
        """
        if availability.version in self.excluded_versions:
            return False
        if self._access_rank(availability.access) > self._access_rank(self.minimum_access):
            return False
        return availability.access in available_credentials

    def reason_excluded(
        self, availability: Availability, *, available_credentials: frozenset[Access]
    ) -> str | None:
        """Return a short explanation of why this availability is excluded.

        Returns ``None`` if the availability is permitted.
        """
        if availability.version in self.excluded_versions:
            return f'version_excluded: {availability.version}'
        if self._access_rank(availability.access) > self._access_rank(self.minimum_access):
            return f'access_excluded_by_policy: {availability.access}'
        if availability.access not in available_credentials:
            return f'auth_required: no_credential_for_{availability.access}'
        return None

    def sort_key(self, availability: Availability) -> tuple[int, ...]:
        """Compute the lexicographic sort key — lower is better."""
        ranks: dict[Axis, int] = {
            Axis.VERSION: self._version_rank(availability.version),
            Axis.FORMAT: self._format_rank(availability.format),
            Axis.ACCESS: self._access_rank(availability.access),
        }
        return tuple(ranks[a] for a in self.axes)

    def _version_rank(self, v: Version) -> int:
        return _index_or_max(self.version_order, v)

    def _format_rank(self, f: Format) -> int:
        return _index_or_max(self.format_order, f)

    def _access_rank(self, a: Access) -> int:
        return _index_or_max(self.access_order, a)


def _index_or_max[T](order: tuple[T, ...], value: T) -> int:
    try:
        return order.index(value)
    except ValueError:
        return len(order)


PUBLISHED_FIRST = RankingPolicy(
    name='published_first',
    axes=(Axis.VERSION, Axis.FORMAT, Axis.ACCESS),
    version_order=(Version.PUBLISHED, Version.ACCEPTED_MANUSCRIPT, Version.PREPRINT),
    format_order=(Format.JATS, Format.LATEX, Format.PDF),
    access_order=(Access.OPEN, Access.TDM_TOKEN, Access.SUBSCRIPTION),
    minimum_access=Access.TDM_TOKEN,
    excluded_versions=frozenset(),
)

FIDELITY_FIRST = evolve(
    PUBLISHED_FIRST,
    name='fidelity_first',
    axes=(Axis.FORMAT, Axis.VERSION, Axis.ACCESS),
)


PRESETS: Mapping[str, RankingPolicy] = {
    PUBLISHED_FIRST.name: PUBLISHED_FIRST,
    FIDELITY_FIRST.name: FIDELITY_FIRST,
}


def credentials_from_settings(crossref_tdm_token: str | None) -> frozenset[Access]:
    """Derive the set of credentials we hold from the current configuration.

    Open access is always present. Subscription tier is reserved for a
    future capability and is not currently configurable.
    """
    creds = {Access.OPEN}
    if crossref_tdm_token:
        creds.add(Access.TDM_TOKEN)
    return frozenset(creds)
