"""
doi_lookup_lib.py
-------------------
Shared DOI/URL lookup logic: queries against Crossref, OpenAlex, Semantic
Scholar, DataCite, GESIS, arXiv, PubMed, CORE, OpenAIRE, DNB, HAL, and
CiNii, plus title-similarity scoring and the language-first routing chain.

This module has no CLI and no cache of its own. It lives with the GUI app in
the self-contained system folder and is imported by both lookup_core.py and
the separate url_and_doi/find_doi_url.py research pipeline, so the lookup
logic lives in exactly one place instead of drifting into two copies. Each caller keeps its own
cache file and its own way of driving these functions (batch-with-early-
exit vs. a GUI where the user picks which sources to query).

See url_and_doi/find_doi_url.py's module docstring for the background on why each
source is included, its relative reliability, and the language-first
routing rationale (German -> DNB, French -> HAL, Japanese -> CiNii).

FAILURE VS. GENUINE ZERO: every query_*() function returns a
(candidates, error) tuple, not just a bare list. `error` is None when the
request completed normally (even with zero candidates - a real negative),
and a short string (e.g. "timed out", "HTTP 500") when the request itself
never completed - meaning that source genuinely wasn't checked, and its
absence from the candidate pool shouldn't be read as "this source has
nothing." process_row()/explain_match() collect these into a
`failed_sources` list that's attached to every result dict, and
build_result() reports a dedicated "incomplete" status (instead of
"not_found") when nothing was found AND at least one source never
responded - see build_result()'s docstring.
"""

import re
import random
import time
import threading
import unicodedata
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import requests
from rapidfuzz import fuzz
# langdetect: used only to guess a title's language for the language-first
# routing chain (see detect_title_language()/LANGUAGE_SOURCE_MAP/
# process_row()). DetectorFactory.seed = 0 makes detect() deterministic -
# without it, langdetect's internal RNG means calling it twice on the same
# text could occasionally return two different guesses. Fixed once, here,
# at import time, so results (and cache keys, and routing decisions) stay
# reproducible across runs.
from langdetect import detect as _langdetect_detect, LangDetectException
from langdetect.detector_factory import DetectorFactory as _LangDetectorFactory
_LangDetectorFactory.seed = 0

# Thresholds for the combined score (title similarity + author/year bonus,
# capped at 100). Anything below REVIEW_THRESHOLD is never written to
# doi/url by build_result() - only the closest candidate is kept for
# reference.
AUTO_ACCEPT_THRESHOLD = 92   # >= this score: auto-accept
REVIEW_THRESHOLD = 75        # >= this score but < auto-accept: flag as "needs review"
_STATUS_CALLBACK = None
_STATUS_CALLBACK_LOCK = threading.Lock()


def set_status_callback(callback):
    """Set an optional process-wide callback for rate-limit/retry UI updates."""
    global _STATUS_CALLBACK
    with _STATUS_CALLBACK_LOCK:
        _STATUS_CALLBACK = callback


def _emit_status(event):
    with _STATUS_CALLBACK_LOCK:
        callback = _STATUS_CALLBACK
    if callback:
        try:
            callback(event)
        except Exception:
            pass


def _describe_exception(e: Exception) -> str:
    """Short, human-readable reason a source query failed to complete -
    used everywhere below to tell "this source responded and genuinely has
    nothing" apart from "we couldn't tell because the request itself
    failed" (network error, timeout, rate limit, bad response, ...)."""
    if isinstance(e, requests.exceptions.Timeout):
        return "timed out"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "connection error"
    if isinstance(e, requests.exceptions.HTTPError):
        resp = getattr(e, "response", None)
        code = resp.status_code if resp is not None else "?"
        return f"HTTP {code}"
    if isinstance(e, requests.exceptions.RequestException):
        return "request error"
    return type(e).__name__


def normalize_for_compare(s: str) -> str:
    """
    Normalize text before comparison:
    - Strip HTML tags (Crossref/OpenAlex titles often contain <sup>, <scp>, etc.)
    - Strip Latin-script accents (e.g. é -> e, ö -> o) so formatting
      differences don't artificially lower the similarity score for a
      genuine match
    - Lowercase

    CAVEAT ON JAPANESE: NFKD decomposition (used below to isolate accent
    marks so they can be stripped) decomposes a precomposed voiced/
    semi-voiced kana - が (ga), ば (ba), だ (da), ぱ (pa), etc. - into its
    base kana (か/は/た/は) plus a COMBINING dakuten/handakuten mark
    (U+3099/U+309A), via the exact same Unicode mechanism it uses for e.g.
    é -> e + combining acute accent. A naive "strip every combining mark"
    would therefore silently turn が into か, ば into は, and so on - not a
    cosmetic formatting difference like a French accent, but a genuinely
    different kana/sound/word. We explicitly exempt those two combining
    marks from stripping (and re-normalize back to NFC afterward) so
    Japanese titles queried via CiNii (see query_cinii()) aren't corrupted
    by this. Every OTHER script's diacritics/accents (French, German,
    Spanish, Vietnamese tone marks, etc.) are still stripped as before -
    for those, this is a deliberate, low-risk trade-off: since this
    function is applied identically to both the target title and every
    candidate title, stripping accents mainly guards against genuine
    matches being missed due to encoding differences between sources
    (e.g. one database storing "café" and another storing "cafe"), and the
    fuzzy whole-title comparison in score_candidate() makes a false-positive
    collision from a single accent-stripped word unlikely to swing the
    result on its own.
    """
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)  # strip HTML tags
    s = s.translate(str.maketrans({
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
        # Quotation marks are typographic decoration, not title evidence.
        # Removing every common straight/curly form prevents equivalent titles
        # from receiving different token orders in RapidFuzz's token_sort_ratio.
        "'": "", "\"": "", "\u2018": "", "\u2019": "", "\u201a": "", "\u201b": "",
        "\u201c": "", "\u201d": "", "\u201e": "", "\u201f": "", "\u02bc": "", "\uff07": "",
        "\u00ab": "", "\u00bb": "", "\u2039": "", "\u203a": "", "\uff02": "",
        "\u00a0": " ", "\u2007": " ", "\u202f": " ",
    }))
    s = unicodedata.normalize("NFKD", s)
    # Strip every combining mark EXCEPT the Japanese voiced/semi-voiced
    # sound marks (dakuten "゛"/handakuten "゜") - see the CAVEAT above.
    s = "".join(
        c for c in s
        if not unicodedata.combining(c) or c in ("゙", "゚")
    )
    s = unicodedata.normalize("NFC", s)  # recompose (e.g. か+U+3099 back into が)
    return s.lower()


def author_comparison_forms(value: str) -> set:
    """Return original and standardized forms; either may establish a match."""
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value:
        return set()
    original = unicodedata.normalize("NFC", value).casefold()
    standardized = normalize_for_compare(value).casefold()
    return {form for form in (original, standardized) if form}


