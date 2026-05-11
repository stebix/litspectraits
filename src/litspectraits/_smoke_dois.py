"""Per-publisher smoke-test DOIs (``docs/overview-v3.md`` §12, §22).

Single source of truth for the known-OA DOIs that both
:func:`litspectraits.doctor.run_doctor` and the future Step 12 end-to-end
smoke tests dispatch through. Keeping the constants in one module is
explicit in §22:

    Hardcoded OA DOI per publisher in ``src/litspectraits/_smoke_dois.py``
    (or alongside doctor's table). Shared by ``doctor`` (§12) and the
    smoke tests so the constants stay in sync.

Selection criteria
------------------

Each DOI must satisfy three properties:

1. **Open access** — entitlement-free so doctor's smoke test passes from
   any IP that holds a valid publisher credential, on either tier of
   each publisher's API. A non-OA DOI here makes doctor false-fail on
   entitlement (Elsevier ``META_ABS``, Springer ``NotOpenAccessError``
   under the OA tier) or ``AuthRejectedError`` (Wiley / Springer TDM
   tier) for operators outside the Würzburg allow-list — exactly the
   *failures* doctor is meant to detect, but for the wrong reason.

   For Springer specifically: the OA tier (``SPRINGER_OA_API_KEY``) and
   the premium TDM tier (``SPRINGER_TDM_API_KEY``) must both be able to
   serve the chosen DOI. OA imposes the stricter constraint (only OA
   content is reachable), so pick the DOI to satisfy that — the TDM
   tier will then trivially cover it too.
2. **Stable** — published in a journal whose DOIs are not regularly
   redirected or withdrawn. BMC / Heliyon back-catalog typically holds.
3. **Inside the publisher's TDM corpus** — for Springer Nature this
   means the premium TDM index actually carries the article; for
   Elsevier the ``view=FULL`` envelope returns ``<originalText>``.

Operational notes
-----------------

*These constants need a verification pass before any operator runs*
``litspectraits doctor`` *against the real APIs.* Each entry below is
flagged ``# TODO: verify``. The fallout from a wrong choice is doctor
reporting a publisher-side error that isn't real; the local code path
is unaffected.

When a smoke DOI starts failing on a publisher API call rather than an
invariant assertion, suspect the DOI before the code (see §22's
"DOI churn risk" note). Refresh the constant; do not silently fall back
to a different one.
"""

from typing import Final

from litspectraits.manifest import Publisher

SMOKE_DOI: Final[dict[Publisher, str]] = {
   # 2026-05-11: hand picked very recent OA paper
   # Rosette MRF from Seiberlich group
   Publisher.WILEY: '10.1002/mrm.70299',

   # 2026-05-11: hand picked very recent OA paper
   # MAGMA UTE paper from Nan Yin
   Publisher.SPRINGER_NATURE: '10.1007/s10334-026-01362-7',

   # 2026-05-11: hand picked very recent OA paper
   # Image quality assessment by Saher Saeed
   Publisher.ELSEVIER: '10.1016/j.mri.2026.110656',
}
"""DOI smoke-test registry, keyed by :class:`~litspectraits.manifest.Publisher`.

A missing publisher key would surface as a :class:`KeyError` in
``doctor.run_doctor`` — covered by ``test_doctor.py``'s table-coverage
assertion so the gap fires at test time, not in production.
"""
