"""Command-line interface (bootstrap stub).

The full v3 CLI surface — ``ingest`` / ``extract`` / ``sideload`` /
``doctor`` / ``show`` — lands in step 9 of the order-of-work
(``docs/overview-v3.md`` §17.9). This stub exists so that the package
imports cleanly while steps 2-8 are being built; it is intentionally
empty.
"""

import typer

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help='DOI-driven literature ingestion pipeline (v3 — under construction).',
)
