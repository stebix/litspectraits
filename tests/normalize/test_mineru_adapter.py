"""Contract-stub tests for the MinerU adapter.

The walk is deferred (``docs/mineru-backend-spec.md`` §10 step 4); these
pin the seam that already exists — the function is importable from the
package surface and fails loud rather than returning a half-built
Document.
"""

import pytest

from litspectraits.normalize import normalize_mineru_document


def test_normalize_mineru_document_is_a_loud_stub() -> None:
    with pytest.raises(NotImplementedError, match='contract stub'):
        normalize_mineru_document({'pdf_info': []})
