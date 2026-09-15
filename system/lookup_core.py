"""
lookup_core.py
---------------
Application layer between the GUI and the query/scoring logic in
doi_lookup_lib.py in this directory. The research pipeline imports the same
system module rather than keeping a second copy. This
module adds the pieces that are specific to the GUI app rather than the
lookup logic itself:

- SOURCES: the list of selectable data sources shown in the GUI, with the
  human-readable label/notes and what optional input (email, API key)
  each one takes.
- run_search() / lookup_one(): thin wrappers that only query the sources
  the user enabled, instead of all of them - the shared library's
  process_row()/explain_match() always query everything (or use the
  ISSP-specific language-routing order), which isn't what a general-
  purpose "look up any literature" tool wants.
- Local cache and CSV/Excel/JSON file I/O, which are specific to this app
  (find_doi_url.py keeps its own separate cache file).
"""

import json
import ipaddress
from io import BytesIO
import os
import re
import shutil
import socket
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlparse
from html import unescape
from html.parser import HTMLParser

import pandas as pd
import requests

try:  # Package import: import system.lookup_core
    from . import doi_lookup_lib as lib
except ImportError:  # Direct app/script import from inside system/
    import doi_lookup_lib as lib

_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_HERE, "lookup_cache.json")
VERIFICATION_CACHE_FILE = os.path.join(_HERE, "verification_cache.json")
ABSTRACT_CACHE_FILE = os.path.join(_HERE, "abstract_cache.jsonl")
SETTINGS_FILE = os.path.join(_HERE, "app_settings.json")
_VERIFICATION_CACHE_LOCK = __import__("threading").Lock()
_ABSTRACT_CACHE_LOCK = __import__("threading").Lock()

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
VERIFY_TIMEOUT = 15
VERIFICATION_LOGIC_VERSION = "4.0"
MAX_METADATA_BYTES = 512 * 1024
MAX_CONTAINER_PAGE_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 15 * 1024 * 1024
PDF_TEXT_PAGES = 5
_URL_LIMITERS = {}
_URL_LIMITERS_LOCK = __import__("threading").Lock()


class _CitationMetadataParser(HTMLParser):
    """Extract public citation/DC/OpenGraph metadata without rendering JS."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.json_ld = []
        self._in_json_ld = False
        self._json_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): v for k, v in attrs if k}
        if tag.lower() == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").lower()
            value = attrs.get("content")
            if key and value:
                self.meta.setdefault(key, []).append(value.strip())
        elif tag.lower() == "script" and "ld+json" in (attrs.get("type") or "").lower():
            self._in_json_ld, self._json_parts = True, []

    def handle_data(self, data):
        if self._in_json_ld:
            self._json_parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._in_json_ld:
            try:
                self.json_ld.append(json.loads("".join(self._json_parts)))
            except (ValueError, TypeError):
                pass
            self._in_json_ld, self._json_parts = False, []


def _public_http_url(url):
    """Reject non-web, local and private-network targets before fetching."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False, "Only public HTTP(S) URLs can be verified."
    try:
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or default_port)}
    except socket.gaierror:
        return False, "The URL hostname could not be resolved."
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return False, "Local and private-network URLs are not allowed."
    return True, ""


def _first(meta, *keys):
    for key in keys:
        values = meta.get(key.lower()) or []
        if values and values[0]:
            return values[0]
    return ""


def _jsonld_articles(value):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_articles(item)
    elif isinstance(value, dict):
        graph = value.get("@graph")
        if graph:
            yield from _jsonld_articles(graph)
        kind = value.get("@type", "")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(str(k).lower() in {"scholarlyarticle", "article", "chapter", "book"} for k in kinds):
            yield value


def _metadata_candidate(html, final_url):
    parser = _CitationMetadataParser()
    parser.feed(html)
    title = _first(parser.meta, "citation_title", "dc.title", "dcterms.title", "og:title")
    authors = (parser.meta.get("citation_author") or parser.meta.get("dc.creator") or
               parser.meta.get("dcterms.creator") or [])
    year = _first(parser.meta, "citation_publication_date", "citation_date", "dc.date", "article:published_time")
    doi = normalize_doi(_first(parser.meta, "citation_doi", "dc.identifier", "dcterms.identifier"))
    publisher = _first(parser.meta, "citation_publisher", "dc.publisher", "dcterms.publisher")
    item_type = _first(parser.meta, "citation_type", "dc.type", "dcterms.type")
    for block in parser.json_ld:
        for article in _jsonld_articles(block):
            title = title or article.get("headline") or article.get("name") or ""
            raw_authors = article.get("author") or []
            if not isinstance(raw_authors, list):
                raw_authors = [raw_authors]
            authors = authors or [a.get("name", "") if isinstance(a, dict) else str(a) for a in raw_authors]
            year = year or article.get("datePublished") or ""
            doi = doi or normalize_doi(article.get("doi") or "")
            raw_publisher = article.get("publisher") or ""
            if isinstance(raw_publisher, dict):
                raw_publisher = raw_publisher.get("name", "")
            publisher = publisher or raw_publisher
            item_type = item_type or article.get("@type", "")
            break
    year_match = re.search(r"(?:19|20)\d{2}", str(year))
    return {"title": str(title).strip(), "authors": authors,
            "year": int(year_match.group()) if year_match else None, "doi": doi,
            "url": final_url, "source": "Web citation metadata",
            "publisher": str(publisher or ""), "item_type": str(item_type or "")}


def _safe_get(session, url, timeout, max_redirects=5):
    """GET a public URL while re-checking every redirect target."""
    current = str(url).strip()
    headers = {"User-Agent": "LiteratureLookup/1.0 (citation metadata verification)"}
    for _ in range(max_redirects + 1):
        allowed, reason = _public_http_url(current)
        if not allowed:
            raise ValueError(reason)
        host = urlparse(current).hostname.lower()
        with _URL_LIMITERS_LOCK:
            limiter = _URL_LIMITERS.setdefault(host, lib._RateLimiter(1.0))
        response = None
        for attempt in range(3):
            limiter.wait()
            response = session.get(current, timeout=timeout, allow_redirects=False,
                                   headers=headers, stream=True)
            if response.status_code != 429:
                break
            if attempt < 2:
                delay = lib._retry_after_seconds(response, attempt)
                lib._emit_status({"kind": "rate_limit", "source": host,
                                  "delay": round(delay, 1), "attempt": attempt + 1,
                                  "max_attempts": 3})
                response.close()
                limiter.defer(delay)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                return response
            current = urljoin(current, location)
            continue
        return response
    raise ValueError("The URL redirected too many times.")


def _read_limited_response(response, limit):
    chunks, size = [], 0
    for chunk in response.iter_content(chunk_size=16384):
        if not chunk:
            continue
        remaining = limit - size
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        size += min(len(chunk), remaining)
        if len(chunk) > remaining:
            break
    return b"".join(chunks)


# Common install locations checked when "tesseract" isn't already on PATH -
# the official Windows installer (UB-Mannheim build) doesn't always add
# itself to PATH, and a user may have installed it somewhere else entirely.
# Set the TESSERACT_CMD environment variable to override with any path.
_TESSERACT_FALLBACK_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def _locate_tesseract():
    """Point pytesseract at the tesseract executable if it isn't already
    resolvable on PATH. Safe to call every time - cheap, and does nothing
    once pytesseract already has a working tesseract_cmd."""
    if shutil.which("tesseract"):
        return
    import pytesseract
    if pytesseract.pytesseract.tesseract_cmd not in (None, "", "tesseract") and \
            os.path.isfile(pytesseract.pytesseract.tesseract_cmd):
        return  # already configured (e.g. by a previous call) and still valid
    for candidate in [os.environ.get("TESSERACT_CMD", "")] + _TESSERACT_FALLBACK_PATHS:
        if candidate and os.path.isfile(candidate):
            pytesseract.pytesseract.tesseract_cmd = candidate
            return


def _pdf_text(pdf_bytes):
    """Extract a bounded amount of native PDF text; OCR is optional fallback."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(pdf_bytes))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:PDF_TEXT_PAGES])
    except Exception:
        text = ""
    if len(text.strip()) >= 100:
        return text, False, "native_text"
    try:
        import fitz
        import pytesseract
        from PIL import Image
        _locate_tesseract()
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        for page in document[:2]:
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(pytesseract.image_to_string(image))
        ocr_text = "\n".join(pages)
        if len(ocr_text.strip()) >= 100:
            return ocr_text, True, "ocr"
        return text, False, "ocr_no_text"
    except Exception:
        return text, False, "ocr_unavailable"


def _match_document_text(title, author, year, text):
    normalized_text = lib.normalize_for_compare(text)
    title_score = float(lib.fuzz.partial_ratio(lib.normalize_for_compare(str(title or "")), normalized_text))
    surnames = lib.clean_authors(str(author or ""))
    author_match = not surnames or any(lib.normalize_for_compare(name) in normalized_text for name in surnames)
    year_match = not year or str(year).split(".")[0] in normalized_text
    matched = title_score >= 85 and author_match and year_match
    return matched, round(title_score, 2), author_match, year_match


class _PageTextParser(HTMLParser):
    """Collect text and embedded page data from a bounded HTML response."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        if str(data or "").strip():
            self.parts.append(str(data))


