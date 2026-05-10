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
   any IP that holds a valid TDM credential. A non-OA DOI here makes
   doctor false-fail on entitlement (Elsevier ``META_ABS``) or
   ``AuthRejectedError`` (Wiley / Springer) for operators outside the
   Würzburg allow-list — exactly the *failures* doctor is meant to
   detect, but for the wrong reason.
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
    # TODO: verify — pick a Wiley OA Open journal (Wiley OnlineLibrary
    # surfaces these as "Open Access — published under a CC BY licence").
    # Hindawi-portfolio titles migrated to Wiley TDM are good candidates.
    Publisher.WILEY: '10.1002/advs.202002917',
    # TODO: verify — BMC titles (10.1186/...) are Springer-Nature OA by
    # default and live in the premium TDM corpus.
    Publisher.SPRINGER_NATURE: '10.1186/s12880-024-01211-w',
    # TODO: verify — Heliyon (10.1016/j.heliyon...) is Elsevier's gold-OA
    # title; ``view=FULL`` returns the populated ``<originalText>`` for
    # OA papers without an institutional token.
    Publisher.ELSEVIER: '10.1016/j.heliyon.2024.e26000',
}
"""DOI smoke-test registry, keyed by :class:`~litspectraits.manifest.Publisher`.

A missing publisher key would surface as a :class:`KeyError` in
``doctor.run_doctor`` — covered by ``test_doctor.py``'s table-coverage
assertion so the gap fires at test time, not in production.
"""
