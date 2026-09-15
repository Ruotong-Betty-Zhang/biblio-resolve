"""Tests for issp_module_tags.py.

Each test builds its own example text/dataframe in code; no live network
calls (DOI resolution is exercised with a fake resolver function) and no
real sentence-transformers model load (the semantic tier is exercised with
a fake embedding model / fake matcher, since downloading and running the
real model would make this suite slow and require internet on first run).

classify_record() returns a list of results (a record can have more than
one module tag), sorted best-confidence-first. `first()` below is a small
helper for tests that expect exactly one clean result.
"""

import unittest
from unittest import mock

import numpy as np
import pandas as pd

import issp_module_tags as tagger


class FakeEmbeddingModel:
    """Stands in for a real sentence-transformers model: a fixed lookup
    from exact text to a hand-picked vector, so similarity behaviour is
    deterministic and testable without downloading anything."""

    def __init__(self, mapping):
        self.mapping = mapping

    def encode(self, texts, normalize_embeddings=True, batch_size=64, show_progress_bar=False):
        array = np.array([self.mapping[text] for text in texts], dtype=float)
        if normalize_embeddings:
            norms = np.linalg.norm(array, axis=1, keepdims=True)
            norms[norms == 0] = 1
            array = array / norms
        return array


def tags_of(results):
    return sorted(item["tag"] for item in results if item["tag"])


def first(results):
    assert len(results) == 1, f"expected exactly one result, got {results}"
    return results[0]