def clean_author(author_field: str) -> str:
    """Extract the first author's last name. Used for querying the APIs
    (Crossref's query.author etc. expect a single name) and for cache keys."""
    if not author_field:
        return ""
    first = author_field.split(";")[0].strip()
    # Zotero export format is usually "Last, First"
    return first.split(",")[0].strip()


def clean_authors(author_field: str) -> list:
    """Extract the last names of ALL authors (not just the first). Used only
    for scoring (see _author_bonus()) — matching against every author instead
    of just the first makes the author bonus robust to a candidate record
    that happens to omit or reorder authors (common with truncated author
    lists from some sources)."""
    if not author_field:
        return []
    lastnames = []
    for part in author_field.split(";"):
        part = part.strip()
        if not part:
            continue
        lastname = part.split(",")[0].strip()
        if lastname:
            lastnames.append(lastname)
    return lastnames


def clean_title(title: str) -> str:
    """Strip bracketed English translations etc. that would distort similarity scoring."""
    if not title:
        return ""
    t = re.sub(r"\[[^\]]*\]", "", title)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def detect_title_language(title: str):
    """
    Guess the language of a (already clean_title()-processed) title string,
    returning a 2-letter ISO 639-1 code like "de"/"fr"/"ja"/"en", or None if
    detection isn't possible/reliable.

    This drives the STAGE 1 "language-first routing" described in
    find_doi_url.py's module docstring: process_row() looks up the returned
    code in LANGUAGE_SOURCE_MAP to decide whether to try a regional
    specialist source (DNB/HAL/CiNii) before the generic English-oriented
    ones.
    """
    # Guard clause: langdetect raises an exception on empty/whitespace-only
    # input rather than returning something sensible, and very short
    # strings (e.g. a 2-word title) give unreliable guesses anyway - so we
    # simply decline to guess below a small length threshold.
    if not title or len(title.strip()) < 8:
        return None

    try:
        # langdetect.detect() runs its statistical n-gram language model
        # over the text and returns its single best-guess ISO 639-1 code
        # (a string like "de"). Internally it constructs a fresh Detector
        # object per call using the seeded, deterministic RNG we fixed at
        # import time - this makes detect() thread-safe for our purposes:
        # each call gets its own local Detector instance, so concurrent
        # calls from different worker threads don't share any mutable
        # state with each other (the only shared object is the read-only,
        # pre-trained language-profile data loaded once at import time,
        # which every Detector instance only ever reads from, never writes to).
        lang = _langdetect_detect(title)
    except LangDetectException:
        # Raised for input langdetect can't extract any n-gram features
        # from at all (e.g. a title that's pure numbers/punctuation) -
        # treat that the same as "couldn't determine a language".
        return None

    return lang


def query_datacite(title: str, author: str, rows: int = 5):
    """
    Query DataCite for candidate records.
    DataCite mainly covers DOIs for theses, datasets, and working papers -
    the kind of content Crossref doesn't index.

    Returns (candidates, error) - see the module docstring.
    """
    url = "https://api.datacite.org/dois"
    params = {
        "query": f"{title} {author}",
        "page[size]": rows,
    }
    try:
        r = _get_with_retry("datacite", url, params=params, timeout=15)
        r.raise_for_status()
        items = r.json().get("data", [])
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for it in items:
        attrs = it.get("attributes", {})
        titles = attrs.get("titles", [])
        cand_title = titles[0].get("title", "") if titles else ""
        doi = attrs.get("doi")
        link = f"https://doi.org/{doi}" if doi else attrs.get("url")
        authors = []
        for c in attrs.get("creators", []):
            fam = c.get("familyName")
            if fam:
                authors.append(fam)
            elif c.get("name"):
                # Some records only provide a full "name" string; roughly
                # take the last word as the surname
                authors.append(c["name"].split()[-1])
        year = attrs.get("publicationYear")
        abstract = ""
        for description in attrs.get("descriptions", []) or []:
            if str(description.get("descriptionType", "")).casefold() == "abstract":
                abstract = description.get("description") or ""
                break
        candidates.append({
            "source": "datacite",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "authors": authors,
            "year": year,
            "abstract": abstract,
        })
    time.sleep(0.1)  # light self-throttle, polite regardless of concurrency level
    return candidates, None


def _sparql_escape(s: str) -> str:
    """Escape a string for safe embedding inside a SPARQL string literal
    (backslash and double-quote are the two characters that would otherwise
    break out of the quoted literal)."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def query_gesis(title: str, author: str, rows: int = 5):
    """
    Query the GESIS Knowledge Graph (SPARQL endpoint) for candidate records —
    both publications (schema:ScholarlyArticle) and survey datasets
    (schema:Dataset). GESIS runs the ISSP data archive and SSOAR (the
    open-access repository for German-language social science publications),
    content Crossref/OpenAlex/DataCite/Semantic Scholar often don't index.

    IMPORTANT CAVEAT, unlike the other sources: this is a literal
    SPARQL CONTAINS substring filter, not a ranked full-text search. To
    keep some tolerance for messy/partial titles, we filter on the title's
    first few significant words (ANDed CONTAINS clauses) rather than
    requiring the whole title to match verbatim - but this is inherently
    weaker than Crossref/OpenAlex/Semantic Scholar's bibliographic search
    and may return zero candidates for a title that score_candidate()'s
    fuzzy comparison would otherwise have matched just fine.

    `author` is accepted for signature consistency with the other query_*
    functions but is NOT used to filter server-side here - author matching
    happens downstream in score_candidate(), same as for the other
    sources' results.

    Returns (candidates, error) - see the module docstring.
    """
    words = [w for w in re.split(r"\s+", clean_title(title)) if len(w) > 3]
    # Strip leading/trailing punctuation (e.g. "1997:" -> "1997") so a
    # colon/comma/etc. that happens to be attached in OUR title text
    # doesn't require an exact-punctuation match against GESIS's title text.
    words = [w.strip(".,;:!?()[]\"'") for w in words]
    words = [w for w in words if len(w) > 3][:4]
    if not words:
        # Nothing usable to filter on - not a failure, just nothing to ask.
        return [], None

    filters = " && ".join(
        f'CONTAINS(LCASE(?title), LCASE("{_sparql_escape(w)}"))' for w in words
    )

    query = f"""
PREFIX schema: <https://schema.org/>
PREFIX gesiskg: <https://data.gesis.org/gesiskg/schema/>
SELECT ?id ?title ?doi ?date ?portalUrl ?schemaUrl ?archivedAt ?abstract
       (GROUP_CONCAT(DISTINCT ?familyName; separator="|") AS ?authors)
       (GROUP_CONCAT(DISTINCT ?pi; separator="|") AS ?investigators)
