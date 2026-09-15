import os
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

import pandas as pd

try:
    from . import lookup_core as core
except ImportError:
    import lookup_core as core


class FakeResponse:
    def __init__(self, status_code=200, payload=None, url="https://doi.org/example", body=b"", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.url = url
        self.body = body
        self.headers = headers or {}
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 404:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=16384):
        yield self.body

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, *args, **kwargs):
        return next(self.responses)


class VerificationTests(unittest.TestCase):
    def test_doi_url_lookup_routes_only_the_matching_regional_source(self):
        enabled = [
            "crossref", "gesis", "openalex", "semantic_scholar", "core",
            "openaire", "datacite", "dnb", "hal", "cinii", "arxiv", "pubmed",
        ]
        expected_regional = {"de": "dnb", "fr": "hal", "ja": "cinii"}
        for language, regional_source in expected_regional.items():
            with self.subTest(language=language), patch.object(
                    core.lib, "detect_title_language", return_value=language):
                ordered, detected = core.ordered_lookup_sources("Example title", enabled)
                self.assertEqual(detected, language)
                self.assertEqual(ordered[0], regional_source)
                self.assertEqual(
                    [source for source in ordered if source in {"dnb", "hal", "cinii"}],
                    [regional_source],
                )
                self.assertEqual(ordered[-2:], ["arxiv", "pubmed"])

    def test_doi_url_lookup_skips_regional_sources_for_other_languages(self):
        enabled = ["crossref", "dnb", "hal", "cinii", "arxiv", "pubmed"]
        with patch.object(core.lib, "detect_title_language", return_value="en"):
            ordered, detected = core.ordered_lookup_sources("Example title", enabled)
        self.assertEqual(detected, "en")
        self.assertEqual(ordered, ["crossref", "arxiv", "pubmed"])

    def test_doi_url_cache_key_records_language_routing_version(self):
        key = core.cache_key("Example", "Author", "2020", ["crossref"])
        self.assertTrue(key.startswith(core.DOI_URL_LOOKUP_CACHE_VERSION + "|||"))

    def test_windows_1252_csv_is_detected_without_losing_punctuation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "windows-export.csv")
            content = "Title,Author\r\nSmart \u2013 punctuation,O\u2019Brien\r\n"
            with open(path, "wb") as stream:
                stream.write(content.encode("cp1252"))
            loaded = core.read_records_file(path)
        self.assertEqual(loaded.loc[0, "Title"], "Smart \u2013 punctuation")
        self.assertEqual(loaded.loc[0, "Author"], "O\u2019Brien")
        self.assertEqual(loaded.attrs.get("source_encoding"), "cp1252")

    def test_title_score_ignores_straight_curly_and_bookish_quotes(self):
        variants = [
            "\u2019Panelizing\u2019 Repeated Cross Sections",
            "\u201cPanelizing\u201d Repeated Cross Sections",
            "'Panelizing' Repeated Cross Sections",
            '"Panelizing" Repeated Cross Sections',
            "\u00abPanelizing\u00bb Repeated Cross Sections",
        ]
        normalized = {core.lib.normalize_for_compare(value) for value in variants}
        self.assertEqual(normalized, {"panelizing repeated cross sections"})
        self.assertEqual(core.lib.fuzz.token_sort_ratio(
            core.lib.normalize_for_compare(variants[0]),
            core.lib.normalize_for_compare(variants[1])), 100.0)

    def test_tag_cleanup_keeps_only_tags_with_all_uppercase_letters(self):
        cleaned, kept, removed = core.clean_uppercase_tags(
            "IST; SURVEY DESIGN; automatic tag; MixedCase; COVID-19; 2024")
        self.assertEqual(cleaned, "IST; SURVEY DESIGN; COVID-19")
        self.assertEqual(kept, ["IST", "SURVEY DESIGN", "COVID-19"])
        self.assertEqual(removed, ["automatic tag", "MixedCase", "2024"])

    def test_tag_cleanup_supports_newline_separator(self):
        cleaned, kept, removed = core.clean_uppercase_tags("IST\nimported\nMODULE A", "New line")
        self.assertEqual(cleaned, "IST\nMODULE A")
        self.assertEqual(kept, ["IST", "MODULE A"])
        self.assertEqual(removed, ["imported"])

    def test_author_matches_original_or_standardized_diacritic_and_hyphen_forms(self):
        target = ["Est\u00e9vez\u2010Abe"]
        candidate = ["Estevez-Abe"]
        bonus, match_type, _ = core.lib._author_bonus(target, candidate)
        self.assertEqual(bonus, 15)
        self.assertEqual(match_type, "normalized_exact")
        self.assertNotIn("authors", core._identity_conflicts(
            "Est\u00e9vez\u2010Abe, Margarita", 2015,
            {"authors": candidate, "year": 2015, "publication_years": [2015]}))

    def test_author_preserves_exact_accented_match(self):
        bonus, match_type, _ = core.lib._author_bonus(["Br\u00e9chon"], ["Br\u00e9chon"])
        self.assertEqual(bonus, 15)
        self.assertEqual(match_type, "exact")

    def test_registered_matching_doi_is_verified(self):
        payload = {"message": {
            "title": ["Attitudes toward income inequality"],
            "author": [{"family": "Kelley"}],
            "issued": {"date-parts": [[2001]]},
            "DOI": "10.1234/example",
            "URL": "https://doi.org/10.1234/example",
        }}
        result = core.verify_reference(
            "Attitudes toward income inequality", "Kelley", "2001",
            "10.1234/example", "https://doi.org/10.1234/example",
            session=FakeSession([FakeResponse(payload=payload)]),
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "verified")

    def test_registered_wrong_paper_is_rejected(self):
        payload = {"message": {
            "title": ["A completely unrelated chemistry paper"],
            "author": [{"family": "Other"}],
            "issued": {"date-parts": [[1990]]},
        }}
        result = core.verify_reference(
            "Attitudes toward income inequality", "Kelley", "2001",
            "10.1234/example", session=FakeSession([FakeResponse(payload=payload)]),
        )
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "mismatch")
        self.assertIn("Authors", result["message"])
        self.assertIn("Publication year", result["message"])

    def test_secondary_metadata_conflict_becomes_verified_warning(self):
        payload = {"message": {
            "title": ["A useful paper"], "author": [{"family": "Smith"}],
            "issued": {"date-parts": [[2024]]}, "container-title": ["Correct Journal"],
            "ISSN": ["1234-5678"], "DOI": "10.1234/example"}}
        result = core.verify_reference(
            "A useful paper", "Smith", "2024", "10.1234/example",
            session=FakeSession([FakeResponse(payload=payload)]), issn="9999-9999")
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "verified_with_warning")
        self.assertEqual(result["metadata_warnings"], ["issn"])
        self.assertTrue(result["core_identity_match"])
        self.assertFalse(result["secondary_metadata_match"])
        self.assertIn("ISSN", result["message"])

    def test_equivalent_item_type_names_are_accepted(self):
        payload = {"message": {
            "title": ["A useful paper"], "author": [{"family": "Smith"}],
            "type": "journal-article", "DOI": "10.1234/type-equivalent"}}
        result = core.verify_reference(
            "A useful paper", "Smith", doi="10.1234/type-equivalent", item_type="JOUR",
            session=FakeSession([FakeResponse(payload=payload)]))
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("item_type", result["metadata_conflicts"])

    def test_different_item_type_is_a_verified_warning(self):
        payload = {"message": {
            "title": ["A useful paper"], "author": [{"family": "Smith"}],
            "type": "journal-article", "DOI": "10.1234/type-conflict"}}
        result = core.verify_reference(
            "A useful paper", "Smith", doi="10.1234/type-conflict", item_type="CHAP",
            session=FakeSession([FakeResponse(payload=payload)]))
        self.assertTrue(result["verified"])
        self.assertTrue(result["paper_match"])
        self.assertEqual(result["status"], "verified_with_warning")
        self.assertIn("item_type", result["metadata_warnings"])
        self.assertIn("item type", result["message"].lower())

    def test_book_chapter_full_and_short_container_titles_match(self):
        payload = {"message": {
            "title": ["The Past, Present, and Future of Statistical Weights in International Survey Projects"],
            "subtitle": ["Implications for Survey Data Harmonization"],
            "author": [{"family": "Zieliński"}, {"family": "Powałko"}, {"family": "Kołczyńska"}],
            "published-print": {"date-parts": [[2018]]}, "type": "book-chapter",
            "container-title": ["Advances in Comparative Survey Methods"],
            "ISBN": ["9781118884980"], "DOI": "10.1002/9781118884997.ch47"}}
        result = core.verify_reference(
            "The Past, Present, and Future of Statistical Weights in International Survey Projects: Implications for Survey Data Harmonization",
            "Zieliński, Marcin W.; Powałko, Przemek; Kołczyńska, Marta", "2019",
            "10.1002/9781118884997.ch47", session=FakeSession([FakeResponse(payload=payload)]),
            item_type="CHAP",
            journal="Advances in Comparative Survey Methods : Multinational, Multiregional, and Multicultural Contexts (3MC)",
            isbn="978-1-118-88498-0")
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metadata_warnings"], [])

    def test_crossref_main_title_and_subtitle_are_combined(self):
        payload = {"message": {
            "title": ["The Vicious Circle"],
            "subtitle": ["Does disappointment with political authorities contribute to political passivity in Latvia?"],
            "author": [{"family": "Mieriņa"}],
            "published-print": {"date-parts": [[2014]]},
            "published-online": {"date-parts": [[2012]]},
            "DOI": "10.1080/14616696.2012.749414",
            "URL": "https://doi.org/10.1080/14616696.2012.749414",
            "container-title": ["European Societies"], "volume": "16", "issue": "4", "page": "615-637",
        }}
        result = core.verify_reference(
            "The Vicious Circle: Does disappointment with political authorities contribute to political passivity in Latvia?",
            "Mierina, Inta", "2014", "10.1080/14616696.2012.749414",
            session=FakeSession([FakeResponse(payload=payload)]),
            journal="European Societies", volume="16", issue="4", pages="615-637")
        self.assertTrue(result["verified"])
        self.assertEqual(result["title_match_variant"], "combined_title")
        self.assertEqual(result["author_match_type"], "normalized_exact")
        self.assertEqual(result["title_score"], 100)
        self.assertEqual(result["publication_years"], [2014, 2012])

    def test_unrelated_subtitle_cannot_reduce_main_title_match(self):
        payload = {"message": {
            "title": ["A useful paper"],
            "subtitle": ["A long subtitle absent from the imported record"],
            "author": [{"family": "Smith"}], "issued": {"date-parts": [[2024]]},
            "DOI": "10.1234/main-title", "URL": "https://doi.org/10.1234/main-title"}}
        result = core.verify_reference(
            "A useful paper", "Smith", "2024", "10.1234/main-title",
            session=FakeSession([FakeResponse(payload=payload)]))
        self.assertTrue(result["verified"])
        self.assertEqual(result["title_match_variant"], "main_title")
        self.assertEqual(result["title_score"], 100)

    def test_semicolon_separated_parallel_title_uses_best_language(self):
        payload = {"message": {
            "title": ["Le renouveau religieux des immigrés et de leurs descendants en France"],
            "author": [{"family": "Lagrange"}], "issued": {"date-parts": [[2014]]},
            "type": "journal-article", "DOI": "10.3917/rfs.552.0201"}}
        result = core.verify_reference(
            "The religious revival among immigrants and their descendants in France; "
            "Le renouveau religieux des immigrés et de leurs descendants en France",
            "Lagrange, Hugues", doi="10.3917/rfs.552.0201",
            session=FakeSession([FakeResponse(payload=payload)]), item_type="JOUR")
        self.assertTrue(result["verified"])
        self.assertEqual(result["title_score"], 100)
        self.assertEqual(result["title_match_variant"], "parallel_title_2_vs_main_title")

    @patch.object(core, "_safe_get")
    def test_declared_english_page_can_verify_different_language_registry_title(self, safe_get):
        payload = {"message": {
            "title": ["Un titre français différent"], "author": [{"family": "Lagrange"}],
            "type": "journal-article", "DOI": "10.1234/multilingual"}}
        landing = FakeResponse(
            status_code=200, url="https://publisher.example/article?lang=fr",
            body=b'<html><body><a href="?lang=en">EN</a></body></html>',
            headers={"Content-Type": "text/html"})
        english = FakeResponse(
            status_code=200, url="https://publisher.example/article?lang=en",
            body=(b'<html><head><meta name="citation_author" content="Lagrange"></head>'
                  b'<body>The Religious Revival Among Immigrants and Their Descendants in France</body></html>'),
            headers={"Content-Type": "text/html"})
        safe_get.side_effect = [landing, english]
        result = core.verify_reference(
            "The Religious Revival Among Immigrants and Their Descendants in France",
            "Lagrange, Hugues", doi="10.1234/multilingual",
            session=FakeSession([FakeResponse(payload=payload)]), item_type="JOUR")
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "verified_via_landing_page")
        self.assertEqual(result["alternate_language_url"],
                         "https://publisher.example/article?lang=en")
        self.assertEqual(result["decision_rule"], "publisher_page_title_and_authors")

    @patch.object(core, "_safe_get")
    def test_registry_missing_doi_uses_jstage_english_landing_page(self, safe_get):
        japanese_url = "https://www.jstage.jst.go.jp/article/bunken/70/11/70_36/_article/-char/ja/"
        english_url = "https://www.jstage.jst.go.jp/article/bunken/70/11/70_36/_article/-char/en/"
        japanese = FakeResponse(
            status_code=200, url=japanese_url,
            body="""<html><body><h1>世界を読み解く国際比較調査ISSP</h1>
                <p>村田 ひろ子</p><a href="javascript:;">English</a></body></html>""".encode("utf-8"),
            headers={"Content-Type": "text/html;charset=utf-8"})
        english = FakeResponse(
            status_code=200, url=english_url,
            body=b"""<html><head><meta name="citation_doi" content="10.24634/bunken.70.11_36"></head>
                <body><h1>The International Social Survey Programme (ISSP) Analyzing the World
                with Its Cross-National Surveys: The Significance and the Challenges</h1>
                <p>Hiroko Murata</p></body></html>""",
            headers={"Content-Type": "text/html;charset=utf-8"})
        safe_get.side_effect = [japanese, english]
        result = core.verify_reference(
            "The International Social Survey Programme (ISSP) Analyzing the World with Its "
            "Cross-National Surveys: The Significance and the Challenges",
            "Murata, Hiroko", "2020", doi="10.24634/bunken.70.11_36",
            url="https://doi.org/10.24634/bunken.70.11_36",
            session=FakeSession([FakeResponse(status_code=404), FakeResponse(status_code=404)]))
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "verified_via_landing_page")
        self.assertEqual(result["identifier_match_type"], "doi_resolution_page")
        self.assertEqual(result["decision_rule"], "doi_resolution_publisher_page")
        self.assertEqual(result["alternate_language_url"], english_url)
        self.assertEqual(result["resolved_url"], english_url)

    def test_print_year_prevents_online_first_false_conflict(self):
        payload = {"message": {
            "title": ["Example online first paper"], "author": [{"family": "Smith"}],
            "published-print": {"date-parts": [[2013]]},
            "published-online": {"date-parts": [[2011]]}, "DOI": "10.1234/online-first"}}
        result = core.verify_reference(
            "Example online first paper", "Smith", "2013", "10.1234/online-first",
            session=FakeSession([FakeResponse(payload=payload)]))
        self.assertTrue(result["verified"])
        self.assertNotIn("year", result["metadata_conflicts"])
        self.assertEqual(result["year_difference"], 0)

    def test_publisher_is_checked_when_supplied(self):
        payload = {"message": {
            "title": ["A useful paper"], "author": [{"family": "Smith"}],
            "publisher": "John Wiley & Sons, Ltd", "type": "journal-article",
            "DOI": "10.1234/publisher"}}
        matching = core.verify_reference(
            "A useful paper", "Smith", doi="10.1234/publisher", publisher="Wiley",
            item_type="journalArticle", session=FakeSession([FakeResponse(payload=payload)]))
        self.assertTrue(matching["verified"])
        self.assertEqual(matching["metadata_warnings"], [])
        self.assertEqual(matching["metadata_item_type"], "journal-article")
        self.assertEqual(matching["metadata_publisher"], "John Wiley & Sons, Ltd")
        self.assertEqual(matching["metadata_doi"], "10.1234/publisher")

        conflicting = core.verify_reference(
            "A useful paper", "Smith", doi="10.1234/publisher", publisher="Elsevier",
            item_type="journalArticle", session=FakeSession([FakeResponse(payload=payload)]))
        self.assertTrue(conflicting["verified"])
        self.assertEqual(conflicting["status"], "verified_with_warning")
        self.assertIn("publisher", conflicting["metadata_warnings"])

    def test_chapter_with_parent_book_doi_is_container_match(self):
        payload = {"message": {
            "title": ["Handbook of Attitudes, Volume 2: Applications"],
            "subtitle": ["2nd Edition"],
            "author": [{"given": "Dolores", "family": "Albarracin"}],
            "publisher": "Routledge", "type": "book",
            "issued": {"date-parts": [[2018]]},
            "ISBN": ["9781315178080"], "DOI": "10.4324/9781315178080"}}
        result = core.verify_reference(
            "The Role of Attitudes in Migration",
            "Esses, Victoria M.; Hamilton, Leah K.; Gaucher, Danielle", "",
            "10.4324/9781315178080", session=FakeSession([FakeResponse(payload=payload)]),
            item_type="CHAP", publisher="Routledge",
            container_title_hint="Handbook of Attitudes, Volume 2: Applications")
        self.assertFalse(result["verified"])
        self.assertFalse(result["paper_match"])
        self.assertTrue(result["container_match"])
        self.assertEqual(result["status"], "container_match")
        self.assertEqual(result["verification_level"], "container_match")
        self.assertEqual(result["decision_rule"], "parent_book_identifier")
        self.assertIn("parent book", result["message"])

    def test_unrelated_book_doi_is_not_container_match(self):
        payload = {"message": {
            "title": ["An Unrelated Handbook"], "author": [{"family": "Other"}],
            "type": "book", "DOI": "10.1234/unrelated-book"}}
        result = core.verify_reference(
            "A chapter", "Chapter Author", doi="10.1234/unrelated-book",
            session=FakeSession([FakeResponse(payload=payload)]), item_type="CHAP",
            container_title_hint="The Correct Parent Book")
        self.assertFalse(result["container_match"])
        self.assertEqual(result["status"], "mismatch")

    @patch.object(core, "_safe_get")
    def test_parent_book_page_can_verify_chapter_relationship(self, safe_get):
        payload = {"message": {
            "title": ["Handbook of Attitudes, Volume 2: Applications"],
            "author": [{"family": "Albarracin"}], "publisher": "Routledge",
            "type": "book", "DOI": "10.4324/9781315178080"}}
        html = b"""<html><body><h2>The Role of Attitudes in Migration</h2>
            <p>Victoria M. Esses, Leah K. Hamilton, and Danielle Gaucher</p></body></html>"""
        safe_get.return_value = FakeResponse(
            status_code=200, url="https://publisher.example/book", body=html,
            headers={"Content-Type": "text/html; charset=utf-8"})
        result = core.verify_reference(
            "The Role of Attitudes in Migration",
            "Esses, Victoria M.; Hamilton, Leah K.; Gaucher, Danielle", doi="10.4324/9781315178080",
            session=FakeSession([FakeResponse(payload=payload)]), item_type="CHAP",
            container_title_hint="Handbook of Attitudes, Volume 2: Applications")
        self.assertTrue(result["verified"])
        self.assertTrue(result["paper_match"])
        self.assertTrue(result["container_match"])
        self.assertEqual(result["status"], "verified_via_container_page")
        self.assertEqual(result["identifier_match_type"], "parent_book_doi")
        self.assertEqual(result["container_page_matched_authors"], ["esses", "hamilton", "gaucher"])

    @patch.object(core, "_safe_get")
    def test_parent_book_page_without_author_evidence_stays_container_match(self, safe_get):
        payload = {"message": {
            "title": ["Handbook of Attitudes, Volume 2: Applications"],
            "author": [{"family": "Albarracin"}], "type": "book",
            "DOI": "10.4324/9781315178080"}}
        html = b"<html><body>The Role of Attitudes in Migration</body></html>"
        safe_get.return_value = FakeResponse(
            status_code=200, url="https://publisher.example/book", body=html,
            headers={"Content-Type": "text/html"})
        result = core.verify_reference(
            "The Role of Attitudes in Migration",
            "Esses, Victoria M.; Hamilton, Leah K.; Gaucher, Danielle", doi="10.4324/9781315178080",
            session=FakeSession([FakeResponse(payload=payload)]), item_type="CHAP",
            container_title_hint="Handbook of Attitudes, Volume 2: Applications")
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "container_match")
        self.assertTrue(result["container_page_checked"])
        self.assertTrue(result["container_page_title_found"])
        self.assertEqual(result["container_page_matched_authors"], [])

    @patch.object(core, "_safe_get")
    def test_blocked_parent_book_page_stays_container_match(self, safe_get):
        payload = {"message": {
            "title": ["The Correct Parent Book"], "author": [{"family": "Editor"}],
            "type": "book", "DOI": "10.1234/parent-book"}}
        safe_get.return_value = FakeResponse(
            status_code=403, url="https://publisher.example/book",
            headers={"Content-Type": "text/html"})
        result = core.verify_reference(
            "A Chapter", "Chapter Author", doi="10.1234/parent-book",
            session=FakeSession([FakeResponse(payload=payload)]), item_type="CHAP",
            container_title_hint="The Correct Parent Book")
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "container_match")
        self.assertEqual(result["access_status"], "access_blocked")
        self.assertFalse(result["container_page_checked"])

    def test_exact_doi_can_accept_truncated_main_title_with_supporting_metadata(self):
        payload = {"message": {
            "title": ["The Vicious Circle"], "author": [{"family": "Mieriņa"}],
            "published-print": {"date-parts": [[2014]]},
            "DOI": "10.1080/14616696.2012.749414", "container-title": ["European Societies"]}}
        result = core.verify_reference(
            "The Vicious Circle: Does disappointment with political authorities contribute to political passivity in Latvia?",
            "Mierina, Inta", "2014", "10.1080/14616696.2012.749414",
            session=FakeSession([FakeResponse(payload=payload)]), journal="European Societies")
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification_level"], "verified_partial_metadata")
        self.assertEqual(result["decision_rule"], "exact_doi_partial_title_author_year")

    def test_doi_and_doi_url_must_agree(self):
        result = core.verify_reference(
            "Any title", doi="10.1234/one", url="https://doi.org/10.1234/two",
            session=FakeSession([]),
        )
        self.assertEqual(result["status"], "mismatch")

    @patch.object(core, "_public_http_url", return_value=(True, ""))
    def test_citation_metadata_can_verify_plain_url(self, _allowed):
        html = b'''<html><head>
          <meta name="citation_title" content="Attitudes toward income inequality">
          <meta name="citation_author" content="Kelley">
          <meta name="citation_publication_date" content="2001">
        </head></html>'''
        response = FakeResponse(url="https://example.org/paper", body=html,
                                headers={"Content-Type": "text/html; charset=utf-8"})
        result = core.verify_reference(
            "Attitudes toward income inequality", "Kelley", "2001", url="https://example.org/paper",
            session=FakeSession([response]),
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["metadata_source"], "Web citation metadata")

    def test_private_url_is_rejected(self):
        with patch.object(core.socket, "getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 80))]):
            result = core.verify_reference("x", url="http://localhost/paper", session=FakeSession([]))
        self.assertEqual(result["status"], "invalid")

    @patch.object(core, "_call_source")
    def test_enabled_source_can_verify_when_registries_have_no_record(self, source_call):
        source_call.return_value = ([{"title": "Attitudes toward income inequality",
                                     "authors": ["Kelley"], "year": 2001,
                                     "doi": "10.1234/example",
                                     "url": "https://doi.org/10.1234/example",
                                     "source": "openalex"}], None)
        result = core.verify_reference(
            "Attitudes toward income inequality", "Kelley", "2001", doi="10.1234/example",
            session=FakeSession([FakeResponse(status_code=404), FakeResponse(status_code=404)]),
            enabled_sources=["openalex"],
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification_sources"], ["openalex"])

    @patch.object(core, "_call_source")
    def test_similar_paper_with_different_doi_is_not_verification(self, source_call):
        source_call.return_value = ([{"title": "Attitudes toward income inequality",
                                     "authors": ["Kelley"], "year": 2001,
                                     "doi": "10.9999/different",
                                     "url": "https://doi.org/10.9999/different",
                                     "source": "openalex"}], None)
        result = core.verify_reference(
            "Attitudes toward income inequality", "Kelley", "2001", doi="10.1234/example",
            session=FakeSession([FakeResponse(status_code=404), FakeResponse(status_code=404)]),
            enabled_sources=["openalex"],
        )
        self.assertFalse(result["verified"])

    @patch.object(core, "_call_source")
    def test_source_cross_check_stops_after_first_verified_match(self, source_call):
        source_call.return_value = ([{"title": "Attitudes toward income inequality",
                                     "authors": ["Kelley"], "year": 2001,
                                     "doi": "10.1234/example",
                                     "url": "https://doi.org/10.1234/example",
                                     "source": "openalex"}], None)
        result = core.verify_reference(
            "Attitudes toward income inequality", "Kelley", "2001", doi="10.1234/example",
            session=FakeSession([FakeResponse(status_code=404), FakeResponse(status_code=404)]),
            enabled_sources=["openalex", "semantic_scholar", "pubmed"],
        )
        self.assertTrue(result["verified"])
        self.assertEqual(source_call.call_count, 1)
        self.assertEqual(result["sources_checked"], ["openalex"])

    @patch.object(core.lib.requests, "get")
    def test_429_is_retried_with_shared_source_cooldown(self, request_get):
        limited = FakeResponse(status_code=429, headers={"Retry-After": "1"})
        success = FakeResponse(status_code=200)
        request_get.side_effect = [limited, success]
        limiter = MagicMock()
        with patch.dict(core.lib._SOURCE_RATE_LIMITERS, {"datacite": limiter}):
            response = core.lib._get_with_retry("datacite", "https://example.org", max_attempts=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_get.call_count, 2)
        limiter.defer.assert_called_once_with(1.0)

    @patch.object(core, "_public_http_url", return_value=(True, ""))
    @patch.object(core, "_pdf_text")
    def test_pdf_text_can_verify_a_plain_pdf_link(self, pdf_text, _allowed):
        pdf_text.return_value = (
            "Attitudes toward income inequality Kelley 2001 full paper text. " * 3, False, "native_text")
        response = FakeResponse(url="https://example.org/paper.pdf", body=b"%PDF-test",
                                headers={"Content-Type": "application/pdf"})
        result = core.verify_reference(
            "Attitudes toward income inequality", "Kelley", "2001",
            url="https://example.org/paper.pdf", session=FakeSession([response]))
        self.assertTrue(result["verified"])
        self.assertTrue(result["pdf_detected"])
        self.assertEqual(result["verification_level"], "verified_pdf_text")

    @patch.object(core, "_public_http_url", return_value=(True, ""))
    def test_403_is_access_blocked_not_a_broken_link(self, _allowed):
        result = core.verify_reference(
            "Paper", url="https://example.org/paper",
            session=FakeSession([FakeResponse(status_code=403)]))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["access_status"], "access_blocked")

    @patch.object(core, "_call_source")
    def test_high_confidence_requires_second_source(self, source_call):
        registry = {"message": {"title": ["Attitudes toward income inequality"],
                    "author": [{"family": "Kelley"}], "issued": {"date-parts": [[2001]]},
                    "DOI": "10.1234/example", "URL": "https://doi.org/10.1234/example"}}
        source_call.return_value = ([], None)
        result = core.verify_reference(
            "Attitudes toward income inequality", "Kelley", "2001", doi="10.1234/example",
            session=FakeSession([FakeResponse(payload=registry), FakeResponse()]),
            enabled_sources=["openalex"], mode="high_confidence")
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification_level"], "insufficient_independent_evidence")

    @patch.object(core, "_call_source")
    def test_high_confidence_accepts_independent_confirmation(self, source_call):
        registry = {"message": {"title": ["Attitudes toward income inequality"],
                    "author": [{"family": "Kelley"}], "issued": {"date-parts": [[2001]]},
                    "DOI": "10.1234/example", "URL": "https://doi.org/10.1234/example"}}
        source_call.return_value = ([{"title": "Attitudes toward income inequality",
            "authors": ["Kelley"], "year": 2001, "doi": "10.1234/example",
            "url": "https://doi.org/10.1234/example", "source": "openalex"}], None)
        result = core.verify_reference(
            "Attitudes toward income inequality", "Kelley", "2001", doi="10.1234/example",
            session=FakeSession([FakeResponse(payload=registry), FakeResponse()]),
            enabled_sources=["openalex"], mode="high_confidence")
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification_level"], "verified_high_confidence")


class ZoteroFormatTests(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame([{"Key": "smith2024", "Item Type": "journalArticle",
            "Title": "A useful paper", "Author": "Smith, Jane; Doe, John",
            "Publication Year": 2024, "Publication Title": "Test Journal",
            "DOI": "10.1234/test", "Link": "https://doi.org/10.1234/test"}])

    def _round_trip(self, fmt, extension):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "records" + extension)
            core.write_records_file(self.df, path, fmt)
            loaded = core.read_records_file(path)
        self.assertEqual(loaded.iloc[0]["Title"], "A useful paper")
        self.assertEqual(loaded.iloc[0]["DOI"], "10.1234/test")

    def test_ris_round_trip(self):
        self._round_trip("ris", ".ris")

    def test_bibtex_round_trip(self):
        self._round_trip("bibtex", ".bib")

    def test_csl_json_round_trip(self):
        self._round_trip("csl_json", ".json")

    def test_zotero_csv_uses_url_column(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "records.csv")
            core.write_records_file(self.df, path, "csv")
            loaded = pd.read_csv(path)
        self.assertEqual(loaded.iloc[0]["Url"], "https://doi.org/10.1234/test")


if __name__ == "__main__":
    unittest.main()
