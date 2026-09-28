"""Tests for semantic_model_comparison.py (no real model is loaded).

Run: python -m unittest test_semantic_model_comparison (from inside system/)
"""

import unittest

import numpy as np
import pandas as pd

import semantic_model_comparison as comparison
import issp_module_tags as tagger

LONG_RELIGION_ABSTRACT = (
    "We analyse ZA7570 to study how church attendance, prayer, and belief in God vary with "
    "age and education across thirty countries, and how secular attitudes spread.")


class SemanticModelComparisonTests(unittest.TestCase):

    def test_mask_removes_hard_evidence_but_keeps_topic_words(self):
        masked = comparison.mask_explicit_evidence(
            "ISSP Religion module (ZA7570, doi 10.4232/1.13161): religious belief and prayer.")
        self.assertNotIn("Religion", masked)
        self.assertNotIn("7570", masked)
        self.assertNotIn("10.4232", masked)
        self.assertIn("religious belief", masked)  # topic content is what models should use

    def test_language_group(self):
        self.assertEqual(comparison.guess_language_group(LONG_RELIGION_ABSTRACT), "English")
        self.assertEqual(comparison.guess_language_group(
            "Die Analyse untersucht Einstellungen zur Religion in Deutschland und Europa."),
            "Non-English")

    def test_build_evaluation_set_uses_high_confidence_tags_and_masks(self):
        df = pd.DataFrame({
            "Title": ["Paper A", "Paper B", "Religion"],
            "Abstract": [LONG_RELIGION_ABSTRACT, "No module evidence here at all.", ""],
        })
        evaluation = comparison.build_evaluation_set(df, ["Title", "Abstract"])
        # Paper B has no gold tag; the third record has nothing left once
        # "Religion" is masked, so only Paper A remains.
        self.assertEqual(list(evaluation["row"]), [0])
        self.assertEqual(evaluation.loc[0, "gold"], ["RELIG"])
        self.assertEqual(evaluation.loc[0, "gold_method"], "za_number")
        self.assertNotIn("ZA7570", evaluation.loc[0, "text"])

    def test_score_similarities(self):
        tags = ["A", "B", "C", "D"]
        similarities = [
            [0.9, 0.1, 0.0, 0.0],  # top-1 right, confident
            [0.2, 0.5, 0.48, 0.0],  # gold C is 2nd: top-3 only; margin too small to tag
            [0.1, 0.0, 0.0, 0.3],  # wrong (gold A), below min similarity
        ]
        metrics, predictions = comparison.score_similarities(
            similarities, tags, [["A"], ["C"], ["A"]], ["English", "English", "Non-English"],
            min_similarity=0.35, min_margin=0.05)
        self.assertAlmostEqual(metrics["top1_accuracy"], 1 / 3)
        self.assertAlmostEqual(metrics["top3_accuracy"], 1.0)
        self.assertAlmostEqual(metrics["top1_accuracy_english"], 0.5)
        self.assertAlmostEqual(metrics["top1_accuracy_non_english"], 0.0)
        self.assertAlmostEqual(metrics["current_thresholds_coverage"], 1 / 3)
        self.assertAlmostEqual(metrics["current_thresholds_precision"], 1.0)
        self.assertEqual(list(predictions["top1"]), ["A", "B", "D"])

    def test_multi_gold_record_counts_any_gold_as_correct(self):
        metrics, _ = comparison.score_similarities(
            [[0.1, 0.9]], ["A", "B"], [["A", "B"]], ["English"])
        self.assertEqual(metrics["top1_accuracy"], 1.0)

    def test_evaluate_model_with_fake_model_and_prefix(self):
        seen = []

        class FakeModel:
            max_seq_length = 128

            def encode(self, texts, normalize_embeddings=True, **kwargs):
                seen.extend(texts)
                # Each module description and each text maps to its own
                # one-hot vector: the text "church" goes to RELIG.
                tags = list(tagger.TOPIC_DESCRIPTIONS)
                vectors = []
                for text in texts:
                    vector = np.zeros(len(tags))
                    body = text.removeprefix("query: ")
                    for i, tag in enumerate(tags):
                        if body == tagger.TOPIC_DESCRIPTIONS[tag] or (tag == "RELIG" and "church" in body):
                            vector[i] = 1.0
                    vectors.append(vector)
                return np.array(vectors)

        evaluation = pd.DataFrame({"row": [7], "gold": [["RELIG"]], "gold_method": ["za_number"],
                                   "text": ["church attendance"], "language": ["English"]})
        metrics, predictions = comparison.evaluate_model(
            {"name": "fake", "label": "Fake", "prefix": "query: "}, evaluation, model=FakeModel())
        self.assertEqual(metrics["top1_accuracy"], 1.0)
        self.assertEqual(metrics["max_tokens"], 128)
        self.assertEqual(predictions.loc[0, "row"], 7)
        self.assertTrue(all(text.startswith("query: ") for text in seen))

    def test_per_module_recall(self):
        predictions = pd.DataFrame({"gold": ["A", "A; B", "B"], "top1": ["A", "B", "A"]})
        rows = {row["module"]: row for row in comparison.per_module_recall(predictions, "M")}
        self.assertEqual(rows["A"]["records"], 2)
        self.assertAlmostEqual(rows["A"]["top1_recall"], 0.5)
        self.assertAlmostEqual(rows["B"]["top1_recall"], 0.5)


if __name__ == "__main__":
    unittest.main()