class _AlternateLanguageParser(HTMLParser):
    """Find publisher-declared English/alternate-language links."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []
        self._anchor = None
        self._anchor_text = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): str(v or "") for k, v in attrs if k}
        if tag.lower() == "link" and "alternate" in attrs.get("rel", "").lower():
            if attrs.get("hreflang", "").lower().startswith("en") and attrs.get("href"):
                self.urls.append(attrs["href"])
        elif tag.lower() == "a" and attrs.get("href"):
            self._anchor = attrs["href"]
            self._anchor_text = []
            language = (attrs.get("hreflang") or attrs.get("lang") or "").lower()
            if language.startswith("en"):
                self.urls.append(self._anchor)

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor_text.append(str(data or ""))

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._anchor is not None:
            label = " ".join(self._anchor_text).strip().casefold()
            if label in {"en", "eng", "english", "english version"}:
                self.urls.append(self._anchor)
            self._anchor, self._anchor_text = None, []


def _target_title_variants(title):
    """Return full and credible semicolon-separated parallel title forms."""
    full = str(title or "").strip()
    variants = [("full_title", full)] if full else []
    parts = [part.strip() for part in re.split(r"\s*;\s*", full) if part.strip()]
    if len(parts) >= 2 and all(len(lib.normalize_for_compare(part).split()) >= 4 for part in parts):
        variants.extend((f"parallel_title_{index + 1}", part) for index, part in enumerate(parts))
    return variants


def _alternate_language_urls(html, base_url, limit=2):
    parser = _AlternateLanguageParser()
    try:
        parser.feed(str(html or ""))
    except Exception:
        return []
    base_host = (urlparse(base_url).hostname or "").lower()
    urls = []
    parsed_base = urlparse(base_url)
    if ((parsed_base.hostname or "").lower().endswith("jstage.jst.go.jp") and
            "/-char/ja/" in parsed_base.path):
        urls.append(base_url.replace("/-char/ja/", "/-char/en/"))
    for raw in parser.urls:
        candidate = urljoin(base_url, raw)
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() == base_host:
            if candidate not in urls and candidate != base_url:
                urls.append(candidate)
        if len(urls) >= limit:
            break
    return urls


def _find_page_or_language_evidence(session, title, author, html, base_url, timeout):
    """Check the current page, then at most two publisher-declared language variants."""
    evidence = _chapter_page_evidence(title, author, html)
    if evidence["matched"]:
        return evidence, base_url, ""
    for alternate_url in _alternate_language_urls(html, base_url):
        try:
            alternate = _safe_get(session, alternate_url, timeout)
            alternate_type = (alternate.headers.get("Content-Type") or "").lower()
            if 200 <= alternate.status_code < 400 and "html" in alternate_type:
                alternate_bytes = _read_limited_response(alternate, MAX_CONTAINER_PAGE_BYTES)
                alternate_html = alternate_bytes.decode(
                    alternate.encoding or "utf-8", errors="replace")
                alternate_evidence = _chapter_page_evidence(title, author, alternate_html)
                resolved = alternate.url
                alternate.close()
                if alternate_evidence["matched"]:
                    return alternate_evidence, resolved, resolved
            else:
                alternate.close()
        except Exception:
            continue
    return evidence, base_url, ""


def _chapter_page_evidence(title, author, html):
    """Require an exact chapter title plus most supplied surnames nearby."""
    parser = _PageTextParser()
    try:
        parser.feed(str(html or ""))
    except Exception:
        pass
    metadata_parser = _CitationMetadataParser()
    try:
        metadata_parser.feed(str(html or ""))
    except Exception:
        pass
    metadata_values = [value for values in metadata_parser.meta.values() for value in values]
    normalized_text = re.sub(
        r"\s+", " ", lib.normalize_for_compare(" ".join(parser.parts + metadata_values))).strip()
    candidates = []
    searchable_text = normalized_text[:300000]
    for name, value in _target_title_variants(title):
        normalized_value = re.sub(r"\s+", " ", lib.normalize_for_compare(value)).strip()
        exact_position = searchable_text.find(normalized_value) if normalized_value else -1
        title_score = (100.0 if exact_position >= 0 else
                       float(lib.fuzz.partial_ratio(normalized_value, searchable_text))
                       if normalized_value else 0.0)
        position = exact_position
        if position < 0 and title_score >= 90:
            words = normalized_value.split()
            for anchor in (" ".join(words[:8]), " ".join(words[-8:])):
                if anchor and (position := searchable_text.find(anchor)) >= 0:
                    break
        candidates.append((name, value, position, title_score))
    matched_variant, matched_title, position, title_score = max(
        candidates, key=lambda item: (item[3], item[2]), default=("", "", -1, 0.0))
    normalized_title = re.sub(r"\s+", " ", lib.normalize_for_compare(matched_title)).strip()
    surnames = [lib.normalize_for_compare(name) for name in lib.clean_authors(str(author or ""))]
    required = max(1, (2 * len(surnames) + 2) // 3) if surnames else 0
    if position < 0 or title_score < 90:
        return {"matched": False, "title_found": False, "matched_title_variant": "",
                "title_score": round(title_score, 2), "matched_authors": [],
                "required_authors": required}
    nearby = normalized_text[max(0, position - 1200):position + len(normalized_title) + 1200]
    matched_authors = [name for name in surnames if name and name in nearby]
    matched = bool(position >= 0 and (not surnames or len(matched_authors) >= required))
    return {"matched": matched, "title_found": True, "matched_title_variant": matched_variant,
            "title_score": round(title_score, 2),
            "matched_authors": matched_authors, "required_authors": required}


def _access_status(code):
    if 200 <= code < 400: return "reachable"
    if code == 403: return "access_blocked"
    if code == 429: return "rate_limited"
    if code in {404, 410}: return "broken"
    if code >= 500: return "temporarily_unavailable"
    return "http_error"


def normalize_doi(value):
    """Return a bare DOI, accepting a DOI URL or a ``doi:`` prefix."""
    text = str(value or "").strip()
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    match = re.search(r"(?:https?://(?:dx\.)?doi\.org/)(.+)$", text, re.IGNORECASE)
    if match:
        text = unquote(match.group(1))
    return text.strip().rstrip(".,;)")


def _doi_from_url(url):
    parsed = urlparse(str(url or "").strip())
    if parsed.netloc.lower() in {"doi.org", "dx.doi.org", "www.doi.org"}:
        return normalize_doi(parsed.path.lstrip("/"))
    return ""


def _registry_candidate(doi, session, timeout, registry):
    """Fetch one authoritative DOI record and map it to our matcher shape."""
    quoted = requests.utils.quote(doi, safe="")
    headers = {"User-Agent": "LiteratureLookup/1.0"}
    if registry == "crossref":
        response = session.get(f"https://api.crossref.org/works/{quoted}", timeout=timeout, headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        item = response.json().get("message", {})
        titles, subtitles, authors = item.get("title") or [], item.get("subtitle") or [], item.get("author") or []
        main_title = titles[0] if titles else ""
        subtitle = subtitles[0] if subtitles else ""
        combined_title = f"{main_title}: {subtitle}" if main_title and subtitle else (main_title or subtitle)
        date_sources = [item.get("published-print"), item.get("published-online"), item.get("issued")]
        publication_years = []
        for date_source in date_sources:
            parts = (date_source or {}).get("date-parts") or []
            if parts and parts[0] and parts[0][0] not in publication_years:
                publication_years.append(parts[0][0])
        return {"title": combined_title, "main_title": main_title, "subtitle": subtitle,
                "combined_title": combined_title, "title_match_variant": "combined_title" if subtitle else "main_title",
                "authors": [a.get("family", "") for a in authors if a.get("family")],
                "year": publication_years[0] if publication_years else None,
                "publication_years": publication_years, "doi": item.get("DOI") or doi,
                "url": item.get("URL") or f"https://doi.org/{doi}", "source": "Crossref verification",
                "item_type": item.get("type", ""), "journal": (item.get("container-title") or [""])[0],
                "publisher": item.get("publisher", ""),
                "volume": item.get("volume", ""), "issue": item.get("issue", ""),
                "pages": item.get("page", ""), "isbn": ";".join(item.get("ISBN") or []),
                "issn": ";".join(item.get("ISSN") or [])}
    response = session.get(f"https://api.datacite.org/dois/{quoted}", timeout=timeout, headers=headers)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    attrs = response.json().get("data", {}).get("attributes", {})
    titles, creators = attrs.get("titles") or [], attrs.get("creators") or []
    datacite_title = titles[0].get("title", "") if titles else ""
    return {"title": datacite_title, "main_title": datacite_title, "subtitle": "",
            "combined_title": datacite_title, "title_match_variant": "main_title",
            "authors": [c.get("familyName") or c.get("name", "") for c in creators],
            "year": attrs.get("publicationYear"), "publication_years": [attrs.get("publicationYear")]
            if attrs.get("publicationYear") else [], "doi": attrs.get("doi") or doi,
            "url": attrs.get("url") or f"https://doi.org/{doi}", "source": "DataCite verification",
            "item_type": ((attrs.get("types") or {}).get("resourceTypeGeneral") or ""),
            "journal": "", "publisher": attrs.get("publisher", ""),
            "volume": "", "issue": "", "pages": "",
            "isbn": "", "issn": ""}


def _item_type_family(value):
    value = str(value or "").casefold().replace("_", "-")
    if "chapter" in value or "section" in value or "component" in value or value == "chap": return "chapter"
    if "article" in value or "journal" in value or value == "jour": return "article"
    if "conference" in value or "proceeding" in value or value in {"cpaper", "conf"}: return "conference"
    if "book" in value or "monograph" in value: return "book"
    if "thesis" in value or "dissertation" in value: return "thesis"
    if "dataset" in value: return "dataset"
    if "report" in value or value == "rprt": return "report"
    return ""


def _container_titles_match(target, found):
    """Match full/short container names without accepting tiny generic fragments."""
    target_norm = re.sub(r"[^\w]+", " ", lib.normalize_for_compare(str(target or ""))).strip()
    found_norm = re.sub(r"[^\w]+", " ", lib.normalize_for_compare(str(found or ""))).strip()
    if not target_norm or not found_norm:
        return True
    if target_norm == found_norm:
        return True
    shorter, longer = sorted((target_norm, found_norm), key=len)
    contained = shorter in longer and len(shorter.split()) >= 3 and len(shorter) / len(longer) >= 0.30
    return contained or lib.fuzz.token_sort_ratio(target_norm, found_norm) >= 60


def _publisher_names_match(target, found):
    """Allow common long/short publisher forms, e.g. Wiley vs John Wiley & Sons."""
    target_norm = re.sub(r"[^\w]+", " ", lib.normalize_for_compare(str(target or ""))).strip()
    found_norm = re.sub(r"[^\w]+", " ", lib.normalize_for_compare(str(found or ""))).strip()
    if not target_norm or not found_norm or target_norm == found_norm:
        return True
    shorter, longer = sorted((target_norm, found_norm), key=len)
    return ((shorter in longer and len(shorter) >= 4) or
            lib.fuzz.token_set_ratio(target_norm, found_norm) >= 70)


def _is_container_match(candidate, context):
    """Identify a chapter record whose DOI resolves to its parent book."""
    if not context or _item_type_family(context.get("item_type")) != "chapter":
        return False
    if _item_type_family(candidate.get("item_type")) != "book":
        return False
    target = str(context.get("container_title_hint") or context.get("journal") or "").strip()
    if not target:
        return False
    candidate_titles = {
        str(candidate.get("main_title") or "").strip(),
        str(candidate.get("combined_title") or "").strip(),
        str(candidate.get("title") or "").strip(),
    }
    target_norm = lib.normalize_for_compare(target)
    for found in filter(None, candidate_titles):
        found_norm = lib.normalize_for_compare(found)
        shorter, longer = sorted((target_norm, found_norm), key=len)
        if shorter == longer:
            return True
        if shorter in longer and len(shorter.split()) >= 3:
            return True
        if lib.fuzz.token_set_ratio(target_norm, found_norm) >= 85:
            return True
    return False


def _metadata_conflicts(candidate, context):
    """Return secondary metadata warnings, selected by publication type."""
    if not context:
        return []
    conflicts = []
    target_family = _item_type_family(context.get("item_type"))
    found_family = _item_type_family(candidate.get("item_type"))
    family = target_family or found_family
    if target_family and found_family and target_family != found_family:
        conflicts.append("item_type")
    target_publisher = str(context.get("publisher") or "")
    found_publisher = str(candidate.get("publisher") or "")
    if target_publisher and found_publisher and not _publisher_names_match(target_publisher, found_publisher):
        conflicts.append("publisher")

    fields_by_family = {
        "article": {"volume", "issue", "pages", "issn", "journal"},
        "chapter": {"pages", "isbn", "journal"},
        "book": {"isbn", "journal"},
        "conference": {"pages", "isbn", "journal"},
        "report": {"journal"},
        "thesis": set(), "dataset": set(),
    }
    relevant = fields_by_family.get(family, {"volume", "issue", "pages", "isbn", "issn", "journal"})
    for key in ("volume", "issue"):
        if key not in relevant:
            continue
        target, found = str(context.get(key) or "").strip(), str(candidate.get(key) or "").strip()
        if target and found and lib.normalize_for_compare(target) != lib.normalize_for_compare(found):
            conflicts.append(key)
    target_pages = re.sub(r"\s+", "", str(context.get("pages") or "").replace("–", "-").replace("—", "-"))
    found_pages = re.sub(r"\s+", "", str(candidate.get("pages") or "").replace("–", "-").replace("—", "-"))
    if "pages" in relevant and target_pages and found_pages and target_pages != found_pages:
        conflicts.append("pages")
    for key in ("isbn", "issn"):
        if key not in relevant:
            continue
        target = re.sub(r"[^0-9x]", "", str(context.get(key) or "").casefold())
        found = re.sub(r"[^0-9x]", "", str(candidate.get(key) or "").casefold())
        if target and found and target not in found and found not in target:
            conflicts.append(key)
    target_journal, found_journal = str(context.get("journal") or ""), str(candidate.get("journal") or "")
    if "journal" in relevant and target_journal and found_journal and not _container_titles_match(
            target_journal, found_journal):
        conflicts.append("journal")
    return conflicts


def _identity_conflicts(author, year, candidate):
    """Detect explicit author/year contradictions that a strong title must not override."""
    conflicts = []
    target_authors = [lib.author_comparison_forms(a)
                      for a in lib.clean_authors(str(author or "")) if a]
    found_authors = [lib.author_comparison_forms(a)
                     for a in (candidate.get("authors") or []) if a]
    if target_authors and found_authors and not any(
            any(target == found or target in found or found in target
                for target in target_forms for found in found_forms)
            for target_forms in target_authors for found_forms in found_authors):
        conflicts.append("authors")
    target_year = re.search(r"\b(?:18|19|20|21)\d{2}\b", str(year or ""))
    found_years = candidate.get("publication_years") or [candidate.get("year")]
    parsed_years = [int(match.group()) for value in found_years
                    if (match := re.search(r"\b(?:18|19|20|21)\d{2}\b", str(value or "")))]
    if target_year and parsed_years and min(abs(int(target_year.group()) - found) for found in parsed_years) > 1:
        conflicts.append("year")
    return conflicts


_CONFLICT_LABELS = {
    "authors": "Authors", "year": "Publication year", "journal": "Journal / publication title",
    "item_type": "Item type", "volume": "Volume", "issue": "Issue", "pages": "Pages",
    "isbn": "ISBN", "issn": "ISSN", "publisher": "Publisher",
}


def _metadata_mismatch_message(prefix, details, conflicts):
    """Explain which checks caused a metadata mismatch in exported results."""
    if conflicts:
        labels = [_CONFLICT_LABELS.get(field, field.replace("_", " ").title()) for field in conflicts]
        return f"{prefix} Conflicting fields: {', '.join(labels)}."
    failed = []
    title_score = float(details.get("title_score", 0) or 0)
    total_score = float(details.get("total_score", 0) or 0)
    if title_score < 80:
        failed.append(f"title similarity {title_score:.1f}/100 (minimum 80)")
    if details.get("author_match_type", "none") == "none":
        failed.append("authors did not match")
    year_diff = details.get("year_diff")
    if year_diff is not None and year_diff > 1:
        failed.append(f"publication year differs by {year_diff} years")
    if total_score < 75:
        failed.append(f"overall score {total_score:.1f}/100 (minimum 75)")
    return f"{prefix} Checks not met: {'; '.join(failed) or 'insufficient matching metadata'}."


def _score_title_variants(title, author, year, candidate):
    """Score main and main+subtitle independently; keep the better result.

    A subtitle may improve a match but can never reduce the score that the
    source's main title would have received on its own.
    """
    variants = [("main_title", candidate.get("main_title") or candidate.get("title", ""))]
    combined = candidate.get("combined_title") or candidate.get("title", "")
    if candidate.get("subtitle") and combined and combined != variants[0][1]:
        variants.append(("combined_title", combined))
    scored = []
    for target_name, target_title in _target_title_variants(title):
        for variant_name, variant_title in variants:
            variant_candidate = dict(candidate, title=variant_title)
            details = lib.score_candidate_detailed(
                lib.clean_title(target_title), lib.clean_authors(str(author or "")), year,
                variant_candidate)
            target_norm = re.sub(r"\s+", " ", lib.normalize_for_compare(lib.clean_title(target_title))).strip()
            variant_norm = re.sub(r"\s+", " ", lib.normalize_for_compare(variant_title)).strip()
            shorter, longer = sorted((target_norm, variant_norm), key=len)
            if (shorter and shorter != longer and shorter in longer and len(shorter.split()) >= 4
                    and len(shorter) / len(longer) >= 0.50):
                details["title_score"] = 100.0
                details["title_component"] = 75.0
                details["total_score"] = round(min(
                    100, 75.0 + float(details.get("author_bonus", 0)) + float(details.get("year_bonus", 0))), 2)
                variant_name = f"contained_{variant_name}"
            details["title_match_variant"] = (variant_name if target_name == "full_title"
                                              else f"{target_name}_vs_{variant_name}")
            scored.append(details)
    return max(scored, key=lambda item: (float(item.get("total_score", 0)),
                                         float(item.get("title_score", 0))))


def _source_family(source_id):
    """Group correlated indexes so high-confidence mode needs genuinely distinct evidence."""
    families = {
        "crossref": "doi_registry", "datacite": "doi_registry",
        "openalex": "scholarly_graph", "semantic_scholar": "scholarly_graph",
        "core": "repository", "openaire": "repository", "hal": "repository",
        "arxiv": "repository", "gesis": "repository",
        "pubmed": "subject_index", "cinii": "national_catalogue", "dnb": "national_catalogue",
    }
    return families.get(source_id, source_id)


def _verify_primary_reference(title, author="", year="", doi="", url="", session=None,
                              timeout=VERIFY_TIMEOUT, bibliographic_context=None,
                              skip_registry=False):
    """Verify that a DOI/link exists and its metadata describes the paper."""
    session = session or requests.Session()
    bare_doi = "" if skip_registry else (normalize_doi(doi) or _doi_from_url(url))
    result = {"status": "unverified", "verified": False, "link_valid": False,
              "paper_match": False, "score": 0.0, "doi": bare_doi,
              "resolved_url": "", "metadata_title": "", "metadata_authors": [],
              "metadata_year": None, "metadata_source": "", "metadata_item_type": "",
              "metadata_publisher": "", "metadata_container_title": "", "metadata_doi": "",
              "message": "",
              "identifier_valid": False, "landing_page_reachable": None,
              "access_status": "not_checked", "content_type": "",
              "pdf_detected": False, "pdf_text_extracted": False,
              "ocr_used": False, "ocr_status": "not_needed",
              "verification_level": "unverified", "metadata_main_title": "",
              "metadata_subtitle": "", "metadata_combined_title": "",
              "title_match_variant": "", "title_score": 0.0,
              "author_match_type": "none", "year_difference": None,
              "publication_years": [], "identifier_match_type": "none",
              "decision_rule": "insufficient_evidence", "core_identity_match": False,
              "secondary_metadata_match": None, "metadata_conflicts": [], "metadata_warnings": [],
              "container_match": False, "container_page_checked": False,
              "container_page_title_found": False, "container_page_matched_authors": [],
              "container_evidence_source": ""}
    result.update(landing_page_evidence_checked=False, alternate_language_url="",
                  page_matched_title_variant="", page_title_evidence_score=0.0,
                  page_matched_authors=[])
    if bare_doi:
        if not DOI_RE.match(bare_doi):
            result.update(status="invalid", message="The DOI format is invalid.")
            return result
        url_doi = _doi_from_url(url)
        if url_doi and url_doi.lower() != bare_doi.lower():
            result.update(status="mismatch", message="The DOI and DOI URL point to different records.")
            return result
        candidate, errors = None, []
        for registry in ("crossref", "datacite"):
            try:
                candidate = _registry_candidate(bare_doi, session, timeout, registry)
                if candidate:
                    break
            except requests.RequestException as exc:
                errors.append(str(exc))
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(f"invalid metadata response: {exc}")
        if not candidate:
            # Some valid publisher DOIs (notably J-STAGE records) are absent
            # from both registries. Resolve the supplied DOI/URL and inspect
            # the publisher page before declaring the identifier invalid.
            fallback_url = url if str(url or "").lower().startswith(("http://", "https://")) else f"https://doi.org/{bare_doi}"
            try:
                web_result = _verify_primary_reference(
                    title, author, year, doi="", url=fallback_url, session=session,
                    timeout=timeout, bibliographic_context=bibliographic_context,
                    skip_registry=True)
            except Exception as exc:
                errors.append(f"landing page fallback: {exc}")
                web_result = None
            if web_result and (web_result.get("link_valid") or web_result.get("landing_page_reachable")):
                web_result.update(
                    doi=bare_doi, identifier_valid=True,
                    identifier_match_type="doi_resolution_page")
                if web_result.get("verified"):
                    web_result["message"] = (
                        "The DOI was not present in Crossref or DataCite, but it resolved to a publisher "
                        "page whose title and author evidence match this record. " + web_result.get("message", ""))
                    web_result["decision_rule"] = "doi_resolution_publisher_page"
                    web_result["verification_level"] = "verified_via_doi_landing_page"
                return web_result
            status = "unavailable" if errors else "invalid"
            message = ("Verification services did not respond and the DOI landing page could not be verified; try again later."
                       if errors else "The DOI was not found in Crossref or DataCite, and its landing page did not provide matching evidence.")
            result.update(status=status, message=message)
            return result
        details = _score_title_variants(title, author, year, candidate)
        score = float(details.get("total_score", 0))
        warnings = _metadata_conflicts(candidate, bibliographic_context)
        conflicts = _identity_conflicts(author, year, candidate)
        standard_match = float(details.get("title_score", 0)) >= 80 and score >= 75 and not conflicts
        target_norm = lib.normalize_for_compare(str(title or ""))
        main_norm = lib.normalize_for_compare(candidate.get("main_title", ""))
        partial_title_support = bool(main_norm and len(main_norm.split()) >= 3 and target_norm.startswith(main_norm))
        author_support = details.get("author_match_type") in {"exact", "normalized_exact", "loose"}
        year_support = details.get("year_diff") is None or details.get("year_diff") <= 1 or bool(
            set(map(str, candidate.get("publication_years", []))) & {str(year).strip()})
        partial_metadata_match = partial_title_support and author_support and year_support and not conflicts
        paper_match = standard_match or partial_metadata_match
        container_match = not paper_match and _is_container_match(candidate, bibliographic_context)
        selected_variant = details.get("title_match_variant", "main_title")
        decision_rule = ("combined_title_metadata_match" if standard_match and "combined_title" in selected_variant else
                         "complete_metadata_match" if standard_match else
                         "exact_doi_partial_title_author_year" if partial_metadata_match else
                         "bibliographic_metadata_mismatch")
        status = ("verified_with_warning" if paper_match and warnings else
                  "verified" if paper_match else
                  "container_match" if container_match else "mismatch")
        if paper_match and warnings:
            decision_rule += "_with_secondary_warnings"
        result.update(status=status, verified=paper_match, container_match=container_match,
                      link_valid=True, paper_match=paper_match, score=round(float(score), 2),
                      core_identity_match=paper_match, secondary_metadata_match=not warnings,
                      identifier_valid=True,
                      identifier_match_type="exact_doi", decision_rule=decision_rule,
                      resolved_url=candidate["url"], metadata_title=candidate["title"],
                      metadata_main_title=candidate.get("main_title", candidate["title"]),
                      metadata_subtitle=candidate.get("subtitle", ""),
                      metadata_combined_title=candidate.get("combined_title", candidate["title"]),
                      title_match_variant=selected_variant,
                      title_score=round(float(details.get("title_score", 0)), 2),
                      author_match_type=details.get("author_match_type", "none"),
                      year_difference=details.get("year_diff"),
                      publication_years=candidate.get("publication_years", []),
                      metadata_authors=candidate["authors"], metadata_year=candidate["year"],
                      metadata_source=candidate["source"], metadata_item_type=candidate.get("item_type", ""),
                      metadata_publisher=candidate.get("publisher", ""),
                      metadata_container_title=candidate.get("journal", ""),
                      metadata_doi=normalize_doi(candidate.get("doi")),
                      metadata_conflicts=conflicts, metadata_warnings=warnings,
                      message=((f"The DOI and core paper identity match. Secondary metadata warnings: {', '.join(_CONFLICT_LABELS.get(f, f) for f in warnings)}."
                               if warnings else
                               "The DOI is registered; its combined main title/subtitle, normalized author, and date metadata match this paper."
                               if "combined_title" in selected_variant else
                               "The DOI is registered and its main-title metadata matches this paper.") if paper_match else
                               (f"The DOI resolves to the parent book '{candidate.get('main_title') or candidate.get('title')}', "
                                "which matches this chapter's Publication Title, but the DOI metadata does not "
                                "identify the chapter title or chapter authors.") if container_match else
                               _metadata_mismatch_message("The DOI exists, but the paper metadata did not match.", details, conflicts)))
        if container_match:
            result["decision_rule"] = "parent_book_identifier"
        result["verification_level"] = ("verified_with_secondary_warnings" if paper_match and warnings else
                                        "verified_partial_metadata" if partial_metadata_match and not standard_match else
                                        "container_match" if container_match else
                                        "verified_identifier" if paper_match else "identifier_metadata_mismatch")
        # DOI registration and landing-page health are deliberately separate.
        try:
            landing = _safe_get(session, f"https://doi.org/{bare_doi}", timeout)
            result["access_status"] = _access_status(landing.status_code)
            result["landing_page_reachable"] = 200 <= landing.status_code < 400
            result["link_valid"] = bool(result["landing_page_reachable"])
            result["resolved_url"] = landing.url or result["resolved_url"]
            result["content_type"] = (landing.headers.get("Content-Type") or "").split(";")[0].lower()
            if result["landing_page_reachable"]:
                result["verification_level"] = (("verified_active_with_warnings" if warnings else "verified_active")
                                                if paper_match else result["verification_level"])
            if result["landing_page_reachable"] and "html" in result["content_type"]:
                html_bytes = _read_limited_response(landing, MAX_CONTAINER_PAGE_BYTES)
                encoding = landing.encoding or "utf-8"
                landing_html = html_bytes.decode(encoding, errors="replace")
                evidence = _chapter_page_evidence(title, author, landing_html)
                result.update(
                    landing_page_evidence_checked=True,
                    page_matched_title_variant=evidence.get("matched_title_variant", ""),
                    page_title_evidence_score=evidence.get("title_score", 0.0),
                    page_matched_authors=evidence["matched_authors"])
                if container_match:
                    result.update(
                        container_page_checked=True,
                        container_page_title_found=evidence["title_found"],
                        container_page_matched_authors=evidence["matched_authors"],
                        container_evidence_source="Publisher landing page")
                    if evidence["matched"]:
                        result.update(
                            status="verified_via_container_page", verified=True, paper_match=True,
                            core_identity_match=True,
                            verification_level="verified_via_parent_book_page",
                            identifier_match_type="parent_book_doi",
                            decision_rule="parent_book_page_chapter_title_and_authors",
                            message=(f"The DOI identifies the parent book '{candidate.get('main_title') or candidate.get('title')}'. "
                                     "The publisher landing page also contains the chapter title and matching "
                                     f"chapter author evidence ({', '.join(evidence['matched_authors'])}), so the "
                                     "chapter-to-book relationship is verified; this is still a parent-book DOI, "
                                     "not a chapter-specific DOI."))
                elif not paper_match:
                    evidence, evidence_url, alternate_url = _find_page_or_language_evidence(
                        session, title, author, landing_html, landing.url, timeout)
                    result["alternate_language_url"] = alternate_url
                    result.update(
                        page_matched_title_variant=evidence.get("matched_title_variant", ""),
                        page_title_evidence_score=evidence.get("title_score", 0.0),
                        page_matched_authors=evidence["matched_authors"])
                    if evidence["matched"]:
                        result.update(
                            status="verified_via_landing_page", verified=True, paper_match=True,
                            core_identity_match=True,
                            verification_level="verified_via_publisher_page",
                            decision_rule="publisher_page_title_and_authors",
                            resolved_url=evidence_url,
                            message=("The DOI registry metadata used a different-language title, but the "
                                     "publisher page contains a matching title version and matching author "
                                     f"evidence ({', '.join(evidence['matched_authors']) or 'author not required'})."))
            elif result["landing_page_reachable"] and "pdf" in result["content_type"]:
                result["pdf_detected"] = True
                declared = int(landing.headers.get("Content-Length") or 0)
                if declared > MAX_PDF_BYTES:
                    result["ocr_status"] = "pdf_too_large"
                    result["verification_level"] = "verified_identifier_pdf_too_large" if paper_match else result["verification_level"]
                else:
                    pdf_bytes = _read_limited_response(landing, MAX_PDF_BYTES + 1)
                    if len(pdf_bytes) > MAX_PDF_BYTES:
                        result["ocr_status"] = "pdf_too_large"
                    else:
                        text, ocr_used, extraction_status = _pdf_text(pdf_bytes)
                        result.update(pdf_text_extracted=len(text.strip()) >= 100,
                                      ocr_used=ocr_used, ocr_status=extraction_status)
                        if len(text.strip()) >= 100 and paper_match:
                            result["verification_level"] = "verified_active_pdf_text"
            landing.close()
        except Exception:
            result["access_status"] = "temporarily_unavailable"
            result["landing_page_reachable"] = None
            result["link_valid"] = False
        return result
    if not url:
        result.update(status="invalid", message="No DOI or URL was provided.")
        return result
    try:
        response = _safe_get(session, url, timeout)
        result.update(link_valid=response.status_code < 400, resolved_url=response.url,
                      landing_page_reachable=response.status_code < 400,
                      access_status=_access_status(response.status_code))
        if not result["link_valid"]:
            status = "invalid" if response.status_code in {404, 410} else "unavailable"
            result.update(status=status, message=f"The URL returned HTTP {response.status_code} ({result['access_status']}).")
            response.close()
            return result
        content_type = (response.headers.get("Content-Type") or "").lower()
        result["content_type"] = content_type.split(";")[0]
        if "pdf" in content_type:
            result["pdf_detected"] = True
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_PDF_BYTES:
                response.close()
                result.update(status="unverified", verification_level="pdf_too_large",
                              ocr_status="pdf_too_large",
                              message=f"The PDF is reachable but exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB verification limit.")
                return result
            pdf_bytes = _read_limited_response(response, MAX_PDF_BYTES + 1)
            response.close()
            if len(pdf_bytes) > MAX_PDF_BYTES:
                result.update(status="unverified", verification_level="pdf_too_large",
                              ocr_status="pdf_too_large",
                              message=f"The PDF is reachable but exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB verification limit.")
                return result
            text, ocr_used, extraction_status = _pdf_text(pdf_bytes)
            result.update(pdf_text_extracted=len(text.strip()) >= 100, ocr_used=ocr_used,
                          ocr_status=extraction_status)
            if len(text.strip()) < 100:
                result.update(status="unverified", verification_level="pdf_needs_ocr",
                              message="The PDF is reachable but has no usable text layer, and OCR produced no usable text.")
                return result
            matched, score, author_match, year_match = _match_document_text(title, author, year, text)
            result.update(status="verified" if matched else "mismatch", verified=matched,
                          paper_match=matched, score=score,
                          core_identity_match=matched, secondary_metadata_match=None,
                          verification_level="verified_pdf_text" if matched else "pdf_text_mismatch",
                          metadata_source="PDF OCR" if ocr_used else "PDF text",
                          message=("The PDF is reachable and its extracted text matches the title, author and year."
                                   if matched else "The PDF is reachable, but its extracted text does not match the title, author and year."))
            return result
        if "html" not in content_type:
            result.update(status="unverified", verification_level="unsupported_content",
                          message="The URL is reachable, but its content type cannot provide bibliographic evidence.")
            response.close()
            return result
        html_bytes = _read_limited_response(response, MAX_METADATA_BYTES)
        encoding = response.encoding or "utf-8"
        html = html_bytes.decode(encoding, errors="replace")
        evidence, evidence_url, alternate_url = _find_page_or_language_evidence(
            session, title, author, html, response.url, timeout)
        result.update(
            landing_page_evidence_checked=True,
            alternate_language_url=alternate_url,
            page_matched_title_variant=evidence.get("matched_title_variant", ""),
            page_title_evidence_score=evidence.get("title_score", 0.0),
            page_matched_authors=evidence["matched_authors"])
        candidate = _metadata_candidate(html, response.url)
        response.close()
        if evidence["matched"]:
            result.update(
                status="verified_via_landing_page", verified=True, paper_match=True,
                score=100.0, core_identity_match=True, identifier_valid=bool(candidate.get("doi")),
                identifier_match_type="page_metadata_or_visible_text",
                verification_level="verified_via_publisher_page",
                decision_rule="publisher_page_title_and_authors",
                resolved_url=evidence_url, metadata_title=candidate.get("title") or title,
                metadata_authors=candidate.get("authors") or lib.clean_authors(str(author or "")),
                metadata_year=candidate.get("year"),
                metadata_source=(candidate.get("source") if candidate.get("title") else "Publisher page"),
                metadata_item_type=candidate.get("item_type", ""),
                metadata_publisher=candidate.get("publisher", ""),
                metadata_container_title=candidate.get("journal", ""),
                metadata_doi=normalize_doi(candidate.get("doi")),
                doi=candidate.get("doi") or normalize_doi(doi),
                message=("The publisher page contains a matching title version and matching author "
                         f"evidence ({', '.join(evidence['matched_authors']) or 'author not required'})."))
            return result
        if not candidate["title"]:
            result.update(status="unverified", verification_level="html_metadata_missing",
                          message="The URL is reachable, but it has no usable citation metadata.")
            return result
        details = _score_title_variants(title, author, year, candidate)
        score = float(details.get("total_score", 0))
        warnings = _metadata_conflicts(candidate, bibliographic_context)
        conflicts = _identity_conflicts(author, year, candidate)
        paper_match = float(details.get("title_score", 0)) >= 80 and score >= 75 and not conflicts
        status = "verified_with_warning" if paper_match and warnings else ("verified" if paper_match else "mismatch")
        result.update(status=status, verified=paper_match,
                      paper_match=paper_match, score=round(score, 2), metadata_title=candidate["title"],
                      core_identity_match=paper_match, secondary_metadata_match=not warnings,
                      metadata_authors=candidate["authors"], metadata_year=candidate["year"],
                      metadata_source=candidate["source"], doi=candidate["doi"],
                      metadata_item_type=candidate.get("item_type", ""),
                      metadata_publisher=candidate.get("publisher", ""),
                      metadata_container_title=candidate.get("journal", ""),
                      metadata_doi=normalize_doi(candidate.get("doi")),
                      metadata_conflicts=conflicts, metadata_warnings=warnings,
                      message=((f"The URL and core paper identity match. Secondary metadata warnings: {', '.join(_CONFLICT_LABELS.get(f, f) for f in warnings)}."
                               if warnings else "The URL is reachable and its citation metadata matches this paper.") if paper_match else
                                _metadata_mismatch_message(
                                    "The URL is reachable, but its citation metadata did not match.", details, conflicts)))
        result["verification_level"] = ("verified_active_with_warnings" if paper_match and warnings else
                                        "verified_active" if paper_match else "html_metadata_mismatch")
    except ValueError as exc:
        result.update(status="invalid", message=str(exc))
    except requests.RequestException:
        result.update(status="unavailable", message="The URL could not be reached.")
    return result


def _canonical_url(value):
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    path = unquote(parsed.path or "/").rstrip("/") or "/"
    return f"{host}{path}" if host else ""


def _verify_with_enabled_sources(title, author, year, doi, url, enabled_sources,
                                 email="", s2_api_key=None, core_api_key=None,
                                 required_confirmations=1, bibliographic_context=None):
    """Cross-check selected sources in priority order, stopping on success."""
    target_doi = normalize_doi(doi) or _doi_from_url(url)
    target_url = _canonical_url(url)
    author_lastnames = lib.clean_authors(str(author or ""))
    checked, failures, evidence, verified_evidence = [], [], [], []
    # Use the same quality/coverage priority as lookup.  Stop as soon as one
    # source independently returns the same identifier with matching metadata;
    # later APIs are not called for that record.
    enabled_set = set(enabled_sources or [])
    ordered_sources = [source_id for source_id in SOURCE_ORDER if source_id in enabled_set]
    ordered_sources += [source_id for source_id in (enabled_sources or []) if source_id not in ordered_sources]
    for source_id in ordered_sources:
        checked.append(source_id)
        try:
            candidates, error = _call_source(source_id, lib.clean_title(str(title or "")),
                                             str(author or ""), email, s2_api_key, core_api_key)
        except Exception as exc:
            candidates, error = [], str(exc)
        if error:
            failures.append({"source": source_id, "error": error})
        for candidate in candidates:
            candidate_doi = normalize_doi(candidate.get("doi")) or _doi_from_url(candidate.get("url"))
            same_identifier = bool(target_doi and candidate_doi and target_doi.lower() == candidate_doi.lower())
            if not same_identifier and target_url:
                same_identifier = _canonical_url(candidate.get("url")) == target_url
            if not same_identifier:
                continue
            details = lib.score_candidate_detailed(lib.clean_title(str(title or "")), author_lastnames, year, candidate)
            warnings = _metadata_conflicts(candidate, bibliographic_context)
            conflicts = _identity_conflicts(author, year, candidate)
            if conflicts:
                details["total_score"] = min(float(details.get("total_score", 0)), 74)
            item = (float(details.get("total_score", 0)), float(details.get("title_score", 0)),
                    source_id, candidate, details, conflicts, warnings)
            evidence.append(item)
            if item[1] >= 80 and item[0] >= 75:
                verified_evidence.append(item)
                break
        else:
            continue
        if len({_source_family(item[2]) for item in verified_evidence}) >= required_confirmations:
            evidence = verified_evidence
            break
    if not evidence:
        return None, checked, failures
    evidence.sort(key=lambda item: item[0], reverse=True)
    score, title_score, best_source, candidate, best_details, best_conflicts, best_warnings = evidence[0]
    supporting_sources = sorted({item[2] for item in evidence})
    confirmations = len({_source_family(item[2]) for item in verified_evidence})
    paper_match = title_score >= 80 and score >= 75 and confirmations >= required_confirmations
    insufficient = title_score >= 80 and score >= 75 and not paper_match
    status = ("verified_with_warning" if paper_match and best_warnings else "verified") if paper_match else (
        "unverified" if insufficient else "mismatch")
    result = {"status": status, "verified": paper_match,
              "link_valid": True, "paper_match": paper_match, "score": round(score, 2),
              "core_identity_match": paper_match, "secondary_metadata_match": not best_warnings,
              "identifier_valid": True, "landing_page_reachable": None,
              "access_status": "not_checked", "content_type": "",
              "pdf_detected": False, "pdf_text_extracted": False,
              "ocr_used": False, "ocr_status": "not_needed",
              "verification_level": ("verified_source_with_warnings" if paper_match and best_warnings else
                                     "verified_source_identifier" if paper_match else
                                     "insufficient_independent_evidence" if insufficient else "source_metadata_mismatch"),
              "doi": normalize_doi(candidate.get("doi")) or target_doi,
              "resolved_url": candidate.get("url") or url, "metadata_title": candidate.get("title", ""),
              "metadata_authors": candidate.get("authors", []), "metadata_year": candidate.get("year"),
              "metadata_source": source_label(best_source),
              "metadata_item_type": candidate.get("item_type", ""),
              "metadata_publisher": candidate.get("publisher", ""),
              "metadata_container_title": candidate.get("journal", ""),
              "metadata_doi": normalize_doi(candidate.get("doi")),
              "verification_sources": supporting_sources,
              "metadata_conflicts": best_conflicts, "metadata_warnings": best_warnings,
              "sources_checked": checked, "source_failures": failures,
              "message": ((f"The same DOI/URL and core paper identity were confirmed by {', '.join(source_label(s) for s in supporting_sources)}. "
                           f"Secondary metadata warnings: {', '.join(_CONFLICT_LABELS.get(f, f) for f in best_warnings)}."
                           if best_warnings else
                           f"The same DOI/URL was confirmed by {', '.join(source_label(s) for s in supporting_sources)} and its metadata matches this paper.")
                          if paper_match else
                          f"Matching metadata was found, but high-confidence mode requires {required_confirmations} independent source confirmations."
                          if insufficient else
                           _metadata_mismatch_message(
                               f"The same DOI/URL was found by {', '.join(source_label(s) for s in supporting_sources)}, but the paper metadata did not match.",
                               best_details, best_conflicts))}
    return result, checked, failures


def verify_reference(title, author="", year="", doi="", url="", session=None, timeout=VERIFY_TIMEOUT,
                     enabled_sources=None, email="", s2_api_key=None, core_api_key=None,
                     mode="fast", item_type="", journal="", volume="", issue="", pages="",
                     isbn="", issn="", publisher="", container_title_hint=""):
    """Verify via DOI registries/web metadata, then selected sources as cross-checks."""
    context = {"item_type": item_type, "journal": journal, "volume": volume, "issue": issue,
               "pages": pages, "isbn": isbn, "issn": issn, "publisher": publisher,
               "container_title_hint": container_title_hint}
    primary = _verify_primary_reference(title, author, year, doi, url, session=session,
                                        timeout=timeout, bibliographic_context=context)
    primary["verified_at"] = datetime.now(timezone.utc).isoformat()
    primary["verification_mode"] = mode
    primary["logic_version"] = VERIFICATION_LOGIC_VERSION
    primary.setdefault("verification_sources", [primary["metadata_source"]] if primary.get("metadata_source") else [])
    primary.setdefault("sources_checked", [])
    primary.setdefault("source_failures", [])
    # Direct authoritative metadata already proves the record, so do not
    # spend additional source quota merely to repeat the same conclusion.
    if primary["verified"] and mode != "high_confidence":
        primary["sources_checked"] = list(primary["verification_sources"])
        return primary
    # A malformed DOI or contradictory DOI URL is definitive and should not
    # be rescued by a title search in another database.
    definitive_input_error = primary["status"] in {"invalid", "mismatch"} and (
        "format" in primary.get("message", "").lower() or "different records" in primary.get("message", "").lower())
    if definitive_input_error:
        return primary
    if not enabled_sources:
        if mode == "high_confidence" and primary["verified"]:
            primary.update(status="unverified", verified=False,
                           verification_level="insufficient_independent_evidence",
                           message="Identifier metadata matches, but no independent second source was enabled.")
        return primary
    primary_source_id = "crossref" if "Crossref" in primary.get("metadata_source", "") else (
        "datacite" if "DataCite" in primary.get("metadata_source", "") else None)
    secondary_sources = [s for s in (enabled_sources or []) if s != primary_source_id]
    required = 1 if primary["verified"] or mode != "high_confidence" else 2
    source_result, checked, failures = _verify_with_enabled_sources(
        title, author, year, primary.get("doi") or doi, url, secondary_sources, email, s2_api_key, core_api_key,
        required_confirmations=required, bibliographic_context=context)
    primary["sources_checked"], primary["source_failures"] = checked, failures
    if source_result:
        source_result["verified_at"] = primary["verified_at"]
        source_result["verification_mode"] = mode
        source_result["logic_version"] = VERIFICATION_LOGIC_VERSION
    if source_result and source_result["verified"] and primary["verified"]:
        source_result["verification_sources"] = sorted(set(
            primary.get("verification_sources", []) + source_result["verification_sources"]))
        source_result["verification_level"] = "verified_high_confidence" if mode == "high_confidence" else source_result["verification_level"]
        return source_result
    if source_result and source_result["verified"]:
        return source_result
    if source_result and not primary["verified"] and primary.get("status") != "container_match":
        return source_result
    if source_result:
        primary["verification_sources"] = sorted(set(primary["verification_sources"] + source_result["verification_sources"]))
    if mode == "high_confidence" and primary["verified"]:
        primary.update(status="unverified", verified=False,
                       verification_level="insufficient_independent_evidence",
                       message="Identifier metadata matches, but no independent second source confirmed it.")
    return primary


# ---------------------------------------------------------------------------
# Selectable sources
# ---------------------------------------------------------------------------
# `needs` lists which optional extra inputs (see SourcesPage in the GUI)
# this source can use. "email" and "s2_api_key" are optional everywhere
# they're accepted (the source still works without them, just with a
# stricter rate limit or a less polite identification). "core_api_key" is
# the one exception: CORE has no keyless tier at all, so it contributes
# nothing unless a key is supplied.
SOURCES = [
    {"id": "crossref", "label": "Crossref", "default_on": True, "needs": ["email"],
     "note": "General scholarly literature (journal articles, books, chapters). No key needed; "
             "an email is optional and only makes Crossref's rate limit more generous."},
    {"id": "openalex", "label": "OpenAlex", "default_on": True, "needs": [],
     "note": "Broad coverage including theses and working papers. No key needed."},
    {"id": "semantic_scholar", "label": "Semantic Scholar", "default_on": True, "needs": ["s2_api_key"],
     "note": "~200M papers across all fields. No key needed; an optional free API key raises the "
             "rate limit from ~100 requests/5min to 1/sec."},
    {"id": "datacite", "label": "DataCite", "default_on": True, "needs": [],
     "note": "DOIs for theses, datasets, and working papers that Crossref doesn't index. No key needed."},
    {"id": "openaire", "label": "OpenAIRE", "default_on": False, "needs": [],
     "note": "European research output (funders/institutions). No key needed."},
    {"id": "gesis", "label": "GESIS", "default_on": False, "needs": [],
     "note": "German social-science data archive (ISSP, SSOAR). Best for social-science / German "
             "survey literature. No key needed."},
    {"id": "core", "label": "CORE", "default_on": False, "needs": ["core_api_key"],
     "note": "300M+ records from institutional repositories worldwide. REQUIRES a free API key "
             "(register at core.ac.uk/services/api) - without one, CORE contributes nothing."},
    {"id": "dnb", "label": "DNB (German National Library)", "default_on": False, "needs": [],
     "note": "Germany's national bibliography. Best for German-language titles. No key needed."},
    {"id": "hal", "label": "HAL (France)", "default_on": False, "needs": [],
     "note": "France's national open-archive repository. Best for French-language titles. No key needed."},
    {"id": "cinii", "label": "CiNii (Japan)", "default_on": False, "needs": [],
     "note": "Japan's national scholarly search. Best for Japanese-language titles. No key needed."},
    {"id": "arxiv", "label": "arXiv", "default_on": False, "needs": [],
     "note": "Physics, math, CS, and quantitative finance/economics preprints. No key needed."},
    {"id": "pubmed", "label": "PubMed", "default_on": False, "needs": ["email"],
     "note": "Biomedical / life-science / public-health literature. No key needed; an email is optional."},
]

DEFAULT_ENABLED_SOURCES = [s["id"] for s in SOURCES if s["default_on"]]
SOURCE_LABELS = {s["id"]: s["label"] for s in SOURCES}


def source_label(source_id):
    """Human-readable name for a source id, e.g. "semantic_scholar" ->
    "Semantic Scholar" - used wherever a failed/used source is shown to
    the user instead of the raw internal id."""
    return SOURCE_LABELS.get(source_id, source_id)

# Query priority order for the early-exit batch/single lookup below -
# mirrors the generic-chain priority in doi_lookup_lib.process_row()
# (broadest/most-reliable sources first), minus the ISSP-specific
# language-first routing, since this app lets the user pick sources
# directly instead of auto-detecting a title's language.
SOURCE_ORDER = [
    "crossref", "gesis", "openalex", "semantic_scholar", "core", "openaire",
    "datacite", "dnb", "hal", "cinii", "arxiv", "pubmed",
]

REGIONAL_SOURCE_BY_LANGUAGE = {
    "de": "dnb",
    "fr": "hal",
    "ja": "cinii",
}
REGIONAL_SOURCE_IDS = set(REGIONAL_SOURCE_BY_LANGUAGE.values())
DOI_URL_LOOKUP_CACHE_VERSION = "language-routed-v2"
ABSTRACT_CACHE_VERSION = "source-first-v1"


def ordered_lookup_sources(title, enabled_sources):
    """Return the DOI/URL batch route for one title.

    Regional catalogues are mutually exclusive and language-routed, matching
    the original ``find_doi_url.py`` behaviour: German titles may try DNB,
    French titles HAL, and Japanese titles CiNii. Other titles do not spend
    requests on those catalogues. arXiv and PubMed remain the final fallbacks.

    This helper is used only by DOI/URL lookup. Abstract/full-text retrieval
    has its own network and safety controls and is intentionally unaffected.
    """
    enabled_set = set(enabled_sources or [])
    detected_language = lib.detect_title_language(lib.clean_title(str(title or "")))
    routed_regional = REGIONAL_SOURCE_BY_LANGUAGE.get(detected_language)

    ordered = []
    if routed_regional and routed_regional in enabled_set:
        ordered.append(routed_regional)
    ordered.extend(
        source_id for source_id in SOURCE_ORDER
        if source_id in enabled_set and source_id not in REGIONAL_SOURCE_IDS
    )
    return ordered, detected_language


def _call_source(source_id, title, author, email="", s2_api_key=None, core_api_key=None):
    """Returns (candidates, error) - every lib.query_*() does, see
    doi_lookup_lib.py's module docstring."""
    if source_id == "crossref":
        return lib.query_crossref(title, author, email)
    if source_id == "openalex":
        return lib.query_openalex(title, author)
    if source_id == "semantic_scholar":
        return lib.query_semantic_scholar(title, author, api_key=s2_api_key)
    if source_id == "datacite":
        return lib.query_datacite(title, author)
    if source_id == "gesis":
        return lib.query_gesis(title, author)
    if source_id == "arxiv":
        return lib.query_arxiv(title, author)
    if source_id == "pubmed":
        return lib.query_pubmed(title, author, email)
    if source_id == "core":
        return lib.query_core(title, author, api_key=core_api_key)
    if source_id == "openaire":
        return lib.query_openaire(title, author)
    if source_id == "dnb":
        return lib.query_dnb(title, author)
    if source_id == "hal":
        return lib.query_hal(title, author)
    if source_id == "cinii":
        return lib.query_cinii(title, author)
    return [], None


