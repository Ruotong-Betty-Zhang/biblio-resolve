# Literature Lookup: Limitations and Interpretation Guide

This application is a research-assistance tool, not an authoritative DOI
registry, systematic-review decision maker, or guarantee of full-text access.
Keep the exported evidence fields and manually review consequential or
ambiguous records.

## 1. Matching and verification accuracy

- A high match score is evidence of bibliographic similarity, not proof by
  itself. Short or generic titles, common surnames, translated titles,
  transliteration, consortium authors, missing authors, and inconsistent dates
  can produce false positives or false negatives.
- A one-year date difference is tolerated because online-first and print years
  often differ. This can be too permissive for different editions or annual
  reports, and too strict for unusually delayed publications.
- Author comparison is surname-oriented. Name changes, initials, particles,
  hyphenation, East Asian name order, institutional authors, and incomplete API
  author lists can weaken it. ORCID is not currently used as an identity key.
- Volume, issue, pages, journal, item type, ISBN, and ISSN are conflict checks
  only when both the input and candidate contain them. Missing fields do not
  count as agreement. Article numbers and unusual pagination may be represented
  differently by different providers.
- DOI registration proves that an identifier exists, not that the deposited
  metadata is complete or correct. Deposits can be stale, partial, duplicated,
  or wrong. Corrections, retractions, supplements, editorials, conference
  abstracts, chapters, and version-specific DOIs may be confused with a target
  work when the input metadata is incomplete.
- URL reachability proves only that a server responded. It does not prove that
  the page is the requested paper. Conversely, HTTP 403, 429, bot protection,
  login pages, regional restrictions, or temporary 5xx responses do not prove
  that a DOI or publication is invalid.
- Fast verification stops after authoritative same-identifier metadata passes.
  It is cheaper and faster but does not require independent corroboration.
- High-confidence verification requires confirmation from a different source
  family. Those sources can still reuse Crossref, publisher, or repository
  metadata, so they are not statistically independent in a strict sense.
  Failure to obtain confirmation produces `unverified`; it does not mean the
  link is wrong.
- The threshold was not derived from a large, representative gold-standard set
  of manually labelled link-to-paper matches. An earlier comparison against
  `Approve` labels showed weak separation because study inclusion and link
  correctness are different questions. Scores are not calibrated probabilities.

## 2. Source coverage and search order

- No source covers every scholarly work. Crossref and DataCite contain deposited
  DOI metadata; OpenAlex and Semantic Scholar are broad aggregators; CORE,
  OpenAIRE, HAL, and arXiv emphasize repositories/open content; PubMed is
  biomedical; GESIS, DNB, and CiNii have regional or disciplinary strengths.
- Batch lookup uses a fixed source priority and stops after a sufficiently strong
  result. A lower-priority source may contain a better or more complete record
  that is never queried. Verification also stops once its required evidence is
  reached to conserve quota.
- The GUI does not automatically route by language. DNB, HAL, or CiNii must be
  enabled manually for German-, French-, or Japanese-heavy collections.
- CORE requires a usable API key. Enabled CORE without a key is deliberately
  skipped and is not reported as a network failure.
- The application currently requests mainly bibliographic matching fields.
  Although some APIs expose abstracts, open-access locations, or full text, the
  lookup export does not yet collect them for semantic extraction. No source
  provides a standard `IST module` field.
- Database metadata can lag behind publishers and repositories. New, obscure,
  non-English, non-DOI, grey-literature, and older records are more likely to
  remain unresolved.

## 3. Rate limits, network behavior, and reproducibility

- External APIs can change schemas, endpoints, quotas, authentication rules, or
  availability without notice. A provider change can break a source until the
  application is updated.
- OpenAlex uses a credit/daily-budget model. Semantic Scholar's unauthenticated
  pool is heavily limited, and CORE throughput is limited. Any provider may
  return 429 or impose dynamic, undocumented throttling.
- The application retries 429 and temporary server failures with delays, but a
  retry limit is necessary. Persistent failures remain `unavailable` or make a
  search `incomplete`. More concurrent workers can worsen throttling and do not
  accelerate globally rate-limited sources.
- Results can change between runs as records, OA locations, redirects, and
  rankings change. Retain timestamps, logic versions, checked sources, and
  source failures for auditability.
- `lookup_cache.json` and `verification_cache.json` reuse answers for a period.
  A corrected record or repaired link may not be seen until expiry or cache
  removal. These are local JSON files, not a transactional database, and are not
  suitable for concurrent writes by multiple app instances.
