"""Format-dispatched extraction (``docs/overview-v3.md`` §11).

One extractor module per :class:`~litspectraits.manifest.Format`:

- :mod:`litspectraits.extract.pdf` — ``docling``; lands in step 10b. Spec
  in ``docs/extract-pdf-plan.md``.
- :mod:`litspectraits.extract.jats` — ``lxml``; lands in step 10c.
- :mod:`litspectraits.extract.elsevier` — ``lxml``; lands in step 10d.
  Emits the same JATS-flavored dict shape so downstream readers can stay
  publisher-agnostic.

Routing through :func:`extract` is keyed on
:attr:`AcquisitionRecord.format`. Failures raise an
:class:`~litspectraits.errors.ExtractError` subclass before any
``documents/sha256/<aa>/<sha>/`` write — same fail-loud discipline as ingest (§14).

Step 10a landed the package, the error taxonomy, and
:class:`~litspectraits.manifest.ExtractRecord`; steps 10b-10d wired the
three concrete leaves.
"""

from litspectraits.extract._dispatch import extract

__all__ = ['extract']
