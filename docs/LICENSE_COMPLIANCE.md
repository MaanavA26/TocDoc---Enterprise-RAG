# Dependency License-Compatibility Audit (BSL 1.1 sellability)

**Scope of this document.** A due-diligence review of third-party dependency
licenses across every component of TocDoc, assessed against how the product is
licensed and shipped: source-available under the **Business Source License 1.1
(BUSL-1.1)**, sold commercially, and **self-hosted by customers** inside their
own infrastructure. The question this audit answers is narrow and specific: *do
any dependencies carry a license that is incompatible with selling and
distributing TocDoc to customers who run it themselves?*

This is a read-only analysis. No code, dependency pin, or manifest was modified.
Where a hard blocker is identified, it is reported for the code owner's decision
rather than acted upon.

> **This is not legal advice.** It is an engineering-level inventory and risk
> assessment intended to surface issues for review by the owner and, where
> warranted, counsel.

---

## 1. Summary verdict

**NOT CLEAN — one hard blocker found.**

The dependency set is, with a single exception, composed of permissive licenses
(MIT, Apache-2.0, BSD) plus a small number of file-level weak-copyleft licenses
(MPL-2.0) that are compatible with commercial distribution and customer
self-hosting. Those pose no obstacle to selling under BUSL-1.1.

The exception is the blocker:

> ### PyMuPDF — AGPL-3.0-or-commercial (Artifex). HARD BLOCKER.
>
> `PyMuPDF` is **dual-licensed: GNU Affero General Public License v3.0
> (AGPL-3.0) OR a paid Artifex commercial license.** Under the free option it
> is **AGPL**, the strongest network-copyleft license. For a product that is
> *sold* and *self-hosted/redistributed* to customers, shipping PyMuPDF under
> its AGPL option means TocDoc itself would have to be offered under AGPL terms
> to recipients — directly incompatible with selling proprietary/commercial
> licenses under BUSL-1.1. This is exactly the class of dependency this audit
> exists to catch.
>
> PyMuPDF appears in **`services/ingestion`** (`PyMuPDF==1.27.2.3`) and in the
> **`eval`** harness (`PyMuPDF==1.26.0`). It is **actively imported and used**
> in `services/ingestion/custom_rag.py` (`import fitz`) for PDF byte
> extraction — it is not a dormant/optional dependency.
>
> **Owner decision required (not taken here):** either (a) purchase an Artifex
> commercial license for PyMuPDF that permits proprietary redistribution, or
> (b) replace PyMuPDF with a permissively-licensed PDF library. Note
> `pypdf==6.12.2` (BSD-3-Clause) is **already pinned** in
> `services/ingestion/requirements.txt`, though it is **not currently imported
> anywhere** — so it is a candidate replacement, not a drop-in already wired.
> Do not treat either option as resolved until the owner chooses.

Everything else is sellable. See the flags section (§4) for the MPL-2.0
weak-copyleft entries, which are compatible but warrant a NOTICE entry.

---

## 2. Components audited

| Component | Manifest | Notes |
|---|---|---|
| Q&A service | `services/qna/requirements.txt` | Fully-pinned resolved closure (langchain 1.x family) |
| Ingestion service | `services/ingestion/requirements.txt` | Fully-pinned resolved closure (langchain 1.x family) |
| Teams bot adapter | `services/teams-bot/requirements.txt` | `botbuilder-core` + reuses the in-repo SDK (`-e ../../clients/python`) |
| Python client SDK | `clients/python/pyproject.toml` | Top-level deps: `httpx`, `pydantic`; optional `langchain` extra: `langchain-core` |
| Eval / RAGAS harness | `eval/requirements.txt` | Decoupled dev/CI-only stack (langchain 0.3.x + `ragas`, `datasets`) |
| Web (Node) | *(none — see below)* | **No `web/` directory and no `package.json` exists in the repo.** |

> **`web/package.json` was in the original audit request but does not exist.**
> A repo-wide search (`find . -name package.json`, excluding `node_modules`)
> returned nothing, and there is no `web/` directory. TocDoc has **no Node
> component**; there is no JavaScript/npm dependency surface to audit. Surfaced
> here so the owner can confirm this matches their understanding.

