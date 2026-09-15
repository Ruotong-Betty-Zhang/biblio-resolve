import os
import json
import tempfile
import unittest
from unittest import mock

import pandas as pd

try:
    from . import abstract_note_tools as tools
    from . import doi_lookup_lib as lookup_lib
    from . import lookup_core as core
except ImportError:
    import abstract_note_tools as tools
    import doi_lookup_lib as lookup_lib
    import lookup_core as core


class FakeResponse:
    def __init__(self, html, url="https://example.org/article", content_type="text/html; charset=utf-8"):
        self.status_code = 200
        self.headers = {"Content-Type": content_type}
        self.url = url
        self.text = html
        self.content = html.encode("utf-8")
        self.encoding = "utf-8"

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, html, url="https://example.org/article", content_type="text/html; charset=utf-8"):
        self.response = FakeResponse(html, url=url, content_type=content_type)

    def get(self, *_args, **_kwargs):
        return self.response


class FakeJsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class AbstractNoteToolsTests(unittest.TestCase):
    def test_note_url_fills_only_records_without_existing_link_or_doi(self):
        frame = pd.DataFrame([
            {"Title": "A", "Notes": "See https://example.org/a", "Url": "", "DOI": ""},
            {"Title": "B", "Notes": "See https://example.org/note-b", "Url": "https://example.org/original", "DOI": ""},
            {"Title": "C", "Notes": "See https://example.org/note-c", "Url": "", "DOI": "10.1234/c"},
            {"Title": "D", "Notes": "No URL here", "Url": "", "DOI": ""},
        ])
        result, stats, columns = tools.analyze_notes_and_add_links(frame)
        self.assertEqual(result.loc[0, columns["url_column"]], "https://example.org/a")
        self.assertEqual(result.loc[1, columns["url_column"]], "https://example.org/original")
        self.assertEqual(result.loc[2, columns["url_column"]], "")
        self.assertEqual(
            stats["Records without a link before recovery that gained a link from Note"], 1)
        self.assertEqual(stats["Records without a link whose Note contains a URL"], 1)
        self.assertEqual(stats["Records with a link after Note recovery"], 3)

    def test_note_url_keeps_balanced_doi_parentheses(self):
        note = "DOI: https://doi.org/10.2990/1471-5457(2004)23[55:BADIE]2.0.CO;2."
        self.assertEqual(
            tools.extract_urls_from_note(note),
            ["https://doi.org/10.2990/1471-5457(2004)23[55:BADIE]2.0.CO;2"])

    def test_extracts_html_citation_abstract(self):
        html = '<html><head><meta name="citation_abstract" content="This is a sufficiently long abstract for metadata extraction and testing."></head></html>'
        abstract, source = tools.extract_abstract_from_html(html)
        self.assertIn("sufficiently long abstract", abstract)
        self.assertEqual(source, "HTML citation metadata")

    def test_extracts_json_ld_abstract(self):
        html = '<script type="application/ld+json">{"@type":"ScholarlyArticle","abstract":"A long JSON-LD abstract that contains enough text to be accepted by the extractor."}</script>'
        abstract, source = tools.extract_abstract_from_html(html)
        self.assertIn("JSON-LD abstract", abstract)
        self.assertEqual(source, "JSON-LD")

    def test_html_meta_without_identity_attributes_does_not_crash(self):
        html = ('<html><head><meta charset="utf-8">'
                '<meta name="citation_abstract" content="This is a sufficiently long abstract '
                'that remains extractable after a metadata tag without identity attributes.">'
                '</head></html>')

        abstract, source = tools.extract_abstract_from_html(html)

        self.assertIn("sufficiently long abstract", abstract)
        self.assertEqual(source, "HTML citation metadata")

    def test_fetch_abstract_tolerates_missing_response_url_and_content_type(self):
        html = ('<meta name="citation_abstract" content="This abstract is long enough to '
                'confirm that missing response metadata does not abort processing.">')
        result = tools.fetch_abstract(
            "https://example.org/fallback", session=FakeSession(html, url=None, content_type=None))

        self.assertEqual(result["status"], "abstract_found")
        self.assertEqual(result["url"], "https://example.org/fallback")

    def test_fetch_abstract_turns_unexpected_page_error_into_record_failure(self):
        class BrokenSession:
            def get(self, *_args, **_kwargs):
                raise AttributeError("broken response metadata")

        result = tools.fetch_abstract("https://example.org/broken", session=BrokenSession())

        self.assertEqual(result["status"], "parse_failed")
        self.assertIn("broken response metadata", result["error"])

    def test_source_abstract_is_used_before_webpage_fallback(self):
        class WebpageMustNotBeCalled:
            def get(self, *_args, **_kwargs):
                raise AssertionError("webpage fallback should not run")

        frame = pd.DataFrame([{
            "Title": "Example article", "Author": "Smith, Jane", "Publication Year": "2020",
            "Url": "https://example.org/article", "DOI": "10.1234/example", "Abstract": "",
        }])
        source_result = {
            "abstract": "This source-provided abstract is long enough to be accepted without opening the webpage.",
            "source": "Crossref API", "url": "https://doi.org/10.1234/example",
            "verification_status": "matched", "review_tag": "abstract_match_confirmed",
            "metadata_title": "Example article", "metadata_authors": ["Smith"],
            "metadata_year": 2020, "metadata_dois": ["10.1234/example"],
            "failed_sources": [],
        }

        result, stats, _columns = tools.find_abstracts(
            frame, source_lookup=lambda **_record: source_result,
            session=WebpageMustNotBeCalled(), request_delay=0)

        self.assertEqual(result.loc[0, "Abstract Fetch Status"], "abstract_found_source")
        self.assertEqual(stats["Abstracts found from Sources"], 1)
        self.assertEqual(stats["Abstracts found from links"], 0)

    def test_source_lookup_can_find_abstract_when_record_has_no_link(self):
        frame = pd.DataFrame([{
            "Title": "Unlinked article", "Author": "Smith, Jane", "Publication Year": "2020",
            "Url": "", "DOI": "", "Abstract": "",
        }])
        source_result = {
            "abstract": "This abstract came from a selected metadata source despite the record having no link.",
            "source": "OpenAlex API", "url": "https://openalex.org/W1",
            "verification_status": "matched", "review_tag": "abstract_match_confirmed",
            "metadata_title": "Unlinked article", "metadata_authors": ["Smith"],
            "metadata_year": 2020, "metadata_dois": [], "failed_sources": [],
        }

        result, stats, _columns = tools.find_abstracts(
            frame, source_lookup=lambda **_record: source_result, request_delay=0)

        self.assertEqual(result.loc[0, "Abstract Fetch Status"], "abstract_found_source")
        self.assertEqual(stats["Abstracts found from Sources"], 1)
        self.assertEqual(stats["Records skipped because no link is available"], 0)

    def test_abstract_cache_resumes_without_calling_sources_or_webpage(self):
        frame = pd.DataFrame([{
            "Title": "Cached article", "Author": "Smith, Jane", "Publication Year": "2020",
            "Url": "https://example.org/article", "DOI": "10.1234/cached", "Abstract": "",
        }])
        cached_result = {
            "abstract": "This abstract was restored from the persistent cache without another request.",
            "source": "Crossref API", "url": "https://doi.org/10.1234/cached",
            "status": "abstract_found_source", "verification_status": "matched",
            "review_tag": "abstract_match_confirmed", "failed_sources": [],
        }

        def must_not_run(**_record):
            raise AssertionError("source lookup should not run after a cache hit")

        result, stats, _columns = tools.find_abstracts(
            frame, source_lookup=must_not_run,
            cache_lookup=lambda **_record: {
                "result": cached_result, "result_origin": "source",
                "status": "abstract_found_source", "source_errors": [],
            }, request_delay=0)

        self.assertIn("persistent cache", result.loc[0, "Abstract"])
        self.assertTrue(result.loc[0, "From Abstract Cache"])
        self.assertEqual(stats["Records resumed from Abstract cache"], 1)
        self.assertEqual(stats["Abstracts found from Sources"], 1)

    def test_abstract_cache_retries_expired_failures(self):
        key = "record"
        cache = {key: {
            "saved_at": 1_000,
            "payload": {"status": "fetch_failed", "result": {"status": "fetch_failed"}},
        }}
        with mock.patch.object(core.time, "time", return_value=1_000 + 25 * 3600):
            self.assertIsNone(core.cached_abstract(cache, key))

    def test_abstract_cache_checkpoints_and_loads_each_record(self):
        payload = {"status": "abstract_found_source", "result": {
            "abstract": "A persisted abstract long enough to represent a completed lookup."
        }}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "abstract_cache.jsonl")
            with mock.patch.object(core, "ABSTRACT_CACHE_FILE", path):
                core.append_abstract_cache_entry("one", payload, saved_at=1_000)
                loaded = core.load_abstract_cache()

        self.assertEqual(loaded["one"]["payload"], payload)

    def test_core_source_lookup_requires_a_strong_bibliographic_match(self):
        candidate = {
            "source": "crossref", "title": "Example article", "authors": ["Smith"],
            "year": 2020, "doi": "10.1234/example", "url": "https://doi.org/10.1234/example",
            "abstract": "This source abstract is sufficiently long to pass the acceptance requirement.",
        }
        with mock.patch.object(core, "_call_source", return_value=([candidate], None)):
            result = core.lookup_abstract_from_sources(
                "Example article", "Smith, Jane", "2020", "10.1234/example", "",
                ["crossref"])

        self.assertIn("sufficiently long", result["abstract"])
        self.assertEqual(result["source"], "Crossref API")

    def test_crossref_candidates_include_source_abstract(self):
        payload = {"message": {"items": [{
            "title": ["Example article"], "DOI": "10.1234/example",
            "author": [{"family": "Smith"}], "issued": {"date-parts": [[2020]]},
            "abstract": "<jats:p>This Crossref abstract is sufficiently long for source-first retrieval.</jats:p>",
        }]}}
        with mock.patch.object(
                lookup_lib, "_get_with_retry", return_value=FakeJsonResponse(payload)):
            candidates, error = lookup_lib.query_crossref("Example article", "Smith", rows=1)

        self.assertIsNone(error)
        self.assertIn("Crossref abstract", candidates[0]["abstract"])

    def test_openalex_queries_title_only_and_reconstructs_abstract(self):
        payload = {"results": [{
            "display_name": "Example article", "doi": "https://doi.org/10.1234/example",
            "authorships": [{"author": {"display_name": "Jane Smith"}}],
            "publication_year": 2020, "primary_location": {}, "open_access": {},
            "abstract_inverted_index": {"Example": [0], "abstract": [1], "text": [2]},
        }]}
        with mock.patch.object(
                lookup_lib, "_get_with_retry", return_value=FakeJsonResponse(payload)) as request:
            candidates, error = lookup_lib.query_openalex("Example article", "Smith", rows=1)

        self.assertIsNone(error)
        self.assertEqual(request.call_args.kwargs["params"]["search"], "Example article")
        self.assertEqual(candidates[0]["abstract"], "Example abstract text")

    def test_matching_abstract_is_saved_and_tagged_confirmed(self):
        html = """<html><head>
<meta name="citation_title" content="Attitudes toward income inequality">
<meta name="citation_author" content="Jonathan Kelley">
<meta name="citation_publication_date" content="2001-04-01">
<meta name="citation_doi" content="10.1234/example">
<meta name="citation_abstract" content="This abstract is long enough to be retained and belongs to the matching article record.">
</head></html>"""
        frame = pd.DataFrame([{
            "Title": "Attitudes toward income inequality", "Author": "Kelley, Jonathan",
            "Publication Year": "2001", "DOI": "10.1234/example", "Url": "",
            "Abstract Note": "",
        }])
        result, stats, _columns = tools.find_abstracts(
            frame, session=FakeSession(html), request_delay=0)

        self.assertIn("long enough", result.loc[0, "Abstract Note"])
        self.assertEqual(result.loc[0, "Abstract Fetch Status"], "abstract_found")
        self.assertEqual(result.loc[0, "Abstract Match Status"], "matched")
        self.assertEqual(result.loc[0, "Abstract Review Tag"], "abstract_match_confirmed")
        self.assertEqual(result.loc[0, "Tags"], "")
        self.assertEqual(stats["Abstract matches confirmed"], 1)

    def test_mismatching_abstract_is_still_saved_with_dedicated_tag(self):
        html = """<html><head>
<meta name="citation_title" content="A completely unrelated medical trial">
<meta name="citation_author" content="Alice Brown">
<meta name="citation_abstract" content="This abstract is deliberately long enough to be saved even though the article identity does not match.">
</head></html>"""
        frame = pd.DataFrame([{
            "Title": "Attitudes toward income inequality", "Author": "Kelley, Jonathan",
            "Url": "https://example.org/wrong", "DOI": "", "Abstract Note": "",
        }])
        result, stats, _columns = tools.find_abstracts(
            frame, session=FakeSession(html), request_delay=0)

        self.assertIn("be saved", result.loc[0, "Abstract Note"])
        self.assertEqual(result.loc[0, "Abstract Match Status"], "possible_mismatch")
        self.assertEqual(result.loc[0, "Abstract Review Tag"], "abstract_found_possible_mismatch")
        self.assertEqual(result.loc[0, "Tags"], "ABSTRACT_FOUND_POSSIBLE_MISMATCH")
        self.assertEqual(stats["Abstracts found with possible mismatch"], 1)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "mismatch.ris")
            core.write_records_file(result, path, "ris")
            loaded = core.read_records_file(path)
        self.assertIn("ABSTRACT_FOUND_POSSIBLE_MISMATCH", loaded.loc[0, "Keywords"])
        self.assertIn("abstract check", loaded.loc[0, "Notes"])
        self.assertIn("be saved", loaded.loc[0, "Abstract"])

    def test_abstract_without_identity_metadata_is_saved_for_review(self):
        html = ('<meta name="citation_abstract" content="This abstract is long enough to save, '
                'but the page exposes no title or author identity metadata.">')
        frame = pd.DataFrame([{
            "Title": "Attitudes toward income inequality", "Author": "Kelley, Jonathan",
            "Url": "https://example.org/no-metadata", "DOI": "", "Abstract Note": "",
        }])
        result, stats, _columns = tools.find_abstracts(
            frame, session=FakeSession(html), request_delay=0)

        self.assertIn("long enough", result.loc[0, "Abstract Note"])
        self.assertEqual(result.loc[0, "Abstract Match Status"], "insufficient_metadata")
        self.assertEqual(result.loc[0, "Abstract Review Tag"], "abstract_found_needs_review")
        self.assertEqual(result.loc[0, "Tags"], "ABSTRACT_FOUND_NEEDS_REVIEW")
        self.assertEqual(stats["Abstracts found needing review"], 1)

    def test_ris_round_trip_preserves_notes_and_abstract(self):
        frame = pd.DataFrame([{
            "Title": "Example", "Author": "Smith, Jane", "Url": "https://example.org",
            "Notes": "A note with https://example.org/extra", "Abstract Note": "A saved abstract.",
            "Tags": "IST; SURVEY DESIGN"
        }])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "records.ris")
            core.write_records_file(frame, path, "ris")
            loaded = core.read_records_file(path)
        self.assertIn("https://example.org/extra", loaded.loc[0, "Notes"])
        self.assertEqual(loaded.loc[0, "Abstract"], "A saved abstract.")
        self.assertEqual(loaded.loc[0, "Keywords"], "IST; SURVEY DESIGN")

    def test_ris_and_bibtex_restore_kept_verification_fields_for_manual_review(self):
        frame = pd.DataFrame([{
            "Title": "Example", "Author": "Smith, Jane", "Notes": "Original user note",
            "Verification Status": "verified_with_warning",
            "Verification Score": 94.5,
            "Verification Message": "Title and author matched.",
            "From Verification Cache": False,
        }])
        portable = [
            "Verification Status", "Verification Score", "Verification Message",
            "From Verification Cache",
        ]
        with tempfile.TemporaryDirectory() as directory:
            for fmt, extension in (("ris", ".ris"), ("bibtex", ".bib")):
                with self.subTest(fmt=fmt):
                    path = os.path.join(directory, "verified" + extension)
                    core.write_records_file(frame, path, fmt, portable_columns=portable)
                    loaded = core.read_records_file(path)
                    self.assertEqual(loaded.loc[0, "Verification Status"], "verified_with_warning")
                    self.assertEqual(loaded.loc[0, "Verification Score"], "94.5")
                    self.assertEqual(loaded.loc[0, "From Verification Cache"], "False")
                    self.assertEqual(loaded.loc[0, "Notes"], "Original user note\nLiterature Lookup verification: verified_with_warning; score 94.5; Title and author matched.")

    def test_ris_reader_closes_empty_er_and_collects_kw_tags(self):
        content = (
            "TY  - JOUR\n"
            "TI  - First record\n"
            "KW  - FAMGEN\n"
            "KW - imported lowercase tag\n"
            "ER  - \n\n"
            "TY - JOUR\n"
            "TI - Second record\n"
            "KW - WORKORI\n"
            "ER -\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tags.ris")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(content)
            loaded = core.read_records_file(path)

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded.loc[0, "Keywords"], "FAMGEN; imported lowercase tag")
        self.assertEqual(loaded.loc[1, "Keywords"], "WORKORI")

    def test_bibtex_and_csl_json_preserve_tags(self):
        frame = pd.DataFrame([{
            "Key": "example", "Title": "Example", "Item Type": "journalArticle",
            "Tags": "IST; MODULE A"
        }])
        with tempfile.TemporaryDirectory() as directory:
            bib_path = os.path.join(directory, "records.bib")
            json_path = os.path.join(directory, "records.json")
            core.write_records_file(frame, bib_path, "bibtex")
            core.write_records_file(frame, json_path, "csl_json")
            bib = core.read_records_file(bib_path)
            csl = core.read_records_file(json_path)
            with open(json_path, "r", encoding="utf-8") as stream:
                exported = json.load(stream)
        self.assertEqual(bib.loc[0, "Keywords"], "IST; MODULE A")
        self.assertEqual(csl.loc[0, "Keywords"], "IST; MODULE A")
        self.assertEqual(exported[0]["keyword"], "IST; MODULE A")
        self.assertEqual(exported[0]["tags"], [{"tag": "IST"}, {"tag": "MODULE A"}])

    def test_bibtex_reads_double_braced_keywords(self):
        content = """@article{nennstiel_new_2026,
  title = {A New Puzzle of Equality? Gender Gaps in Gen Z's Gender Equality Attitudes in Europe},
  url = {https://sciety.org/articles/activity/10.31235/osf.io/6h9gd_v1},
  author = {Nennstiel, Richard and Siegert, Christina},
  date = {2026},
  keywords = {{FAMGEN}},
}
"""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "double-braced.bib")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(content)
            loaded = core.read_records_file(path)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded.loc[0, "Keywords"], "FAMGEN")

    def test_bibtex_removes_keyword_protection_braces(self):
        content = """@article{example,
  title = {Example},
  keywords = {{WORKORI}, ordinary keyword, {NATID}},
}
"""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "protected-keywords.bib")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(content)
            loaded = core.read_records_file(path)

        self.assertEqual(loaded.loc[0, "Keywords"], "WORKORI, ordinary keyword, NATID")

    def test_export_format_defaults_to_imported_file_type(self):
        expected = {
            "records.csv": "CSV table (.csv)",
            "records.xlsx": "Excel (.xlsx)",
            "records.xls": "Excel (.xlsx)",
            "records.ris": "RIS (.ris)",
            "records.bib": "BibTeX (.bib)",
            "records.bibtex": "BibTeX (.bib)",
            "records.json": "CSL JSON (.json)",
        }
        for filename, label in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(core.preferred_output_format_label(filename), label)

    def test_tsv_is_not_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "records.tsv")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("Title\tTags\nExample\tIST\n")
            with self.assertRaisesRegex(ValueError, "Unsupported file format"):
                core.read_records_file(path)

    def test_fetch_abstract_exposes_full_text_for_html(self):
        html = ('<html><body><p>This abstract is long enough to confirm that missing '
                'response metadata does not abort processing.</p><p>Methods: we used ZA7570 '
                'for the analysis.</p></body></html>')
        result = tools.fetch_abstract("https://example.org/page", session=FakeSession(html))
        self.assertIn("full_text", result)
        self.assertIn("ZA7570", result["full_text"])

    def test_fetch_abstract_full_text_key_present_on_every_failure_path(self):
        oversized = FakeSession("x" * 10, content_type="application/pdf")
        result = tools.fetch_abstract(
            "https://example.org/big.pdf", session=oversized, max_pdf_bytes=1)
        self.assertEqual(result["status"], "pdf_too_large")
        self.assertEqual(result["full_text"], "")

        class BrokenSession:
            def get(self, *_a, **_k):
                raise AttributeError("broken")

        result = tools.fetch_abstract("https://example.org/broken", session=BrokenSession())
        self.assertEqual(result["status"], "parse_failed")
        self.assertEqual(result["full_text"], "")

    def test_pdf_bundle_falls_back_to_ocr_when_no_text_layer(self):
        class FakePage:
            def extract_text(self):
                return ""

        class FakeReader:
            def __init__(self, *_args, **_kwargs):
                self.pages = [FakePage()]
                self.metadata = {}

        ocr_recovered = "Methods: we used ZA7570 for the analysis. " + ("filler text. " * 10)
        with mock.patch("pypdf.PdfReader", FakeReader), \
             mock.patch.object(core, "_pdf_text", return_value=(ocr_recovered, True, "ocr")):
            _abstract, _source, _identity, text = tools._pdf_bundle(b"fake pdf bytes")
        self.assertIn("ZA7570", text)

    def test_pdf_bundle_keeps_native_text_when_ocr_finds_nothing_better(self):
        class FakePage:
            def extract_text(self):
                return "Native text layer with plenty of content. " * 5

        class FakeReader:
            def __init__(self, *_args, **_kwargs):
                self.pages = [FakePage()]
                self.metadata = {}

        with mock.patch("pypdf.PdfReader", FakeReader), \
             mock.patch.object(core, "_pdf_text") as ocr_mock:
            _abstract, _source, _identity, text = tools._pdf_bundle(b"fake pdf bytes")
        ocr_mock.assert_not_called()  # native text was already long enough
        self.assertIn("Native text layer", text)

    def test_pdf_bundle_max_pages_extends_native_extraction(self):
        class FakePage:
            def __init__(self, label):
                self.label = label

            def extract_text(self):
                return f"Page {self.label} content. " * 5

        class FakeReader:
            def __init__(self, *_args, **_kwargs):
                self.pages = [FakePage(i) for i in range(10)]
                self.metadata = {}

        with mock.patch("pypdf.PdfReader", FakeReader):
            _abstract, _source, _identity, short_text = tools._pdf_bundle(b"x", max_pages=3)
            _abstract, _source, _identity, long_text = tools._pdf_bundle(b"x", max_pages=8)
        self.assertNotIn("Page 5", short_text)
        self.assertIn("Page 5", long_text)


if __name__ == "__main__":
    unittest.main()
