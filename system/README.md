# Literature Lookup Tool

A desktop app for looking up literature DOIs/links, built for colleagues
without a technical background. Main workflow tabs include:

1. **Single Lookup** — type in a title (author/year optional), search the
   sources you've enabled, and copy the DOI or link for the best match.
2. **Batch Import** — pick a file with many records (Zotero CSV, RIS,
   BibTeX/BibLaTeX, CSL JSON, or Excel),
   auto-detect the title/author/year columns, run the lookup on all of
   them, and export the enriched records in any of those Zotero-compatible
   formats. DOI and URL results use the standard fields Zotero recognizes.
3. **Sources** — pick which of 12 free databases to query (Crossref,
   OpenAlex, Semantic Scholar, DataCite, OpenAIRE, GESIS, CORE, DNB, HAL,
   CiNii, arXiv, PubMed), and optionally supply a contact email or API
   keys that improve some sources' rate limits (CORE requires a key to
   return anything at all). Your selection and keys are saved automatically
   (`app_settings.json`, next to this script) and restored next time you
   open the app — no need to reconfigure every session.
4. **Verification** — independently import a Zotero-compatible file that
   already contains titles and DOI/URL values, map the five relevant fields,
   batch-verify every record, and export the verification report. No lookup
   or other preceding task is required. DOI registries/web metadata provide
   the first verification layer; databases enabled on the **Sources** tab
   then cross-check the same DOI or canonical URL. A merely similar title
   with a different identifier is not accepted as verification.

5. **Abstract Finder** — map title, author, year, URL, DOI, and Abstract fields,
   then fill missing abstracts from the existing URL (or DOI URL when no URL is
   present). A found abstract is always retained. Page/PDF title, author, year,
   and DOI evidence is compared with the input record and written to dedicated
   evidence columns for later review. Possible mismatches receive the portable
   Zotero tag `ABSTRACT_FOUND_POSSIBLE_MISMATCH`; insufficient identity evidence
   receives `ABSTRACT_FOUND_NEEDS_REVIEW`.
6. **Note Link Recovery** — recover the first HTTP(S) URL from Notes only for
   records that currently have neither a URL nor a DOI.
7. **Keyword Cleanup** — retain user-authored uppercase keywords while removing
   imported lowercase terms.
8. **Manual Review** — open a previously exported result without making any
   API requests. Choose exactly which columns are visible (Title, Year,
   Author, DOI, and URL are selected automatically), click DOI/URL values,
   record a decision and notes, add custom fields, and save a reviewed CSV or
   Excel copy. The source file is not overwritten automatically.

Abstract identity checking is advisory and deliberately separate from abstract
retention. `Abstract Match Status` is `matched`, `possible_mismatch`, or
`insufficient_metadata`; `Abstract Review Tag` records the corresponding review
state. CSV/Excel retain all evidence columns. RIS, BibTeX, and CSL JSON retain
the abstract and portable review tag, and append the detailed check message to
the record Note so it survives a Zotero round trip.

## Architecture

After selecting a candidate in **Single Lookup**, click **Verify result**
for an independent second pass. The verifier confirms DOI registration
through Crossref (with DataCite as fallback), then compares the registry
title, authors, and year with the requested paper. For a plain non-DOI URL,
it reads public citation/Dublin Core/OpenGraph/ScholarlyArticle metadata from
that one HTML page and performs the same comparison. It does not execute
JavaScript or download PDFs, and local/private-network URLs are rejected.
Reachability alone is never reported as proof that it is the correct paper.

Verification keeps identifier validity separate from landing-page health.
DOI registration can therefore remain valid when a publisher returns 403,
429, or a temporary 5xx response. PDF links are downloaded only up to 15 MB;
the first five pages are checked for a native text layer. If no usable text
exists, optional OCR checks the first two pages when PyMuPDF, pytesseract, and
the external Tesseract executable are available. Otherwise the result records
`OCR Status = ocr_unavailable` and remains unverified rather than being marked
as a mismatch or broken link.

The Verification tab has two modes:

- **Fast** stops at the first authoritative same-identifier metadata match.
- **High confidence** requires an independent second enabled source to return
  the same DOI/canonical URL with matching metadata. If that confirmation is
  unavailable, the result remains `unverified` rather than being promoted.

Standard Zotero fields (publication title, volume, issue, pages, ISBN, ISSN,
and item type) are passed into verification automatically when present.
Populated journal/volume/issue/identifier conflicts prevent auto-verification.
Crossref main titles and subtitles are combined before matching, author names
are compared with Unicode/diacritic normalization, and all available online and
print publication years are retained to avoid false conflicts from online-first
dates. Exports include the title variant, component score, normalized author
match type, year difference, and available publication years used as evidence.
Exports record the UTC verification timestamp, logic version, mode, evidence,
access state, and any metadata conflicts.

Verification is checkpointed after every completed record in
`verification_cache.json`. Stable results are reused for 30 days, insufficient-
metadata results for 7 days, and temporary network failures for only 1 hour.
The **Stop** button finishes current requests safely; starting the same file
again resumes from checkpoints. During a 429/5xx retry the UI displays the
source, wait duration, and attempt number.

