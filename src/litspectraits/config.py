"""Environment-driven configuration.

A single :class:`Settings` value object loaded from environment variables.
``python-dotenv`` is loaded once at process entry (CLI / pytest session) so
library code can stay env-only.

Only ``LITSPECTRAITS_CONTACT_EMAIL`` fails at startup. Publisher credentials
are optional here; the corresponding retriever raises
:class:`~litspectraits.errors.MissingCredentialError` when the credential is
needed and absent (loud-failure model — see ``docs/overview-v3.md`` §13).
"""

import os
from pathlib import Path
from typing import Final

from attrs import frozen
from platformdirs import user_data_dir

_APP_NAME: Final = 'litspectraits'
_REQUIRED_VARS: Final = ('LITSPECTRAITS_CONTACT_EMAIL',)

_DEFAULT_RATE_LIMIT_WILEY: Final = 3.0
_DEFAULT_RATE_LIMIT_SPRINGER: Final = 5.0
_DEFAULT_RATE_LIMIT_ELSEVIER: Final = 6.0
_DEFAULT_HTTP_TIMEOUT_S: Final = 30.0


class MissingConfigError(RuntimeError):
    """Raised when required configuration is missing from the environment."""


def _parse_float(env_name: str, default: float) -> float:
    """Read ``env_name`` as a float; fall back to ``default`` when unset.

    Raises
    ------
    MissingConfigError
        If the variable is set but does not parse as a float.
    """
    raw = os.environ.get(env_name)
    if raw is None or raw == '':
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise MissingConfigError(f'{env_name}={raw!r} is not a valid float') from exc


def _parse_cidr_list(env_name: str) -> tuple[str, ...]:
    """Parse a comma-separated CIDR list. Empty / unset → empty tuple."""
    raw = os.environ.get(env_name, '')
    return tuple(item.strip() for item in raw.split(',') if item.strip())