---

## 3. Methodology — how each license was determined

Two facts shaped the method:

1. The service manifests pin **bleeding-edge / future versions** (e.g.
   `aiohttp==3.14.0`, `numpy==2.4.6`, `pandas==3.0.3`) that are not yet
   published on the available package index, so a faithful pinned-version
   install of the full closure is not possible here.
2. The Q&A/ingestion stack (langchain 1.x) and the eval stack (langchain 0.3.x)
   have **mutually conflicting pins**, so no single environment can hold both
   closures at once.

Approach used, in priority order:

- **`pip-licenses` on installed package metadata (primary).** A clean venv was
  built and packages installed via the device pip proxy
  (`--index-url <internal dev proxy>`), then `pip-licenses` read each package's
  declared `License` / `License-Expression` / `License ::` classifier metadata.
  The **verdict-critical and niche packages were installed at their exact
  pinned versions** — `PyMuPDF==1.27.2.3`, `annotated-doc==0.0.4`,
  `langchain-protocol==0.0.16`, `langchain-classic==1.0.7`, `uuid_utils==0.16.0`,
  `ormsgpack==1.12.2` — so their license metadata is authoritative for the
  shipped version.
- **`pip-licenses` on latest-available versions (for the rest).** Mature
  packages whose exact pin was unavailable on the index were installed at their
  latest available version to read the declared license. License terms for
  these long-established packages are version-stable (a package does not change
  its license between minor releases without it being a notable event), so the
  reported license is reliable even though the version differs from the pin.
  This applies to most of the large Azure / langchain / FastAPI / numeric set.
- **Source verification of usage (for the blocker).** PyMuPDF's actual import
  and use was confirmed by grepping the codebase (`import fitz` in
  `services/ingestion/custom_rag.py`).

The internet (pypi.org, package homepages) was **not reachable** from this
environment; only the internal dev pip proxy was usable. No package license was
asserted from memory alone without metadata confirmation, except where a
latest-version metadata read stands in for an unavailable exact pin (called out
above). Nothing in scope landed in "unknown."

---

## 4. Licenses grouped by type

Counts below reflect the **direct + resolved-transitive** packages observed
across the pinned service closures plus the eval/teams-bot extras. Where a
package declares a multi-license expression (e.g. `Apache-2.0 OR MIT`), it is
listed once under the most representative permissive bucket and the full
expression is shown.

### 4a. MIT (and MIT-family)
`PyJWT`, `SQLAlchemy`, `annotated-doc`, `annotated-types`, `anyio`, `attrs`,
`azure-ai-documentintelligence`, `azure-common`, `azure-core`,
`azure-core-tracing-opentelemetry`, `azure-identity`, `azure-keyvault-secrets`,
`azure-monitor-opentelemetry`, `azure-monitor-opentelemetry-exporter`,
`azure-search-documents`, `azure-storage-blob`, `beautifulsoup4`,
`botbuilder-core`, `botbuilder-schema`, `botframework-connector`,
`botframework-streaming`, `cffi`, `charset-normalizer`, `dataclasses-json`,
`docstring_parser`, `ecdsa`, `et_xmlfile`, `fastapi`, `filelock`, `h11`,
`httpx-sse`, `instructor`, `jiter`, `langchain`, `langchain-classic`,
`langchain-community`, `langchain-core`, `langchain-openai`,
`langchain-protocol`, `langchain-text-splitters`, `langgraph`,
`langgraph-checkpoint`, `langgraph-prebuilt`, `langgraph-sdk`, `langsmith`,
`markdown-it-py`, `marshmallow`, `mdurl`, `msal`, `msal-extensions`, `msrest`,
`mypy_extensions`, `openpyxl`, `pydantic`, `pydantic-settings`, `pydantic_core`,
`python-docx`, `python-jose`, `python-pptx`, `PyYAML`, `rich`, `six`,
`soupsieve`, `sqlmodel`, `tiktoken`, `typer`, `typing-inspect`,
`typing-inspection`, `urllib3`, `zipp`, `appdirs`, `pillow` (MIT-CMU).