- Stop cannot cancel an HTTP request already in progress. Current requests must
  finish; queued rows may appear cancelled until the file is run again.

## 4. Web pages, PDFs, OCR, and access rights

- HTML verification relies on public citation, Dublin Core, OpenGraph, or schema
  metadata. JavaScript-rendered metadata, consent screens, CAPTCHAs, anti-bot
  systems, login pages, and unusual markup may be unreadable.
- Private, loopback, and local-network URLs are rejected to reduce request risks.
  Redirect and DNS checks reduce risk but cannot make arbitrary remote content
  completely risk-free.
- A reachable PDF is not automatically accepted. Native text or OCR evidence
  must match. Only the first five pages are used for native extraction and the
  first two pages for OCR, so information located elsewhere can be missed.
- PDFs above 15 MB are not fully processed. Encrypted, damaged, image-heavy,
  multi-column, handwritten, nonstandard-font, and low-quality scanned files can
  fail extraction.
- OCR requires PyMuPDF, pytesseract, an external Tesseract executable, and the
  appropriate language packs. OCR quality depends on scan resolution, layout,
  and language; OCR-derived evidence can be wrong and needs manual review.
- Crossref `link` metadata or an OA URL does not guarantee free, lawful, or
  durable access. Users remain responsible for subscriptions, licences,
  copyright, terms of service, and text-and-data-mining permissions. The tool
  does not bypass paywalls or access controls.
- Abstracts are not full text and often omit intervention details. A correct
  abstract may not establish which IST module was used; Methods, appendices,
  supplements, or manual review may be necessary.
- Abstract Finder identity checks are advisory rather than independent database
  verification. They compare the input title/author (and year/DOI when present)
  with public page metadata or text. Publisher suffixes, translated titles,
  truncated author lists, generic page descriptions, and sparse PDF metadata
  can therefore produce false mismatch or insufficient-metadata tags. The
  abstract is intentionally still saved so these records can be reviewed.
- Abstract Finder uses the existing URL first and a DOI URL only when URL is
  empty. It does not switch to DOI after an existing URL fails or query a second
  database for a replacement abstract.

## 5. Files, exports, settings, and Manual Review

- Column detection is heuristic. Ambiguous names such as `Name` or `Date`, or
  multiple DOI/URL columns, can map incorrectly. Confirm mappings before a run.
- CSV delimiter and encoding detection is best-effort. Malformed quoting,
  embedded line breaks, mixed encodings, formulas, merged cells, and unusual
  spreadsheet structures may not round-trip exactly.
- RIS, BibTeX, CSL JSON, CSV, and Excel have different data models. Conversion
  can lose formatting, creator roles, nested dates, attachments, tags, notes,
  relations, or custom Zotero fields. Always retain the original library export.
- Some pages show only a preview; their exports contain all rows. Very wide
  manual-review tables can be difficult to read on small screens.
- Manual Review does not validate edits, track reviewer identity, resolve
  disagreements, or support simultaneous reviewers. Decisions remain in memory
  until saved; a crash can lose unsaved work. Saving defaults to a copy, but a
  user can choose an existing path and overwrite it.
- DOI and URL clicks open the system browser and can expose the address to the
  destination, trigger a download, or lead to untrusted content. Imported links
  require the same caution as links in any spreadsheet.
- API keys and settings are stored locally in `app_settings.json` without
  encryption. Do not share or commit that file.
- English is used for application-generated interface text. Imported titles,
  author names, notes, and column headers are preserved exactly and may remain in
  any language. This is intentional: translating source data could damage
  matching fidelity and Zotero round-tripping.

## 6. Correct interpretation of status values

- `Verified` means identifier/link evidence matches the bibliographic record
  under the implemented rules. It does not mean the paper is methodologically
  sound, relevant, peer reviewed, unretracted, or eligible for inclusion.
- `Mismatch` means available evidence conflicts. `Unverifiable` means evidence
  was insufficient. `Unavailable` means access or services failed. Do not merge
  these states into one negative category.
- An `Approve` decision answers a review-inclusion question; verification answers
  a link-to-paper identity question. They are not interchangeable labels.
- Future IST module extraction would be a separate semantic task. It should save
  quoted evidence, location, source, extraction method, and uncertainty, with
  distinct `Not reported` and `Unavailable` states rather than treating missing
  evidence as absence.
