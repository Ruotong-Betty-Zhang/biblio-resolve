"""Reusable note-link recovery and webpage abstract extraction.

The GUI imports this module, but the functions are deliberately independent
of the UI so they can also be used from a notebook or another Python script.
"""

from __future__ import annotations

import io
import json
import re
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

import pandas as pd
import requests

try:  # Package import: python -m system.test_abstract_note_tools
    from . import doi_lookup_lib as lookup_lib
    from . import lookup_core as core
except ImportError:  # Direct app/script import from inside system/
    import doi_lookup_lib as lookup_lib
    import lookup_core as core


NOTE_ALIASES = ["notes", "note", "extra", "research notes", "manual notes"]
URL_ALIASES = ["url", "link", "resolved url", "resource url"]
DOI_ALIASES = ["doi"]
ABSTRACT_ALIASES = ["abstract", "abstract note", "summary"]
TAG_ALIASES = ["tags", "tag", "keywords", "keyword"]
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
ABSTRACT_REVIEW_TAGS = {
    "abstract_found_possible_mismatch": "ABSTRACT_FOUND_POSSIBLE_MISMATCH",
    "abstract_found_needs_review": "ABSTRACT_FOUND_NEEDS_REVIEW",
}


def guess_column(columns, aliases):
    exact = {str(column).strip().casefold(): column for column in columns}
    for alias in aliases:
        if alias.casefold() in exact:
            return exact[alias.casefold()]
    for column in columns:
        folded = str(column).strip().casefold()
        if any(alias.casefold() in folded for alias in aliases):
            return column
    return None


def clean_value(value):
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return ""
    return str(value).strip()


def _replace_abstract_review_tag(value, review_tag):
    """Replace only this feature's review tags, preserving every user tag."""
    special = set(ABSTRACT_REVIEW_TAGS.values())
    tags = [item.strip() for item in re.split(r"\s*(?:;|\n|,)\s*", clean_value(value)) if item.strip()]
    tags = [item for item in tags if item not in special]
    new_tag = ABSTRACT_REVIEW_TAGS.get(review_tag, "")
    if new_tag and new_tag not in tags:
        tags.append(new_tag)
    return "; ".join(tags)


def extract_urls_from_note(note):
    """Return unique HTTP(S) links in order, including links inside HTML notes."""
    found = []
    for match in URL_RE.findall(unescape(clean_value(note))):
        url = match.rstrip(".,;:!?>")
        for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
            while url.endswith(closing) and url.count(closing) > url.count(opening):
                url = url[:-1]
        if url and url not in found:
            found.append(url)
    return found


def record_link(row, url_column=None, doi_column=None):
    url = clean_value(row.get(url_column, "")) if url_column else ""
    if url:
        return url
    doi = clean_value(row.get(doi_column, "")) if doi_column else ""
    if not doi:
        return ""
    if doi.casefold().startswith(("http://", "https://")):
        return doi
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    return f"https://doi.org/{doi}"


def analyze_notes_and_add_links(dataframe, note_column=None, url_column=None, doi_column=None):
    """Add the first Note URL only when the record has neither URL nor DOI."""
    frame = dataframe.copy()
    note_column = note_column or guess_column(frame.columns, NOTE_ALIASES)
    url_column = url_column or guess_column(frame.columns, URL_ALIASES)
    doi_column = doi_column or guess_column(frame.columns, DOI_ALIASES)
    if url_column is None:
        url_column = "Url"
        frame[url_column] = ""
    frame["Note URLs Found"] = ""
    frame["Link Added From Note"] = False

    before_links, notes, link_and_note = 0, 0, 0
    no_link_with_note, no_link_note_url, added = 0, 0, 0
    for index, row in frame.iterrows():
        note = clean_value(row.get(note_column, "")) if note_column else ""
        link_before = record_link(row, url_column, doi_column)
        urls = extract_urls_from_note(note)
        if link_before:
            before_links += 1
        if note:
            notes += 1
        if link_before and note:
            link_and_note += 1
        if not link_before and note:
            no_link_with_note += 1
            if urls:
                no_link_note_url += 1
        frame.at[index, "Note URLs Found"] = "; ".join(urls)
        if not link_before and urls:
            frame.at[index, url_column] = urls[0]
            frame.at[index, "Link Added From Note"] = True
            added += 1

    after_links = sum(bool(record_link(row, url_column, doi_column)) for _, row in frame.iterrows())
    total = len(frame)
    stats = {
        "Total records": total,
        "Records with a link before Note recovery": before_links,
        "Records without a link before Note recovery": total - before_links,
        "Records with a Note": notes,
        "Records with both a link and a Note": link_and_note,
        "Records with a link but no Note": before_links - link_and_note,
        "Records without a link but with a Note": no_link_with_note,
        "Records without a link whose Note contains a URL": no_link_note_url,
        "Records without a link before recovery that gained a link from Note": added,
        "Records with a link after Note recovery": after_links,
        "Records still without a link": total - after_links,
    }
    return frame, stats, {"note_column": note_column, "url_column": url_column, "doi_column": doi_column}


