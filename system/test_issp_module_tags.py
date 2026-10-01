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

import os
import tempfile
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


# Long enough (MIN_READABLE_WORDS) to count as real text that was read.
UNRELATED_ABSTRACT = ("This macroeconomics paper models how central bank interest rate "
                      "decisions affect inflation expectations, bond yields, and investment "
                      "across advanced economies over three decades.")


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

    def test_religion_environment_social_networks_count_standalone(self):
        for text, expected in [
                ("This paper is broadly about religion in modern society.", "RELIG"),
                ("Attitudes toward the environment in Europe.", "ENV"),
                ("Social networks and wellbeing in later life.", "SOCNET")]:
            result = first(tagger.classify_record(text))
            self.assertEqual(result["tag"], expected)
            self.assertEqual(result["confidence"], "high")
            self.assertEqual(result["method"], "exact_module_name")

    def test_exact_name_needs_whole_word(self):
        # "environmental" must not count as the module name "environment".
        result = tagger.classify_record("A short note on environmental law.")
        self.assertEqual(tags_of(result), [])

    def test_generic_name_alone_is_not_promoted_to_high(self):
        # "Citizenship" / "National Identity" alone are ordinary vocabulary
        # - must not be treated as an exact-name hit without ISSP nearby.
        for text in ["This paper is broadly about citizenship in modern society.",
                     "This paper is broadly about national identity in Asia."]:
            self.assertEqual(tags_of(tagger.classify_record(text)), [])

    def test_generic_name_near_issp_is_high_confidence(self):
        result = first(tagger.classify_record(
            "This paper uses the ISSP Citizenship module for its analysis."))
        self.assertEqual(result["tag"], "CIT")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["method"], "exact_module_name")

    def test_generic_name_near_spelled_out_issp_name_is_also_high_confidence(self):
        # Formal reports often spell out the name on first mention instead
        # of using the acronym - this must count just as much as "ISSP".
        result = first(tagger.classify_record(
            "This study uses the International Social Survey Programme "
            "National Identity module for its analysis."))
        self.assertEqual(result["tag"], "NATID")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["method"], "exact_module_name")

        result = first(tagger.classify_record(
            "This study uses the International Social Survey Program "
            "National Identity module for its analysis."))  # American spelling
        self.assertEqual(result["tag"], "NATID")

    def test_generic_name_far_from_issp_does_not_count(self):
        far_text = "ISSP data. " + ("filler word " * 40) + "This is about citizenship."
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

    def test_replace_issp_module_tags_keeps_every_existing_tag(self):
        # Existing module tags may be curated by hand: they are never removed.
        updated = tagger.replace_issp_module_tags("WORKORI; MYPROJECT; some-other-tag", ["RELIG"])
        tags = [t.strip() for t in updated.split(";")]
        self.assertEqual(tags, ["WORKORI", "MYPROJECT", "some-other-tag", "RELIG"])

    def test_replace_issp_module_tags_handles_empty_existing_value(self):
        self.assertEqual(tagger.replace_issp_module_tags("", ["ENV"]), "ENV")
        self.assertEqual(tagger.replace_issp_module_tags(None, ["ENV"]), "ENV")

    def test_replace_issp_module_tags_adds_more_than_one(self):
        updated = tagger.replace_issp_module_tags("MYPROJECT", ["ENV", "ROG"])
        tags = [t.strip() for t in updated.split(";")]
        self.assertEqual(set(tags), {"MYPROJECT", "ENV", "ROG"})

    def test_replace_issp_module_tags_with_no_new_tags_changes_nothing(self):
        self.assertEqual(tagger.replace_issp_module_tags("RELIG; MYPROJECT", []), "RELIG; MYPROJECT")

    def test_existing_tag_is_not_duplicated(self):
        self.assertEqual(tagger.replace_issp_module_tags("ENV; MYPROJECT", ["ENV"]), "ENV; MYPROJECT")

    # --- tag_issp_modules (batch) -------------------------------------------

    def test_tag_issp_modules_batch_writes_expected_columns_and_merges_tags(self):
        df = pd.DataFrame({
            "Title": ["Paper A", "Paper B", "Paper C"],
            "Abstract": [
                "Uses ZA7570 dataset.",
                "A study of environmental attitude and pro-environmental behaviour.",
                UNRELATED_ABSTRACT,
            ],
            "DOI": ["10.1/aaa", "10.2/bbb", "10.3/ccc"],
            "Keywords": ["EXISTINGTAG", "", "OTHERTAG"],
        })
        output_df, stats, tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], doi_column="DOI",
            tag_column="Keywords", use_network_doi_lookup=False, use_semantic_matching=False,
            keyword_min_confidence="medium")

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
        self.assertEqual(stats["not reported"], 1)

    def test_status_separates_not_reported_from_unavailable(self):
        df = pd.DataFrame({
            "Title": ["Paper A", "Paper B", "Paper C"],
            "Abstract": ["Uses ZA7570 dataset.", UNRELATED_ABSTRACT, ""],
        })
        output_df, stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], title_column="Title",
            use_network_doi_lookup=False, use_semantic_matching=False)
        self.assertEqual(list(output_df["ISSP Module Status"]),
                         ["Tagged", "Not reported", "Unavailable"])
        self.assertIn("Searched Abstract", output_df.loc[1, "ISSP Module Status Reason"])
        self.assertIn("full-text download not enabled", output_df.loc[2, "ISSP Module Status Reason"])
        self.assertEqual(stats["not reported"], 1)
        self.assertEqual(stats["unavailable"], 1)

    def test_marker_only_notes_are_not_readable_text(self):
        self.assertFalse(tagger.has_readable_text("<p>(ISSP)</p>"))
        self.assertFalse(tagger.has_readable_text(
            "<p>Export Date: 24 November 2025; Cited By: 3</p>\n<p>(ISSP) (EVS)</p>"))
        self.assertFalse(tagger.has_readable_text(
            "<p>http://search.proquest.com/docview/886579133?accountid=14657</p>"))
        self.assertTrue(tagger.has_readable_text(UNRELATED_ABSTRACT))

        df = pd.DataFrame({"Title": ["Paper A"], "Abstract": [""], "Notes": ["<p>(ISSP)</p>"]})
        output_df, _stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract", "Notes"], title_column="Title",
            use_network_doi_lookup=False, use_semantic_matching=False)
        self.assertEqual(output_df.loc[0, "ISSP Module Status"], "Unavailable")

    def test_failed_full_text_with_title_only_is_unavailable(self):
        df = pd.DataFrame({"Title": ["Paper A"], "Abstract": [""],
                           "Url": ["https://example.org/paper"]})
        with mock.patch.object(tagger.abstract_tools, "fetch_abstract",
                               return_value={"status": "fetch_failed", "full_text": ""}):
            output_df, _stats, _tag_column = tagger.tag_issp_modules(
                df, text_columns=["Title", "Abstract"], title_column="Title", url_column="Url",
                use_network_doi_lookup=False, use_semantic_matching=False,
                fetch_full_text=True, use_full_text_cache=False, request_delay=0)
        self.assertEqual(output_df.loc[0, "ISSP Module Status"], "Unavailable")
        self.assertIn("download failed", output_df.loc[0, "ISSP Module Status Reason"])

    def test_evidence_location_and_quote_name_the_field_and_sentence(self):
        df = pd.DataFrame({
            "Title": ["Attitudes in Europe"],
            "Abstract": ["Background text. We analyse ZA7570 for twelve countries. More text."],
            "Notes": ["Also mentions job satisfaction and work commitment."],
        })
        output_df, _stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract", "Notes"], title_column="Title",
            use_network_doi_lookup=False, use_semantic_matching=False)
        self.assertEqual(output_df.loc[0, "ISSP Module Tag"], "RELIG; WORKORI")
        self.assertEqual(output_df.loc[0, "ISSP Module Evidence Location"],
                         "RELIG: Abstract | WORKORI: Notes")
        self.assertEqual(
            output_df.loc[0, "ISSP Module Evidence Quote"],
            "RELIG: “We analyse ZA7570 for twelve countries.” | "
            "WORKORI: “Also mentions job satisfaction and work commitment.”")

    def test_semantic_tag_location_is_whole_text(self):
        class FakeMatcher:
            def classify_batch(self, texts):
                return [("HLTH", 0.6) for _ in texts]

        df = pd.DataFrame({"Title": ["Paper A"], "Abstract": ["Nothing specific here."]})
        output_df, _stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], use_network_doi_lookup=False,
            semantic_matcher=FakeMatcher())
        self.assertEqual(output_df.loc[0, "ISSP Module Evidence Location"],
                         "HLTH: " + tagger.SEMANTIC_LOCATION)
        self.assertEqual(output_df.loc[0, "ISSP Module Evidence Quote"], "")

    def test_module_tags_stay_out_of_keywords_by_default(self):
        df = pd.DataFrame({"Title": ["Paper A"],
                           "Abstract": ["We use ISSP 2018 data (ZA7570) from Germany and France."],
                           "Keywords": ["MYPROJECT"]})
        output_df, _stats, _column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], tag_column="Keywords",
            use_network_doi_lookup=False, use_semantic_matching=False)
        # The module tag is only in the review columns; country tags still go in.
        self.assertEqual(output_df.loc[0, "ISSP Tags (high)"], "RELIG")
        keywords = output_df.loc[0, "Keywords"].split("; ")
        self.assertNotIn("RELIG", keywords)
        self.assertIn("MYPROJECT", keywords)
        self.assertTrue(any(tag.startswith("DATA - ") for tag in keywords), keywords)

    def test_low_confidence_tags_stay_out_of_keywords_at_medium(self):
        class FakeMatcher:
            def classify_batch(self, texts):
                return [("HLTH", 0.6) for _ in texts]

        df = pd.DataFrame({"Title": ["Paper A", "Paper B"],
                           "Abstract": ["Nothing specific here.", "We use ISSP data (ZA7570)."],
                           "Keywords": ["ENV; MYPROJECT", ""]})
        output_df, _stats, _column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], tag_column="Keywords",
            use_network_doi_lookup=False, semantic_matcher=FakeMatcher(), keyword_min_confidence="medium")
        # The meaning-only match is reported, but not written into the keywords;
        # the existing curated ENV tag is kept even though no evidence was found.
        self.assertEqual(output_df.loc[0, "ISSP Module Tag"], "HLTH")
        self.assertEqual(output_df.loc[0, "ISSP Module Confidence"], "low")
        self.assertEqual(output_df.loc[0, "Keywords"], "ENV; MYPROJECT")
        # A high-confidence match (ZA number) is added.
        self.assertIn("RELIG", output_df.loc[1, "Keywords"])  # ZA7570 = Religion III

        # One column per confidence level, for review.
        self.assertEqual(output_df.loc[0, "ISSP Tags (low)"], "HLTH")
        self.assertEqual(output_df.loc[0, "ISSP Tags (high)"], "")
        self.assertEqual(output_df.loc[1, "ISSP Tags (high)"], "RELIG")

        output_df, _stats, _column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], tag_column="Keywords",
            use_network_doi_lookup=False, semantic_matcher=FakeMatcher(), keyword_min_confidence="low")
        self.assertEqual(output_df.loc[0, "Keywords"], "ENV; MYPROJECT; HLTH")

    def test_long_sentence_quote_is_shortened_around_the_match(self):
        long_sentence = ("word " * 100) + "ZA7570" + (" word" * 100) + "."
        df = pd.DataFrame({"Abstract": [long_sentence]})
        output_df, _stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Abstract"], use_network_doi_lookup=False,
            use_semantic_matching=False)
        quote = output_df.loc[0, "ISSP Module Evidence Quote"]
        self.assertIn("ZA7570", quote)
        self.assertLess(len(quote), tagger.MAX_QUOTE_CHARS + 20)

    def test_country_evidence_column(self):
        df = pd.DataFrame({"Abstract": ["Other papers are based on Japan. This paper uses UK data."]})
        output_df, _stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Abstract"], use_network_doi_lookup=False,
            use_semantic_matching=False)
        self.assertEqual(output_df.loc[0, "ISSP Data Countries"], "Great Britain")
        self.assertEqual(output_df.loc[0, "ISSP Data Country Evidence"],
                         "Great Britain [Abstract]: “This paper uses UK data.”")

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
            use_semantic_matching=False, keyword_min_confidence="high")
        self.assertEqual(tag_column, "Tags")
        self.assertEqual(output_df.loc[0, "Tags"], "RELIG")

    def test_tag_issp_modules_creates_tag_column_when_none_found(self):
        df = pd.DataFrame({"Title": ["Paper A"], "Abstract": ["Uses ZA7570 dataset."]})
        output_df, _stats, tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], use_network_doi_lookup=False,
            use_semantic_matching=False, keyword_min_confidence="high")
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
        self.assertEqual(stats["not reported"] + stats["unavailable"], 0)

    def test_tag_issp_modules_semantic_pass_skipped_when_disabled(self):
        class MatcherThatShouldNotBeCalled:
            def classify_batch(self, texts):
                raise AssertionError("semantic tier should not run when disabled")

        df = pd.DataFrame({
            "Title": ["Paper A"],
            "Abstract": [UNRELATED_ABSTRACT],
        })
        output_df, stats, _tag_column = tagger.tag_issp_modules(
            df, text_columns=["Title", "Abstract"], use_network_doi_lookup=False,
            use_semantic_matching=False, semantic_matcher=MatcherThatShouldNotBeCalled())
        self.assertEqual(output_df.loc[0, "ISSP Module Method"], "no_evidence")
        self.assertEqual(stats["not reported"], 1)

    # --- country/data-source tags -----------------------------------------

    def test_country_mentioned_near_data_context_is_extracted(self):
        result = tagger.extract_data_countries(
            "We use survey data from Germany and France collected in 2018.")
        self.assertEqual(result, {"Germany", "France"})

    def test_country_mentioned_near_spelled_out_issp_name_is_extracted(self):
        result = tagger.extract_data_countries(
            "This uses the International Social Survey Programme for Germany.")
        self.assertEqual(result, {"Germany"})

    def test_country_in_previous_sentence_does_not_count(self):
        # Japan is only in the sentence about someone else's paper; the
        # data phrase is in the next sentence.
        result = tagger.extract_data_countries(
            "Other papers are based on Japan. This paper uses UK data.")
        self.assertEqual(result, {"Great Britain"})
        result = tagger.extract_data_countries(
            "Earlier work studied Germany. We use data from the ISSP 2018 wave.")
        self.assertEqual(result, set())

    def test_title_and_abstract_are_separate_sentences(self):
        # Fields are joined with newlines, so a country at the end of the
        # title never pairs with a data phrase at the start of the abstract.
        result = tagger.extract_data_countries("Religion in Poland\nData from 30 countries.")
        self.assertEqual(result, set())

    def test_abbreviations_do_not_split_a_sentence(self):
        result = tagger.extract_data_countries(
            "We use data from the U.S. and e.g. Germany in 2018.")
        self.assertEqual(result, {"USA", "Germany"})

    def test_country_followed_by_data_noun_counts(self):
        self.assertEqual(tagger.extract_data_countries("We analyse Swiss and Japan survey waves."),
                         {"Switzerland", "Japan"})
        self.assertEqual(tagger.extract_data_countries("The UK's survey data."), {"Great Britain"})
        self.assertEqual(tagger.extract_data_countries("Using UK household panel data."),
                         {"Great Britain"})

    def test_bare_us_only_counts_in_capitals(self):
        self.assertEqual(tagger.extract_data_countries("Data from US respondents."), {"USA"})
        self.assertEqual(tagger.extract_data_countries("Give us data from them."), set())

    def test_nationality_adjective_needs_a_data_noun(self):
        self.assertEqual(tagger.extract_data_countries("We use German respondents from ALLBUS."),
                         {"Germany"})
        self.assertEqual(tagger.extract_data_countries(
            "Using the Chinese General Social Survey 2010."), {"China"})
        self.assertEqual(tagger.extract_data_countries(
            "We analyse East German and West German samples."), {"Germany"})
        # An adjective next to a data phrase is not enough on its own.
        self.assertEqual(tagger.extract_data_countries("Data from the German economy."), set())

    def test_person_named_german_is_not_a_country(self):
        # "German" is also a first name / surname - a possessive between
        # the adjective and the data noun means it is a person.
        self.assertEqual(tagger.extract_data_countries("Data from German Lopez's survey."), set())

    def test_non_issp_member_countries_are_tagged(self):
        self.assertEqual(tagger.extract_data_countries("This paper uses data from China and Brazil."),
                         {"China", "Brazil"})
        self.assertIn("China", tagger.ALL_COUNTRIES)
        self.assertGreater(len(tagger.ALL_COUNTRIES), 190)

    def test_longest_country_name_wins(self):
        self.assertEqual(tagger.extract_data_countries(
            "Data from Northern Ireland and Papua New Guinea."),
            {"Northern Ireland", "Papua New Guinea"})
        self.assertEqual(tagger.extract_data_countries("Data from South Sudan."), {"South Sudan"})

    def test_region_words_are_not_countries(self):
        self.assertEqual(tagger.extract_data_countries(
            "Data from Latin America and North American respondents."), set())
        self.assertEqual(tagger.extract_data_countries(
            "African American respondents in the survey."), set())

    def test_case_sensitive_codes(self):
        self.assertEqual(tagger.extract_data_countries("Survey data from the PRC and UAE."),
                         {"China", "United Arab Emirates"})

    def test_country_evidence_returns_the_sentence(self):
        text = "Other papers are based on Japan. This paper uses UK data."
        start, end = tagger.extract_data_country_evidence(text)["Great Britain"]
        self.assertEqual(text[start:end], "This paper uses UK data.")

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
        self.assertEqual(tags, ["RELIG", "MYPROJECT", "DATA - Japan", "DATA - Germany"])

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
                fetch_full_text=True, use_full_text_cache=False, request_delay=0)

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
                fetch_full_text=True, use_full_text_cache=False, request_delay=0)
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

    # --- full-text fetch cache ----------------------------------------------

    def test_full_text_cache_key_distinguishes_url_and_page_count(self):
        key_a = tagger.full_text_cache_key("https://example.org/a", 15)
        key_a_again = tagger.full_text_cache_key("https://EXAMPLE.org/a", 15)  # canonicalized
        key_b = tagger.full_text_cache_key("https://example.org/b", 15)
        key_a_pages = tagger.full_text_cache_key("https://example.org/a", 3)
        self.assertEqual(key_a, key_a_again)
        self.assertNotEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_a_pages)

    def test_cached_full_text_respects_ttl_for_success_vs_failure(self):
        now = tagger.time.time()
        cache = {
            "fresh-success": {"saved_at": now, "payload": {"status": "abstract_found"}},
            "old-success": {"saved_at": now - tagger.FULL_TEXT_SUCCESS_TTL - 10,
                            "payload": {"status": "abstract_found"}},
            "fresh-failure": {"saved_at": now, "payload": {"status": "fetch_failed"}},
            "old-failure": {"saved_at": now - tagger.FULL_TEXT_FAILURE_TTL - 10,
                           "payload": {"status": "fetch_failed"}},
        }
        self.assertIsNotNone(tagger.cached_full_text(cache, "fresh-success"))
        self.assertIsNone(tagger.cached_full_text(cache, "old-success"))  # past the 180-day success TTL
        self.assertIsNotNone(tagger.cached_full_text(cache, "fresh-failure"))
        self.assertIsNone(tagger.cached_full_text(cache, "old-failure"))
        self.assertIsNone(tagger.cached_full_text(cache, "missing-key"))

    def test_tag_issp_modules_second_run_hits_cache_instead_of_refetching(self):
        call_count = [0]

        def fake_fetch_abstract(url, session=None, max_pdf_pages=3, **kwargs):
            call_count[0] += 1
            return {"status": "abstract_found", "abstract": "",
                    "full_text": "Methods: we use ZA7570."}

        df = pd.DataFrame({
            "Title": ["Paper A"], "Abstract": ["No ISSP mention here."],
            "Url": ["https://example.org/paper"], "DOI": [""],
        })

        with tempfile.TemporaryDirectory() as directory:
            cache_path = os.path.join(directory, "full_text_cache.jsonl")
            with mock.patch.object(tagger, "FULL_TEXT_CACHE_FILE", cache_path), \
                 mock.patch.object(tagger.abstract_tools, "fetch_abstract",
                                   side_effect=fake_fetch_abstract):
                for _ in range(2):
                    output_df, stats, _tag_column = tagger.tag_issp_modules(
                        df, text_columns=["Title", "Abstract"], url_column="Url",
                        doi_column="DOI", use_network_doi_lookup=False,
                        use_semantic_matching=False, fetch_full_text=True,
                        use_full_text_cache=True, request_delay=0)

        self.assertEqual(call_count[0], 1)  # second run served entirely from cache
        self.assertEqual(stats["full text served from cache"], 1)
        self.assertEqual(stats["full text freshly fetched"], 0)
        self.assertEqual(output_df.loc[0, "ISSP Module Tag"], "RELIG")

    def test_tag_issp_modules_cache_disabled_always_refetches(self):
        call_count = [0]

        def fake_fetch_abstract(url, session=None, max_pdf_pages=3, **kwargs):
            call_count[0] += 1
            return {"status": "abstract_found", "abstract": "", "full_text": "Methods: ZA7570."}

        df = pd.DataFrame({
            "Title": ["Paper A"], "Abstract": ["No ISSP mention here."],
            "Url": ["https://example.org/paper"], "DOI": [""],
        })

        with tempfile.TemporaryDirectory() as directory:
            cache_path = os.path.join(directory, "full_text_cache.jsonl")
            with mock.patch.object(tagger, "FULL_TEXT_CACHE_FILE", cache_path), \
                 mock.patch.object(tagger.abstract_tools, "fetch_abstract",
                                   side_effect=fake_fetch_abstract):
                for _ in range(2):
                    tagger.tag_issp_modules(
                        df, text_columns=["Title", "Abstract"], url_column="Url",
                        doi_column="DOI", use_network_doi_lookup=False,
                        use_semantic_matching=False, fetch_full_text=True,
                        use_full_text_cache=False, request_delay=0)

        self.assertEqual(call_count[0], 2)  # cache disabled - both runs fetched fresh


if __name__ == "__main__":
    unittest.main()
