"""Structured logging configuration.

Wires :mod:`structlog` so that both library log calls and stdlib loggers
(``httpx``, ``urllib3``, …) share the same renderer, controlled by
``LITSPECTRAITS_LOG_FORMAT``: ``rich`` for human-readable output (default)
or ``json`` for log aggregation.
"""

import logging
import os
import sys
from typing import Final

import structlog

_RICH_FORMAT: Final = 'rich'
_JSON_FORMAT: Final = 'json'
_DEFAULT_FORMAT: Final = _RICH_FORMAT


def configure_logging(*, force: bool = False, level: int = logging.INFO) -> None:
    """Configure ``structlog`` and the stdlib root logger.

    Idempotent: subsequent calls are no-ops unless ``force=True``.

    Parameters
    ----------
    force : bool, optional
        Reconfigure even when already configured. Useful in tests.
    level : int, optional
        Root log level. Defaults to ``logging.INFO``.

    Raises
    ------
    ValueError
        If ``LITSPECTRAITS_LOG_FORMAT`` is set to an unsupported value.
    """
    if not force and structlog.is_configured():
        return

    log_format = os.environ.get('LITSPECTRAITS_LOG_FORMAT', _DEFAULT_FORMAT).lower()
    if log_format not in {_RICH_FORMAT, _JSON_FORMAT}:
        raise ValueError(
            f'unknown LITSPECTRAITS_LOG_FORMAT={log_format!r}; '
            f'expected {_RICH_FORMAT!r} or {_JSON_FORMAT!r}'
        )

    timestamper = structlog.processors.TimeStamper(fmt='iso', utc=True)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
    ]

    if log_format == _JSON_FORMAT:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(
            colors=True,
            exception_formatter=structlog.dev.RichTracebackFormatter(),
        )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
