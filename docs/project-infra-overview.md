# project infra  overview

## infra

- uv based project -> always run pythonin local uv based `.venv`
- later: use `just` for complex task running

## styling

- ruff lint and formatting
- pyright static type checking
- line length 99
- single quotes preferred - escape for nested sequences

## Docstrings

Numpy-style docstrings


## Code quality

- type hints almost everywhere
- no `from __future__ import annotations`
- No excessive exception catching and silent fallback code -> rather fail loudly