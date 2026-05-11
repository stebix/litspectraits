"""Format → extractor dispatch (``docs/overview-v3.md`` §11).

A three-way ``match`` on :attr:`AcquisitionRecord.format`. The branches
will be replaced with calls to ``extract_pdf`` / ``extract_jats`` /
``extract_elsevier`` in steps 10b / 10c / 10d; in step 10a every branch
raises :class:`NotImplementedError` so the wiring is exercised by tests
even though no extractor body has landed yet.

The :class:`~litspectraits.errors.ExtractError` taxonomy is the
load-bearing piece of this step — the per-format modules will plug into a
dispatcher that already speaks the right error language.
"""

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
        as ingest). Unused at this dispatch level; threaded through.
    reextract : bool, default False
        Whether to overwrite an existing extraction whose serialized
        ``document.json`` differs from the one we are about to write.
        Surfaces as :class:`~litspectraits.errors.ExtractIntegrityError`
        when ``False`` and bytes differ.

    Returns
    -------
    ExtractRecord
        Returned by the leaf extractor. Not currently produced — every
        branch raises :class:`NotImplementedError` until its commit lands.

    Raises
    ------
    NotImplementedError
        For every :class:`Format` value until the corresponding extractor
        commit lands (10b for PDF, 10c for JATS, 10d for Elsevier).
    litspectraits.errors.ExtractError
        Any extractor-side failure once a leaf is wired. The class itself
        is the contract.
    """
    del store, reextract  # consumed by leaf extractors in 10b/c/d
    match record.format:
        case Format.PDF:
            raise NotImplementedError('extract_pdf lands in step 10b')
        case Format.JATS_XML:
            raise NotImplementedError('extract_jats lands in step 10c')
        case Format.ELSEVIER_XML:
            raise NotImplementedError('extract_elsevier lands in step 10d')