# ---------------------------------------------------------------------------
# Single lookup (queries every enabled source, returns everything ranked)
# ---------------------------------------------------------------------------

def run_search(title, author, year, enabled_sources, email="", s2_api_key=None, core_api_key=None):
    """Returns (results, failed_sources). `failed_sources` is a list of
    {"source": id, "error": msg} for any enabled source that errored out
    instead of genuinely responding with zero candidates - an empty
    `results` list paired with a non-empty `failed_sources` means "we
    don't actually know if there's a match," not "there is no match"."""
    query_title = lib.clean_title(title)
    author_lastnames = [author] if author else []

    all_candidates = []
    failed_sources = []
    for source_id in enabled_sources:
        candidates, error = _call_source(source_id, query_title, author, email, s2_api_key, core_api_key)
        if error:
            failed_sources.append({"source": source_id, "error": error})
        all_candidates += candidates

    results = []
    for c in all_candidates:
        score, _ = lib.score_candidate(query_title, author_lastnames, year, c)
        results.append({**c, "score": score})
    results.sort(key=lambda r: -r["score"])
    return results, failed_sources


def _candidate_abstract_text(value):
    """Return a usable abstract string from heterogeneous source payloads."""
    if isinstance(value, dict):
        preferred = value.get("description") or value.get("text") or value.get("value")
        value = preferred if preferred is not None else " ".join(
            str(item) for item in value.values() if item)
    elif isinstance(value, list):
        parts = []
        for item in value:
            text = _candidate_abstract_text(item)
            if text:
                parts.append(text)
        value = " ".join(parts)
    raw = str(value or "").strip()
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(raw))).strip()
    return raw if len(plain) >= 40 else ""


