"""Tests for :mod:`litspectraits.progress` — the ambient spinner reporter."""

import io

from rich.console import Console

from litspectraits import progress
from litspectraits.progress import command_progress, report_stage


def _recording_console() -> tuple[Console, io.StringIO]:
    """A Console that captures output as if attached to a terminal."""
    buffer = io.StringIO()
    return Console(file=buffer, force_terminal=True, width=80), buffer


def test_report_stage_is_noop_without_active_spinner() -> None:
    # No active manager: must not raise and must not touch any terminal.
    assert progress._active_status.get() is None
    report_stage('nobody is listening')
    assert progress._active_status.get() is None


def test_command_progress_disabled_yields_without_binding() -> None:
    console, buffer = _recording_console()
    with command_progress('label', enabled=False, console=console):
        # Disabled: the contextvar stays unbound so report_stage is a no-op.
        assert progress._active_status.get() is None
        report_stage('still nothing')
    assert buffer.getvalue() == ''


def test_command_progress_enabled_binds_and_unbinds() -> None:
    console, _buffer = _recording_console()
    assert progress._active_status.get() is None
    with command_progress('starting', enabled=True, console=console):
        assert progress._active_status.get() is not None
    # Token is reset on exit, even for the enabled path.
    assert progress._active_status.get() is None


def test_report_stage_updates_visible_label() -> None:
    console, buffer = _recording_console()
    with command_progress('starting', enabled=True, console=console):
        report_stage('Fetching from Wiley…')
    assert 'Fetching from Wiley' in buffer.getvalue()
