"""Command-line interface.

Three commands, all rendered with Rich for human output and ``--json`` for
piping. ``.env`` is loaded once at process entry; library code remains
env-only.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Final

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from litspectraits._logging import configure_logging
from litspectraits.acquisition.fetch import acquire
from litspectraits.acquisition.manifest import AcquisitionRecord
from litspectraits.acquisition.sideload import sideload as sideload_artifact
from litspectraits.acquisition.store import ArtifactStore, NoSourceAvailableError
from litspectraits.config import MissingConfigError, Settings
from litspectraits.http import http_client
from litspectraits.resolver.policy import PRESETS, RankingPolicy
from litspectraits.resolver.resolver import resolve as resolver_resolve
from litspectraits.resolver.types import Format, ResolveResult, Version

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help='DOI-driven literature ingestion pipeline.',
)

_CONSOLE: Final = Console(stderr=True)
_STDOUT: Final = Console()


def _bootstrap() -> Settings:
    """Load ``.env`` if present, configure logging, return Settings.

    Exits with a friendly message on missing required configuration so
    operators see what to set, not a Python traceback.
    """
    load_dotenv()
    try:
        settings = Settings.from_env()
    except MissingConfigError as exc:
        _CONSOLE.print(f'[bold red]configuration error:[/] {exc}')
        raise typer.Exit(code=2) from exc
    configure_logging()
    return settings


def _select_policy(name: str) -> RankingPolicy:
    try:
        return PRESETS[name]
    except KeyError as exc:
        valid = ', '.join(sorted(PRESETS))
        raise typer.BadParameter(f'unknown policy {name!r}. Available: {valid}') from exc


def _render_resolve_table(result: ResolveResult) -> Table:
    table = Table(title=f'resolve {result.doi}', show_lines=False)
    table.add_column('source')
    table.add_column('version')
    table.add_column('format')
    table.add_column('access')
    table.add_column('status')
    table.add_column('note')
    excluded_by_url = {a.url: reason for a, reason in result.excluded}
    chosen_url = result.chosen.url if result.chosen is not None else None
    for availability in result.candidates:
        is_chosen = availability.url == chosen_url
        excluded_reason = excluded_by_url.get(availability.url)
        if is_chosen:
            status = '[bold green]chosen[/]'
            note = ''
        elif excluded_reason is not None:
            status = '[yellow]excluded[/]'
            note = excluded_reason
        else:
            status = '[dim]candidate[/]'
            note = ''
        table.add_row(
            availability.source_kind.value,
            availability.version.value,
            availability.format.value,
            availability.access.value,
            status,
            note,
        )
    if not result.candidates:
        table.add_row('(none)', '', '', '', '[red]no source[/]', '')
    return table


def _render_probe_table(result: ResolveResult) -> Table:
    table = Table(title='probes', show_lines=False)
    table.add_column('probe')
    table.add_column('hits', justify='right')
    table.add_column('ms', justify='right')
    table.add_column('error')
    for outcome in result.probes:
        error = outcome.error or ''
        table.add_row(
            outcome.probe,
            str(len(outcome.availabilities)),
            str(outcome.duration_ms),
            error,
        )
    return table


def _record_panel(record: AcquisitionRecord, title: str) -> Panel:
    body = (
        f'[bold]doi[/]      {record.doi}\n'
        f'[bold]sha256[/]   {record.sha256}\n'
        f'[bold]size[/]     {record.byte_size:,} bytes\n'
        f'[bold]format[/]   {record.source.format.value}\n'
        f'[bold]version[/]  {record.source.version.value}\n'
        f'[bold]source[/]   {record.source.source_kind.value}\n'
        f'[bold]origin[/]   {record.origin.value}\n'
        f'[bold]license[/]  {record.source.license or "—"}\n'
        f'[bold]path[/]     {record.artifact_path}'
    )
    return Panel(body, title=title, border_style='green')


@app.command()
def resolve(
    doi: Annotated[str, typer.Argument(help='DOI to resolve.')],
    policy: Annotated[
        str,
        typer.Option('--policy', help='Ranking policy preset.'),
    ] = 'published_first',
    json_output: Annotated[
        bool,
        typer.Option('--json', help='Emit cattrs-unstructured JSON to stdout.'),
    ] = False,
) -> None:
    """Resolve a DOI to its best available acquisition route."""
    settings = _bootstrap()
    selected = _select_policy(policy)
    store = ArtifactStore(settings.data_dir)
    result = asyncio.run(resolver_resolve(doi, settings=settings, store=store, policy=selected))

    if json_output:
        from litspectraits.acquisition.manifest import converter

        _STDOUT.print_json(json.dumps(converter.unstructure(result), default=str))
        return

    _CONSOLE.print(_render_probe_table(result))
    _CONSOLE.print(_render_resolve_table(result))
    if result.chosen is None:
        _CONSOLE.print('[bold red]no source selected[/] — no fetchable availability')
        raise typer.Exit(code=1)


@app.command()
def ingest(
    doi: Annotated[str, typer.Argument(help='DOI to ingest.')],
    policy: Annotated[
        str,
        typer.Option('--policy', help='Ranking policy preset.'),
    ] = 'published_first',
    force: Annotated[
        bool,
        typer.Option('--force', help='Re-download even if already cached.'),
    ] = False,
) -> None:
    """Resolve and acquire the chosen artifact for a DOI."""
    settings = _bootstrap()
    selected = _select_policy(policy)
    store = ArtifactStore(settings.data_dir)

    async def _run() -> AcquisitionRecord:
        result = await resolver_resolve(doi, settings=settings, store=store, policy=selected)
        if result.chosen is None:
            _CONSOLE.print(_render_resolve_table(result))
            raise NoSourceAvailableError(f'no fetchable source for DOI {result.doi}')
        async with http_client(settings) as client:
            return await acquire(result, store=store, client=client, force=force)

    try:
        record = asyncio.run(_run())
    except NoSourceAvailableError as exc:
        _CONSOLE.print(f'[bold red]ingest failed:[/] {exc}')
        raise typer.Exit(code=1) from exc

    _CONSOLE.print(_record_panel(record, title='acquired'))


@app.command()
def sideload(
    doi: Annotated[str, typer.Argument(help='DOI of the paper.')],
    path: Annotated[Path, typer.Argument(help='File to sideload.')],
    version: Annotated[
        Version,
        typer.Option('--version', help='Operator-asserted version axis.'),
    ],
    fmt: Annotated[
        Format,
        typer.Option('--format', help='Operator-asserted format axis.'),
    ],
    license_assertion: Annotated[
        str,
        typer.Option('--license', help='License string to record.'),
    ],
    note: Annotated[
        str,
        typer.Option('--note', help='Free-text justification for the manifest.'),
    ] = '',
    source_url: Annotated[
        str | None,
        typer.Option('--source-url', help='URL the operator hit, when known.'),
    ] = None,
) -> None:
    """Register a manually-retrieved artifact in the store."""
    settings = _bootstrap()
    store = ArtifactStore(settings.data_dir)
    record = sideload_artifact(
        doi=doi,
        path=path,
        version=version,
        format=fmt,
        license_assertion=license_assertion,
        note=note,
        source_url=source_url,
        settings=settings,
        store=store,
    )
    _CONSOLE.print(_record_panel(record, title='sideloaded'))


def main() -> None:  # pragma: no cover
    """Console-script entry point."""
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == '__main__':  # pragma: no cover
    main()