def lookup_abstract_from_sources(title, author, year, doi, url, enabled_sources,
                                 email="", s2_api_key=None, core_api_key=None):
    """Find a strongly matched abstract in enabled metadata sources.

    Sources are checked in the same user-selected order as DOI/URL lookup.
    A result is accepted only for an exact DOI match or an auto-accept
    bibliographic score, preventing a merely similar title from supplying
    the wrong abstract.
    """
    query_title = lib.clean_title(str(title or ""))
    if not query_title or not enabled_sources:
        return {"abstract": "", "failed_sources": []}

    target_authors = lib.clean_authors(str(author or ""))
    target_doi = normalize_doi(doi) or _doi_from_url(url)
    ordered_sources, _detected_language = ordered_lookup_sources(query_title, enabled_sources)
    failed_sources = []

    for source_id in ordered_sources:
        candidates, error = _call_source(
            source_id, query_title, lib.clean_author(str(author or "")),
            email, s2_api_key, core_api_key)
        if error:
            failed_sources.append({"source": source_id, "error": error})
            continue

        accepted = []
        for candidate in candidates:
            abstract = _candidate_abstract_text(candidate.get("abstract"))
            if not abstract:
                continue
            details = lib.score_candidate_detailed(query_title, target_authors, year, candidate)
            candidate_doi = normalize_doi(candidate.get("doi")) or _doi_from_url(candidate.get("url"))
            doi_match = bool(target_doi and candidate_doi and target_doi == candidate_doi)
            doi_conflict = bool(target_doi and candidate_doi and target_doi != candidate_doi)
            if doi_conflict or not (doi_match or details["total_score"] >= lib.AUTO_ACCEPT_THRESHOLD):
                continue
            accepted.append((doi_match, details["total_score"], abstract, candidate, details))

        if not accepted:
            continue

        doi_match, score, abstract, candidate, details = max(
            accepted, key=lambda item: (item[0], item[1]))
        candidate_year = candidate.get("year")
        year_match = None
        if year and candidate_year:
            try:
                year_match = abs(int(year) - int(candidate_year)) <= 1
            except (TypeError, ValueError):
                year_match = None
        return {
            "abstract": abstract,
            "source": source_label(source_id) + " API",
            "url": candidate.get("url") or "",
            "verification_status": "matched",
            "review_tag": "abstract_match_confirmed",
            "verification_message": (
                f"Abstract obtained from {source_label(source_id)}; "
                f"bibliographic match score {score:.1f}/100" +
                (" with exact DOI match." if doi_match else ".")),
            "title_score": details.get("title_score", 0.0),
            "author_match": (details.get("author_match_type") != "none") if target_authors else None,
            "year_match": year_match,
            "doi_match": doi_match if target_doi and candidate_doi else None,
            "metadata_title": candidate.get("title") or "",
            "metadata_authors": candidate.get("authors") or [],
            "metadata_year": candidate_year or "",
            "metadata_dois": [candidate_doi] if candidate_doi else [],
            "failed_sources": failed_sources,
        }

    return {"abstract": "", "failed_sources": failed_sources}


