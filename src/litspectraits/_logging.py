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

# Canonical console log-level vocabulary, shared with :mod:`litspectraits.config`
# (env validation) and :mod:`litspectraits.cli` (the ``--log-level`` choice).
# ``warning`` is the default: quiet enough to kill the INFO/DEBUG flood a
# ``mineru`` extract produces, but still surfaces operationally useful lines
# (rate-limit backoff, docling-degraded) that ``error`` would swallow.
_NAME_TO_LEVEL: Final[dict[str, int]] = {
    'critical': logging.CRITICAL,
    'error': logging.ERROR,
    'warning': logging.WARNING,
    'info': logging.INFO,
    'debug': logging.DEBUG,
}
LEVEL_NAMES: Final[tuple[str, ...]] = tuple(_NAME_TO_LEVEL)
DEFAULT_LEVEL_NAME: Final = 'warning'

# Third-party loggers that spew at INFO/DEBUG. Levels are set by *name* so we
# never import these (heavy) packages just to quiet them — ``getLogger(name)``
# gates records at creation, before any handler a library attached to itself,
# so it works whether the logger propagates to root or not. ``mineru`` is the
# stdlib target the loguru bridge (:mod:`litspectraits.extract.mineru`)
# forwards into.
_NOISY_STDLIB_LOGGERS: Final[tuple[str, ...]] = (
    'httpx',
    'httpcore',
    'urllib3',
    'PIL',
    'transformers',
    'torch',
    'datasets',
    'filelock',
    'docling',
    'docling_core',
    'docling_ibm_models',
    'mineru',
)


def level_to_int(name: str) -> int:
    """Map a canonical level name to its :mod:`logging` integer.

    Raises
    ------
    KeyError
        If ``name`` (case-insensitively) is not one of :data:`LEVEL_NAMES`.
    """
    return _NAME_TO_LEVEL[name.strip().lower()]


def configure_logging(*, force: bool = False, level: int = logging.WARNING) -> None:
    """Configure ``structlog`` and the stdlib root logger.

    Idempotent: subsequent calls are no-ops unless ``force=True``.

    Parameters
    ----------
    force : bool, optional
        Reconfigure even when already configured. Useful in tests.
    level : int, optional
        Console log level. Defaults to ``logging.WARNING`` (see
        :data:`DEFAULT_LEVEL_NAME`). Gates *display* only — the structlog
        processor chain always runs (see the body), so a record below
        ``level`` is created and dropped at the stdlib layer, never at the
        bound logger.

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

    # Gate *display* at the stdlib layer (root + handler ``setLevel(level)``),
    # not at the structlog bound logger. The wrapper stays permissive so every
    # ``.info()``/``.debug()`` still runs the full processor chain — the record
    # is only dropped later, by the stdlib logger's level check, before it
    # reaches the handler. Two payoffs: ``structlog.testing.capture_logs`` (which
    # swaps in a recording processor) sees events at *any* configured level, and
    # the level knob can move without a global-state gotcha. The efficiency cost
    # — formatting a handful of below-threshold events — is nil for a CLI.
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    handler.setLevel(level)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def tame_third_party_logging(level: int) -> None:
    """Corral the output channels that bypass our structlog console handler.

    ``configure_logging`` only governs records that reach the root logger's
    handler. A ``mineru`` extract has three louder escape hatches, silenced
    here so the resolved ``level`` is the single knob for *all* console noise:

    - **stdlib loggers that attach their own handlers** (docling, transformers,
      torch) — their records would render regardless of the root level, so
      each is pinned to ``level`` by name, dropping them at record creation.
    - **tqdm progress bars** — drawn straight to ``stderr``, never through
      ``logging``. ``TQDM_DISABLE`` is set whenever the console is quiet
      (``level`` above INFO) and cleared for ``-v``/``-vv``. This both quiets
      the bars that omit an explicit ``disable=`` *and* serves as the
      quiet/verbose **signal** the MinerU backend reads: tqdm's env var only
      fills an *unset* ``disable`` argument, so MinerU's VLM client (which
      passes ``disable=`` explicitly) is silenced separately, by
      :func:`litspectraits.extract.mineru._silence_mineru_tqdm` patching the
      tqdm class when this flag is set.
    - **loguru** (MinerU's logger) — bridged into stdlib ``logging`` from the
      MinerU backend *after* ``import mineru`` (which reconfigures loguru), so
      ``LOGURU_LEVEL`` is seeded here as a cheap floor for anything the bridge
      does not intercept.

    Idempotent and import-free: safe to call on every startup, and it never
    imports the packages it quiets.
    """
    for name in _NOISY_STDLIB_LOGGERS:
        logging.getLogger(name).setLevel(level)
    if level > logging.INFO:
        os.environ['TQDM_DISABLE'] = '1'
        os.environ.setdefault('LOGURU_LEVEL', logging.getLevelName(level))
    else:
        os.environ.pop('TQDM_DISABLE', None)