### 4b. Apache-2.0
`aiosignal`, `datasets`, `diskcache`, `distro`, `frozenlist`, `hf-xet`,
`huggingface_hub`, `importlib_metadata`, `multidict`, `openai`,
`opentelemetry-api`, `opentelemetry-instrumentation` (+ all
`opentelemetry-instrumentation-*` sub-packages),
`opentelemetry-resource-detector-azure`, `opentelemetry-sdk`,
`opentelemetry-semantic-conventions`, `opentelemetry-util-http`, `propcache`,
`pyarrow`, `pytesseract`, `python-multipart`, `ragas`, `requests`,
`requests-toolbelt`, `rsa`, `tenacity`, `yarl`.
Multi-licensed (Apache option available): `aiohttp` (Apache-2.0 AND MIT),
`cryptography` (Apache-2.0 OR BSD-3-Clause), `ormsgpack` (Apache-2.0 OR MIT),
`packaging` (Apache-2.0 OR BSD-2-Clause), `regex` (Apache-2.0 AND CNRI-Python).

### 4c. BSD (2-Clause / 3-Clause)
`asgiref`, `click`, `dill`, `fsspec`, `httpcore`, `httpx`, `idna`, `isodate`,
`Jinja2`, `jsonpatch`, `jsonpickle`, `jsonpointer`, `lxml`, `MarkupSafe`,
`multiprocess`, `nest-asyncio`, `networkx`, `numpy`
(BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0), `oauthlib`, `pandas`,
`psutil`, `pyasn1`, `pycparser`, `Pygments`, `pypdf`, `python-dateutil`
(Apache/BSD dual), `python-dotenv`, `requests-oauthlib`, `scikit-network`,
`scipy`, `starlette`, `uuid_utils`, `uvicorn`, `websockets`, `wrapt`,
`XlsxWriter`, `xxhash`, `zstandard`.

### 4d. MPL-2.0 (weak / file-level copyleft) — compatible, attribution warranted
| Package | Component(s) | Declared license |
|---|---|---|
| `certifi` | all services + eval | Mozilla Public License 2.0 (MPL-2.0) |
| `tqdm` | eval (via `datasets`/`ragas`) | MPL-2.0 AND MIT |
| `orjson` | qna, ingestion | MPL-2.0 AND (Apache-2.0 OR MIT) |

**Why these are not a blocker.** MPL-2.0 is *file-level* (weak) copyleft: its
obligations attach only to modifications of the MPL-covered files themselves and
do **not** propagate to the larger work that merely uses the library. Using
these as unmodified dependencies in a proprietary, self-hosted, commercially
sold product is permitted. The only practical obligation is to make the MPL
source available *if you modify and ship those files* — which TocDoc does not.
They are flagged here per the task's request for an explicit MPL group, and
because they should appear in the NOTICE file.

### 4e. LGPL
**None found.** No LGPL dependency is present in any component. (No dynamic-link
nuance to assess.)

### 4f. GPL (non-Affero)
**None found** as a standalone GPL dependency. The only GPL-family exposure is
via PyMuPDF's *Affero* option — see below.

### 4g. AGPL — HARD BLOCKER
| Package | Component(s) | Version | License | Risk |
|---|---|---|---|---|
| `PyMuPDF` | `services/ingestion`, `eval` | `1.27.2.3` / `1.26.0` | Dual: **AGPL-3.0** OR Artifex Commercial | Network/strong copyleft. AGPL would require offering TocDoc itself under AGPL to customers — incompatible with selling proprietary licenses under BUSL-1.1. **Actively used** (`import fitz` in `services/ingestion/custom_rag.py`). |

This is the verdict-determining finding. Full discussion in §1.

### 4h. PSF / ISC / other permissive (compatible)
`typing_extensions` (PSF-2.0), `aiohappyeyeballs` (PSF), `greenlet`
(MIT AND PSF-2.0), `shellingham` (ISC). All permissive and compatible.

### 4i. Unknown / could-not-determine
**None.** Every in-scope package resolved to a declared license via metadata.
The only residual uncertainty is *transitive* coverage, addressed in §6.

---

## 5. Recommended NOTICE / third-party-attribution approach