# ---------------------------------------------------------------------------
# Batch lookup (queries enabled sources in priority order, stops early once
# an auto-accept-quality match is found - same early-exit philosophy as
# doi_lookup_lib.process_row(), scoped to just the sources the user picked)
# ---------------------------------------------------------------------------

def lookup_one(title, author_field, year, enabled_sources, email="", s2_api_key=None, core_api_key=None):
    """Returns a single result dict, always including a "failed_sources"
    list ({"source": id, "error": msg} for any source that errored out
    instead of genuinely returning zero candidates). When nothing at all
    was found AND at least one enabled source failed, status is
    "incomplete" rather than "not_found" - a clean negative can't be
    claimed when part of the search never actually completed."""
    query_title = lib.clean_title(str(title or ""))
    if not query_title.strip():
        return {"status": "no_title", "doi": "", "url": "", "matched_title": "", "source": "", "score": 0, "failed_sources": []}

    author = lib.clean_author(str(author_field or ""))
    author_lastnames = lib.clean_authors(str(author_field or ""))

    ordered_sources, detected_language = ordered_lookup_sources(query_title, enabled_sources)
    all_candidates = []
    failed_sources = []
    for source_id in ordered_sources:
        candidates, error = _call_source(source_id, query_title, author, email, s2_api_key, core_api_key)
        if error:
            failed_sources.append({"source": source_id, "error": error})
        all_candidates += candidates
        if all_candidates:
            result = lib.build_result(query_title, author_lastnames, year, all_candidates, failed_sources)
            if result["status"] == "auto_accepted":
                return {**result, "score": result["match_score"],
                        "detected_lang": detected_language}

    if not all_candidates:
        return {
            "status": "incomplete" if failed_sources else "not_found",
            "doi": "", "url": "", "matched_title": "", "source": "", "score": 0,
            "failed_sources": failed_sources,
        }

    result = lib.build_result(query_title, author_lastnames, year, all_candidates, failed_sources)
    return {**result, "score": result["match_score"],
            "detected_lang": detected_language}


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def cache_key(title, author_field, year, enabled_sources):
    sources_part = ",".join(sorted(enabled_sources))
    return (f"{DOI_URL_LOOKUP_CACHE_VERSION}|||{lib.clean_title(str(title or ''))}|||"
            f"{lib.clean_author(str(author_field or ''))}|||{year}|||{sources_part}")