class _AbstractHTMLParser(HTMLParser):
    META_KEYS = {
        "citation_abstract", "dc.description", "dcterms.description", "dcterms.abstract",
        "description", "og:description", "twitter:description", "eprints.abstract",
    }
    TITLE_KEYS = {"citation_title", "dc.title", "dcterms.title", "og:title"}
    AUTHOR_KEYS = {"citation_author", "dc.creator", "dcterms.creator", "author"}
    YEAR_KEYS = {"citation_publication_date", "citation_date", "dc.date", "dcterms.date"}
    DOI_KEYS = {"citation_doi", "dc.identifier", "dcterms.identifier"}
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta_values = []
        self.json_values = []
        self._json_depth = 0
        self._json_parts = []
        self._abstract_depth = 0
        self._abstract_parts = []
        self.identity_values = {"titles": [], "authors": [], "years": [], "dois": []}
        self._document_title_depth = 0
        self._document_title_parts = []
        self.page_text_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(key).casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "meta":
            key = (attrs.get("name") or attrs.get("property") or attrs.get("itemprop") or "").casefold()
            content = attrs.get("content", "").strip()
            if key in self.META_KEYS and content:
                self.meta_values.append(content)
            for keys, bucket in ((self.TITLE_KEYS, "titles"), (self.AUTHOR_KEYS, "authors"),
                                 (self.YEAR_KEYS, "years"), (self.DOI_KEYS, "dois")):
                if key in keys and content:
                    self.identity_values[bucket].append(content)
        if tag.casefold() == "title":
            self._document_title_depth += 1
        if tag.casefold() == "script" and "ld+json" in attrs.get("type", "").casefold():
            self._json_depth += 1
        marker = " ".join((attrs.get("id", ""), attrs.get("class", ""), attrs.get("itemprop", ""))).casefold()
        if self._abstract_depth and tag.casefold() not in self.VOID_TAGS:
            self._abstract_depth += 1
        elif "abstract" in marker and tag.casefold() in {"div", "section", "article", "p"}:
            self._abstract_depth = 1

    def handle_endtag(self, tag):
        if self._document_title_depth and tag.casefold() == "title":
            self._document_title_depth -= 1
        if self._json_depth and tag.casefold() == "script":
            self._json_depth -= 1
            if not self._json_depth and self._json_parts:
                self.json_values.append("".join(self._json_parts))
                self._json_parts = []
        if self._abstract_depth:
            self._abstract_depth -= 1

    def handle_data(self, data):
        if data.strip():
            self.page_text_parts.append(data)
        if self._document_title_depth:
            self._document_title_parts.append(data)
        if self._json_depth:
            self._json_parts.append(data)
        if self._abstract_depth:
            self._abstract_parts.append(data)


def _clean_abstract(value):
    value = re.sub(r"<[^>]+>", " ", unescape(clean_value(value)))
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"^abstract\s*[:.—-]?\s*", "", value, flags=re.IGNORECASE)
    return value if len(value) >= 40 else ""


def _clean_metadata_text(value):
    value = re.sub(r"<[^>]+>", " ", unescape(clean_value(value)))
    return re.sub(r"\s+", " ", value).strip()


def _json_abstract(value):
    if isinstance(value, dict):
        for key in ("abstract", "description"):
            abstract = _clean_abstract(value.get(key, ""))
            if abstract:
                return abstract
        for child in value.values():
            abstract = _json_abstract(child)
            if abstract:
                return abstract
    elif isinstance(value, list):
        for child in value:
            abstract = _json_abstract(child)
            if abstract:
                return abstract
    return ""


