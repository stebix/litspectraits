"""Tests for :mod:`litspectraits.config` env parsing and MinerU cache wiring."""

import os
from pathlib import Path

import pytest

from litspectraits.config import MissingConfigError, Settings, apply_mineru_model_cache_env


@pytest.fixture(autouse=True)
def _contact_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Settings.from_env`` requires the contact email; supply a dummy."""
    monkeypatch.setenv('LITSPECTRAITS_CONTACT_EMAIL', 'test@example.com')


def test_log_level_defaults_to_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LITSPECTRAITS_LOG_LEVEL', raising=False)
    settings = Settings.from_env()
    assert settings.log_level == 'warning'


def test_log_level_reads_and_lowercases_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LITSPECTRAITS_LOG_LEVEL', 'DEBUG')
    settings = Settings.from_env()
    assert settings.log_level == 'debug'


def test_log_level_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LITSPECTRAITS_LOG_LEVEL', 'chatty')
    with pytest.raises(MissingConfigError, match='not a valid log level'):
        Settings.from_env()


def test_from_env_reads_mineru_model_cache_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LITSPECTRAITS_MINERU_MODEL_CACHE_DIR', '/data/mineru-weights')
    settings = Settings.from_env()
    assert settings.mineru_model_cache_dir == Path('/data/mineru-weights')


def test_from_env_mineru_model_cache_dir_defaults_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LITSPECTRAITS_MINERU_MODEL_CACHE_DIR', raising=False)
    settings = Settings.from_env()
    assert settings.mineru_model_cache_dir is None


def test_apply_env_noop_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LITSPECTRAITS_MINERU_MODEL_CACHE_DIR', raising=False)
    monkeypatch.delenv('HF_HOME', raising=False)
    monkeypatch.delenv('MODELSCOPE_CACHE', raising=False)
    apply_mineru_model_cache_env()
    assert 'HF_HOME' not in os.environ
    assert 'MODELSCOPE_CACHE' not in os.environ


def test_apply_env_sets_hf_home_and_modelscope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LITSPECTRAITS_MINERU_MODEL_CACHE_DIR', '/data/mineru-weights')
    monkeypatch.delenv('HF_HOME', raising=False)
    monkeypatch.delenv('MODELSCOPE_CACHE', raising=False)
    apply_mineru_model_cache_env()
    assert os.environ['HF_HOME'] == '/data/mineru-weights'
    assert os.environ['MODELSCOPE_CACHE'] == '/data/mineru-weights'


def test_apply_env_overrides_existing_hf_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """The litspectraits knob is explicit, so it wins over a stray HF_HOME."""
    monkeypatch.setenv('LITSPECTRAITS_MINERU_MODEL_CACHE_DIR', '/data/mineru-weights')
    monkeypatch.setenv('HF_HOME', '/somewhere/else')
    apply_mineru_model_cache_env()
    assert os.environ['HF_HOME'] == '/data/mineru-weights'


def test_apply_env_preserves_explicit_modelscope_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ModelScope cache is preserved (filled in only when absent)."""
    monkeypatch.setenv('LITSPECTRAITS_MINERU_MODEL_CACHE_DIR', '/data/mineru-weights')
    monkeypatch.setenv('MODELSCOPE_CACHE', '/my/modelscope')
    apply_mineru_model_cache_env()
    assert os.environ['MODELSCOPE_CACHE'] == '/my/modelscope'