class IsspModuleTagsTests(unittest.TestCase):

    # --- tier 1: ZA number ---------------------------------------------

    def test_za_number_is_high_confidence(self):
        result = first(tagger.classify_record("Data file ZA7570 Version 2.1.0."))
        self.assertEqual(result["tag"], "RELIG")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["method"], "za_number")

    def test_za_number_with_hyphen_and_space_variants_are_recognized(self):
        for text in ("ZA7570", "ZA-7570", "ZA 7570", "za7570"):
            with self.subTest(text=text):
                result = first(tagger.classify_record(text))
                self.assertEqual(result["tag"], "RELIG")

    def test_unknown_za_number_is_ignored(self):
        result = tagger.classify_record("See ZA99999 for details.")
        self.assertEqual(tags_of(result), [])

    def test_cumulated_study_za_number_maps_to_single_topic(self):
        result = first(tagger.classify_record("Trend analysis using ZA4747 (Role of Government I-IV)."))
        self.assertEqual(result["tag"], "ROG")

    def test_separate_country_za_number_maps_correctly(self):
        # ZA7774: Religion IV - ISSP 2018 (Estonia) - a late/standalone
        # country release, not part of the main Integrated file.
        result = first(tagger.classify_record("Estonian subsample, see ZA7774."))
        self.assertEqual(result["tag"], "RELIG")

    # --- tier 1: GESIS DOI ------------------------------------------------

    def test_gesis_doi_resolves_via_injected_resolver(self):
        text = "Data source: https://doi.org/10.4232/1.13629"

        def fake_resolver(doi):
            self.assertEqual(doi, "10.4232/1.13629")
            return "International Social Survey Programme: Religion IV - ISSP 2018"

        result = first(tagger.classify_record(text, doi_resolver=fake_resolver))
        self.assertEqual(result["tag"], "RELIG")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["method"], "gesis_doi")

    def test_doi_resolver_failure_falls_through_instead_of_crashing(self):
        text = "Data source: https://doi.org/10.4232/1.13629"

        def failing_resolver(doi):
            raise ConnectionError("network unavailable")

        result = tagger.classify_record(text, doi_resolver=failing_resolver)
        self.assertEqual(tags_of(result), [])

    def test_doi_resolver_not_provided_skips_tier_without_crashing(self):
        result = tagger.classify_record(
            "Data source: https://doi.org/10.4232/1.13629", doi_resolver=None)
        self.assertEqual(tags_of(result), [])

    # --- tier 1: exact module name --------------------------------------

    def test_exact_distinctive_name_is_high_confidence_standalone(self):
        result = first(tagger.classify_record(
            "This paper uses the Family and Changing Gender Roles dataset."))
        self.assertEqual(result["tag"], "FAMGEN")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["method"], "exact_module_name")

    def test_generic_name_alone_is_not_promoted_to_high(self):
        # "Religion" alone is ordinary vocabulary - must not be treated as
        # an exact-name hit without an ISSP mention nearby.
        result = tagger.classify_record(
            "This paper is broadly about religion in modern society.")
        self.assertEqual(tags_of(result), [])

    def test_generic_name_near_issp_is_high_confidence(self):
        result = first(tagger.classify_record(
            "This paper uses the ISSP Religion module for its analysis."))
        self.assertEqual(result["tag"], "RELIG")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["method"], "exact_module_name")

    def test_generic_name_near_spelled_out_issp_name_is_also_high_confidence(self):
        # Formal reports often spell out the name on first mention instead
        # of using the acronym - this must count just as much as "ISSP".
        result = first(tagger.classify_record(
            "This study uses the International Social Survey Programme "
            "Religion module for its analysis."))
        self.assertEqual(result["tag"], "RELIG")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["method"], "exact_module_name")

        result = first(tagger.classify_record(
            "This study uses the International Social Survey Program "
            "Religion module for its analysis."))  # American spelling
        self.assertEqual(result["tag"], "RELIG")

    def test_generic_name_far_from_issp_does_not_count(self):
        far_text = "ISSP data. " + ("filler word " * 40) + "This is about religion."
        result = tagger.classify_record(far_text)
        self.assertEqual(tags_of(result), [])

    # --- tier 2: scored keywords -----------------------------------------

    def test_keyword_match_is_medium_confidence(self):
        result = first(tagger.classify_record(
            "This study examines environmental attitude and pro-environmental behaviour "
            "across 30 countries using nationally representative survey data."))
        self.assertEqual(result["tag"], "ENV")
        self.assertEqual(result["confidence"], "medium")
        self.assertEqual(result["method"], "keyword")

    def test_single_incidental_keyword_is_not_enough_to_tag(self):
        # Only one WORKORI phrase ("job satisfaction") appears once - below
        # MIN_KEYWORD_SCORE, so this must not be tagged just because a
        # labour-economics paper happens to use one overlapping term.
        result = tagger.classify_record(
            "A macroeconomics paper about labor markets that briefly notes job "
            "satisfaction trends alongside wage growth.")
        self.assertEqual(tags_of(result), [])

    def test_two_modules_both_clearing_the_threshold_are_both_returned(self):
        # This used to be rejected as "ambiguous" - a paper can genuinely
        # use more than one module, so both should now come back.
        result = tagger.classify_record(
            "Compares job satisfaction and work commitment with income inequality and "
            "social stratification across countries.")
        self.assertEqual(tags_of(result), ["SOCINEQ", "WORKORI"])
        for item in result:
            self.assertEqual(item["confidence"], "medium")
            self.assertEqual(item["method"], "keyword")

    # --- tier 3: semantic similarity (always runs) ------------------------

    def test_semantic_tier_adds_a_module_even_when_keyword_tier_already_found_one(self):
        def fake_matcher(text):
            return ("ROG", 0.5)

        result = tagger.classify_record(
            "This study examines environmental attitude and pro-environmental behaviour.",
            embedding_matcher=fake_matcher)
        self.assertEqual(tags_of(result), ["ENV", "ROG"])
        by_tag = {item["tag"]: item for item in result}
        self.assertEqual(by_tag["ENV"]["confidence"], "medium")
        self.assertEqual(by_tag["ROG"]["confidence"], "low")
        self.assertEqual(by_tag["ROG"]["method"], "semantic_similarity")

    def test_semantic_tier_does_not_duplicate_a_module_already_found(self):
        calls = []

        def fake_matcher(text):
            calls.append(text)
            return ("ENV", 0.9)

        result = tagger.classify_record(
            "This study examines environmental attitude and pro-environmental behaviour.",
            embedding_matcher=fake_matcher)
        self.assertEqual(tags_of(result), ["ENV"])
        self.assertEqual(result[0]["confidence"], "medium")  # kept the better tier
        self.assertEqual(calls, ["This study examines environmental attitude and "
                                  "pro-environmental behaviour."])  # still invoked

    def test_semantic_tier_used_when_nothing_else_found(self):
        def fake_matcher(text):
            return ("ROG", 0.5)

        result = first(tagger.classify_record(
            "A paper with no ISSP mention, no exact name, and no matching keywords at all.",
            embedding_matcher=fake_matcher))
        self.assertEqual(result["tag"], "ROG")
        self.assertEqual(result["confidence"], "low")
        self.assertEqual(result["method"], "semantic_similarity")

    def test_semantic_matcher_failure_falls_through_to_no_evidence(self):
        def failing_matcher(text):
            raise RuntimeError("model not loaded")

        result = tagger.classify_record(
            "A paper with no ISSP mention, no exact name, and no matching keywords at all.",
            embedding_matcher=failing_matcher)
        self.assertEqual(tags_of(result), [])

    def test_semantic_module_matcher_picks_closest_topic_with_fake_model(self):
        tags = list(tagger.TOPIC_DESCRIPTIONS.keys())
        n = len(tags)
        identity = np.eye(n)
        mapping = {tagger.TOPIC_DESCRIPTIONS[tag]: identity[i] for i, tag in enumerate(tags)}
        env_index = tags.index("ENV")
        other_index = (env_index + 1) % n
        mapping["clearly about pollution and climate change"] = (
            identity[env_index] * 0.95 + identity[other_index] * 0.05)
        relig_index, natid_index = tags.index("RELIG"), tags.index("NATID")
        mapping["ambiguous between two topics"] = (
            identity[relig_index] * 0.5 + identity[natid_index] * 0.5)
        mapping["totally unrelated to any topic"] = np.full(n, 0.01)

        matcher = tagger.SemanticModuleMatcher(model=FakeEmbeddingModel(mapping))

        tag, _score = matcher.classify_one("clearly about pollution and climate change")
        self.assertEqual(tag, "ENV")

        tag, _score = matcher.classify_one("ambiguous between two topics")
        self.assertIsNone(tag)

        tag, _score = matcher.classify_one("totally unrelated to any topic")
        self.assertIsNone(tag)

    def test_build_semantic_matcher_returns_none_on_failure(self):
        # Simulate sentence-transformers not being installed / model load
        # failing - callers must get None back, never an exception.
        with mock.patch.object(tagger, "SemanticModuleMatcher", side_effect=RuntimeError("boom")):
            self.assertIsNone(tagger.build_semantic_matcher())

    # --- no evidence / disabled tiers -------------------------------------

    def test_no_evidence_at_all(self):
        result = tagger.classify_record("A completely unrelated paper about macroeconomics.")
        self.assertEqual(first(result)["tag"], None)
        self.assertEqual(first(result)["method"], "no_evidence")

    def test_empty_text_does_not_crash(self):
        self.assertEqual(first(tagger.classify_record(""))["tag"], None)
        self.assertEqual(first(tagger.classify_record(None))["tag"], None)

    def test_year_mentions_are_not_used(self):
        # The ISSP-year tier is disabled for now - "ISSP 2018" alone (no
        # ZA/DOI/exact-name/keyword evidence) must not produce a tag.
        result = tagger.classify_record("This paper uses ISSP 2018 data.")
        self.assertEqual(tags_of(result), [])

    def test_year_table_covers_recently_added_modules(self):
        # YEAR_TO_TAG is kept as reference data even though it is not wired
        # into classify_record() right now.
        self.assertEqual(tagger.YEAR_TO_TAG[2022], "FAMGEN")
        self.assertEqual(tagger.YEAR_TO_TAG[2023], "NATID")
        self.assertEqual(tagger.YEAR_TO_TAG[2024], "DIGSOC")
        self.assertNotIn(2025, tagger.YEAR_TO_TAG)
        self.assertNotIn(2026, tagger.YEAR_TO_TAG)

    # --- topic_from_title (used for DOI-resolved titles) ------------------

    def test_topic_from_title_matches_various_wordings(self):
        cases = [
            ("International Social Survey Programme: Religion IV - ISSP 2018", "RELIG"),
            ("International Social Survey Programme: Social Networks and Support Systems - ISSP 1986", "SOCNET"),
            ("International Social Survey Programme: Social Relations and Support Systems - ISSP 2001", "SOCNET"),
            ("International Social Survey Programme: Family and Changing Gender Roles IV - ISSP 2012", "FAMGEN"),
            ("International Social Survey Programme: Role of Government V - ISSP 2016", "ROG"),
        ]
        for title, expected_tag in cases:
            with self.subTest(title=title):
                self.assertEqual(tagger.topic_from_title(title), expected_tag)

    # --- replace_issp_module_tags ------------------------------------------

    def test_replace_issp_module_tags_preserves_other_tags(self):
        updated = tagger.replace_issp_module_tags("WORKORI; MYPROJECT; some-other-tag", ["RELIG"])
        tags = [t.strip() for t in updated.split(";")]
        self.assertIn("MYPROJECT", tags)
        self.assertIn("some-other-tag", tags)
        self.assertIn("RELIG", tags)
        self.assertNotIn("WORKORI", tags)

    def test_replace_issp_module_tags_handles_empty_existing_value(self):
        self.assertEqual(tagger.replace_issp_module_tags("", ["ENV"]), "ENV")
        self.assertEqual(tagger.replace_issp_module_tags(None, ["ENV"]), "ENV")

    def test_replace_issp_module_tags_adds_more_than_one(self):
        updated = tagger.replace_issp_module_tags("MYPROJECT", ["ENV", "ROG"])
        tags = [t.strip() for t in updated.split(";")]
        self.assertEqual(set(tags), {"MYPROJECT", "ENV", "ROG"})

    def test_replace_issp_module_tags_with_no_new_tags_only_strips_old_ones(self):
        updated = tagger.replace_issp_module_tags("RELIG; MYPROJECT", [])
        tags = [t.strip() for t in updated.split(";")]
        self.assertEqual(tags, ["MYPROJECT"])

    # --- tag_issp_modules (batch) -------------------------------------------

    def test_tag_issp_modules_batch_writes_expected_columns_and_merges_tags(self):
        df = pd.DataFrame({
            "Title": ["Paper A", "Paper B", "Paper C"],
            "Abstract": [
                "Uses ZA7570 dataset.",
                "A study of environmental attitude and pro-environmental behaviour.",
                "Unrelated macroeconomics paper.",
            ],
            "DOI": ["10.1/aaa", "10.2/bbb", "10.3/ccc"],
            "Keywords": ["EXISTINGTAG", "", "OTHERTAG"],
        })
        output_df, stats, tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], doi_column="DOI",
            tag_column="Keywords", use_network_doi_lookup=False, use_semantic_matching=False)

        self.assertEqual(tag_column, "Keywords")
        self.assertEqual(output_df.loc[0, "ISSP Module Tag"], "RELIG")
        self.assertEqual(output_df.loc[0, "ISSP Module Confidence"], "high")
        self.assertIn("RELIG", output_df.loc[0, "Keywords"])
        self.assertIn("EXISTINGTAG", output_df.loc[0, "Keywords"])

        self.assertEqual(output_df.loc[1, "ISSP Module Tag"], "ENV")
        self.assertEqual(output_df.loc[1, "ISSP Module Confidence"], "medium")

        self.assertEqual(output_df.loc[2, "ISSP Module Tag"], "")
        self.assertEqual(output_df.loc[2, "Keywords"], "OTHERTAG")

        self.assertEqual(stats["Total records"], 3)
        self.assertEqual(stats["high confidence"], 1)
        self.assertEqual(stats["medium confidence"], 1)
        self.assertEqual(stats["no match"], 1)

    def test_tag_issp_modules_records_multiple_tags_semicolon_separated(self):
        df = pd.DataFrame({
            "Title": ["Paper A"],
            "Abstract": [
                "Compares job satisfaction and work commitment with income inequality "
                "and social stratification across countries.",
            ],
        })
        output_df, stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], use_network_doi_lookup=False,
            use_semantic_matching=False)
        tags = set(output_df.loc[0, "ISSP Module Tag"].split("; "))
        self.assertEqual(tags, {"SOCINEQ", "WORKORI"})
        self.assertEqual(output_df.loc[0, "ISSP Module Confidence"], "medium; medium")
        self.assertEqual(stats["medium confidence"], 1)  # one record, best confidence

    def test_tag_issp_modules_guesses_tag_column_when_not_specified(self):
        df = pd.DataFrame({
            "Title": ["Paper A"],
            "Abstract": ["Uses ZA7570 dataset."],
            "Tags": [""],
        })
        output_df, _stats, tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], use_network_doi_lookup=False,
            use_semantic_matching=False)
        self.assertEqual(tag_column, "Tags")
        self.assertEqual(output_df.loc[0, "Tags"], "RELIG")

    def test_tag_issp_modules_creates_tag_column_when_none_found(self):
        df = pd.DataFrame({"Title": ["Paper A"], "Abstract": ["Uses ZA7570 dataset."]})
        output_df, _stats, tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], use_network_doi_lookup=False,
            use_semantic_matching=False)
        self.assertEqual(tag_column, "Manual Tags")
        self.assertEqual(output_df.loc[0, "Manual Tags"], "RELIG")

    def test_tag_issp_modules_respects_cancel_event(self):
        import threading
        df = pd.DataFrame({
            "Title": [f"Paper {i}" for i in range(5)],
            "Abstract": ["Uses ZA7570 dataset."] * 5,
        })
        cancel_event = threading.Event()
        cancel_event.set()
        output_df, stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], use_network_doi_lookup=False,
            use_semantic_matching=False, cancel_event=cancel_event)
        self.assertEqual(stats["Cancelled records"], 5)

    def test_tag_issp_modules_uses_injected_semantic_matcher_on_every_record(self):
        class FakeMatcher:
            def classify_batch(self, texts):
                return [("HLTH", 0.6) for _ in texts]

        df = pd.DataFrame({
            "Title": ["Paper A", "Paper B"],
            "Abstract": [
                "Uses ZA7570 dataset.",
                "An abstract with no ISSP mention or matching keywords at all.",
            ],
        })
        output_df, stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], use_network_doi_lookup=False,
            use_semantic_matching=True, semantic_matcher=FakeMatcher())

        # Paper A: za_number already found RELIG; semantic tier still runs
        # and adds its own HLTH suggestion alongside it.
        tags_a = set(output_df.loc[0, "ISSP Module Tag"].split("; "))
        self.assertEqual(tags_a, {"RELIG", "HLTH"})
        # Paper B: nothing else found anything, semantic tier is the only hit.
        self.assertEqual(output_df.loc[1, "ISSP Module Tag"], "HLTH")
        self.assertEqual(output_df.loc[1, "ISSP Module Method"], "semantic_similarity")
        self.assertEqual(stats["high confidence"], 1)  # paper A's best is still high
        self.assertEqual(stats["low confidence"], 1)   # paper B is low (semantic only)
        self.assertEqual(stats["no match"], 0)

    def test_tag_issp_modules_semantic_pass_skipped_when_disabled(self):
        class MatcherThatShouldNotBeCalled:
            def classify_batch(self, texts):
                raise AssertionError("semantic tier should not run when disabled")

        df = pd.DataFrame({
            "Title": ["Paper A"],
            "Abstract": ["An abstract with no ISSP mention or matching keywords at all."],
        })
        output_df, stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], use_network_doi_lookup=False,
            use_semantic_matching=False, semantic_matcher=MatcherThatShouldNotBeCalled())
        self.assertEqual(output_df.loc[0, "ISSP Module Method"], "no_evidence")
        self.assertEqual(stats["no match"], 1)

    # --- country/data-source tags -----------------------------------------

    def test_country_mentioned_near_data_context_is_extracted(self):
        result = tagger.extract_data_countries(
            "We use survey data from Germany and France collected in 2018.")
        self.assertEqual(result, {"Germany", "France"})

    def test_country_mentioned_near_spelled_out_issp_name_is_extracted(self):
        result = tagger.extract_data_countries(
            "This uses the International Social Survey Programme. Germany was included.")
        self.assertEqual(result, {"Germany"})

    def test_country_mentioned_without_data_context_is_not_extracted(self):
        # This is a literature-review-style citation of someone else's
        # study, not the author's own data source.
        result = tagger.extract_data_countries(
            "Prior research in Germany found similar attitudes toward inequality.")
        self.assertEqual(result, set())

    def test_multiple_aliases_for_same_country_still_dedupe(self):
        result = tagger.extract_data_countries(
            "Data from the United States and additional data from the USA subsample.")
        self.assertEqual(result, {"USA"})

    def test_replace_data_country_tags_preserves_other_tags_including_module_tags(self):
        updated = tagger.replace_data_country_tags(
            "RELIG; MYPROJECT; DATA - Japan", ["DATA - Germany"])
        tags = [t.strip() for t in updated.split(";")]
        self.assertEqual(set(tags), {"RELIG", "MYPROJECT", "DATA - Germany"})

    def test_data_country_tag_format(self):
        self.assertEqual(tagger.data_country_tag("Germany"), "DATA - Germany")

    # --- full-text fetching (fetch_abstract mocked - no live network) ------

    def test_tag_issp_modules_fetches_full_text_and_uses_it_for_classification(self):
        def fake_fetch_abstract(url, session=None, max_pdf_pages=3, **kwargs):
            self.assertEqual(url, "https://example.org/paper")
            return {"status": "abstract_found", "abstract": "",
                    "full_text": "Methods: we use ZA7570 and survey data from Germany."}

        df = pd.DataFrame({
            "Title": ["Paper A"],
            "Abstract": ["No ISSP mention here."],
            "Url": ["https://example.org/paper"],
            "DOI": [""],
        })
        with mock.patch.object(tagger.abstract_tools, "fetch_abstract",
                               side_effect=fake_fetch_abstract):
            output_df, _stats, _tag_column = tagger.tag_issp_modules(
                df, text_columns=["Title", "Abstract"], url_column="Url", doi_column="DOI",
                use_network_doi_lookup=False, use_semantic_matching=False,
                fetch_full_text=True, request_delay=0)

        self.assertEqual(output_df.loc[0, "ISSP Module Tag"], "RELIG")
        self.assertEqual(output_df.loc[0, "ISSP Module Method"], "za_number")
        self.assertEqual(output_df.loc[0, "ISSP Data Countries"], "Germany")
        self.assertIn("DATA - Germany", output_df.loc[0, "Manual Tags"])
        self.assertEqual(output_df.loc[0, "ISSP Full Text Fetch Status"], "abstract_found")

    def test_tag_issp_modules_records_no_link_status_when_nothing_to_fetch(self):
        df = pd.DataFrame({
            "Title": ["Paper A"], "Abstract": ["No ISSP mention."],
            "Url": [""], "DOI": [""],
        })
        with mock.patch.object(tagger.abstract_tools, "fetch_abstract") as fetch_mock:
            output_df, _stats, _tag_column = tagger.tag_issp_modules(
                df, text_columns=["Title", "Abstract"], url_column="Url", doi_column="DOI",
                use_network_doi_lookup=False, use_semantic_matching=False,
                fetch_full_text=True, request_delay=0)
        fetch_mock.assert_not_called()
        self.assertEqual(output_df.loc[0, "ISSP Full Text Fetch Status"], "no_link")

    def test_tag_issp_modules_does_not_fetch_when_disabled(self):
        df = pd.DataFrame({
            "Title": ["Paper A"], "Abstract": ["No ISSP mention."],
            "Url": ["https://example.org/paper"], "DOI": [""],
        })
        with mock.patch.object(tagger.abstract_tools, "fetch_abstract") as fetch_mock:
            output_df, _stats, _tag_column = tagger.tag_issp_modules(
                df, text_columns=["Title", "Abstract"], url_column="Url", doi_column="DOI",
                use_network_doi_lookup=False, use_semantic_matching=False,
                fetch_full_text=False)
        fetch_mock.assert_not_called()
        self.assertEqual(output_df.loc[0, "ISSP Full Text Fetch Status"], "")


if __name__ == "__main__":
    unittest.main()