def _json_author_names(value):
    if isinstance(value, str):
        return [clean_value(value)] if clean_value(value) else []
    if isinstance(value, dict):
        name = clean_value(value.get("name", ""))
        if not name:
            name = " ".join(filter(None, (clean_value(value.get("givenName", "")),
                                           clean_value(value.get("familyName", "")))))
        return [name] if name else []
    if isinstance(value, list):
        names = []
        for item in value:
            names.extend(_json_author_names(item))
        return names
    return []


def _json_identifier(value):
    if isinstance(value, str):
        return clean_value(value)
    if isinstance(value, dict):
        return clean_value(value.get("value", "") or value.get("@id", "") or value.get("name", ""))
    return ""


def _json_identity(value):
    """Return identity metadata from the first article-like JSON-LD object."""
    if isinstance(value, dict):
        raw_type = value.get("@type", "")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        types = {clean_value(item).casefold() for item in types}
        work_types = {"scholarlyarticle", "article", "newsarticle", "report", "thesis",
                      "chapter", "bookchapter", "creativework"}
        is_work = bool(types & work_types or value.get("abstract") or value.get("headline"))
        if is_work:
            title = clean_value(value.get("headline", "") or value.get("title", "") or value.get("name", ""))
            authors = _json_author_names(value.get("author", []))
            year = clean_value(value.get("datePublished", "") or value.get("dateCreated", ""))
            doi = clean_value(value.get("doi", "") or _json_identifier(value.get("identifier", "")))
            if title or authors or year or doi:
                return {"title": title, "authors": authors, "year": year, "dois": [doi] if doi else []}
        for child in value.values():
            identity = _json_identity(child)
            if identity:
                return identity
    elif isinstance(value, list):
        for child in value:
            identity = _json_identity(child)
            if identity:
                return identity
    return {}


def _first_year(value):
    match = re.search(r"\b(18|19|20|21)\d{2}\b", clean_value(value))
    return match.group(0) if match else ""


def _html_bundle(html):
    parser = _AbstractHTMLParser()
    parser.feed(html or "")
    json_objects = []
    for text in parser.json_values:
        try:
            json_objects.append(json.loads(text))
        except (ValueError, TypeError):
            continue

    identity = {
        "title": clean_value(parser.identity_values["titles"][0]) if parser.identity_values["titles"] else "",
        "authors": [clean_value(value) for value in parser.identity_values["authors"] if clean_value(value)],
        "year": _first_year(parser.identity_values["years"][0]) if parser.identity_values["years"] else "",
        "dois": [clean_value(value) for value in parser.identity_values["dois"] if clean_value(value)],
    }
    for value in json_objects:
        candidate = _json_identity(value)
        if not candidate:
            continue
        identity["title"] = identity["title"] or candidate.get("title", "")
        identity["authors"] = identity["authors"] or candidate.get("authors", [])
        identity["year"] = identity["year"] or _first_year(candidate.get("year", ""))
        identity["dois"] = identity["dois"] or candidate.get("dois", [])
        break
    if not identity["title"]:
        identity["title"] = _clean_metadata_text(" ".join(parser._document_title_parts))

    for value in parser.meta_values:
        abstract = _clean_abstract(value)
        if abstract:
            return abstract, "HTML citation metadata", identity, " ".join(parser.page_text_parts)
    for value in json_objects:
        abstract = _json_abstract(value)
        if abstract:
            return abstract, "JSON-LD", identity, " ".join(parser.page_text_parts)
    abstract = _clean_abstract(" ".join(parser._abstract_parts))
    source = "Visible abstract section" if abstract else ""
    return abstract, source, identity, " ".join(parser.page_text_parts)


def extract_abstract_from_html(html):
    abstract, source, _identity, _page_text = _html_bundle(html)
    return abstract, source


