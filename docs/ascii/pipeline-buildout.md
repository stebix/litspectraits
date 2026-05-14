# Pipeline buildout (ASCII)

Snapshot of the current v3 pipeline as wired in `src/litspectraits/`. The
dashed measurement-records lane at the bottom is design-only (see
`docs/agentic-buildout-sketch.md`); everything above it corresponds to
code on `trunk` today.

Steps 1-9 of `overview-v3.md` §17 are landed; Step 10 extraction has
10a-10d wired and 10g (frozen docling settings) just committed.

```
                                   ┌─────────────────────────────────────────┐
                                   │  cli.py (Click): ingest / extract /     │
                                   │  sideload / doctor / show               │
                                   └───────────────────┬─────────────────────┘
                                                       │
        ┌──────────────────────────────────────────────┼──────────────────────────────┐
        │                                              │                              │
        ▼                                              ▼                              ▼
┌───────────────┐                            ┌───────────────────┐          ┌──────────────────┐
│  doctor.py    │                            │   ingest.ingest() │          │  sideload.py     │
│  egress IP +  │                            │   orchestrator    │          │  PDF-only manual │
│  per-publisher│                            │  (§0 happy path)  │          │  load + Manual-  │
│  creds + dock-│                            └─────────┬─────────┘          │  Provenance      │
│  ling preflt. │                                      │                    └────────┬─────────┘
└───────────────┘                                      │                             │
                                                       ▼                             │
                                       ┌─────────────────────────────┐               │
                                       │ 1. doi.normalize()          │               │
                                       │    InvalidDOIError (≠Ingest)│               │
                                       └──────────────┬──────────────┘               │
                                                      ▼                              │
                                       ┌─────────────────────────────┐               │
                                       │ 2. metadata.fetch_metadata  │               │
                                       │    CrossRef + polite-pool   │               │
                                       │    UA/mailto                │               │
                                       │    → CrossRefMetadata       │               │
                                       │    DOINotFoundError (404)   │               │
                                       └──────────────┬──────────────┘               │
                                                      ▼                              │
                                       ┌─────────────────────────────┐               │
                                       │ 3. publisher_for_doi()      │               │
                                       │    DOI-prefix table; warn   │               │
                                       │    on free-text mismatch    │               │
                                       └──────────────┬──────────────┘               │
                                                      ▼                              │
                                       ┌─────────────────────────────┐               │
                                       │ 4. retrievers.dispatch      │               │
                                       │    retriever_for(publisher) │               │
                                       └──────┬───────┬───────┬──────┘               │
                                              │       │       │                      │
                                  ┌───────────┘       │       └────────────┐         │
                                  ▼                   ▼                    ▼         │
                          ┌──────────────┐   ┌────────────────┐   ┌────────────────┐ │
                          │ wiley.py     │   │ springer.py    │   │ elsevier.py    │ │
                          │ wiley-tdm SDK│   │ dual-tier:     │   │ raw httpx +    │ │
                          │ (token+IP)   │   │  OA api ↔ TDM  │   │ view=FULL;     │ │
                          │ → Format.PDF │   │  api key       │   │ META_ABS guard │ │
                          │              │   │ → JATS_XML     │   │ → ELSEVIER_XML │ │
                          └──────┬───────┘   └────────┬───────┘   └────────┬───────┘ │
                                 │                    │                    │         │
                                 └────────────┬───────┴────────────────────┘         │
                                              │                                      │
                            (shared: _ratelimit token bucket, base.Retriever,        │
                             SDK-exception → IngestError translation)                │
                                              │                                      │
                                              ▼                                      │
                                ┌─────────────────────────────┐                      │
                                │ RetrievePayload (tmp_path,  │                      │
                                │   sha256, byte_size, format,│                      │
                                │   fetched_url, sdk_version) │                      │
                                └──────────────┬──────────────┘                      │
                                               ▼                                     ▼
                                ┌──────────────────────────────────────────────────────┐
                                │ 5. sniff.verify()                                    │
                                │    4 KiB magic-byte re-check at orchestrator boundary│
                                │    %PDF- / JATS root / Elsevier root                 │
                                │    SniffMismatchError                                │
                                └──────────────┬───────────────────────────────────────┘
                                               ▼
                                ┌──────────────────────────────────────────────────────┐
                                │ 6. store.commit(): tmp/ → artifacts/<fmt>/aa/<sha>   │
                                │    via os.replace; write manifest; append by_doi.jsonl│
                                │    idempotent on (sha256, byte_size)                 │
                                └──────────────┬───────────────────────────────────────┘
                                               ▼
                                  ┌────────────────────────────┐
                                  │  AcquisitionRecord written │
                                  │  (manifest.json on disk)   │
                                  └──────────────┬─────────────┘
                                                 │
                                                 │  cli `extract <doi>`
                                                 ▼
                                  ┌────────────────────────────┐
                                  │ extract._dispatch.extract()│
                                  │   match record.format      │
                                  └──┬───────────┬───────────┬─┘
                                     │           │           │
                                     ▼           ▼           ▼
                              ┌──────────┐ ┌──────────┐ ┌────────────┐
                              │ pdf.py   │ │ jats.py  │ │ elsevier.py│
                              │ docling  │ │ lxml +   │ │ lxml +     │
                              │ 6-stage  │ │ shared   │ │ shared     │
                              │ (Egret + │ │ _lxml_   │ │ _lxml_     │
                              │ formula  │ │ helpers  │ │ helpers    │
                              │ enrich)  │ │          │ │            │
                              └────┬─────┘ └────┬─────┘ └────┬───────┘
                                   └────────────┼────────────┘
                                                ▼
                                  ┌────────────────────────────┐
                                  │  ExtractRecord +           │
                                  │  documents/<sha>/document  │
                                  │  .json (atomic)            │
                                  └──────────────┬─────────────┘
                                                 │
                                                 ▼
                                  ╔═════════════════════════════╗
                                  ║  PLANNED (sketch only):     ║
                                  ║  Ingestor → Auditor →       ║
                                  ║  Corrector agents emit      ║
                                  ║  append-only measurement    ║
                                  ║  records keyed back to      ║
                                  ║  Block offsets              ║
                                  ╚═════════════════════════════╝

Filesystem layout under <data_dir>/:
  artifacts/{pdf,jats,elsevier}/sha256/<aa>/<sha>.<ext>     ← bytes
  manifests/sha256/<aa>/<sha>.manifest.json                 ← AcquisitionRecord
  documents/<sha>/document.json                             ← ExtractRecord output
  index/by_doi.jsonl   (doi, sha256, format, manifest_path) ← single-scan lookup
  tmp/                                                      ← wiped on store init

Cross-cutting:
  config.Settings   — env-driven creds, rate limits, contact email (loud at boot)
  _logging          — structlog + Rich/JSON switch; DOI bound via contextvars
  errors            — IngestError taxonomy (one exit code per subclass)
  http              — shared httpx.AsyncClient w/ polite-pool UA
```