def abstract_cache_key(title, author, year, doi, url, enabled_sources):
    """Build a stable key without storing contact details or API keys."""
    payload = {
        "v": ABSTRACT_CACHE_VERSION,
        "title": lib.normalize_for_compare(str(title or "")),
        "author": lib.normalize_for_compare(str(author or "")),
        "year": str(year or ""),
        "doi": normalize_doi(doi),
        "url": _canonical_url(url),
        "sources": list(enabled_sources or []),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def load_abstract_cache():
    with _ABSTRACT_CACHE_LOCK:
        try:
            with open(ABSTRACT_CACHE_FILE, "r", encoding="utf-8") as stream:
                cache = {}
                for line in stream:
                    try:
                        entry = json.loads(line)
                        cache[entry["key"]] = {
                            "saved_at": entry["saved_at"], "payload": entry["payload"],
                        }
                    except (KeyError, TypeError, ValueError):
                        # A final partial line can remain after a sudden shutdown.
                        continue
                return cache
        except OSError:
            return {}


def cached_abstract(cache, key):
    """Return a cached record; retry failures sooner than definitive misses."""
    entry = cache.get(key)
    if not entry:
        return None
    payload = entry.get("payload", {})
    age = time.time() - float(entry.get("saved_at", 0))
    result = payload.get("result") or {}
    if result.get("abstract"):
        ttl = 365 * 86400
    elif payload.get("status") in {"fetch_failed", "parse_failed"}:
        ttl = 24 * 3600
    else:
        ttl = 7 * 86400
    return payload if age <= ttl else None


def save_abstract_cache(cache):
    with _ABSTRACT_CACHE_LOCK:
        temp_path = ABSTRACT_CACHE_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as stream:
            for key, entry in cache.items():
                stream.write(json.dumps({"key": key, **entry}, ensure_ascii=False) + "\n")
        os.replace(temp_path, ABSTRACT_CACHE_FILE)


def append_abstract_cache_entry(key, payload, saved_at=None):
    """Checkpoint one completed record without rewriting the full cache."""
    entry = {"key": key, "saved_at": saved_at or time.time(), "payload": payload}
    with _ABSTRACT_CACHE_LOCK:
        with open(ABSTRACT_CACHE_FILE, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()


def verification_cache_key(title, author, year, doi, url, enabled_sources, mode, context=None):
    payload = {"v": VERIFICATION_LOGIC_VERSION, "title": lib.normalize_for_compare(str(title or "")),
               "author": lib.normalize_for_compare(str(author or "")), "year": str(year or ""),
               "doi": normalize_doi(doi), "url": _canonical_url(url),
               "sources": sorted(enabled_sources or []), "mode": mode, "context": context or {}}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def load_verification_cache():
    with _VERIFICATION_CACHE_LOCK:
        try:
            with open(VERIFICATION_CACHE_FILE, "r", encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, ValueError):
            return {}


def cached_verification(cache, key):
    entry = cache.get(key)
    if not entry:
        return None
    result = entry.get("result", {})
    age = time.time() - float(entry.get("saved_at", 0))
    ttl = 3600 if result.get("status") == "unavailable" else (
        7 * 86400 if result.get("status") == "unverified" else 30 * 86400)
    return result if age <= ttl else None


def save_verification_cache(cache):
    with _VERIFICATION_CACHE_LOCK:
        temp_path = VERIFICATION_CACHE_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as stream:
            json.dump(cache, stream, ensure_ascii=False, indent=2)
        os.replace(temp_path, VERIFICATION_CACHE_FILE)


# ---------------------------------------------------------------------------
# Persisted app settings: which sources are enabled, plus the optional
# email/API keys - saved locally next to this script so the user doesn't
# have to re-pick their sources every time they open the app.
# ---------------------------------------------------------------------------

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# File I/O: CSV / Excel / JSON
# ---------------------------------------------------------------------------

# Both English and Chinese column-name variants are recognized, since an
# imported file might come from either an English or Chinese-language
# reference manager.
TITLE_ALIASES = ["title", "标题", "题目", "文献标题", "name"]
AUTHOR_ALIASES = ["author", "authors", "作者", "creators"]
YEAR_ALIASES = ["publication year", "year", "年份", "出版年份", "年", "date"]
DOI_ALIASES = ["doi", "digital object identifier"]
URL_ALIASES = ["url", "link", "uri", "网址", "链接"]
PUBLISHER_ALIASES = ["publisher", "出版社", "publishing house"]
ITEM_TYPE_ALIASES = ["item type", "type", "document type", "resource type", "文献类型"]
JOURNAL_ALIASES = ["publication title", "journal", "container title", "期刊", "刊名"]
VOLUME_ALIASES = ["volume", "卷"]
ISSUE_ALIASES = ["issue", "number", "期"]
PAGES_ALIASES = ["pages", "page", "页码"]
ISBN_ALIASES = ["isbn"]
ISSN_ALIASES = ["issn"]
TAG_ALIASES = ["tags", "tag", "keywords", "keyword", "标签", "标记"]


def split_tags(value, delimiter="Auto"):
    """Split a tag cell without treating punctuation inside a tag as a separator."""
    text = str(value or "").strip()
    if not text:
        return [], "; "
    choices = {"Semicolon (;)": ";", "New line": "\n", "Comma (, )": ","}
    separator = choices.get(delimiter)
    if separator is None:
        separator = ";" if ";" in text else ("\n" if "\n" in text or "\r" in text else ",")
    parts = re.split(r"\r?\n" if separator == "\n" else re.escape(separator), text)
    output_separator = "\n" if separator == "\n" else f"{separator} "
    return [part.strip() for part in parts if part.strip()], output_separator


def is_uppercase_tag(tag):
    """A kept tag must contain a letter and every cased letter must be uppercase."""
    text = str(tag or "").strip()
    return any(char.isalpha() for char in text) and text.isupper()


def clean_uppercase_tags(value, delimiter="Auto"):
    tags, output_separator = split_tags(value, delimiter)
    kept = [tag for tag in tags if is_uppercase_tag(tag)]
    removed = [tag for tag in tags if not is_uppercase_tag(tag)]
    return output_separator.join(kept), kept, removed


def guess_column(columns, aliases):
    lower_map = {str(c).strip().lower(): c for c in columns}
    for alias in aliases:
        if alias in lower_map:
            return lower_map[alias]
    for c in columns:
        cl = str(c).strip().lower()
        if any(alias in cl for alias in aliases):
            return c
    return None


def _text_encoding_candidates(path):
    """Return safe decoding candidates, preferring an encoding indicated by a BOM."""
    with open(path, "rb") as stream:
        prefix = stream.read(4)
    if prefix.startswith(b"\xef\xbb\xbf"):
        return ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
    if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
        return ["utf-16", "utf-8-sig", "cp1252", "latin-1"]
    return ["utf-8-sig", "cp1252", "latin-1"]


def _read_delimited_file(path):
    """Read an unknown CSV/text export without silently dropping characters."""
    failures = []
    for encoding in _text_encoding_candidates(path):
        try:
            df = pd.read_csv(path, encoding=encoding, sep=None, engine="python")
            df.attrs["source_encoding"] = encoding
            return df
        except (UnicodeError, pd.errors.ParserError) as exc:
            failures.append(f"{encoding}: {exc}")
    raise ValueError("Unable to decode or parse this delimited file. Tried: " + "; ".join(failures))


def _read_json_file(path):
    failures = []
    for encoding in _text_encoding_candidates(path):
        try:
            with open(path, "r", encoding=encoding) as stream:
                return json.load(stream), encoding
        except UnicodeError as exc:
            failures.append(f"{encoding}: {exc}")
    raise ValueError("Unable to decode this JSON file. Tried: " + "; ".join(failures))


def read_records_file(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        df = _read_delimited_file(path)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    elif ext == ".json":
        data, source_encoding = _read_json_file(path)
        if isinstance(data, list) and data and isinstance(data[0], dict) and (
                "issued" in data[0] or isinstance(data[0].get("author"), list)):
            data = [_from_csl_item(item) for item in data]
        if isinstance(data, dict):
            # Common shape {"records": [...]}: take the first list-valued field
            list_values = [v for v in data.values() if isinstance(v, list)]
            data = list_values[0] if list_values else [data]
        df = pd.DataFrame(data)
        df.attrs["source_encoding"] = source_encoding
    elif ext == ".ris":
        df = pd.DataFrame(_read_ris(path))
    elif ext in (".bib", ".bibtex"):
        df = pd.DataFrame(_read_bibtex(path))
    else:
        raise ValueError(f"Unsupported file format: {ext}")
    df.columns = [str(c) for c in df.columns]
    return df


OUTPUT_FORMATS = {
    "CSV table (.csv)": ("csv", ".csv"),
    "Excel (.xlsx)": ("excel", ".xlsx"),
    "CSL JSON (.json)": ("csl_json", ".json"),
    "RIS (.ris)": ("ris", ".ris"),
    "BibTeX (.bib)": ("bibtex", ".bib"),
}


def preferred_output_format_label(path):
    """Return the closest supported export format for an imported file."""
    extension = os.path.splitext(str(path or ""))[1].lower()
    return {
        ".csv": "CSV table (.csv)",
        ".xlsx": "Excel (.xlsx)",
        ".xls": "Excel (.xlsx)",
        ".json": "CSL JSON (.json)",
        ".ris": "RIS (.ris)",
        ".bib": "BibTeX (.bib)",
        ".bibtex": "BibTeX (.bib)",
    }.get(extension, "CSV table (.csv)")


def write_records_file(df: pd.DataFrame, path: str, fmt: str, portable_columns=None):
    if fmt == "csv":
        export_df = df.copy()
        # Zotero's CSV importer recognizes "Url", whereas the app's result
        # preview/export column is named "Link". Put the verified/found link
        # into the canonical Zotero column while retaining audit columns.
        if "Link" in export_df.columns:
            export_df["Url"] = export_df["Link"]
        export_df.to_csv(path, index=False, encoding="utf-8-sig")
    elif fmt == "excel":
        df.to_excel(path, index=False)
    elif fmt == "csl_json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump([_to_csl_item(row, i) for i, (_, row) in enumerate(df.iterrows())],
                      f, ensure_ascii=False, indent=2)
    elif fmt == "ris":
        _write_ris(df, path, portable_columns=portable_columns)
    elif fmt == "bibtex":
        _write_bibtex(df, path, portable_columns=portable_columns)
    else:
        raise ValueError(f"Unknown output format: {fmt}")


def _value(row, *names):
    columns = {str(k).strip().lower(): k for k in row.index}
    for name in names:
        key = columns.get(name.lower())
        if key is not None:
            value = row.get(key)
            if value is not None and not pd.isna(value) and str(value).strip():
                return str(value).strip()
    return ""


def _split_authors(value):
    return [a.strip() for a in re.split(r"\s*(?:;|\band\b)\s*", str(value or "")) if a.strip()]


def _verification_note(row):
    status = _value(row, "Verification Status")
    if not status:
        return ""
    score = _value(row, "Verification Score")
    message = _value(row, "Verification Message")
    return f"Literature Lookup verification: {status}" + (f"; score {score}" if score else "") + (f"; {message}" if message else "")


def _abstract_verification_note(row):
    review_tag = _value(row, "Abstract Review Tag")
    if not review_tag:
        return ""
    message = _value(row, "Abstract Verification Message")
    return f"Literature Lookup abstract check: {review_tag}" + (f"; {message}" if message else "")


def _portable_columns_note(row, columns):
    """Represent custom audit columns in Note fields supported by RIS/BibTeX."""
    lines = []
    for column in columns or []:
        value = _value(row, str(column))
        if value:
            encoded = json.dumps(value, ensure_ascii=False)
            lines.append(f"Literature Lookup field: {column} = {encoded}")
    return "\n".join(lines)


_PORTABLE_FIELD_PREFIX = "Literature Lookup field: "


def _restore_portable_columns(record):
    """Restore custom columns previously carried through a RIS/BibTeX Note."""
    notes = str(record.get("Notes") or "")
    if _PORTABLE_FIELD_PREFIX not in notes:
        return record
    ordinary_notes = []
    for line in notes.splitlines():
        if not line.startswith(_PORTABLE_FIELD_PREFIX):
            ordinary_notes.append(line)
            continue
        assignment = line[len(_PORTABLE_FIELD_PREFIX):]
        if " = " not in assignment:
            ordinary_notes.append(line)
            continue
        column, encoded = assignment.split(" = ", 1)
        column = column.strip()
        if not column:
            ordinary_notes.append(line)
            continue
        try:
            value = json.loads(encoded)
        except (TypeError, ValueError):
            # BibTeX escaping adds one layer around JSON backslashes/braces.
            repaired = encoded.replace("\\\\", "\\").replace("\\{", "{").replace("\\}", "}")
            try:
                value = json.loads(repaired)
            except (TypeError, ValueError):
                ordinary_notes.append(line)
                continue
        record[column] = value
    record["Notes"] = "\n".join(ordinary_notes).strip()
    return record


def _from_csl_item(item):
    authors = []
    for author in item.get("author") or []:
        if isinstance(author, dict):
            authors.append(author.get("literal") or ", ".join(
                x for x in [author.get("family", ""), author.get("given", "")] if x))
    parts = ((item.get("issued") or {}).get("date-parts") or [[]])[0]
    raw_tags = item.get("keyword") or item.get("keywords") or item.get("tags") or ""
    if isinstance(raw_tags, list):
        raw_tags = "; ".join(
            str(value.get("tag", "") if isinstance(value, dict) else value).strip()
            for value in raw_tags if str(value).strip())
    return {"Key": item.get("id", ""), "Item Type": item.get("type", "article-journal"),
            "Title": item.get("title", ""), "Author": "; ".join(authors),
            "Publication Year": parts[0] if parts else "", "Publication Title": item.get("container-title", ""),
            "DOI": item.get("DOI", ""), "Url": item.get("URL", ""),
            "ISBN": item.get("ISBN", ""), "ISSN": item.get("ISSN", ""),
            "Notes": item.get("note", ""),
            "Abstract Note": item.get("abstract", "") or item.get("description", ""),
            "Keywords": raw_tags}


def _to_csl_item(row, index):
    year_match = re.search(r"\d{4}", _value(row, "Publication Year", "Year"))
    item = {"id": _value(row, "Key") or f"item-{index + 1}",
            "type": _value(row, "Item Type") or "article-journal",
            "title": _value(row, "Title", "Matched Title"),
            "author": [{"literal": a} for a in _split_authors(_value(row, "Author", "Authors"))]}
    if year_match:
        item["issued"] = {"date-parts": [[int(year_match.group())]]}
    for target, names in {"container-title": ("Publication Title", "Journal"), "DOI": ("DOI", "doi"),
                          "URL": ("Link", "Url", "URL", "url"), "ISBN": ("ISBN",), "ISSN": ("ISSN",)}.items():
        value = _value(row, *names)
        if value:
            item[target] = value
    note = _verification_note(row)
    original_note = _value(row, "Notes", "Note")
    combined_note = "\n".join(filter(None, [original_note, note, _abstract_verification_note(row)]))
    if combined_note:
        item["note"] = combined_note
    abstract = _value(row, "Abstract Note", "Abstract", "Summary")
    if abstract:
        item["abstract"] = abstract
    tags = _value(row, "Tags", "Tag", "Keywords", "Keyword")
    if tags:
        item["keyword"] = tags
        # CSL JSON commonly stores keywords as one string, whereas Zotero's
        # JSON representation uses an array of tag objects. Include both so a
        # cleaned export remains useful to either consumer.
        tag_values, _separator = split_tags(tags)
        item["tags"] = [{"tag": value} for value in tag_values]
    return item


def _read_ris(path):
    records, current = [], {}
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            # RIS normally uses ``XX  - value``, but exports differ in the
            # amount of whitespace and an empty ``ER  - `` loses its final
            # space after trimming. Accept those harmless variations so an
            # empty ER still closes the current record.
            match = re.match(r"^\s*([A-Za-z0-9]{2})\s*-\s*(.*?)\s*$", line.rstrip("\r\n"))
            if not match:
                continue
            tag, value = match.groups()
            tag = tag.upper()
            if tag == "TY": current = {"Item Type": value}
            elif tag == "ER":
                if current: records.append(_restore_portable_columns(current))
                current = {}
            elif tag in {"TI", "T1"}: current["Title"] = value
            elif tag in {"AU", "A1"}: current["Author"] = "; ".join(filter(None, [current.get("Author"), value]))
            elif tag in {"PY", "Y1", "DA"}:
                found = re.search(r"\d{4}", value)
                current.setdefault("Publication Year", found.group() if found else value)
            elif tag in {"JO", "JF", "T2"}: current.setdefault("Publication Title", value)
            elif tag == "DO": current["DOI"] = value
            elif tag in {"UR", "L1"}: current.setdefault("Url", value)
            elif tag == "SN": current.setdefault("ISSN", value)
            elif tag == "KW": current["Keywords"] = "; ".join(
                filter(None, [current.get("Keywords"), value]))
            elif tag in {"N1", "RN"}: current["Notes"] = "\n".join(
                filter(None, [current.get("Notes"), value]))
            elif tag in {"AB", "N2"}: current["Abstract"] = "\n".join(
                filter(None, [current.get("Abstract"), value]))
    if current: records.append(_restore_portable_columns(current))
    return records


def _write_ris(df, path, portable_columns=None):
    ris_types = {"journalarticle": "JOUR", "article-journal": "JOUR", "book": "BOOK",
                 "booksection": "CHAP", "chapter": "CHAP", "thesis": "THES",
                 "conferencepaper": "CPAPER", "report": "RPRT"}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for _, row in df.iterrows():
            raw_type = _value(row, "Item Type")
            f.write(f"TY  - {ris_types.get(raw_type.lower(), raw_type or 'JOUR')}\n")
            for tag, value in [("TI", _value(row, "Title", "Matched Title")),
                               ("PY", _value(row, "Publication Year", "Year")),
                               ("JO", _value(row, "Publication Title", "Journal")),
                               ("DO", _value(row, "DOI", "doi")),
                               ("UR", _value(row, "Link", "Url", "URL", "url"))]:
                if value: f.write(f"{tag}  - {value}\n")
            for author in _split_authors(_value(row, "Author", "Authors")):
                f.write(f"AU  - {author}\n")
            tags, _separator = split_tags(_value(row, "Tags", "Tag", "Keywords", "Keyword"))
            for tag_value in tags:
                f.write(f"KW  - {tag_value}\n")
            for abstract_line in _value(row, "Abstract Note", "Abstract", "Summary").splitlines():
                if abstract_line.strip(): f.write(f"AB  - {abstract_line.strip()}\n")
            original_note = _value(row, "Notes", "Note")
            note = "\n".join(filter(None, [original_note, _verification_note(row),
                                             _abstract_verification_note(row),
                                             _portable_columns_note(row, portable_columns)]))
            for note_line in note.splitlines():
                if note_line.strip(): f.write(f"N1  - {note_line.strip()}\n")
            f.write("ER  - \n\n")


def _parse_bibtex_fields(body):
    """Parse BibTeX fields while preserving values with nested braces."""
    fields = {}
    cursor = 0
    field_start = re.compile(r"([A-Za-z][\w-]*)\s*=\s*")
    while True:
        match = field_start.search(body, cursor)
        if not match:
            break
        name = match.group(1).lower()
        start = match.end()
        if start >= len(body):
            fields[name] = ""
            break
        opener = body[start]
        if opener == "{":
            depth, index = 1, start + 1
            while index < len(body) and depth:
                if body[index] == "{" and (index == 0 or body[index - 1] != "\\"):
                    depth += 1
                elif body[index] == "}" and (index == 0 or body[index - 1] != "\\"):
                    depth -= 1
                index += 1
            fields[name] = body[start + 1:index - 1 if depth == 0 else index].strip()
            cursor = index
        elif opener == '"':
            index = start + 1
            while index < len(body):
                if body[index] == '"' and body[index - 1] != "\\":
                    break
                index += 1
            fields[name] = body[start + 1:index].strip()
            cursor = index + 1
        else:
            end = body.find(",", start)
            if end < 0:
                end = len(body)
            fields[name] = body[start:end].strip()
            cursor = end + 1
    return fields


def _unwrap_bibtex_keyword_braces(value):
    """Remove capitalization-protection braces around a whole keyword value."""
    text = str(value or "").strip()
    while len(text) >= 2 and text[0] == "{" and text[-1] == "}":
        depth, encloses_all = 0, True
        for index, char in enumerate(text):
            if char == "{" and (index == 0 or text[index - 1] != "\\"):
                depth += 1
            elif char == "}" and (index == 0 or text[index - 1] != "\\"):
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        text = text[1:-1].strip()
    return text


def _normalize_bibtex_keywords(value):
    """Remove BibTeX protection braces from the field and each keyword."""
    text = _unwrap_bibtex_keyword_braces(value)
    if not text:
        return ""
    return ", ".join(
        _unwrap_bibtex_keyword_braces(part)
        for part in text.split(",")
        if _unwrap_bibtex_keyword_braces(part))


def _read_bibtex(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    records = []
    for match in re.finditer(r"@(\w+)\s*\{\s*([^,]+),([\s\S]*?)(?=\n\s*@|\Z)", text):
        kind, key, body = match.groups()
        fields = _parse_bibtex_fields(body)
        keywords = _normalize_bibtex_keywords(
            fields.get("keywords") or fields.get("keyword", ""))
        records.append(_restore_portable_columns({
            "Key": key.strip(), "Item Type": kind, "Title": fields.get("title", ""),
            "Author": fields.get("author", "").replace(" and ", "; "),
            "Publication Year": fields.get("year", ""),
            "Publication Title": fields.get("journal") or fields.get("booktitle", ""),
            "DOI": fields.get("doi", ""), "Url": fields.get("url", ""),
            "ISBN": fields.get("isbn", ""), "ISSN": fields.get("issn", ""),
            "Notes": fields.get("note", ""),
            "Abstract Note": fields.get("abstract", ""),
            "Keywords": keywords,
        }))
    return records


def _write_bibtex(df, path, portable_columns=None):
    used = set()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for i, (_, row) in enumerate(df.iterrows()):
            base = re.sub(r"[^A-Za-z0-9:_-]", "", _value(row, "Key")) or f"item{i + 1}"
            key, suffix = base, 2
            while key in used:
                key, suffix = f"{base}{suffix}", suffix + 1
            used.add(key)
            item_type = _value(row, "Item Type").lower()
            kind = "article" if "article" in item_type or not item_type else ("incollection" if "section" in item_type else "book")
            fields = [("title", _value(row, "Title", "Matched Title")),
                      ("author", " and ".join(_split_authors(_value(row, "Author", "Authors")))),
                      ("year", _value(row, "Publication Year", "Year")),
                      ("journal", _value(row, "Publication Title", "Journal")),
                      ("doi", _value(row, "DOI", "doi")),
                      ("url", _value(row, "Link", "Url", "URL", "url")),
                      ("isbn", _value(row, "ISBN")), ("issn", _value(row, "ISSN")),
                      ("abstract", _value(row, "Abstract Note", "Abstract", "Summary")),
                      ("keywords", _value(row, "Tags", "Tag", "Keywords", "Keyword")),
                      ("note", "\n".join(filter(None, [
                          _value(row, "Notes", "Note"), _verification_note(row),
                          _abstract_verification_note(row),
                          _portable_columns_note(row, portable_columns)])))]
            present = [(name, value) for name, value in fields if value]
            f.write(f"@{kind}{{{key},\n")
            for j, (name, value) in enumerate(present):
                safe = value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
                f.write(f"  {name} = {{{safe}}}{',' if j < len(present) - 1 else ''}\n")
            f.write("}\n\n")