WHERE {{
  {{ ?id a schema:ScholarlyArticle . }}
  UNION
  {{ ?id a schema:Dataset . }}
  ?id schema:name ?title .
  FILTER({filters})
  OPTIONAL {{ ?id gesiskg:doi ?doi . }}
  OPTIONAL {{ ?id schema:datePublished ?date . }}
  OPTIONAL {{ ?id gesiskg:portalUrl ?portalUrl . }}
  OPTIONAL {{ ?id schema:url ?schemaUrl . }}
  OPTIONAL {{ ?id schema:archivedAt ?archivedAt . }}
  OPTIONAL {{ ?id schema:abstract ?abstract . }}
  OPTIONAL {{ ?id schema:author ?authorRes . ?authorRes schema:familyName ?familyName . }}
  OPTIONAL {{ ?id gesiskg:principalInvestigator ?pi . }}
}}
GROUP BY ?id ?title ?doi ?date ?portalUrl ?schemaUrl ?archivedAt ?abstract
LIMIT {rows}
"""

    url = "https://data.gesis.org/gesiskg/sparql"
    try:
        r = _get_with_retry(
            "gesis", url,
            params={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=15,
        )
        r.raise_for_status()
        bindings = r.json()["results"]["bindings"]
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for b in bindings:
        cand_title = b.get("title", {}).get("value", "")
        doi = b.get("doi", {}).get("value")
        if doi:
            doi = doi.replace("https://doi.org/", "").strip()
        portal_url = b.get("portalUrl", {}).get("value")
        schema_url = b.get("schemaUrl", {}).get("value")
        archived_at = b.get("archivedAt", {}).get("value")

        url_is_reference_only = False
        if doi:
            link = f"https://doi.org/{doi}"
        elif portal_url:
            link = portal_url
        elif schema_url:
            link = schema_url
        elif archived_at:
            link = archived_at
        else:
            link = b.get("id", {}).get("value")
            url_is_reference_only = True

        authors_raw = b.get("authors", {}).get("value", "")
        investigators_raw = b.get("investigators", {}).get("value", "")
        authors = [a for a in authors_raw.split("|") if a]
        authors += [pi for pi in investigators_raw.split("|") if pi]
        date_raw = b.get("date", {}).get("value", "")
        year_match = re.search(r"\d{4}", date_raw)
        year = int(year_match.group()) if year_match else None
        candidates.append({
            "source": "gesis",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "url_is_reference_only": url_is_reference_only,
            "authors": authors,
            "year": year,
            "abstract": b.get("abstract", {}).get("value", ""),
        })
    time.sleep(0.1)
    return candidates, None


def query_arxiv(title: str, author: str, rows: int = 5):
    """
    Query the arXiv API (Atom XML, not JSON) for candidate records.
    Covers physics, math, CS, quantitative biology/finance, and a
    (smaller) economics category.

    Returns (candidates, error) - see the module docstring.
    """
    title_words = [w for w in clean_title(title).split() if len(w) > 2][:6]
    query_parts = []
    if title_words:
        title_terms = " AND ".join(f'ti:"{w}"' for w in title_words)
        query_parts.append(f"({title_terms})")
    if author:
        query_parts.append(f'au:"{author}"')
    if not query_parts:
        # Nothing usable to filter on - not a failure, just nothing to ask.
        return [], None
    search_query = " AND ".join(query_parts)

    url = "http://export.arxiv.org/api/query"
    try:
        r = _get_with_retry(
            "arxiv", url,
            params={"search_query": search_query, "start": 0, "max_results": rows},
            timeout=15,
        )
        r.raise_for_status()
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom",
        }
        root = ET.fromstring(r.text)
        entries = root.findall("atom:entry", ns)
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for entry in entries:
        title_el = entry.find("atom:title", ns)
        cand_title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
        if not cand_title:
            continue

        authors = []
        for a in entry.findall("atom:author", ns):
            name_el = a.find("atom:name", ns)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip().split()[-1])

        doi_el = entry.find("arxiv:doi", ns)
        doi = doi_el.text.strip() if doi_el is not None and doi_el.text else None
        id_el = entry.find("atom:id", ns)
        arxiv_url = id_el.text.strip() if id_el is not None and id_el.text else None
        link = f"https://doi.org/{doi}" if doi else arxiv_url

        published_el = entry.find("atom:published", ns)
        year = None
        if published_el is not None and published_el.text:
            m = re.search(r"\d{4}", published_el.text)
            year = int(m.group()) if m else None

        summary_el = entry.find("atom:summary", ns)
        abstract = (summary_el.text or "").strip() if summary_el is not None else ""

        candidates.append({
            "source": "arxiv",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "authors": authors,
            "year": year,
            "abstract": abstract,
        })
    time.sleep(0.1)
    return candidates, None


def query_pubmed(title: str, author: str, email: str = "", rows: int = 5):
    """
    Query PubMed via NCBI E-utilities (esearch to get PMIDs, then esummary
    for details) for candidate records. Indexes biomedical/life-science/
    public-health literature.

    RATE LIMIT NOTE: NCBI allows 3 requests/sec without an API key. `email`
    is optional (identifies the caller to NCBI as a courtesy) - when
    omitted, only the `tool` param is sent.

    Returns (candidates, error) - see the module docstring.
    """
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    term_parts = [clean_title(title)]
    if author:
        term_parts.append(f"{author}[Author]")
    term = " AND ".join(term_parts)

    common = {"tool": "doi_lookup_lib"}
    if email:
        common["email"] = email

    try:
        r = _get_with_retry(
            "pubmed", f"{base}/esearch.fcgi",
            params={"db": "pubmed", "term": term, "retmax": rows, "retmode": "json", **common},
            timeout=15,
        )
        r.raise_for_status()
        pmids = r.json().get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        return [], _describe_exception(e)

    if not pmids:
        return [], None

    abstracts = {}
    try:
        r_abstracts = _get_with_retry(
            "pubmed", f"{base}/efetch.fcgi",
            params={"db": "pubmed", "id": ",".join(pmids), "retmode": "xml", **common},
            timeout=15,
        )
        r_abstracts.raise_for_status()
        abstract_root = ET.fromstring(r_abstracts.text)
        for article in abstract_root.findall(".//PubmedArticle"):
            pmid_el = article.find(".//MedlineCitation/PMID")
            if pmid_el is None or not pmid_el.text:
                continue
            parts = []
            for abstract_el in article.findall(".//Article/Abstract/AbstractText"):
                text = "".join(abstract_el.itertext()).strip()
                label = (abstract_el.attrib.get("Label") or "").strip()
                if text:
                    parts.append(f"{label}: {text}" if label else text)
            abstracts[pmid_el.text.strip()] = " ".join(parts)
    except Exception:
        # PubMed summary metadata can still be used for matching even when
        # its separate abstract endpoint is temporarily unavailable.
        abstracts = {}

    try:
        r2 = _get_with_retry(
            "pubmed", f"{base}/esummary.fcgi",
            params={"db": "pubmed", "id": ",".join(pmids), "retmode": "json", **common},
            timeout=15,
        )
        r2.raise_for_status()
        result = r2.json().get("result", {})
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for pmid in result.get("uids", []):
        doc = result.get(pmid, {})
        cand_title = (doc.get("title") or "").strip()
        if not cand_title:
            continue

        authors = []
        for a in doc.get("authors", []):
            name = a.get("name", "")
            if name:
                authors.append(name.split()[0])

        doi = None
        for aid in doc.get("articleids", []):
            if aid.get("idtype") == "doi":
                doi = aid.get("value")
                break
        link = f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        pubdate = doc.get("pubdate", "")
        m = re.search(r"\d{4}", pubdate)
        year = int(m.group()) if m else None

        candidates.append({
            "source": "pubmed",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "authors": authors,
            "year": year,
            "abstract": abstracts.get(str(pmid), ""),
        })
    time.sleep(0.1)
    return candidates, None


def query_crossref(title: str, author: str, email: str = "", rows: int = 5):
    """Query Crossref's bibliographic search for candidate records.

    `author` and `email` are both optional: an empty "query.author" or
    "mailto" param makes Crossref return ZERO results (not just ignore the
    filter), so both are only added to the request when actually provided.

    Returns (candidates, error) - see the module docstring.
    """
    url = "https://api.crossref.org/works"
    params = {
        "query.bibliographic": title,
        "rows": rows,
    }
    if author:
        params["query.author"] = author
    if email:
        params["mailto"] = email  # join the "polite pool" for more generous rate limits
    try:
        r = _get_with_retry("crossref", url, params=params, timeout=15)
        r.raise_for_status()
        items = r.json()["message"]["items"]
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for it in items:
        cand_title = " ".join(it.get("title", [""]))
        doi = it.get("DOI")
        link = f"https://doi.org/{doi}" if doi else it.get("URL")
        authors = [a.get("family", "") for a in it.get("author", []) if a.get("family")]
        year = None
        date_parts = it.get("issued", {}).get("date-parts", [[None]])
        if date_parts and date_parts[0]:
            year = date_parts[0][0]
        candidates.append({
            "source": "crossref",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "authors": authors,
            "year": year,
            "abstract": it.get("abstract") or "",
        })
    time.sleep(0.1)
    return candidates, None


def query_openalex(title: str, author: str, rows: int = 5):
    """
    Query OpenAlex for candidate records (broader coverage, includes
    thesis/report). Many OpenAlex works have no DOI - for those, prefer a
    genuine direct link to the paper over OpenAlex's own internal record
    page:
      1. DOI, if present
      2. open_access.oa_url - OpenAlex's own best-guess open-access copy
      3. primary_location.landing_page_url / pdf_url - the publisher/
         repository page OpenAlex indexed this work from
      4. only if none of those exist, fall back to OpenAlex's internal
         "https://openalex.org/W..." record page, flagged
         url_is_reference_only.

    Returns (candidates, error) - see the module docstring.
    """
    url = "https://api.openalex.org/works"
    params = {
        # OpenAlex's general search can lose an exact title when an author
        # token is appended to the same free-text query. Query the title
        # alone and apply author matching in the shared scorer below.
        "search": title,
        "per_page": rows,
    }
    try:
        r = _get_with_retry("openalex", url, params=params, timeout=15)
        r.raise_for_status()
        items = r.json()["results"]
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for it in items:
        cand_title = it.get("display_name") or ""
        doi = it.get("doi")  # OpenAlex already returns "https://doi.org/10.xxxx"
        if doi:
            doi = doi.replace("https://doi.org/", "")

        oa_url = (it.get("open_access") or {}).get("oa_url")
        primary_location = it.get("primary_location") or {}
        landing_page_url = primary_location.get("landing_page_url")
        pdf_url = primary_location.get("pdf_url")

        url_is_reference_only = False
        if doi:
            best_url = f"https://doi.org/{doi}"
        elif oa_url:
            best_url = oa_url
        elif landing_page_url:
            best_url = landing_page_url
        elif pdf_url:
            best_url = pdf_url
        else:
            best_url = it.get("id")  # OpenAlex's own metadata page - reference only
            url_is_reference_only = True

        authors = []
        for a in it.get("authorships", []):
            name = a.get("author", {}).get("display_name", "")
            if name:
                authors.append(name.split()[-1])  # rough surname guess (last word)
        year = it.get("publication_year")
        inverted = it.get("abstract_inverted_index") or {}
        abstract_words = []
        for word, positions in inverted.items():
            for position in positions or []:
                abstract_words.append((position, word))
        abstract = " ".join(word for _position, word in sorted(abstract_words))
        candidates.append({
            "source": "openalex",
            "title": cand_title,
            "doi": doi,
            "url": best_url,
            "url_is_reference_only": url_is_reference_only,
            "authors": authors,
            "year": year,
            "abstract": abstract,
        })
    time.sleep(0.1)
    return candidates, None


class _RateLimiter:
    """
    Thread-safe GLOBAL rate limiter: guarantees calls made from ANY worker
    thread are spaced at least `min_interval` seconds apart, no matter how
    many threads are calling concurrently (unlike a plain time.sleep() at
    the end of a function, which only throttles that one thread and lets
    N threads each fire roughly every `min_interval` seconds independently
    - i.e. ~N times faster in aggregate than intended).
    """
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._blocked_until = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            sleep_time = max(self.min_interval - (now - self._last_call), self._blocked_until - now)
            if sleep_time > 0:
                time.sleep(sleep_time)
            self._last_call = time.monotonic()

    def defer(self, seconds: float):
        """Pause all worker threads using this limiter for a shared cooldown."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + max(0, seconds))


