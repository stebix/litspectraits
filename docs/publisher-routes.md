# Design Doc: Programmatic Full-Text Retrieval from Major STM Publishers

**Status:** Draft
**Scope:** A new package (working name: `pubfetch`) providing DOI-driven full-text retrieval from Wiley, Springer Nature, and Elsevier, with a uniform Python API and pluggable per-publisher adapters.

---

## 1. Motivation

Building a literature corpus for downstream tasks (text mining, RAG over MR physics
literature, supervised-extraction of relaxometry values, cross-publisher reproducibility
checks) currently requires interacting with three different vendor APIs, each with its
own auth scheme, response format, entitlement model, and Python wrapper of varying
maturity. This package consolidates those into a single Pythonic interface so that
callers work in terms of `(doi, format) → Article` and not in terms of vendor-specific
URLs and headers.

Out of scope: open-access aggregators (PMC, arXiv, OpenAIRE), paywalled non-Elsevier
journals served via Crossref-affiliated members other than the three above, and any
form of credential sharing or distribution of retrieved corpora.

## 2. Goals and Non-Goals

### Goals

- **G1.** Single Python entry point: `client.fetch(doi)` returns a normalized `Article`
  object regardless of publisher.
- **G2.** Publisher detection from DOI prefix with explicit override.
- **G3.** Native format passthrough (PDF, JATS XML, plain text) with optional
  best-effort normalization to plain text.
- **G4.** Robust handling of the three failure modes that actually occur in practice:
  not entitled, rate-limited, and IP-not-recognized.
- **G5.** Idempotent local caching keyed by `(publisher, doi, format)` so reruns of a
  corpus build do not re-hit the APIs.
- **G6.** Fits into the existing UV monorepo as a workspace package; no global state,
  no environment-variable-only configuration.

### Non-Goals

- Not a search engine. Discovery (DOI lists) is delegated to Crossref / Scopus /
  PubMed / OpenAlex; this package consumes DOIs.
- Not a sharing platform. Per Elsevier's TDM terms in particular, retrieved corpora
  are personal to the API key holder and must not be redistributed.
- Not a unified-XML normalizer. The three publishers emit different schemas (Wiley
  PDFs, Springer JATS, Elsevier full-text XML); harmonizing them into a common
  semantic model is a separate, downstream concern.

## 3. Background: The Publisher TDM Landscape

The three publishers expose conceptually similar services with materially different
mechanics. The asymmetries directly shape the adapter design.

