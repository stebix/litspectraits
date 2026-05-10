"""Per-route fetch attempts recorded during an acquire call.

The acquire step walks ``ResolveResult.permitted_candidates`` in policy
order and records one :class:`AcquisitionAttempt` per route it touches —
including failures. The successful route is the last entry on the resulting
:class:`~litspectraits.acquisition.manifest.AcquisitionRecord`; failures of
prior routes are kept so an operator can see *why* the resolver-preferred
route was rejected (4xx, magic-byte mismatch, mid-stream drop) without
having to re-resolve.

These types live next to the :class:`~litspectraits.acquisition.manifest.AcquisitionRecord`
they belong to rather than in :mod:`litspectraits.resolver.types`, because
they describe acquire-stage events, not the resolver's view of the world.
"""

from datetime import datetime
from enum import StrEnum

from attrs import frozen

from litspectraits.resolver.types import Availability


class AttemptOutcome(StrEnum):
    """Terminal status of a single fetch attempt against one ``Availability``."""

    SUCCESS = 'success'
    HTTP_4XX = 'http_4xx'
    HTTP_5XX = 'http_5xx'
    MAGIC_BYTE_MISMATCH = 'magic_byte_mismatch'
    CONNECTION_DROPPED = 'connection_dropped'
    EMPTY_BODY = 'empty_body'


@frozen
class AcquisitionAttempt:
    """One fetch attempt's audit row.

    Attributes
    ----------
    availability : Availability
        The route that was attempted.
    outcome : AttemptOutcome
        Terminal status. ``SUCCESS`` is recorded only for the route that
        produced the final artifact.
    http_status : int | None
        Populated for :data:`AttemptOutcome.HTTP_4XX` and
        :data:`AttemptOutcome.HTTP_5XX`; ``None`` otherwise.
    sniffed_prefix : bytes | None
        First :data:`~litspectraits.acquisition.sniff.PREVIEW_BYTES` of the
        body for :data:`AttemptOutcome.MAGIC_BYTE_MISMATCH`. Lets an
        operator tell apart 'paywall HTML', 'CAPTCHA page', and 'wrong
        format extension' at a glance.
    duration_ms : int
        Wall-clock duration of the attempt.
    attempted_at : datetime
        UTC timestamp of when the attempt began.
    error : str | None
        Raw exception message, populated for
        :data:`AttemptOutcome.CONNECTION_DROPPED` and unexpected errors;
        used for debug only — do not parse.
    """

    availability: Availability
    outcome: AttemptOutcome
    http_status: int | None
    sniffed_prefix: bytes | None
    duration_ms: int
    attempted_at: datetime
    error: str | None


__all__ = [
    'AcquisitionAttempt',
    'AttemptOutcome',
]
