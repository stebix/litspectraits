"""Format → extractor dispatch (``docs/overview-v3.md`` §11).

A three-way ``match`` on :attr:`AcquisitionRecord.format`. All three legs
are now wired (PDF step 10b, JATS step 10c, Elsevier step 10d).

The :class:`~litspectraits.errors.ExtractError` taxonomy is the
load-bearing piece of step 10a — every leaf extractor plugs into a
dispatcher that already speaks the right error language.
"""

from pathlib import Path

from litspectraits.extract.elsevier import extract_elsevier
from litspectraits.extract.jats import extract_jats
from litspectraits.extract.pdf import extract_pdf
from litspectraits.manifest import AcquisitionRecord, ExtractRecord, Format
from litspectraits.store import ArtifactStore


async def extract(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    reextract: bool = False,
    model_cache_dir: Path | None = None,
) -> ExtractRecord:
    """Dispatch ``record`` to the format-specific extractor.

    Parameters
    ----------
    record : AcquisitionRecord
        Manifest of the committed artifact. ``record.format`` selects the
        leaf extractor; ``record.artifact_path`` (relative to
        ``store.data_dir``) is where the leaf reads bytes from.
    store : ArtifactStore
        Used by leaf extractors to read the artifact and stage
        ``documents/sha256/<aa>/<sha>/`` outputs atomically (same tmp → rename pattern
        as ingest).
    reextract : bool, default False
        Whether to overwrite an existing extraction whose serialized
        ``document.json`` differs from the one we are about to write.
        Surfaces as :class:`~litspectraits.errors.ExtractIntegrityError`
        when ``False`` and bytes differ.
    model_cache_dir : pathlib.Path | None, default None
        Docling model-weights directory; only consulted on the
        :attr:`Format.PDF` leg (the XML extractors carry no model). See
        :func:`litspectraits.extract.pdf.extract_pdf`.

    Returns
    -------
    ExtractRecord
        Returned by the leaf extractor.

    Raises
    ------
    litspectraits.errors.ExtractError
        Any extractor-side failure. The class itself is the contract.
    """
    match record.format:
        case Format.PDF:
            return await extract_pdf(
                record, store, reextract=reextract, model_cache_dir=model_cache_dir
            )
        case Format.JATS_XML:
            return await extract_jats(record, store, reextract=reextract)
        case Format.ELSEVIER_XML:
            return await extract_elsevier(record, store, reextract=reextract)