| Aspect | Wiley | Springer Nature | Elsevier |
|---|---|---|---|
| Endpoint | `api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}` | `api.springernature.com/{api}/...` | `api.elsevier.com/content/article/doi/{doi}` |
| Auth | `Wiley-TDM-Client-Token` header | `api_key` query param | `X-ELS-APIKey` header |
| Default format | **PDF** | **JATS XML** | **XML** (plain text via `Accept`) |
| Entitlement model | Token + IP allowlist | Free OA tier; premium key for closed-access TDM | Token + subscribing-institution IP |
| Official Python SDK | [`WileyLabs/tdm-client`](https://github.com/WileyLabs/tdm-client) (`pip install wiley-tdm`) | [`springernature/springernature_api_client`](https://github.com/springernature/springernature_api_client) | [`ElsevierDev/elsapy`](https://github.com/ElsevierDev/elsapy) |
| Rate limit | 3 req/s | per-minute quota; premium tier increases | per-key, varies by product |
| DOI prefixes (typical) | `10.1002/`, `10.1111/` | `10.1007/`, `10.1038/`, `10.1057/` | `10.1016/`, `10.1006/` |
| Cross-publisher discovery layer | [Crossref TDM service](https://www.crossref.org/services/text-and-data-mining/) — exposes `text-mining` link relations in Crossref metadata; both Elsevier and Springer Nature recommend it as the multi-publisher entry point. |

Two practical consequences:

1. **Output format is not unified at the wire level.** A `pubfetch.fetch(doi)` call
   that hits Wiley returns bytes-of-PDF; the same call against Springer or Elsevier
   returns XML. We accept this and surface format explicitly on the response object,
   rather than pretending it's hidden.
2. **Entitlement is IP-scoped, not just key-scoped, for Wiley and Elsevier.**
   Running from an arbitrary cloud VM or a non-Würzburg IP will silently degrade to
   abstract-only or 403, regardless of how valid the API key is. The dev/prod
   environments must run from a Würzburg-network egress (uni VPN or an on-prem host
   on the homelab side of the VPN tunnel).

## 4. Architecture

```
┌───────────────────────────────────────────────────────┐
│                  pubfetch.Client                      │
│  ┌─────────────────────────────────────────────────┐  │
│  │ resolve_publisher(doi) → PublisherAdapter       │  │
│  └─────────────────────────────────────────────────┘  │
│                        │                              │
│   ┌────────────────────┼────────────────────┐         │
│   ▼                    ▼                    ▼         │
│ WileyAdapter   SpringerAdapter      ElsevierAdapter   │
│   │                    │                    │         │
│   ▼                    ▼                    ▼         │
│ wiley-tdm     springernature_api_client   elsapy      │
│   │                    │                    │         │
│   ▼                    ▼                    ▼         │
│ HTTPS to api.wiley/api.springer/api.elsevier          │
└───────────────────────────────────────────────────────┘
              │
              ▼
        Local cache (filesystem, content-addressed by DOI+fmt)
```

### 4.1 Layering

- **Vendor SDK layer.** We *use* the official wrappers where they exist
  (`wiley-tdm`, `springernature_api_client`, `elsapy`) rather than reimplementing
  HTTP. They are thin, MIT/permissive, and absorb auth-header bookkeeping.
- **Adapter layer.** One module per publisher. Each adapter exposes the same
  protocol (Section 5.2), translating from our `Article` request into the SDK's
  call shape and back.
- **Client layer.** Publisher resolution, caching, retry/backoff, credential
  management, structured logging.

### 4.2 Why not call vendor APIs directly?

The vendor SDKs are essentially `requests.get` with the right headers. We could
reimplement, but doing so means tracking endpoint changes ourselves and re-deriving
quirks (Wiley's redirect chain to `onlinelibrary.wiley.com`, Elsevier's
`view=FULL` parameter semantics, Springer's `s`/`p` pagination). The cost of
depending on the SDKs is small and the maintenance burden they absorb is real.

The one exception worth flagging: `springernature_api_client`'s TDM class is the
only path into Springer's premium full-text API; the third-party wrapper
[`sprynger`](https://github.com/nils-herrmann/sprynger) is a cleaner library but
covers only the three free APIs.

## 5. Interface Design

### 5.1 Data Model

```python
@attrs.frozen
class Article:
    doi: str
    publisher: Publisher                # enum: WILEY | SPRINGER | ELSEVIER
    format: ArticleFormat               # enum: PDF | JATS_XML | ELSEVIER_XML | PLAIN_TEXT
    content: bytes                      # raw payload
    retrieved_at: datetime
    source_url: str                     # for provenance
    entitled: bool                      # False ⇒ content is abstract-only fallback
```

Rationale for `entitled`: Elsevier in particular silently downgrades unentitled
requests to a `META_ABS` view rather than 403'ing. We surface this explicitly so
that callers building corpora can filter rather than discover-by-surprise that
half their "full texts" are abstracts.

### 5.2 Adapter Protocol

```python
class PublisherAdapter(Protocol):
    publisher: ClassVar[Publisher]
    doi_prefixes: ClassVar[frozenset[str]]

    def fetch(self, doi: str, *, format: ArticleFormat | None = None) -> Article: ...
    def supports(self, format: ArticleFormat) -> bool: ...
```

Format negotiation rule: if the caller passes a `format` the adapter does not
support, raise `UnsupportedFormatError` rather than silently substituting. The
default format per adapter is the publisher's native one (PDF for Wiley, JATS for
Springer, XML for Elsevier).

### 5.3 Client Surface

```python
client = pubfetch.Client(
    credentials=pubfetch.Credentials.from_env(),  # or .from_file(path)
    cache_dir="~/.cache/pubfetch",
    rate_limits={"wiley": 3.0, "springer": 5.0, "elsevier": 6.0},  # req/s
)

article = client.fetch("10.1002/mrm.29635")
articles = client.fetch_many(dois, max_workers=4)  # respects per-publisher rate
```

`fetch_many` is the only concurrency surface. Internally it groups by publisher
and applies a per-publisher token-bucket so we never burst above the documented
rate limits regardless of input ordering.

## 6. Per-Publisher Adapter Notes

### 6.1 Wiley

- Endpoint: `https://api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}`
- Auth header: `Wiley-TDM-Client-Token: <token>`
- Native format: PDF only. No XML option.
- Token signup: Wiley TDM console (institutional contract required).
- IP allowlist: enforced; the Wiley TDM console logs the observed source IP, which
  must match the institution's subscribing range.
- SDK call: `wiley_tdm.TDMClient(token=...).download_pdf(doi, dest_dir=...)`.

### 6.2 Springer Nature

- Developer portal: <https://dev.springernature.com>
- Three free APIs (`Meta`, `Metadata`, `OpenAccess`) plus the premium Full Text /
  TDM API. The OpenAccess API returns full text for OA articles only; closed-access
  TDM requires the premium subscription, typically arranged through
  `supportapi@springernature.com` or via the institutional TDM agreement.
- Auth: `api_key` query parameter.
- Native format: JATS XML.
- SDK call: `springernature_api_client.tdm.TDMAPI(api_key=...).search(...)` for
  query-based access, or per-DOI retrieval via the OpenAccess/TDM endpoints.
- Note: third-party `sprynger` is preferable ergonomically but does not implement
  the premium TDM endpoint, so we use the official client for the closed-access
  path and may consider `sprynger` for the OA fallback.

### 6.3 Elsevier

- Developer portal: <https://dev.elsevier.com>
- Endpoint: `https://api.elsevier.com/content/article/doi/{doi}`
- Auth header: `X-ELS-APIKey: <key>` (alternatively `?APIKey=...` query param).
- Native format: XML; plain text available via `Accept: text/plain` (or
  `httpAccept=text/plain` query param). PDF is *not* offered through the TDM API.
- Entitlement: requires the request to originate from a subscribing institution's
  IP range. Unentitled requests return the `META_ABS` (abstract-only) view by
  default, or an error if `view=FULL` is explicitly requested. We always pass
  `view=FULL` and translate the resulting error into `entitled=False`.
- SDK call: `elsapy.elsdoc.FullDoc(doi=...).read(client)` where `client` is an
  `elsapy.elsclient.ElsClient(api_key)`.

### 6.4 Crossref discovery (utility, not an adapter)

For input lists where the publisher is unknown, `pubfetch.crossref.resolve(doi)`
returns the publisher and any `text-mining` link relations advertised in the
Crossref metadata. This is purely a discovery helper; once the publisher is known,
the per-publisher adapter handles retrieval.

## 7. Cross-Cutting Concerns

### 7.1 Credentials

A single `Credentials` object holds all three keys; missing keys are tolerated as
long as no call is made to the corresponding adapter. Loading order:

1. Explicit constructor argument.
2. `~/.config/pubfetch/credentials.toml` (preferred for the dev server).
3. Environment variables: `WILEY_TDM_TOKEN`, `SPRINGER_API_KEY`, `ELSEVIER_API_KEY`.

No keys in source, no keys in the cache, no keys in logs. Logging of failed
requests redacts header values.

### 7.2 Rate Limiting

Per-publisher token-bucket implemented in the client, defaulting to the documented
limits (Wiley 3 req/s; Springer and Elsevier conservative defaults until empirical
measurement). On HTTP 429, exponential backoff with jitter; on three consecutive
429s, the publisher is marked cold for a configurable cooldown.

### 7.3 Caching

Filesystem cache, content-addressed:

```
{cache_dir}/{publisher}/{doi_safe}.{ext}
{cache_dir}/{publisher}/{doi_safe}.meta.json
```

`doi_safe` replaces `/` with `_`. The `.meta.json` sidecar records `retrieved_at`,
`source_url`, `entitled`, and the response status. Cache hits skip the network
entirely. A `force_refresh=True` flag bypasses the cache for individual calls.

### 7.4 Error Taxonomy

```
PubfetchError
├── ConfigurationError       # missing credentials, unknown publisher
├── PublisherError
│   ├── NotEntitledError     # 403 / Elsevier META_ABS fallback
│   ├── NotFoundError        # 404, DOI not in publisher's corpus
│   ├── RateLimitError       # 429 after retries exhausted
│   └── PublisherAPIError    # 5xx, malformed response, etc.
└── UnsupportedFormatError
```

Adapters translate vendor SDK exceptions into this taxonomy so callers don't need
to know which SDK raised what.

### 7.5 Output Normalization (optional, off by default)

`Article.as_text()` performs a best-effort extraction:

- PDF → `pypdfium2` or similar, with a warning that layout-dependent content
  (tables, equations, figure captions) is lossy.
- JATS XML / Elsevier XML → strip tags, preserve paragraph structure.

This is a convenience, not a feature — anything semantically meaningful (section
structure, references, equation handling) belongs in a downstream parser.

## 8. Operational Considerations

### 8.1 IP and Network

The dev/prod environment for any non-trivial corpus build needs to be on the
Würzburg institutional network. Practical options:

- Run from a uni-IP host directly.
- Run from the homelab over the existing AnyConnect/OpenConnect tunnel, with
  the tunnel terminating at a uni gateway. Egress traffic for `*.wiley.com`,
  `*.springer.com`, `*.springernature.com`, and `*.elsevier.com` must route
  through the tunnel.

A `pubfetch doctor` CLI subcommand checks observed egress IP against the expected
range and warns before any retrieval is attempted; this catches the
silently-getting-abstracts failure mode early.

### 8.2 Coordination with University Library

The TDM agreements with all three publishers are negotiated by the library, not
self-service. Specifically:

- Wiley TDM token issuance and IP allowlisting.
- Springer Nature premium API key for closed-access TDM.
- Elsevier API key registration tied to the institutional subscription.

Before the first use against each publisher, confirm with the library that the
intended use (corpus size, retention period, downstream task) falls within the
institutional agreement.

### 8.3 Compliance

- Elsevier explicitly forbids redistribution of retrieved corpora, including
  within the institution. Caches are personal; sharing happens at the DOI-list
  level.
- All three publishers permit non-commercial research use and forbid commercial
  reuse. The package's documentation states this prominently.

## 9. Open Questions

1. **Async or sync?** The existing `mritools` packages are sync-oriented; JAX is
   sync at the API surface. `fetch_many` is the only place concurrency matters,
   and a thread pool is sufficient for I/O-bound work. Defaulting to sync.
2. **Should the cache be content-hashed or DOI-keyed?** DOI-keyed is simpler;
   content-hashed catches the case where a publisher silently re-renders a PDF.
   Going with DOI-keyed for v1 and revisiting if drift is observed.
3. **PMC fallback?** Many Springer/Elsevier articles are also in PMC under OA
   licenses. Adding a PMC adapter would let us bypass entitlement entirely for
   those — worth a v2 epic.
4. **Format normalization scope.** Is `as_text()` enough, or do we want
   structured (sections, references) extraction in this package? Current bias:
   keep this package retrieval-only; structured extraction lives downstream.

## 10. References

- Wiley TDM client: <https://github.com/WileyLabs/tdm-client>
- Springer Nature developer portal: <https://dev.springernature.com>
- Springer Nature official Python client:
  <https://github.com/springernature/springernature_api_client>
- `sprynger` (third-party Springer wrapper):
  <https://github.com/nils-herrmann/sprynger>
- Elsevier developer portal: <https://dev.elsevier.com>
- Elsevier TDM endpoint docs: <https://dev.elsevier.com/tecdoc_text_mining.html>
- `elsapy` (Elsevier SDK): <https://github.com/ElsevierDev/elsapy>
- Crossref TDM service: <https://www.crossref.org/services/text-and-data-mining/>
- Elsevier TDM policy & FAQ:
  <https://www.elsevier.com/about/policies-and-standards/text-and-data-mining>