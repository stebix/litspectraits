"""Format → extractor dispatch (``docs/overview-v3.md`` §11).

A three-way ``match`` on :attr:`AcquisitionRecord.format`. The PDF branch
is wired (step 10b); the JATS and Elsevier branches still raise
:class:`NotImplementedError` until 10c / 10d land.

The :class:`~litspectraits.errors.ExtractError` taxonomy is the
load-bearing piece of step 10a — every leaf extractor plugs into a
dispatcher that already speaks the right error language.
"""

from litspectraits.extract.pdf import extract_pdf
from litspectraits.manifest import AcquisitionRecord, ExtractRecord, Format
from litspectraits.store import ArtifactStore


async def extract(
    record: AcquisitionRecord,
    store: ArtifactStore,
    *,
    reextract: bool = False,
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
        ``documents/<sha>/`` outputs atomically (same tmp → rename pattern
        as ingest).
    reextract : bool, default False
        Whether to overwrite an existing extraction whose serialized
        ``document.json`` differs from the one we are about to write.
        Surfaces as :class:`~litspectraits.errors.ExtractIntegrityError`
        when ``False`` and bytes differ.

    Returns
    -------
    ExtractRecord
        Returned by the leaf extractor.

    Raises
    ------
    NotImplementedError
        For :attr:`Format.JATS_XML` and :attr:`Format.ELSEVIER_XML` until
        steps 10c / 10d land.
    litspectraits.errors.ExtractError
        Any extractor-side failure. The class itself is the contract.
    """
    match record.format:
        case Format.PDF:
            return await extract_pdf(record, store, reextract=reextract)
        case Format.JATS_XML:
            raise NotImplementedError('extract_jats lands in step 10c')
        case Format.ELSEVIER_XML:
            raise NotImplementedError('extract_elsevier lands in step 10d')
