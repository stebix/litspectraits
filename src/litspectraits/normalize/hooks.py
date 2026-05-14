"""cattrs hooks for the normalised :class:`~litspectraits.normalize.models.Document`.

The module-level :data:`converter` is the single (de)serialisation entry
point for the normalize layer; downstream code should not build its own.
:mod:`litspectraits.manifest` keeps an independent converter for the
ingest-side records — the two trees stay structurally identical but
inheritance-disjoint, so they share no hooks today.

The :class:`~litspectraits.normalize.models.Block` tagged-union is
dispatched on the ``type`` field. Unstructuring concrete block
instances works out of the box (cattrs walks the attrs class definition
and emits ``type`` as a literal field); only structuring needs a hook
because the static type is the union and cattrs cannot guess which
variant a dict belongs to without consulting the discriminator.

A second tagged-union structure hook lives in
:mod:`litspectraits.normalize.diff` for the
:data:`~litspectraits.normalize.diff.DualFormatResult` union. That hook
is registered from there against this module's :data:`converter` to
avoid a circular import (``diff`` already depends on ``persistence``,
which depends on this module). Both hooks share the same converter
instance.

The converter also serves :class:`~litspectraits.normalize.persistence.NormalizedMeta`
(the ``meta.json`` sidecar carrying ``normalized_at`` and the route /
completeness summary) — that dataclass is the reason the datetime hooks
are registered below, mirroring
:func:`litspectraits.manifest._build_converter`.
"""

from datetime import datetime
from typing import Any, Final

from cattrs import Converter

from litspectraits.normalize.models import (
    Block,
    EquationBlock,
    FigureBlock,
    TableBlock,
    TextBlock,
)

_BLOCK_BY_TAG: Final[dict[str, type[TextBlock | TableBlock | FigureBlock | EquationBlock]]] = {
    'text': TextBlock,
    'table': TableBlock,
    'figure': FigureBlock,
    'equation': EquationBlock,
}


def _build_converter() -> Converter:
    """Build a converter with the :class:`Block` tagged-union + datetime hooks.

    Datetime hooks mirror :func:`litspectraits.manifest._build_converter`:
    naive datetimes are silently round-tripped (producers are responsible
    for passing tz-aware UTC values). The hooks exist for
    :class:`~litspectraits.normalize.persistence.NormalizedMeta.normalized_at`;
    no field on :class:`~litspectraits.normalize.models.Document` itself
    holds a datetime.

    Notes
    -----
    Unstructure of a concrete :class:`TextBlock` /
    :class:`TableBlock` / etc. just works — cattrs uses
    :func:`type` on the instance and runs the standard attrs
    unstructure, which emits the ``type`` literal field as a string.
    The union-level structure hook is the only thing that needs to
    exist.
    """
    converter = Converter()
    converter.register_unstructure_hook(datetime, lambda value: value.isoformat())
    converter.register_structure_hook(datetime, lambda value, _type: datetime.fromisoformat(value))

    def _structure_block(value: Any, _type: Any) -> Block:
        if not isinstance(value, dict):
            raise TypeError(
                f'block must be a mapping with a `type` discriminator; got {type(value).__name__}'
            )
        tag = value.get('type')
        if tag is None:
            raise ValueError('block is missing the required `type` discriminator')
        cls = _BLOCK_BY_TAG.get(tag)
        if cls is None:
            raise ValueError(
                f'unknown block `type`: {tag!r}; expected one of {sorted(_BLOCK_BY_TAG)}'
            )
        return converter.structure(value, cls)

    converter.register_structure_hook(Block, _structure_block)
    return converter


converter: Final = _build_converter()
