"""Generic atomic-write and sha256 helpers shared across the package.

Two-step pattern used everywhere on-disk state lives:

- :func:`atomic_write` stages bytes inside ``<data_dir>/tmp/`` and then
  :func:`os.replace`s into the canonical path, so a half-written file is
  never visible at the destination (``docs/overview-v3.md`` §3, CLAUDE.md
  "atomic os.replace from tmp to canonical path is non-negotiable").
- :func:`file_sha256` is the read-side counterpart for integrity checks.

Both originally lived in :mod:`litspectraits.extract._lxml_helpers`; they
were lifted here once the normalize layer (E0.5b persistence) needed the
same shape. ``_lxml_helpers`` re-imports from this module so existing
call sites keep working — the goal is one implementation of the atomic
write pattern across the package.
"""

import hashlib
import os
import secrets
from pathlib import Path


def atomic_write(*, tmp_dir: Path, target: Path, body: bytes) -> None:
    """Stage to ``<tmp_dir>/<rand>.part`` then ``os.replace`` into place.

    The random suffix means two concurrent writes to the same target
    cannot collide on the staging filename — same defence the store's
    manifest write applies. The caller is responsible for ensuring
    ``target.parent`` exists (we deliberately do not ``mkdir`` here so
    a missing target directory surfaces as a loud :class:`FileNotFoundError`
    rather than being silently created).

    Cleanup discipline: if :func:`os.replace` raises (disk full,
    permissions, target dir vanished mid-flight), the staging ``.part``
    file is removed before the exception propagates. ``tmp/`` is cleared
    at store startup as a backstop, but doing the in-process cleanup
    avoids leaking staging files into a long-running session — and lets
    callers assert ``tmp_dir`` is empty after a failure.

    Parameters
    ----------
    tmp_dir : pathlib.Path
        Staging directory. Will be created if missing.
    target : pathlib.Path
        Absolute destination path. Its parent directory must already
        exist; ``os.replace`` requires the destination directory.
    body : bytes
        Payload to write.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    staging = tmp_dir / f'{target.name}.{secrets.token_hex(8)}.part'
    staging.write_bytes(body)
    try:
        os.replace(staging, target)
    except OSError:
        staging.unlink(missing_ok=True)
        raise


def file_sha256(path: Path) -> str:
    """Return the hex sha256 of the bytes at ``path`` (64KB-chunked)."""
    digest = hashlib.sha256()
    with path.open('rb') as fp:
        while chunk := fp.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
