"""Ingest error taxonomy.

The v3 happy path raises exactly one of these classes when it cannot make
forward progress; nothing is materialized on failure (``docs/overview-v3.md``
§0, §5, §14). The class itself is the contract — operator hint and CLI
exit code dispatch off ``type(exc)``.

Each instance carries the DOI plus an open-ended ``context`` dict
(publisher, fetched URL, raw error string, SDK version, …). Both attributes
are populated by the base ``__init__``; subclasses add no behavior.
"""


class IngestError(RuntimeError):
    """Base class for all v3 happy-path failures.

    Parameters
    ----------
    message : str, optional
        Human-readable summary. When omitted, a default
        ``ClassName doi=<doi> key=value …`` rendering is synthesized so
        ``str(exc)`` and bare ``logger.exception`` calls remain useful
        before the CLI's Rich panel is in place.
    doi : str
        DOI under which the failure occurred. Always present, even when
        the failure is configuration-shaped, so callers can correlate
        across structured logs.
    **context : object
        Free-form structured fields attached to the failure (e.g.
        ``publisher='wiley'``, ``http_status=403``, ``sdk_version='1.0.0'``).
    """

    doi: str
    context: dict[str, object]

    def __init__(self, message: str = '', *, doi: str, **context: object) -> None:
        if not message:
            extras = ' '.join(f'{key}={value!r}' for key, value in context.items())
            message = f'{type(self).__name__} doi={doi!r}'
            if extras:
                message = f'{message} {extras}'
        super().__init__(message)
        self.doi = doi
        self.context = dict(context)


# Validation / dispatch -------------------------------------------------------


class DOINotFoundError(IngestError):
    """CrossRef returned 404 for the DOI."""


class UnsupportedPublisherError(IngestError):
    """DOI prefix is not in the publisher dispatch table (§6)."""


# Configuration ---------------------------------------------------------------


class MissingCredentialError(IngestError):
    """Required publisher credential is unset and no IP fallback applies.

    Distinct from :class:`AuthRejectedError`: this is a "you forgot to
    configure something" failure detected before any network call. Doctor
    (``litspectraits doctor``) is meant to surface this.
    """


# Publisher-side --------------------------------------------------------------


class AuthRejectedError(IngestError):
    """Publisher returned 401 / 403 — token rejected or IP not allowlisted."""


class EntitlementDowngradeError(IngestError):
    """Elsevier returned a ``META_ABS`` (abstract-only) envelope.

    Raised by the Elsevier retriever when a ``view=FULL`` request comes
    back without an ``<originalText>`` / ``<xocs:doc>`` subtree. Abstracts
    are corpus poison for downstream value extraction; never silently
    accept them (§5).
    """


class RateLimitExhaustedError(IngestError):
    """Publisher returned 429 after the retriever's bounded retry chain."""


class PublisherAPIError(IngestError):
    """Publisher 5xx, malformed response, or unexpected SDK exception."""


# Local validation ------------------------------------------------------------


class MalformedArtifactError(IngestError):
    """Magic-byte sniff rejected a downloaded or sideloaded artifact.

    Catches paywall-HTML-as-PDF, error-page-as-XML, and operator sideload
    of a non-PDF file.
    """


class IntegrityError(IngestError):
    """Hash collision against an existing artifact whose bytes differ.

    Raised on commit rather than silently overwriting.
    """