@frozen
class Settings:
    """Process-wide configuration sourced from environment variables.

    Attributes
    ----------
    contact_email : str
        Mailto address sent to polite-pool APIs (CrossRef) and recorded as
        the operator on manual sideloads.
    data_dir : pathlib.Path
        Root for ``artifacts/``, ``manifests/``, ``index/`` and ``tmp/``.
    docling_model_cache_dir : pathlib.Path | None
        Directory holding the docling model weights (the layout + TableFormer
        repo folders). ``None`` (the default) leaves docling on its own cache
        location — ``docling.datamodel.settings.settings.cache_dir / 'models'``,
        i.e. ``~/.cache/docling/models``. When set, the PDF extractor passes
        it as ``PdfPipelineOptions.artifacts_path`` and ``doctor
        --download-models`` writes there, so this knob is fully independent of
        :attr:`data_dir`.
    http_timeout_s : float
        Per-request timeout in seconds for first-party HTTP calls (CrossRef,
        ``doctor`` IP check, Elsevier retriever). Publisher SDKs (Wiley,
        Springer) manage their own timeouts internally.
    log_format : str
        One of ``'rich'`` or ``'json'``.
    wiley_tdm_token : str | None
        Wiley TDM token. Forwarded into the ``wiley-tdm`` library as
        ``TDM_API_TOKEN`` via a scoped env context. ``None`` means the
        Wiley retriever falls back to IP-based auth (Würzburg egress); it
        raises :class:`~litspectraits.errors.MissingCredentialError` if
        that path also fails.
    springer_oa_api_key : str | None
        Springer Nature **Open Access tier** API key — a standard
        developer-portal key (free, ``dev.springernature.com``). Used by
        the Springer retriever to fetch JATS XML from
        ``api.springernature.com/openaccess/jats`` for open-access DOIs.
        Either this **or** :attr:`springer_tdm_api_key` must be set; if
        both are present the retriever prefers the TDM tier.
    springer_tdm_api_key : str | None
        Springer Nature **premium TDM tier** API key — issued under a
        Full-Text / TDM licence (``datasolutions.springernature.com``).
        When set, the Springer retriever uses the premium endpoint
        ``spdi.public.springernature.app/xmldata/jats``, which covers
        both open-access and subscription-only Springer Nature content.
        No IP fallback.
    elsevier_api_key : str | None
        Elsevier ScienceDirect API key (sent as ``X-ELS-APIKey``). Required.
    elsevier_insttoken : str | None
        Optional Elsevier institutional token (sent as ``X-ELS-Insttoken``).
        Without it, only OA-tier titles are accessible.
    rate_limit_wiley : float
        Wiley retriever rate-limit ceiling (req/s).
    rate_limit_springer : float
        Springer Nature retriever rate-limit ceiling (req/s).
    rate_limit_elsevier : float
        Elsevier retriever rate-limit ceiling (req/s).
    expected_egress_cidrs : tuple[str, ...]
        Optional CIDR allow-list checked by ``litspectraits doctor``. Empty
        means doctor warns on mismatch but does not fail.
    """

    contact_email: str
    data_dir: Path
    docling_model_cache_dir: Path | None
    http_timeout_s: float
    log_format: str
    wiley_tdm_token: str | None
    springer_oa_api_key: str | None
    springer_tdm_api_key: str | None
    elsevier_api_key: str | None
    elsevier_insttoken: str | None
    rate_limit_wiley: float
    rate_limit_springer: float
    rate_limit_elsevier: float
    expected_egress_cidrs: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Settings:
        """Build a :class:`Settings` from the current environment.

        Raises
        ------
        MissingConfigError
            If any required variable is missing or empty, or if a numeric
            override fails to parse.
        """
        missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
        if missing:
            raise MissingConfigError(
                f'missing required environment variables: {", ".join(missing)}. '
                f'Set them in your shell or .env file before running litspectraits.'
            )
        data_dir_raw = os.environ.get('LITSPECTRAITS_DATA_DIR')
        data_dir = Path(data_dir_raw) if data_dir_raw else Path(user_data_dir(_APP_NAME))
        model_cache_raw = os.environ.get('LITSPECTRAITS_DOCLING_MODEL_CACHE_DIR')
        docling_model_cache_dir = Path(model_cache_raw) if model_cache_raw else None
        return cls(
            contact_email=os.environ['LITSPECTRAITS_CONTACT_EMAIL'],
            data_dir=data_dir,
            docling_model_cache_dir=docling_model_cache_dir,
            http_timeout_s=_parse_float('LITSPECTRAITS_HTTP_TIMEOUT_S', _DEFAULT_HTTP_TIMEOUT_S),
            log_format=os.environ.get('LITSPECTRAITS_LOG_FORMAT', 'rich'),
            wiley_tdm_token=os.environ.get('WILEY_TDM_TOKEN') or None,
            springer_oa_api_key=os.environ.get('SPRINGER_OA_API_KEY') or None,
            springer_tdm_api_key=os.environ.get('SPRINGER_TDM_API_KEY') or None,
            elsevier_api_key=os.environ.get('ELSEVIER_API_KEY') or None,
            elsevier_insttoken=os.environ.get('ELSEVIER_INSTTOKEN') or None,
            rate_limit_wiley=_parse_float(
                'LITSPECTRAITS_RATE_LIMIT_WILEY', _DEFAULT_RATE_LIMIT_WILEY
            ),
            rate_limit_springer=_parse_float(
                'LITSPECTRAITS_RATE_LIMIT_SPRINGER', _DEFAULT_RATE_LIMIT_SPRINGER
            ),
            rate_limit_elsevier=_parse_float(
                'LITSPECTRAITS_RATE_LIMIT_ELSEVIER', _DEFAULT_RATE_LIMIT_ELSEVIER
            ),
            expected_egress_cidrs=_parse_cidr_list('LITSPECTRAITS_EXPECTED_EGRESS_CIDRS'),
        )
