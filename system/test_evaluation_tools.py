import unittest

import pandas as pd

try:
    from . import evaluation_tools as tools
except ImportError:
    import evaluation_tools as tools


class TrueTagsFromKeywordsTests(unittest.TestCase):
    def test_extracts_known_codes_ignoring_other_keywords(self):
        value = "WORKORI; 2018 FDZ_IUP ISSP ZA6770 article checked; NATID; Job satisfaction"
        self.assertEqual(tools.true_tags_from_keywords(value), {"WORKORI", "NATID"})

    def test_no_known_codes_returns_empty_set(self):
        self.assertEqual(tools.true_tags_from_keywords("Sociology; Economics"), set())

    def test_empty_value_returns_empty_set(self):
        self.assertEqual(tools.true_tags_from_keywords(""), set())
        self.assertEqual(tools.true_tags_from_keywords(None), set())

    def test_custom_tag_codes(self):
        self.assertEqual(tools.true_tags_from_keywords("FOO; BAR", tag_codes={"FOO"}), {"FOO"})


class PredictedTagsFromRowTests(unittest.TestCase):
    def test_zips_tag_confidence_method_in_order(self):
        row = pd.Series({
            "ISSP Module Tag": "RELIG; ENV",
            "ISSP Module Confidence": "high; low",
            "ISSP Module Method": "za_number; semantic_similarity",
        })
        self.assertEqual(
            tools.predicted_tags_from_row(row),
            [("RELIG", "high", "za_number"), ("ENV", "low", "semantic_similarity")])

    def test_no_predictions_returns_empty_list(self):
        row = pd.Series({"ISSP Module Tag": "", "ISSP Module Confidence": "", "ISSP Module Method": "no_evidence"})
        self.assertEqual(tools.predicted_tags_from_row(row), [])

    def test_mismatched_lengths_fall_back_to_blank_confidence_method(self):
        row = pd.Series({
            "ISSP Module Tag": "RELIG; ENV",
            "ISSP Module Confidence": "high",  # malformed / hand-edited
            "ISSP Module Method": "za_number; semantic_similarity",
        })
        result = tools.predicted_tags_from_row(row)
        self.assertEqual([tag for tag, _c, _m in result], ["RELIG", "ENV"])
        self.assertEqual([c for _t, c, _m in result], ["", ""])