Nearly every permissive license here (MIT, BSD, Apache-2.0) requires that the
**copyright notice and license text be preserved** in redistributions. Because
TocDoc is *shipped to customers* (self-hosted), it is a redistribution, so an
attribution file is required for compliance — independent of the BUSL-1.1
question.

Recommended:

1. **Add a `THIRD_PARTY_NOTICES` / `NOTICE` file** to each redistributed
   artifact (or one consolidated file at repo root and in each service image)
   listing every bundled third-party package, its version, its license, and the
   license text or a pointer to it. This satisfies MIT/BSD notice retention and
   Apache-2.0 §4(d) NOTICE propagation in one place.
2. **Generate it mechanically, do not hand-maintain it.** Run
   `pip-licenses --format=plain-vertical --with-license-file --no-license-path`
   (or `--format=markdown`) inside each service's resolved venv at build time
   and commit/ship the output. This keeps it accurate as pins change and avoids
   the staleness this manual audit would otherwise suffer.
3. **Include MPL-2.0 packages** (`certifi`, `tqdm`, `orjson`) in the NOTICE with
   a pointer to where their (unmodified) source can be obtained — the simplest
   way to honor MPL-2.0 §3.2 when shipping binaries.
4. **Apache-2.0 NOTICE files**: if any Apache-2.0 dependency ships its own
   `NOTICE`, its contents must be reproduced. `pip-licenses --with-notice-file`
   captures these.
5. **Keep BUSL-1.1 (TocDoc's own license) separate** from the third-party
   notices — the NOTICE file attributes *dependencies*; it does not alter
   TocDoc's own licensing.

---

## 6. Caveats and limitations

- **Transitive coverage is strong for the services, partial elsewhere.** The
  `services/qna` and `services/ingestion` `requirements.txt` files are
  **fully-pinned resolved closures** (not top-level-only lists), so the audit of
  those two components effectively covers their entire transitive tree. The
  `eval` manifest is likewise mostly pinned. The gaps:
  - **`clients/python/pyproject.toml` is top-level-only** (`httpx`, `pydantic`,
    and the optional `langchain-core` extra). Their transitive deps
    (`httpcore`, `h11`, `certifi`, `idna`, `anyio`, `sniffio`,
    `pydantic_core`, `typing_extensions`, …) are all present and accounted for
    in the buckets above, but the SDK's own manifest does not pin them — a
    future resolution could pull a different set.
  - **`services/teams-bot`** relies on `botbuilder-core` transitively pulling
    `botframework-connector`/`-schema`/`-streaming` (all MIT, confirmed) plus
    the in-repo SDK; its transitive set is otherwise the SDK's.
- **Latest-version stand-in for unavailable pins.** Several exact pins are not
  yet on the available index (future versions). For those, the license was read
  from the latest available version (§3). This is reliable for license *type*
  but is a documented substitution, not a read of the exact shipped artifact.
- **Dual / multi-license expressions** (e.g. `cryptography`, `packaging`,
  `ormsgpack`, `aiohttp`) let the redistributor pick the most permissive option;
  this audit assumes the permissive option is taken. PyMuPDF's dual license is
  the one case where the free option (AGPL) is *not* acceptable and the paid
  option must be purchased — the reason it is a blocker rather than a free
  choice.
- **Metadata-declared, not text-audited.** Licenses are taken from each
  package's declared metadata/classifiers. A handful of packages historically
  mis-declare metadata; the verdict-critical ones were pinned-version verified,
  but a full license-text diff of all ~150 packages was out of scope.
- **No legal review.** Engineering inventory only; the PyMuPDF decision in
  particular should involve the owner and, if proceeding to sale, counsel.

---

## 7. Bottom line for the owner

- The dependency set is **sellable under BUSL-1.1 with one exception.**
- **Resolve PyMuPDF before sale**: buy the Artifex commercial license, or
  replace it (a BSD-3 `pypdf` is already pinned but not yet wired in).
- **Ship a generated `THIRD_PARTY_NOTICES` file** with each service image to
  satisfy MIT/BSD/Apache/MPL attribution obligations.
- No LGPL, no standalone GPL, no unknown-license, and no Node/web dependency
  surface were found.