_SOURCE_RATE_LIMITERS = {
    "crossref": _RateLimiter(0.25), "datacite": _RateLimiter(0.35),
    "gesis": _RateLimiter(0.75), "arxiv": _RateLimiter(3.0),
    "pubmed": _RateLimiter(0.40), "openalex": _RateLimiter(0.35),
    "semantic_scholar": _RateLimiter(3.1), "semantic_scholar_key": _RateLimiter(1.1),
    "core": _RateLimiter(1.5), "openaire": _RateLimiter(0.75),
    "dnb": _RateLimiter(0.75), "hal": _RateLimiter(0.75), "cinii": _RateLimiter(0.75),
}


def _retry_after_seconds(response, attempt: int) -> float:
    value = (response.headers.get("Retry-After") or "").strip()
    if value:
        try:
            return min(120.0, max(1.0, float(value)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return min(120.0, max(1.0, (retry_at - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(60.0, (2 ** attempt) * 3.0 + random.uniform(0.2, 1.0))


def _get_with_retry(source_id: str, url: str, *, max_attempts: int = 4, **kwargs):
    """Rate-limited GET with shared 429 cooldown and bounded backoff."""
    limiter = _SOURCE_RATE_LIMITERS[source_id]
    last_response = None
    for attempt in range(max_attempts):
        limiter.wait()
        try:
            response = requests.get(url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt == max_attempts - 1:
                raise
            delay = min(30.0, (2 ** attempt) * 2.0 + random.uniform(0.1, 0.8))
            limiter.defer(delay)
            continue
        last_response = response
        if response.status_code == 429:
            # OpenAlex also uses 429 for an exhausted daily credit budget;
            # waiting seconds cannot repair that condition, so fail fast and
            # let the verification pipeline move to the next enabled source.
            if source_id == "openalex" and "insufficient budget" in response.text.lower():
                return response
            if attempt == max_attempts - 1:
                return response
            delay = _retry_after_seconds(response, attempt)
            _emit_status({"kind": "rate_limit", "source": source_id,
                          "delay": round(delay, 1), "attempt": attempt + 1,
                          "max_attempts": max_attempts})
            limiter.defer(delay)
            continue
        if response.status_code in {500, 502, 503, 504} and attempt < max_attempts - 1:
            delay = min(30.0, (2 ** attempt) * 2.0 + random.uniform(0.1, 0.8))
            _emit_status({"kind": "server_retry", "source": source_id,
                          "delay": round(delay, 1), "attempt": attempt + 1,
                          "max_attempts": max_attempts})
            limiter.defer(delay)
            continue
        return response
    return last_response


# Semantic Scholar's shared public pool is enforced server-side per IP, so
# per-thread throttling alone isn't safe with concurrent callers - these
# are process-wide, used by every query_semantic_scholar() call.
_S2_RATE_LIMITER_NO_KEY = _RateLimiter(3.0)     # ~100 requests / 5 min, shared public pool
_S2_RATE_LIMITER_WITH_KEY = _RateLimiter(1.0)   # 1 request/sec, official documented baseline for a
                                                 # personal API key


def query_semantic_scholar(title: str, author: str, rows: int = 5, api_key: str = None):
    """
    Query Semantic Scholar's official Graph API (paper search endpoint) for
    candidate records. Free, no key required for basic access, ~200M papers
    across all fields.

    Link preference: 1. DOI  2. openAccessPdf.url  3. the semanticscholar.org
    paper page (a real, citable page - not flagged url_is_reference_only).

    Request a free key at
    https://www.semanticscholar.org/product/api#api-key-form for a
    dedicated 1 request/sec rate instead of sharing the public pool.

    Returns (candidates, error) - see the module docstring.
    """
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": f"{title} {author}".strip(),
        "fields": "title,externalIds,year,authors,openAccessPdf,url,abstract",
        "limit": rows,
    }
    headers = {"x-api-key": api_key} if api_key else {}
    limiter_id = "semantic_scholar_key" if api_key else "semantic_scholar"
    try:
        r = _get_with_retry(limiter_id, url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        items = r.json().get("data", [])
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for it in items:
        cand_title = it.get("title") or ""
        if not cand_title:
            continue

        external_ids = it.get("externalIds") or {}
        doi = external_ids.get("DOI")
        oa_pdf_url = (it.get("openAccessPdf") or {}).get("url")
        s2_page_url = it.get("url")

        if doi:
            link = f"https://doi.org/{doi}"
        elif oa_pdf_url:
            link = oa_pdf_url
        else:
            link = s2_page_url

        authors = []
        for a in it.get("authors", []):
            name = a.get("name", "")
            if name:
                authors.append(name.split()[-1])

        candidates.append({
            "source": "semantic_scholar",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "authors": authors,
            "year": it.get("year"),
            "abstract": it.get("abstract") or "",
        })
    return candidates, None


# CORE doesn't publish one fixed, universal per-second number for free
# keys - out of caution, uses the same GLOBAL rate-limiter pattern as
# Semantic Scholar rather than trusting a plain per-thread sleep.
_CORE_RATE_LIMITER = _RateLimiter(1.5)


def query_core(title: str, author: str, rows: int = 5, api_key: str = None):
    """
    Query the CORE API v3 search endpoint for candidate records. CORE
    aggregates 300M+ metadata records / 40M+ full-text open-access papers
    from 10,000+ repositories worldwide, including many non-English
    institutional repositories.

    REQUIRES an API key - unlike Crossref/OpenAlex/Semantic Scholar, CORE
    has no keyless search tier. Free registration at
    https://core.ac.uk/services/api. If `api_key` is None, this function
    returns ([], None) immediately WITHOUT making any network request -
    that's a deliberate skip, not a failure, so it does NOT show up in
    failed_sources.

    Returns (candidates, error) - see the module docstring.
    """
    if not api_key:
        return [], None

    url = "https://api.core.ac.uk/v3/search/works"
    query_text = f"{title} {author}".strip()
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        r = _get_with_retry("core", url, params={"q": query_text, "limit": rows}, headers=headers, timeout=15)
        r.raise_for_status()
        items = r.json().get("results", [])
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for it in items:
        cand_title = it.get("title") or ""
        if not cand_title:
            continue

        doi = it.get("doi")
        download_url = it.get("downloadUrl")
        fulltext_urls = it.get("sourceFulltextUrls") or []

        if doi:
            link = f"https://doi.org/{doi}"
        elif download_url:
            link = download_url
        elif fulltext_urls:
            link = fulltext_urls[0]
        else:
            link = it.get("id")
        url_is_reference_only = not (doi or download_url or fulltext_urls)

        authors = []
        for a in it.get("authors", []) or []:
            name = a.get("name", "") if isinstance(a, dict) else str(a)
            if name:
                authors.append(name.split()[-1])

        candidates.append({
            "source": "core",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "url_is_reference_only": url_is_reference_only,
            "authors": authors,
            "year": it.get("yearPublished"),
            "abstract": it.get("abstract") or "",
        })
    return candidates, None  # NOTE: no extra time.sleep() here - _CORE_RATE_LIMITER.wait() already
                              # enforced the pacing before the request was even sent.


def query_openaire(title: str, author: str, rows: int = 5):
    """
    Query the OpenAIRE Graph API's researchProducts search endpoint for
    candidate records. Aggregates research output specifically from
    European research institutions/funders/repositories. Free, no API key
    required for the unauthenticated tier used here.

    Returns (candidates, error) - see the module docstring.
    """
    url = "https://api.openaire.eu/graph/v1/researchProducts"
    params = {
        "search": f"{title} {author}".strip(),
        "type": "publication",
        "pageSize": rows,
        "sortBy": "relevance DESC",
    }
    try:
        r = _get_with_retry("openaire", url, params=params, headers={"accept": "application/json"}, timeout=15)
        r.raise_for_status()
        items = r.json().get("results", [])
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for it in items:
        cand_title = it.get("mainTitle") or ""
        if not cand_title:
            continue

        doi = None
        for pid in it.get("pids", []) or []:
            if isinstance(pid, dict) and str(pid.get("scheme", "")).lower() == "doi":
                doi = pid.get("value")
                break

        landing_url = None
        for instance in it.get("instances", []) or []:
            instance_urls = instance.get("urls", []) or []
            if instance_urls:
                landing_url = instance_urls[0]
                break

        url_is_reference_only = False
        if doi:
            link = f"https://doi.org/{doi}"
        elif landing_url:
            link = landing_url
        else:
            link = it.get("id")
            url_is_reference_only = True

        authors = []
        for a in it.get("authors", []) or []:
            full_name = a.get("fullName", "") if isinstance(a, dict) else str(a)
            if full_name:
                authors.append(full_name.split()[-1])

        date_raw = it.get("publicationDate", "") or ""
        year_match = re.search(r"\d{4}", date_raw)
        year = int(year_match.group()) if year_match else None

        candidates.append({
            "source": "openaire",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "url_is_reference_only": url_is_reference_only,
            "authors": authors,
            "year": year,
            "abstract": it.get("description") or it.get("descriptions") or "",
        })
    return candidates, None


def query_dnb(title: str, author: str, rows: int = 5):
    """
    Query the DNB (Deutsche Nationalbibliothek / German National Library)
    SRU catalog search - Germany's national bibliography, the regional
    first-choice source for German-language titles. Free, keyless, XML
    (Dublin Core) records.

    LINK NOTE: DNB records mostly do NOT carry a DOI - what they reliably
    have is a permanent catalog link (a "https://d-nb.info/<id>" page),
    which is treated as a genuine usable link, not url_is_reference_only.

    Returns (candidates, error) - see the module docstring.
    """
    url = "https://services.dnb.de/sru/dnb"
    cql_parts = [f'TIT="{title}"']
    if author:
        cql_parts.append(f'PER="{author}"')
    cql_query = " and ".join(cql_parts)

    params = {
        "version": "1.1",
        "operation": "searchRetrieve",
        "query": cql_query,
        "recordSchema": "oai_dc",
        "maximumRecords": rows,
    }
    try:
        r = _get_with_retry("dnb", url, params=params, timeout=15)
        r.raise_for_status()
        ns = {
            "srw": "http://www.loc.gov/zing/srw/",
            "dc": "http://purl.org/dc/elements/1.1/",
        }
        root = ET.fromstring(r.text)
        records = root.findall(".//srw:record", ns)
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for rec in records:
        record_data = rec.find("srw:recordData", ns)
        if record_data is None:
            continue

        title_els = record_data.findall("dc:title", ns)
        cand_title = title_els[0].text.strip() if title_els and title_els[0].text else ""
        if not cand_title:
            continue

        authors = []
        for creator_el in record_data.findall("dc:creator", ns):
            name = (creator_el.text or "").strip()
            if not name:
                continue
            if "," in name:
                authors.append(name.split(",")[0].strip())
            else:
                authors.append(name.split()[-1])

        date_els = record_data.findall("dc:date", ns)
        year = None
        for date_el in date_els:
            if date_el.text:
                m = re.search(r"\d{4}", date_el.text)
                if m:
                    year = int(m.group())
                    break

        doi = None
        catalog_url = None
        for id_el in record_data.findall("dc:identifier", ns):
            value = (id_el.text or "").strip()
            if not value:
                continue
            if value.lower().startswith("10.") or "doi.org/" in value.lower():
                doi = value.split("doi.org/")[-1]
            elif value.startswith("http"):
                catalog_url = value

        if doi:
            link = f"https://doi.org/{doi}"
        elif catalog_url:
            link = catalog_url
        else:
            link = None

        candidates.append({
            "source": "dnb",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "authors": authors,
            "year": year,
            "abstract": " ".join(
                (element.text or "").strip()
                for element in record_data.findall("dc:description", ns)
                if (element.text or "").strip()
            ),
        })
    return candidates, None


def query_hal(title: str, author: str, rows: int = 5):
    """
    Query HAL (Hyper Articles en Ligne, https://hal.science) - France's
    national open-archive repository for scholarly output, the regional
    first-choice source for French-language titles. Free, keyless JSON
    REST API built on Apache Solr.

    Returns (candidates, error) - see the module docstring.
    """
    url = "https://api.archives-ouvertes.fr/search/"
    params = {
        "q": f"{title} {author}".strip(),
        "rows": rows,
        "wt": "json",
        "fl": "title_s,authFullName_s,producedDate_s,doiId_s,uri_s,abstract_s",
    }
    try:
        r = _get_with_retry("hal", url, params=params, timeout=15)
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for doc in docs:
        title_list = doc.get("title_s") or []
        cand_title = title_list[0] if title_list else ""
        if not cand_title:
            continue

        authors = []
        for full_name in doc.get("authFullName_s") or []:
            if full_name:
                authors.append(full_name.split()[-1])

        doi = doc.get("doiId_s")
        hal_uri = doc.get("uri_s")
        link = f"https://doi.org/{doi}" if doi else hal_uri

        date_raw = doc.get("producedDate_s", "") or ""
        year_match = re.search(r"\d{4}", date_raw)
        year = int(year_match.group()) if year_match else None

        abstract_values = doc.get("abstract_s") or []
        abstract = abstract_values if isinstance(abstract_values, str) else " ".join(abstract_values)

        candidates.append({
            "source": "hal",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "authors": authors,
            "year": year,
            "abstract": abstract,
        })
    return candidates, None


def query_cinii(title: str, author: str, rows: int = 5):
    """
    Query CiNii Research (https://cir.nii.ac.jp) - Japan's National
    Institute of Informatics' scholarly search, the regional first-choice
    source for Japanese-language titles. Free, keyless OpenSearch-style
    JSON-LD API.

    Returns (candidates, error) - see the module docstring.
    """
    url = "https://cir.nii.ac.jp/opensearch/articles"
    params = {
        "q": f"{title} {author}".strip(),
        "count": rows,
        "format": "json",
    }
    try:
        r = _get_with_retry("cinii", url, params=params, timeout=15)
        r.raise_for_status()
        items = r.json().get("@graph", [])
    except Exception as e:
        return [], _describe_exception(e)

    candidates = []
    for it in items:
        cand_title = it.get("dc:title") or it.get("title") or ""
        if not cand_title:
            continue

        creators = it.get("dc:creator", [])
        if isinstance(creators, str):
            creators = [creators]
        authors = []
        for name in creators:
            if name:
                # Japanese names often have no space between family/given
                # name, so we keep the full string rather than guessing a
                # "last token" surname - _author_bonus()'s substring
                # fallback still matches it loosely.
                authors.append(name.strip())

        doi = it.get("prism:doi")
        cinii_url = it.get("@id")
        link = f"https://doi.org/{doi}" if doi else cinii_url

        date_raw = it.get("prism:publicationDate") or it.get("dc:date") or ""
        year_match = re.search(r"\d{4}", str(date_raw))
        year = int(year_match.group()) if year_match else None

        candidates.append({
            "source": "cinii",
            "title": cand_title,
            "doi": doi,
            "url": link,
            "authors": authors,
            "year": year,
            "abstract": it.get("dc:description") or it.get("description") or "",
        })
    return candidates, None


def _author_bonus(target_author_lastnames: list, cand_authors: list):
    """
    Shared by score_candidate() and score_candidate_detailed().

    Checks ALL of the target's authors against the candidate's author list
    (not just the target's first author) — an exact match from ANY target
    author is worth +15; failing that, a loose/substring match from any
    pair is worth +8.

    Returns (bonus, match_type, matched_author) where matched_author is
    which of the target's authors produced the bonus (or None).
    """
    if not target_author_lastnames:
        return 0, "none", None

    # Compare normalized forms so harmless spelling differences such as
    # "Mierina" vs "Mieriņa" do not suppress an otherwise exact author match.
    candidate_forms = [(a, author_comparison_forms(a)) for a in cand_authors if a]

    for lastname in target_author_lastnames:
        raw = unicodedata.normalize("NFC", str(lastname)).casefold()
        target_forms = author_comparison_forms(lastname)
        for candidate, forms in candidate_forms:
            if target_forms & forms:
                candidate_raw = unicodedata.normalize("NFC", str(candidate)).casefold()
                return 15, "exact" if raw == candidate_raw else "normalized_exact", lastname

    for lastname in target_author_lastnames:
        target_forms = author_comparison_forms(lastname)
        for _, forms in candidate_forms:
            if any(target in candidate or candidate in target
                   for target in target_forms for candidate in forms):
                return 8, "loose", lastname

    return 0, "none", None


def score_candidate(target_title: str, target_author_lastnames: list, target_year, cand: dict):
    """
    Combined scoring:
    - Title similarity is the main component (0-100, weight 0.75)
    - Author surname match bonus (up to +15) — checks ALL target authors
    - Year proximity bonus (within +/-1 year, up to +10)
    Total is capped at 100.
    """
    title_score = fuzz.token_sort_ratio(
        normalize_for_compare(target_title), normalize_for_compare(cand["title"])
    )
    base = title_score * 0.75

    author_bonus, _, _ = _author_bonus(target_author_lastnames, cand.get("authors", []))

    year_bonus = 0
    cand_years = cand.get("publication_years") or [cand.get("year")]
    if target_year and any(year is not None for year in cand_years):
        try:
            if min(abs(int(target_year) - int(year)) for year in cand_years if year is not None) <= 1:
                year_bonus = 10
        except (ValueError, TypeError):
            pass

    return round(min(100, base + author_bonus + year_bonus), 2), title_score


def score_candidate_detailed(target_title: str, target_author_lastnames: list, target_year, cand: dict):
    """
    Same scoring rules as score_candidate(), but returns every component
    separately so a single record's scoring can be inspected/visualized.
    """
    title_score = fuzz.token_sort_ratio(
        normalize_for_compare(target_title), normalize_for_compare(cand["title"])
    )
    title_component = title_score * 0.75

    author_bonus, author_match_type, matched_author = _author_bonus(
        target_author_lastnames, cand.get("authors", [])
    )

    year_bonus = 0
    year_diff = None
    cand_years = cand.get("publication_years") or [cand.get("year")]
    if target_year and any(year is not None for year in cand_years):
        try:
            year_diff = min(abs(int(target_year) - int(year))
                            for year in cand_years if year is not None)
            if year_diff <= 1:
                year_bonus = 10
        except (ValueError, TypeError):
            pass

    total_score = round(min(100, title_component + author_bonus + year_bonus), 2)

    return {
        **cand,
        "title_score": title_score,
        "title_component": round(title_component, 2),
        "author_bonus": author_bonus,
        "author_match_type": author_match_type,
        "matched_author": matched_author,
        "year_bonus": year_bonus,
        "year_diff": year_diff,
        "total_score": total_score,
    }


def best_match(target_title: str, target_author_lastnames: list, target_year, candidates: list):
    """Pick the candidate with the highest combined score."""
    best = None
    best_score = -1
    best_title_score = 0
    for c in candidates:
        score, title_score = score_candidate(target_title, target_author_lastnames, target_year, c)
        if score > best_score:
            best_score = score
            best_title_score = title_score
            best = c
    return best, best_score, best_title_score


def build_result(query_title, author_lastnames, year, all_candidates, failed_sources=None):
    """Given a candidate pool, score it and build the result dict (shared
    by early-exit paths).

    `failed_sources` is an optional list of {"source": id, "error": msg}
    dicts for any source that errored out instead of genuinely returning
    zero results (see the module docstring) - it's attached to the result
    as-is so callers always know which sources, if any, weren't actually
    checked, regardless of what status was reached.
    """
    failed_sources = failed_sources or []
    match, score, title_score = best_match(query_title, author_lastnames, year, all_candidates)

    if score >= AUTO_ACCEPT_THRESHOLD:
        status = "auto_accepted"
    elif score >= REVIEW_THRESHOLD:
        status = "needs_review"
    else:
        status = "low_confidence"

    # A candidate with neither a DOI nor a direct link only has its own
    # internal metadata page to offer - not a real link to the document.
    # Don't let that get written into doi/url looking like a genuine find;
    # downgrade to a distinct status instead.
    if match.get("url_is_reference_only") and status in ("auto_accepted", "needs_review"):
        status = "matched_no_link"

    if status in ("auto_accepted", "needs_review"):
        return {
            "doi": match["doi"] or "",
            "url": match["url"] or "",
            "match_score": score,
            "status": status,
            "matched_title": match["title"],
            "source": match["source"],
            "failed_sources": failed_sources,
        }
    elif status == "matched_no_link":
        return {
            "doi": "",
            "url": "",
            "match_score": score,
            "status": status,
            "matched_title": match["title"],
            "source": match["source"],
            # The metadata page itself - not a document link, kept only so
            # you can manually pull up the record and double-check the
            # match by eye.
            "reference_url": match["url"],
            "failed_sources": failed_sources,
        }
    else:
        return {
            "doi": "",
            "url": "",
            "match_score": score,
            "status": status,
            "matched_title": f"(not accepted, for reference only) {match['title']}",
            "source": match["source"],
            "failed_sources": failed_sources,
        }


# Maps a detect_title_language() result (ISO 639-1 code) to the (name,
# query_function) of the regional specialist source to try FIRST for that
# language, before falling through to the generic chain. A language NOT in
# this dict (including English, "en") simply skips straight to the generic
# chain.
LANGUAGE_SOURCE_MAP = {
    "de": ("dnb", query_dnb),
    "fr": ("hal", query_hal),
    "ja": ("cinii", query_cinii),
}


def process_row(title, author, author_lastnames, year, email="", s2_api_key=None, core_api_key=None):
    """
    Runs the full two-stage lookup pipeline for one record, stopping as
    soon as an auto-accept-quality match is found:

      STAGE 1 - LANGUAGE-FIRST ROUTING. Detect the title's language; if
      it has a regional specialist (German -> DNB, French -> HAL,
      Japanese -> CiNii, see LANGUAGE_SOURCE_MAP), query it first.

      STAGE 2 - GENERIC FALLBACK CHAIN. Crossref -> GESIS -> OpenAlex ->
      Semantic Scholar -> CORE -> OpenAIRE -> DataCite -> arXiv -> PubMed,
      stopping as soon as one reaches auto-accept quality.

    Every source tried along the way that errored out (rather than
    genuinely responding with zero candidates) is collected into
    `failed_sources` and attached to the returned dict via
    build_result() - see the module docstring. If NOTHING was found
    anywhere AND at least one source failed, the status is "incomplete"
    instead of "not_found", since a clean negative can't be claimed when
    part of the search never actually completed.

    Cache lookups/writes are the caller's responsibility (this function is
    side-effect-free and thread-safe on its own).

    `author` (single, first-author-only) is used for querying the APIs.
    `author_lastnames` (full list) is used for scoring — see _author_bonus().
    """
    query_title = clean_title(title)
    all_candidates = []
    failed_sources = []

    def _track(source_name, query_result):
        candidates, error = query_result
        if error:
            failed_sources.append({"source": source_name, "error": error})
        return candidates

    # ---- STAGE 1 - LANGUAGE-FIRST ROUTING ----------------------------------
    detected_lang = detect_title_language(query_title)
    lang_source_name = None
    if detected_lang in LANGUAGE_SOURCE_MAP:
        lang_source_name, lang_query_fn = LANGUAGE_SOURCE_MAP[detected_lang]
        all_candidates += _track(lang_source_name, lang_query_fn(query_title, author))
        if all_candidates:
            result = build_result(query_title, author_lastnames, year, all_candidates, failed_sources)
            if result["status"] == "auto_accepted":
                result["detected_lang"] = detected_lang
                return result
        # Otherwise fall through to STAGE 2, keeping whatever the regional
        # source already returned in the pool.

    # ---- STAGE 2 - GENERIC FALLBACK CHAIN ----------------------------------
    all_candidates += _track("crossref", query_crossref(query_title, author, email))
    if all_candidates:
        result = build_result(query_title, author_lastnames, year, all_candidates, failed_sources)
        if result["status"] == "auto_accepted":
            result["detected_lang"] = detected_lang
            return result

    # GESIS: moved up to position #2 (right after Crossref) given how
    # ISSP-heavy this project's data is.
    all_candidates += _track("gesis", query_gesis(query_title, author))
    if all_candidates:
        result = build_result(query_title, author_lastnames, year, all_candidates, failed_sources)
        if result["status"] == "auto_accepted":
            result["detected_lang"] = detected_lang
            return result

    all_candidates += _track("openalex", query_openalex(query_title, author))
    if all_candidates:
        result = build_result(query_title, author_lastnames, year, all_candidates, failed_sources)
        if result["status"] == "auto_accepted":
            result["detected_lang"] = detected_lang
            return result

    all_candidates += _track("semantic_scholar", query_semantic_scholar(query_title, author, api_key=s2_api_key))
    if all_candidates:
        result = build_result(query_title, author_lastnames, year, all_candidates, failed_sources)
        if result["status"] == "auto_accepted":
            result["detected_lang"] = detected_lang
            return result

    all_candidates += _track("core", query_core(query_title, author, api_key=core_api_key))
    if all_candidates:
        result = build_result(query_title, author_lastnames, year, all_candidates, failed_sources)
        if result["status"] == "auto_accepted":
            result["detected_lang"] = detected_lang
            return result

    all_candidates += _track("openaire", query_openaire(query_title, author))
    if all_candidates:
        result = build_result(query_title, author_lastnames, year, all_candidates, failed_sources)
        if result["status"] == "auto_accepted":
            result["detected_lang"] = detected_lang
            return result

    all_candidates += _track("datacite", query_datacite(query_title, author))
    if all_candidates:
        result = build_result(query_title, author_lastnames, year, all_candidates, failed_sources)
        if result["status"] == "auto_accepted":
            result["detected_lang"] = detected_lang
            return result

    # arXiv and PubMed: neither is a natural fit for sociology/labor-market
    # survey data, so they're queried last.
    all_candidates += _track("arxiv", query_arxiv(query_title, author))
    all_candidates += _track("pubmed", query_pubmed(query_title, author, email))

    if not all_candidates:
        result = {
            "doi": "", "url": "", "match_score": 0,
            "status": "incomplete" if failed_sources else "not_found",
            "matched_title": "", "source": "",
            "failed_sources": failed_sources,
        }
    else:
        result = build_result(query_title, author_lastnames, year, all_candidates, failed_sources)

    result["detected_lang"] = detected_lang
    result["lang_source_tried"] = lang_source_name
    return result


def explain_match(title: str, author_field: str, year, email: str = "", s2_api_key: str = None, core_api_key: str = None):
    """
    Run the full lookup pipeline for ONE record and return EVERYTHING —
    not just the winning candidate — so the whole process can be
    inspected: the query actually sent, every candidate returned by EVERY
    source (including the language-routed regional source, if any), and
    the full score breakdown for each one. Intentionally does NOT use the
    early-exit optimization in process_row().

    Like process_row(), collects any source that errored out into
    `failed_sources` and reports "incomplete" (rather than "not_found")
    when nothing was found and at least one source never responded.
    """
    query_title = clean_title(title)
    author_lastname = clean_author(author_field)
    author_lastnames = clean_authors(author_field)

    detected_lang = detect_title_language(query_title)
    failed_sources = []

    def _track(source_name, query_result):
        candidates, error = query_result
        if error:
            failed_sources.append({"source": source_name, "error": error})
        return candidates

    all_candidates = []
    if detected_lang in LANGUAGE_SOURCE_MAP:
        lang_source_name, lang_query_fn = LANGUAGE_SOURCE_MAP[detected_lang]
        all_candidates += _track(lang_source_name, lang_query_fn(query_title, author_lastname))

    all_candidates += _track("crossref", query_crossref(query_title, author_lastname, email))
    all_candidates += _track("gesis", query_gesis(query_title, author_lastname))
    all_candidates += _track("openalex", query_openalex(query_title, author_lastname))
    all_candidates += _track("semantic_scholar", query_semantic_scholar(query_title, author_lastname, api_key=s2_api_key))
    all_candidates += _track("core", query_core(query_title, author_lastname, api_key=core_api_key))
    all_candidates += _track("openaire", query_openaire(query_title, author_lastname))
    all_candidates += _track("datacite", query_datacite(query_title, author_lastname))
    all_candidates += _track("arxiv", query_arxiv(query_title, author_lastname))
    all_candidates += _track("pubmed", query_pubmed(query_title, author_lastname, email))

    scored = [
        score_candidate_detailed(query_title, author_lastnames, year, c)
        for c in all_candidates
    ]
    scored.sort(key=lambda c: -c["total_score"])

    winner = scored[0] if scored else None
    if winner is None:
        status = "incomplete" if failed_sources else "not_found"
    elif winner["total_score"] >= AUTO_ACCEPT_THRESHOLD:
        status = "auto_accepted"
    elif winner["total_score"] >= REVIEW_THRESHOLD:
        status = "needs_review"
    else:
        status = "low_confidence"

    if winner and winner.get("url_is_reference_only") and status in ("auto_accepted", "needs_review"):
        status = "matched_no_link"

    return {
        "input": {
            "title_raw": title,
            "query_title": query_title,
            "author_raw": author_field,
            "author_lastname": author_lastname,
            "author_lastnames": author_lastnames,
            "year": year,
            "detected_lang": detected_lang,
        },
        "thresholds": {
            "auto_accept": AUTO_ACCEPT_THRESHOLD,
            "review": REVIEW_THRESHOLD,
        },
        "candidates": scored,
        "failed_sources": failed_sources,
        "winner_doi": winner["doi"] if winner else None,
        "status": status,
    }
