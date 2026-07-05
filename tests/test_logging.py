"""Tests for :mod:`litspectraits._logging` — level vocabulary and third-party taming.

These cover the two escape hatches a ``mineru`` extract uses to bypass the
structlog console handler: stdlib loggers with their own handlers, and the
tqdm / loguru channels that skip ``logging`` entirely.
"""

import logging
import os

import pytest
import structlog

from litspectraits._logging import (
    DEFAULT_LEVEL_NAME,
    LEVEL_NAMES,
    configure_logging,
    level_to_int,
    tame_third_party_logging,
)


def test_default_level_name_is_warning() -> None:
    assert DEFAULT_LEVEL_NAME == 'warning'
    assert 'warning' in LEVEL_NAMES


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        ('critical', logging.CRITICAL),
        ('error', logging.ERROR),
        ('warning', logging.WARNING),
        ('info', logging.INFO),
        ('debug', logging.DEBUG),
        ('DEBUG', logging.DEBUG),
        ('  Info ', logging.INFO),
    ],
)
def test_level_to_int(name: str, expected: int) -> None:
    assert level_to_int(name) == expected


def test_level_to_int_rejects_unknown() -> None:
    with pytest.raises(KeyError):
        level_to_int('chatty')


def test_configure_logging_gates_below_level(capsys: pytest.CaptureFixture[str]) -> None:
    """At WARNING, an INFO structlog line is dropped and a WARNING survives."""
    configure_logging(force=True, level=logging.WARNING)
    log = structlog.get_logger('litspectraits.test')
    log.info('should be suppressed', marker='INFO_MARKER')
    log.warning('should appear', marker='WARN_MARKER')
    err = capsys.readouterr().err
    assert 'INFO_MARKER' not in err
    assert 'WARN_MARKER' in err


def test_tame_third_party_pins_stdlib_logger_levels() -> None:
    tame_third_party_logging(logging.ERROR)
    assert logging.getLogger('httpx').level == logging.ERROR
    assert logging.getLogger('docling').level == logging.ERROR
    assert logging.getLogger('mineru').level == logging.ERROR


def test_tame_third_party_toggles_tqdm_by_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """tqdm bars are killed when quiet (> INFO), restored when verbose (<= INFO)."""
    monkeypatch.delenv('TQDM_DISABLE', raising=False)
    tame_third_party_logging(logging.WARNING)
    assert os.environ.get('TQDM_DISABLE') == '1'
    tame_third_party_logging(logging.INFO)
    assert 'TQDM_DISABLE' not in os.environ


def test_tame_third_party_seeds_loguru_level_when_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LOGURU_LEVEL', raising=False)
    tame_third_party_logging(logging.ERROR)
    assert os.environ.get('LOGURU_LEVEL') == 'ERROR'


def test_bridge_mineru_loguru_forwards_to_stdlib(caplog: pytest.LogCaptureFixture) -> None:
    """A loguru message re-emits through the stdlib ``mineru`` logger."""
    pytest.importorskip('loguru')
    from litspectraits.extract.mineru import _bridge_mineru_loguru

    _bridge_mineru_loguru()
    from loguru import logger as loguru_logger

    with caplog.at_level(logging.INFO, logger='mineru'):
        loguru_logger.info('mineru says hello')
    assert any('mineru says hello' in rec.message for rec in caplog.records)