class EvaluateAgainstGroundTruthTests(unittest.TestCase):
    def _run(self, truth_rows, pred_rows):
        truth_df = pd.DataFrame(truth_rows)
        pred_df = pd.DataFrame(pred_rows)
        return tools.evaluate_against_ground_truth(
            truth_df, pred_df, keywords_column="Keywords",
            doi_column_truth="DOI", title_column_truth="Title",
            doi_column_pred="DOI", title_column_pred="Title")

    def test_perfect_prediction_gives_precision_and_recall_of_one(self):
        result = self._run(
            [{"Title": "Paper A", "DOI": "10.1/a", "Keywords": "RELIG"}],
            [{"Title": "Paper A", "DOI": "10.1/a", "ISSP Module Tag": "RELIG",
              "ISSP Module Confidence": "high", "ISSP Module Method": "za_number"}])
        self.assertEqual(result["labeled_records"], 1)
        self.assertEqual(result["matched_records"], 1)
        self.assertEqual(result["overall"]["precision"], 1.0)
        self.assertEqual(result["overall"]["recall"], 1.0)

    def test_missed_true_tag_is_a_false_negative(self):
        result = self._run(
            [{"Title": "Paper A", "DOI": "10.1/a", "Keywords": "RELIG"}],
            [{"Title": "Paper A", "DOI": "10.1/a", "ISSP Module Tag": "",
              "ISSP Module Confidence": "", "ISSP Module Method": "no_evidence"}])
        self.assertEqual(result["overall"]["recall"], 0.0)
        self.assertIsNone(result["overall"]["precision"])  # no predictions made at all
        self.assertEqual(len(result["false_negatives"]), 1)
        self.assertEqual(result["false_negatives"][0]["tag"], "RELIG")

    def test_wrong_predicted_tag_is_a_false_positive(self):
        result = self._run(
            [{"Title": "Paper A", "DOI": "10.1/a", "Keywords": "RELIG"}],
            [{"Title": "Paper A", "DOI": "10.1/a", "ISSP Module Tag": "ENV",
              "ISSP Module Confidence": "low", "ISSP Module Method": "semantic_similarity"}])
        self.assertEqual(result["overall"]["precision"], 0.0)
        self.assertEqual(len(result["false_positives"]), 1)
        self.assertEqual(result["false_positives"][0]["tag"], "ENV")
        # RELIG was never predicted, so it's ALSO a false negative.
        self.assertEqual(len(result["false_negatives"]), 1)

    def test_unlabeled_ground_truth_records_are_excluded_not_scored(self):
        result = self._run(
            [{"Title": "Paper A", "DOI": "10.1/a", "Keywords": "Sociology; Economics"}],
            [{"Title": "Paper A", "DOI": "10.1/a", "ISSP Module Tag": "ENV",
              "ISSP Module Confidence": "low", "ISSP Module Method": "semantic_similarity"}])
        self.assertEqual(result["labeled_records"], 0)
        self.assertEqual(result["overall"]["predicted_count"], 0)

    def test_unmatched_ground_truth_record_counted_separately_not_as_a_miss(self):
        result = self._run(
            [{"Title": "Paper A", "DOI": "10.1/a", "Keywords": "RELIG"}],
            [{"Title": "Paper B", "DOI": "10.1/b", "ISSP Module Tag": "",
              "ISSP Module Confidence": "", "ISSP Module Method": "no_evidence"}])
        self.assertEqual(result["labeled_records"], 1)
        self.assertEqual(result["matched_records"], 0)
        self.assertEqual(result["unmatched_records"], 1)
        self.assertEqual(result["false_negatives"], [])  # excluded, not penalized

    def test_breakdown_by_confidence_and_method_and_tag(self):
        result = self._run(
            [{"Title": "Paper A", "DOI": "10.1/a", "Keywords": "RELIG"},
             {"Title": "Paper B", "DOI": "10.1/b", "Keywords": "ENV"}],
            [{"Title": "Paper A", "DOI": "10.1/a", "ISSP Module Tag": "RELIG",
              "ISSP Module Confidence": "high", "ISSP Module Method": "za_number"},
             {"Title": "Paper B", "DOI": "10.1/b", "ISSP Module Tag": "SOCNET",
              "ISSP Module Confidence": "low", "ISSP Module Method": "semantic_similarity"}])
        self.assertEqual(result["by_confidence"]["high"]["precision"], 1.0)
        self.assertEqual(result["by_confidence"]["low"]["precision"], 0.0)
        self.assertEqual(result["by_method"]["za_number"]["precision"], 1.0)
        self.assertNotIn("recall", result["by_confidence"]["high"])  # not meaningful per-confidence
        self.assertNotIn("recall", result["by_method"]["za_number"])  # not meaningful per-method
        self.assertEqual(result["by_tag"]["RELIG"]["recall"], 1.0)
        self.assertEqual(result["by_tag"]["ENV"]["recall"], 0.0)  # never predicted for Paper B

    def test_multi_tag_record_scores_each_tag_independently(self):
        result = self._run(
            [{"Title": "Paper A", "DOI": "10.1/a", "Keywords": "RELIG; ENV"}],
            [{"Title": "Paper A", "DOI": "10.1/a", "ISSP Module Tag": "RELIG; SOCNET",
              "ISSP Module Confidence": "high; low",
              "ISSP Module Method": "za_number; semantic_similarity"}])
        # RELIG correctly predicted, ENV missed (false negative), SOCNET
        # spuriously predicted (false positive).
        self.assertEqual(result["overall"]["true_positive"], 1)
        self.assertEqual(result["overall"]["false_positive"], 1)
        self.assertEqual(result["overall"]["false_negative"], 1)


if __name__ == "__main__":
    unittest.main()