def _pdf_bundle(content, max_pages=3):
    """max_pages=3 (the default) is enough for an abstract, which is
    always near the front. Full-text search for module/data mentions
    (which tend to live in a Methods/Data section further in) asks for
    more pages via this parameter."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:max_pages])
        metadata = reader.metadata or {}
    except Exception:
        return "", "", {}, ""
    if len(text.strip()) < 100:
        # No usable text layer (e.g. a scanned PDF) - fall back to the same
        # OCR pipeline the Verification tab already uses (PyMuPDF renders
        # the first couple of pages to images, pytesseract reads them).
        # Optional dependency: silently keeps the (empty) native text if
        # PyMuPDF/pytesseract/Tesseract aren't installed.
        ocr_text, _ocr_used, _status = core._pdf_text(content)
        if len(ocr_text.strip()) >= len(text.strip()):
            text = ocr_text
    match = re.search(
        r"\babstract\b\s*[:.—-]?\s*(.{80,5000}?)(?=\n\s*(?:keywords?|index terms|introduction|1\.?\s+introduction)\b)",
        text, flags=re.IGNORECASE | re.DOTALL)
    abstract = _clean_abstract(match.group(1)) if match else ""
    raw_title = getattr(metadata, "title", "") or metadata.get("/Title", "")
    raw_author = getattr(metadata, "author", "") or metadata.get("/Author", "")
    raw_date = (getattr(metadata, "creation_date", "") or metadata.get("/CreationDate", ""))
    dois = re.findall(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, flags=re.IGNORECASE)
    identity = {
        "title": _clean_metadata_text(raw_title),
        "authors": [_clean_metadata_text(raw_author)] if _clean_metadata_text(raw_author) else [],
        "year": _first_year(raw_date),
        "dois": list(dict.fromkeys(dois)),
    }
    return abstract, "PDF text" if abstract else "", identity, text


def extract_abstract_from_pdf(content):
    abstract, source, _identity, _page_text = _pdf_bundle(content)
    return abstract, source


def _normalize_doi(value):
    value = clean_value(value).casefold()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    match = re.search(r"10\.\d{4,9}/\S+", value)
    return match.group(0).rstrip(".,; ") if match else ""


def _contained_title_score(target, found):
    target_norm = re.sub(r"\s+", " ", lookup_lib.normalize_for_compare(target)).strip()
    found_norm = re.sub(r"\s+", " ", lookup_lib.normalize_for_compare(found)).strip()
    if not target_norm or not found_norm:
        return 0.0
    score = float(lookup_lib.fuzz.token_sort_ratio(target_norm, found_norm))
    shorter, longer = sorted((target_norm, found_norm), key=len)
    if (shorter in longer and len(shorter.split()) >= 4
            and len(shorter) / max(1, len(longer)) >= 0.50):
        return 100.0
    return round(score, 2)


def assess_abstract_identity(title="", author="", year="", doi="", identity=None, page_text=""):
    """Classify identity evidence without preventing a found abstract from being saved."""
    identity = identity or {}
    target_title = _clean_metadata_text(title)
    found_title = _clean_metadata_text(identity.get("title", ""))
    found_authors = [_clean_metadata_text(value) for value in identity.get("authors", [])
                     if _clean_metadata_text(value)]
    found_year = _first_year(identity.get("year", ""))
    target_year = _first_year(year)
    target_doi = _normalize_doi(doi)
    found_dois = list(dict.fromkeys(filter(None, (_normalize_doi(value)
                                                  for value in identity.get("dois", [])))))
    normalized_page_text = lookup_lib.normalize_for_compare(page_text)

    title_score = 0.0
    title_evidence = ""
    if target_title and found_title:
        title_score = _contained_title_score(target_title, found_title)
        title_evidence = "page metadata"
    elif target_title and normalized_page_text:
        title_score = round(float(lookup_lib.fuzz.partial_ratio(
            lookup_lib.normalize_for_compare(target_title), normalized_page_text)), 2)
        title_evidence = "page/PDF text"

    target_authors = lookup_lib.clean_authors(author)
    author_match = None
    author_evidence = ""
    if target_authors and found_authors:
        details = lookup_lib.score_candidate_detailed(
            target_title or found_title, target_authors, target_year or None,
            {"title": found_title or target_title, "authors": found_authors,
             "year": int(found_year) if found_year else None})
        author_match = details.get("author_match_type") != "none"
        author_evidence = "page metadata"
    elif target_authors and normalized_page_text:
        author_match = any(lookup_lib.normalize_for_compare(name) in normalized_page_text
                           for name in target_authors)
        author_evidence = "page/PDF text"

    year_match = None
    if target_year and found_year:
        year_match = abs(int(target_year) - int(found_year)) <= 1
    doi_match = None
    if target_doi and found_dois:
        doi_match = target_doi in found_dois

    conflicts = []
    if target_title and title_evidence and title_score < 80:
        conflicts.append(f"title similarity {title_score:.1f}/100 is below 80")
    if target_authors and author_match is False:
        conflicts.append("author name was not found on the page/PDF")
    if year_match is False:
        conflicts.append(f"year differs: input {target_year}, page {found_year}")
    if doi_match is False:
        conflicts.append(f"DOI differs: input {target_doi}, page {', '.join(found_dois)}")

    missing = []
    if not target_title:
        missing.append("input title is missing")
    elif not title_evidence:
        missing.append("page/PDF title evidence is missing")
    if target_authors and author_match is None:
        missing.append("page/PDF author evidence is missing")

    evidence = [f"title {title_score:.1f}/100 via {title_evidence}" if title_evidence else ""]
    if target_authors and author_match is not None:
        evidence.append(f"author {'matched' if author_match else 'not matched'} via {author_evidence}")
    if year_match is not None:
        evidence.append(f"year {'matched' if year_match else 'not matched'}")
    if doi_match is not None:
        evidence.append(f"DOI {'matched' if doi_match else 'not matched'}")
    evidence = [item for item in evidence if item]

    if conflicts:
        verification_status = "possible_mismatch"
        review_tag = "abstract_found_possible_mismatch"
        message = "Abstract saved, but identity checks raised a possible mismatch: " + "; ".join(conflicts) + "."
    elif missing:
        verification_status = "insufficient_metadata"
        review_tag = "abstract_found_needs_review"
        message = "Abstract saved, but identity could not be confirmed: " + "; ".join(missing) + "."
    else:
        verification_status = "matched"
        review_tag = "abstract_match_confirmed"
        message = "Abstract saved and identity checks matched" + (": " + "; ".join(evidence) if evidence else "") + "."

    return {
        "verification_status": verification_status,
        "review_tag": review_tag,
        "verification_message": message,
        "title_score": title_score,
        "author_match": author_match,
        "year_match": year_match,
        "doi_match": doi_match,
        "metadata_title": found_title,
        "metadata_authors": found_authors,
        "metadata_year": found_year,
        "metadata_dois": found_dois,
    }


def fetch_abstract(url, session=None, timeout=20, max_attempts=3, max_pdf_bytes=15_000_000,
                   title="", author="", year="", doi="", max_pdf_pages=3):
    """`max_pdf_pages` only matters for a PDF response - raise it (e.g. to
    search for a Methods/Data section further into the document) without
    affecting the default abstract-finding use, where the abstract is
    always near the front. The returned dict always includes "full_text":
    the raw extracted page/PDF text (native, or OCR'd if the PDF had no
    text layer), for callers that need more than just the abstract."""
    session = session or requests.Session()
    headers = {"User-Agent": "LiteratureLookup/1.0 (abstract metadata extraction)"}
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(url, timeout=timeout, headers=headers, allow_redirects=True)
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                if attempt < max_attempts:
                    retry_after = clean_value(response.headers.get("Retry-After", ""))
                    delay = min(20.0, float(retry_after)) if retry_after.replace(".", "", 1).isdigit() else 2 ** attempt
                    time.sleep(delay)
                    continue
            response.raise_for_status()
            content_type = clean_value(response.headers.get("Content-Type", "")).casefold()
            final_url = clean_value(getattr(response, "url", "")) or clean_value(url)
            if "pdf" in content_type or final_url.casefold().endswith(".pdf"):
                content = response.content
                if len(content) > max_pdf_bytes:
                    return {"abstract": "", "source": "", "url": final_url, "full_text": "",
                            "status": "pdf_too_large", "error": "PDF exceeds 15 MB",
                            "verification_status": "", "review_tag": ""}
                abstract, source, identity, page_text = _pdf_bundle(content, max_pages=max_pdf_pages)
            else:
                response.encoding = response.encoding or "utf-8"
                abstract, source, identity, page_text = _html_bundle(response.text)
            verification = (assess_abstract_identity(title, author, year, doi, identity, page_text)
                            if abstract else {"verification_status": "", "review_tag": "",
                                              "verification_message": "", "title_score": 0.0,
                                              "author_match": None, "year_match": None, "doi_match": None,
                                              "metadata_title": identity.get("title", ""),
                                              "metadata_authors": identity.get("authors", []),
                                              "metadata_year": identity.get("year", ""),
                                              "metadata_dois": identity.get("dois", [])})
            return {"abstract": abstract, "source": source, "url": final_url,
                    "full_text": page_text,
                    "status": "abstract_found" if abstract else "no_abstract_found", "error": "",
                    **verification}
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < max_attempts:
                time.sleep(2 ** attempt)
        except Exception as exc:
            # Malformed or incomplete webpage metadata must not abort the
            # entire batch. Record this page as a failure and continue with
            # the next bibliographic record.
            return {"abstract": "", "source": "", "url": clean_value(url), "full_text": "",
                    "status": "parse_failed", "error": f"{type(exc).__name__}: {exc}",
                    "verification_status": "", "review_tag": ""}
    return {"abstract": "", "source": "", "url": url, "full_text": "",
            "status": "fetch_failed", "error": last_error,
            "verification_status": "", "review_tag": ""}


def find_abstracts(dataframe, url_column=None, doi_column=None, abstract_column=None,
                   progress_callback=None, cancel_event=None, request_delay=0.5, session=None,
                   title_column=None, author_column=None, year_column=None, tag_column=None,
                   source_lookup=None, cache_lookup=None, cache_store=None):
    """Find missing abstracts from metadata Sources first, then URL/DOI pages."""
    frame = dataframe.copy()
    url_column = url_column or guess_column(frame.columns, URL_ALIASES)
    doi_column = doi_column or guess_column(frame.columns, DOI_ALIASES)
    abstract_column = abstract_column or guess_column(frame.columns, ABSTRACT_ALIASES)
    title_column = title_column or guess_column(frame.columns, ["title", "name"])
    author_column = author_column or guess_column(frame.columns, ["author", "authors", "creators"])
    year_column = year_column or guess_column(frame.columns, ["publication year", "year", "date"])
    tag_column = tag_column or guess_column(frame.columns, TAG_ALIASES)
    if abstract_column is None:
        abstract_column = "Abstract"
        frame[abstract_column] = ""
    if tag_column is None:
        tag_column = "Tags"
        frame[tag_column] = ""
    result_columns = (
        "Abstract Fetch Status", "Abstract Source", "Abstract Source URL", "Abstract Fetch Error",
        "Abstract Match Status", "Abstract Review Tag", "Abstract Verification Message",
        "Abstract Page Title", "Abstract Page Authors", "Abstract Page Year", "Abstract Page DOI",
        "Abstract Title Score", "Abstract Author Match", "Abstract Year Match", "Abstract DOI Match",
        "Abstract Source Lookup Errors", "From Abstract Cache",
    )
    for column in result_columns:
        if column not in frame.columns:
            frame[column] = ""

    session = session or requests.Session()
    counts = {"Existing abstracts": 0, "Abstracts found from Sources": 0,
              "Abstracts found from links": 0,
              "Abstract matches confirmed": 0, "Abstracts found needing review": 0,
              "Abstracts found with possible mismatch": 0,
              "Links with no abstract found": 0, "Abstract fetch failures": 0,
              "Records with Source lookup errors": 0,
              "Records resumed from Abstract cache": 0,
              "Records skipped because no link is available": 0, "Cancelled records": 0}
    total = len(frame)
    for completed, (index, row) in enumerate(frame.iterrows(), start=1):
        if cancel_event is not None and cancel_event.is_set():
            remaining = total - completed + 1
            counts["Cancelled records"] += remaining
            remaining_indices = list(frame.index)[completed - 1:]
            frame.loc[remaining_indices, "Abstract Fetch Status"] = "cancelled"
            break
        existing = clean_value(row.get(abstract_column, ""))
        link = record_link(row, url_column, doi_column)
        result = None
        result_origin = ""
        from_cache = False
        for column in result_columns[1:]:
            frame.at[index, column] = ""
        if existing:
            status = "existing_abstract"
            counts["Existing abstracts"] += 1
        else:
            title_value = clean_value(row.get(title_column, "")) if title_column else ""
            author_value = clean_value(row.get(author_column, "")) if author_column else ""
            year_value = clean_value(row.get(year_column, "")) if year_column else ""
            doi_value = clean_value(row.get(doi_column, "")) if doi_column else ""
            record = {
                "title": title_value, "author": author_value, "year": year_value,
                "doi": doi_value, "url": link,
            }
            cached_payload = None
            if cache_lookup is not None:
                try:
                    cached_payload = cache_lookup(**record)
                except Exception:
                    cached_payload = None

            if cached_payload is not None:
                from_cache = True
                counts["Records resumed from Abstract cache"] += 1
                result = cached_payload.get("result")
                result_origin = clean_value(cached_payload.get("result_origin", ""))
                status = clean_value(cached_payload.get("status", "")) or "no_link"
                source_errors = cached_payload.get("source_errors", []) or []
                if source_errors:
                    counts["Records with Source lookup errors"] += 1
                    frame.at[index, "Abstract Source Lookup Errors"] = "; ".join(
                        f"{clean_value(item.get('source'))}: {clean_value(item.get('error'))}"
                        for item in source_errors)
            else:
                source_errors = []

                if source_lookup is not None:
                    try:
                        source_result = source_lookup(**record)
                    except Exception as exc:
                        source_result = {
                            "abstract": "",
                            "failed_sources": [{"source": "source lookup", "error": str(exc)}],
                        }
                    source_errors = source_result.get("failed_sources", []) or []
                    if source_errors:
                        counts["Records with Source lookup errors"] += 1
                        frame.at[index, "Abstract Source Lookup Errors"] = "; ".join(
                            f"{clean_value(item.get('source'))}: {clean_value(item.get('error'))}"
                            for item in source_errors)
                    source_abstract = _clean_abstract(source_result.get("abstract", ""))
                    if source_abstract:
                        result = {
                            **source_result,
                            "abstract": source_abstract,
                            "status": "abstract_found_source",
                            "error": "",
                        }
                        result_origin = "source"

                if result is None and link:
                    result = fetch_abstract(
                        link, session=session, title=title_value, author=author_value,
                        year=year_value, doi=doi_value)
                    result_origin = "link"
                    time.sleep(max(0.0, request_delay))

                status = "no_link" if result is None else result["status"]
                if cache_store is not None:
                    try:
                        cache_store({
                            "result": result, "result_origin": result_origin,
                            "status": status, "source_errors": source_errors,
                        }, **record)
                    except Exception:
                        pass

            if result is None:
                counts["Records skipped because no link is available"] += 1
            else:
                frame.at[index, "Abstract Source"] = result.get("source", "")
                frame.at[index, "Abstract Source URL"] = result.get("url", "")
                frame.at[index, "Abstract Fetch Error"] = result.get("error", "")
            if result is not None and result.get("abstract"):
                frame.at[index, abstract_column] = result["abstract"]
                if result_origin == "source":
                    counts["Abstracts found from Sources"] += 1
                else:
                    counts["Abstracts found from links"] += 1
                frame.at[index, "Abstract Match Status"] = result.get("verification_status", "")
                frame.at[index, "Abstract Review Tag"] = result.get("review_tag", "")
                frame.at[index, tag_column] = _replace_abstract_review_tag(
                    row.get(tag_column, ""), result.get("review_tag", ""))
                frame.at[index, "Abstract Verification Message"] = result.get("verification_message", "")
                frame.at[index, "Abstract Page Title"] = result.get("metadata_title", "")
                frame.at[index, "Abstract Page Authors"] = "; ".join(
                    clean_value(value) for value in result.get("metadata_authors", []) if clean_value(value))
                frame.at[index, "Abstract Page Year"] = result.get("metadata_year", "")
                frame.at[index, "Abstract Page DOI"] = "; ".join(
                    clean_value(value) for value in result.get("metadata_dois", []) if clean_value(value))
                frame.at[index, "Abstract Title Score"] = result.get("title_score", 0.0)
                frame.at[index, "Abstract Author Match"] = result.get("author_match", "")
                frame.at[index, "Abstract Year Match"] = result.get("year_match", "")
                frame.at[index, "Abstract DOI Match"] = result.get("doi_match", "")
                if result.get("verification_status") == "matched":
                    counts["Abstract matches confirmed"] += 1
                elif result.get("verification_status") == "possible_mismatch":
                    counts["Abstracts found with possible mismatch"] += 1
                else:
                    counts["Abstracts found needing review"] += 1
            elif result is not None and status == "no_abstract_found":
                counts["Links with no abstract found"] += 1
            elif result is not None:
                counts["Abstract fetch failures"] += 1
        frame.at[index, "From Abstract Cache"] = from_cache
        frame.at[index, "Abstract Fetch Status"] = status
        if progress_callback:
            progress_callback(
                completed, total, index,
                result.get("review_tag", status) if result is not None else status)
    counts["Total records"] = total
    counts["Records with an abstract after processing"] = int(
        frame[abstract_column].map(clean_value).astype(bool).sum())
    return frame, counts, {"url_column": url_column, "doi_column": doi_column,
                           "abstract_column": abstract_column, "title_column": title_column,
                           "author_column": author_column, "year_column": year_column,
                           "tag_column": tag_column}
