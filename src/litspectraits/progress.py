"""Live progress feedback for long-running CLI commands.

A single indeterminate Rich spinner per command, updated at stage
boundaries via :func:`report_stage`. The reporter is *ambient* — bound
into a :class:`contextvars.ContextVar` by :func:`command_progress` so
pipeline code deep in the call stack can report progress without threading
a handle through every signature. This mirrors how :mod:`structlog` binds
the DOI via ``contextvars`` in :mod:`litspectraits.ingest`, and it composes
across the ``asyncio.run`` boundary: the task copies the current context at
creation, so a :func:`report_stage` call awaited inside the pipeline sees
the spinner the CLI bound before ``asyncio.run``.

Deliberately decoupled from log verbosity. The spinner is *fed* by explicit
:func:`report_stage` calls, not by log records, so the ``--log-level`` knob
and the spinner are independent: raising the level does not change what the
spinner says, and the spinner never competes with a log line for a token.
The CLI (:mod:`litspectraits.cli`) is what ties them together — it disables
the spinner precisely when streaming log lines *would* fight it (level at or
below INFO), and off a TTY or under ``--json``.

When disabled, :func:`command_progress` yields without touching the
terminal and :func:`report_stage` costs a single ``ContextVar.get``.
"""

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

from rich.console import Console
from rich.status import Status

# The live spinner for the current command, or ``None`` when no spinner is
# active (disabled, or outside a ``command_progress`` block).
_active_status: ContextVar[Status | None] = ContextVar(
    'litspectraits_progress_status', default=None
)


def report_stage(label: str) -> None:
    """Update the active spinner's text to ``label``.

    A no-op (one ``ContextVar.get``) when no spinner is active — the common
    case for non-interactive runs, ``--json``, verbose logging, and library
    callers. Safe to call from anywhere in the pipeline.
    """
    status = _active_status.get()
    if status is not None:
        status.update(label)


@contextlib.contextmanager
def command_progress(label: str, *, enabled: bool, console: Console) -> Iterator[None]:
    """Run the enclosed work under a live spinner titled ``label``.

    Parameters
    ----------
    label : str
        Initial spinner text; refined by :func:`report_stage` as the work
        progresses through its stages.
    enabled : bool
        When ``False`` the manager yields immediately without starting a
        spinner — the caller decides eligibility (TTY, ``--json``, log
        level) via :func:`litspectraits.cli._spinner_enabled`.
    console : rich.console.Console
        The ``stderr`` console the spinner draws on. Pass the *same* console
        the command renders its result/error panels on: Rich coordinates
        prints on a shared console with the live region, and the spinner is
        stopped by ``__exit__`` before any post-work rendering runs, so the
        two never overlap.

    Notes
    -----
    A synchronous manager wrapping asynchronous work is intentional: only
    the enter/exit run synchronously; the ``await``\\ s inside yield to the
    event loop while Rich animates the spinner on its own refresh thread.
    """
    if not enabled:
        yield
        return
    with console.status(label, spinner='dots') as status:
        token = _active_status.set(status)
        try:
            yield
        finally:
            _active_status.reset(token)