All Python code required by the desktop tool lives in this `system` directory.
The actual query/scoring logic is in [`doi_lookup_lib.py`](doi_lookup_lib.py),
and [`lookup_core.py`](lookup_core.py) is the app-specific layer on top of it:
selectable sources, local caches, verification, and Zotero-compatible file I/O.
[`abstract_note_tools.py`](abstract_note_tools.py) contains Note-link recovery
and Abstract Finder extraction/matching. The separate CSV research pipeline
imports `system.doi_lookup_lib`; it does not own a second copy.

If you change matching/scoring behavior, do it in `doi_lookup_lib.py` —
both tools pick it up automatically.

## Running it

```bash
pip install -r requirements.txt
python literature_lookup.py
```

A desktop window opens — no command line or browser needed to use it.

## Automated tests

All tool tests are ordinary, readable Python code beside the application in
[`test_verification.py`](test_verification.py) and
[`test_abstract_note_tools.py`](test_abstract_note_tools.py). There are no
private or temporary tests required to reproduce the result. The current
`system` suite contains 69 tests: 43 lookup/verification/file-format tests and
26 Abstract Finder, Note Link Recovery, matching, cache, and export tests.

Run both test files from the `system` folder:

```bash
python -m unittest test_verification test_abstract_note_tools -v
```

Or run it from the project root:

```bash
python -m unittest system.test_verification system.test_abstract_note_tools -v
```

Each test builds its own example metadata and fake API response in code, so
running the suite does not consume API quota and does not depend on live
Crossref, DataCite, publisher, or search-service availability.

## Known limitations

The complete and current limitation guide is in
[`LIMITATIONS.md`](LIMITATIONS.md). Read it before interpreting verification
scores or using results for screening decisions. The summary below highlights
older operational notes.

- Batch mode queries the enabled sources in a fixed priority order,
  stopping early once a confident match is found (same philosophy as
  `find_doi_url.py`'s `process_row()`) — this is faster than querying
  every enabled source for every row, but means a lower-priority source
  might occasionally have found a better match that was never checked.
- Unlike `find_doi_url.py`, this app does not do automatic language
  detection/routing (querying DNB first for German titles, etc.) — since
  users pick sources manually here, enable DNB/HAL/CiNii yourself for
  German/French/Japanese-heavy material.
- OpenAlex has a credit-based daily rate limit (returns
  `429 Insufficient budget` once exhausted); Semantic Scholar without a
  key is capped around 100 requests/5 min. A single lookup rarely hits
  these, but heavy use on the same network/day (including running
  `find_doi_url.py`'s batch pipeline) can exhaust the shared quota —
  Crossref/OpenAlex/DataCite/GESIS are usually unaffected.
- Batch mode caches results in `lookup_cache.json` (next to this script,
  separate from `find_doi_url.py`'s own cache) so re-running the same
  file won't re-hit the APIs. The cache key includes which sources were
  enabled, so changing your source selection re-queries automatically.
- Batch mode runs multiple records concurrently (a "Concurrent workers"
  slider on the Batch Import tab, 1-12, default 4). Semantic Scholar and
  CORE are rate-limited globally regardless of this setting (see
  `_RateLimiter` in `doi_lookup_lib.py`), so extra workers won't speed
  those two up — they help mainly for the sources without a global limit
  (Crossref, OpenAlex, DataCite, GESIS, OpenAIRE, DNB, HAL, CiNii, arXiv,
  PubMed). 4-6 is a good default; drop to 2-3 if you see errors/timeouts;
  go up to 8-12 if you've disabled Semantic Scholar/CORE and want more
  speed.
- **A source failing to respond (timeout, rate limit, server error) is
  never confused with that source genuinely having no match.** Every
  `query_*()` in `doi_lookup_lib.py` returns `(candidates, error)`, not
  just a bare list; `error` is `None` on a real (possibly empty) response
  and a short reason (`"timed out"`, `"HTTP 429"`, ...) when the request
  itself never completed. Batch mode surfaces this two ways: a dedicated
  **"Not found — search incomplete"** status (instead of a plain "Not
  found") when nothing was found and at least one enabled source failed,
  and a **"Failed Sources"** column in the export listing exactly which
  source(s) failed and why, on every row where it happened — regardless of
  whether a match was still found via a different source. Single Lookup
  shows the same information as a note under the results. A source that's
  simply not enabled, or CORE with no API key, is a deliberate skip, not a
  failure, and is never reported this way.

## Packaging it into an app for coworkers

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "Literature Lookup" ^
    --add-data "theme.json;." literature_lookup.py
```

The Python modules are all beside `literature_lookup.py`, so PyInstaller can
discover them without reaching outside `system`. `--add-data "theme.json;."`
is required for the app's color theme — the app looks for it
next to `literature_lookup.py` when run from source, or inside
PyInstaller's onefile temp folder (`sys._MEIPASS`) when packaged; without
this flag the packaged .exe will fail to start with a "file not found"
error instead of opening.

The packaged single-file app will be at `dist/Literature Lookup.exe` —
coworkers can just double-click it, no Python install required.
