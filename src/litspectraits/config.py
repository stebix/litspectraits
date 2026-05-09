"""Environment-driven configuration.

A single :class:`Settings` value object loaded from environment variables.
``python-dotenv`` is loaded once at process entry (CLI / pytest session) so
library code can stay env-only.
"""

import os
from pathlib import Path
from typing import Final

from attrs import frozen
from platformdirs import user_data_dir

_APP_NAME: Final = 'litspectraits'
_REQUIRED_VARS: Final = ('LITSPECTRAITS_CONTACT_EMAIL',)


class MissingConfigError(RuntimeError):
    """Raised when required configuration is missing from the environment."""


@frozen
class Settings:
    """Process-wide configuration sourced from environment variables.

    Attributes
    ----------
    contact_email : str
        Mailto address sent to polite-pool APIs (CrossRef, Unpaywall) and
        recorded as the operator on manual sideloads.
    data_dir : pathlib.Path
        Root for ``artifacts/``, ``manifests/``, ``index/`` and ``tmp/``.
    crossref_tdm_token : str | None
        Optional Crossref Click-Through token. When unset, TDM-gated probes
        record ``access=tdm_token`` Availabilities but never select them.
    http_timeout_s : float
        Per-request timeout in seconds.
    log_format : str
        One of ``'rich'`` or ``'json'``.
    """

    contact_email: str
    data_dir: Path
    crossref_tdm_token: str | None
    http_timeout_s: float
    log_format: str

    @classmethod
    def from_env(cls) -> Settings:
        """Build a :class:`Settings` from the current environment.

        Raises
        ------
        MissingConfigError
            If any required variable is missing or empty.
        """
        missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
        if missing:
            raise MissingConfigError(
                f'missing required environment variables: {", ".join(missing)}. '
                f'Set them in your shell or .env file before running litspectraits.'
            )
        data_dir_raw = os.environ.get('LITSPECTRAITS_DATA_DIR')
        data_dir = Path(data_dir_raw) if data_dir_raw else Path(user_data_dir(_APP_NAME))
        return cls(
            contact_email=os.environ['LITSPECTRAITS_CONTACT_EMAIL'],
            data_dir=data_dir,
            crossref_tdm_token=os.environ.get('LITSPECTRAITS_CROSSREF_TDM_TOKEN') or None,
            http_timeout_s=float(os.environ.get('LITSPECTRAITS_HTTP_TIMEOUT_S', '30')),
            log_format=os.environ.get('LITSPECTRAITS_LOG_FORMAT', 'rich'),
        )
