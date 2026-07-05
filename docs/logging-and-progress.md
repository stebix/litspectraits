# Console logging & progress feedback

Design reference for the per-operator log-level knob and the CLI progress
spinner. Implemented across `_logging.py`, `config.py`, `progress.py`,
`cli.py`, `ingest.py`, and `extract/mineru.py`.

## Problem

A plain `litspectraits extract <doi>` drowned the operator in INFO/DEBUG
output. The noise came from **three independent channels**, only one of
which the old `configure_logging(level=INFO)` governed:

| Source | Mechanism | Governed by the root logger? |
|---|---|---|
| Our stage logs (`_logger.info(...)`) | structlog → stdlib root | yes (but was pinned to INFO) |
| docling / httpx / urllib3 / transformers | stdlib `logging` | only if they propagate to root |
| **MinerU** | **loguru** + **tqdm** | **no — bypasses stdlib logging entirely** |

So "just raise the level" could never quiet an extract: loguru and tqdm
ignore Python's `logging`. The feature had to corral all three, default the
console quiet, and still show a human that work is happening.

## What shipped

- **Default console level `warning`** (`LITSPECTRAITS_LOG_LEVEL`, overridable
  per run by the global `--log-level` / `-v` / `-vv` flags). `warning` — not
  `error` — is the default so operationally useful lines (rate-limit backoff,
  docling-degraded) still surface; errors already reach the operator as Rich
  panels / exceptions, independent of log level.
- **A live spinner** with coarse stage labels on the slow commands
  (`ingest`, `sideload`, `smoke`, `extract`, `normalize`, `doctor`).

## Config surface & precedence

`LITSPECTRAITS_LOG_LEVEL` is the per-operator baseline (lives in `.env`,
validated by `config._parse_log_level`). Per invocation:

```
--log-level LEVEL        # explicit; wins as the base
-v / -vv                 # bump: -v = info, -vv = debug — only ever *more* verbose than the base
```

`cli._resolve_log_level` computes `min(base, verbose_target)` on the numeric
levels, so `--log-level error -v` yields INFO and never the reverse. The env
var is read *inside* `_startup` after `dotenv.load_dotenv()` — not via a Typer
`envvar=`, which resolves at parse time before `.env` is loaded. A malformed
env value degrades to WARNING in `_startup` so logging still initialises; the
precise loud error is raised later by `Settings.from_env`.

`-q` is deliberately **not** a global flag — it belongs to `list --quiet`.
The quiet direction is already the default; the useful global direction is `-v`.

## Key decision: gate display at the stdlib layer, not the bound logger

`configure_logging` keeps the structlog **wrapper permissive**
(`make_filtering_bound_logger(logging.DEBUG)`) and gates *display* at the
stdlib root logger + handler (`setLevel(level)`). Every `.info()`/`.debug()`
therefore runs the full processor chain and is dropped only at the stdlib
level check, before the handler.

Two payoffs:

1. `structlog.testing.capture_logs` (which swaps in a recording processor)
   sees events at **any** configured level. Pinning the bound logger to
   `level` instead — the obvious first cut — silently broke every
   `capture_logs`-based test the moment the default dropped to `warning`,
   because the bound logger drops the event before the recording processor
   ever runs.
2. The level knob can move per process without a global-state gotcha.

## Third-party taming (`tame_third_party_logging`)

- **stdlib loggers with their own handlers** (docling, transformers, torch):
  pinned to `level` *by name* — no import of the heavy package required, and
  the level check fires at record creation regardless of any private handler.
- **tqdm**: `TQDM_DISABLE=1` when the console is quiet (`level` above INFO);
  cleared for `-v`/`-vv`. But see the tqdm gotcha below — the env var is only
  half the story.
- **loguru**: `LOGURU_LEVEL` seeded as a floor, but the real work is the
  bridge below.

### The tqdm gotcha: the env var can't reach explicitly-disabled bars

`TQDM_DISABLE` only fills tqdm's `disable` argument when the caller *omits*
it (tqdm's `@envwrap` mechanism). MinerU's VLM client (`mineru_vl_utils`)
passes it **explicitly**:

```python
with tqdm(total=len(inputs), desc="Predict", disable=not self.use_tqdm) as pbar:  # use_tqdm defaults True
```

so the env var provably cannot silence the "Predict" bars — the ones that
actually flooded the console. `extract.mineru._silence_mineru_tqdm` closes the
gap by patching the tqdm class `__init__` to force `disable=True`, which
overrides the explicit argument. It runs in `_load_mineru` (post-import, like
the loguru bridge), gated on the same `TQDM_DISABLE` signal so `-v` keeps the
bars, and idempotent via a module sentinel. Patching the class reaches every
`from tqdm import tqdm` reference (they hold the class object) and, by
inheritance, `tqdm.auto` (the transformers path). Verified against the real
`mineru` and `mineru_vl_utils` call sites.

The env var still earns its keep: it silences the bars that *do* omit
`disable=`, and it is the quiet/verbose signal the backend patch reads.

### The loguru bridge lives in the MinerU backend, post-import

`extract.mineru._bridge_mineru_loguru` drops MinerU's default loguru sink and
adds one that re-emits each record into `logging.getLogger('mineru')` at the
same numeric level (loguru and stdlib share level numbers). It runs in
`_load_mineru`, **right after `import mineru`** — MinerU reconfigures loguru at
import time, so a bridge installed at CLI startup would be clobbered. Once
bridged, MinerU output obeys the single `mineru` stdlib logger level that
`tame_third_party_logging` pins: silent at the `warning` default, visible
under `-v`.

## The spinner (`progress.py`)

An **ambient** reporter: `command_progress(label, enabled=…, console=…)`
binds a Rich `Status` into a `contextvars.ContextVar`; pipeline code calls
`report_stage(label)` to update it without any handle threaded through
signatures (mirrors how `ingest` binds the DOI via `structlog.contextvars`).
It composes across `asyncio.run` because the task copies the current context.

- **Decoupled from log verbosity.** The spinner is fed by explicit
  `report_stage` calls, not by log records — so the level knob and the
  spinner never contend for a token.
- **Enabled only when it helps** (`cli._spinner_enabled`): interactive
  `stderr` TTY, not `--json`, and level above INFO (at INFO/DEBUG the console
  streams log lines and a live spinner would fight them).
- **Never overlaps result/error rendering.** `command_progress` wraps only the
  core awaited work *inside* each command's `try`; the spinner is stopped by
  `__exit__` (including on exception) before any panel is rendered, and it
  shares the command's `stderr` console so Rich coordinates the two.

`ingest` reports four sub-stages (metadata → retrieve → validate → commit);
`extract`/`normalize`/`sideload`/`doctor` show one backend/format-aware label
for the single long call.

## Known limitations

- If a WARNING log fires *while* the spinner is live, the two write to the
  same `stderr` through different code paths and can briefly glitch. Warnings
  are rare at the default level, so this is accepted for now; the fix (route
  the log handler through the spinner's Rich console) is deferred.
- `TQDM_DISABLE` is best-effort: a library that constructs `tqdm(disable=False)`
  explicitly overrides the env var. None of the current backends do.
