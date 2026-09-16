import unittest

import pandas as pd

try:
    from . import compare_tools as tools
except ImportError:
    import compare_tools as tools


def changes_by_status(changes, status):
    return [c for c in changes if c["status"] == status]


class NormalizeTitleKeyTests(unittest.TestCase):
    def test_case_and_punctuation_insensitive(self):
        self.assertEqual(
            tools.normalize_title_key("The Study: A Test!"),
            tools.normalize_title_key("the study a test"))

    def test_empty_title_gives_empty_key(self):
        self.assertEqual(tools.normalize_title_key(""), "")
        self.assertEqual(tools.normalize_title_key(None), "")


class RecordKeyTests(unittest.TestCase):
    def test_prefers_doi_when_present(self):
        row = pd.Series({"DOI": "10.1234/ABC", "Title": "Some Paper"})
        key = tools.record_key(row, "DOI", "Title")
        self.assertEqual(key[0], "doi")

    def test_falls_back_to_title_when_no_doi(self):
        row = pd.Series({"DOI": "", "Title": "Some Paper"})
        key = tools.record_key(row, "DOI", "Title")
        self.assertEqual(key[0], "title")

    def test_title_key_includes_year_when_given(self):
        row = pd.Series({"Title": "Some Paper", "Year": "2020"})
        key = tools.record_key(row, None, "Title", "Year")
        self.assertEqual(key, ("title", "some paper", "2020"))

    def test_none_when_nothing_usable(self):
        row = pd.Series({"DOI": "", "Title": ""})
        self.assertIsNone(tools.record_key(row, "DOI", "Title"))


class BuildKeyIndexTests(unittest.TestCase):
    def test_duplicate_keys_excluded_from_index(self):
        df = pd.DataFrame({"Title": ["Same Title", "Same Title", "Other"]})
        index, duplicates = tools.build_key_index(df, None, "Title")
        self.assertEqual(len(duplicates), 1)
        self.assertNotIn(("title", "same title", ""), index)
        self.assertIn(("title", "other", ""), index)


class CompareDataframesTests(unittest.TestCase):
    def test_identical_files_produce_no_changes(self):
        df = pd.DataFrame({"Title": ["Paper A", "Paper B"], "DOI": ["10.1/a", "10.1/b"]})
        changes, summary = tools.compare_dataframes(
            df, df.copy(), doi_column_a="DOI", title_column_a="Title",
            doi_column_b="DOI", title_column_b="Title")
        self.assertEqual(changes, [])
        self.assertEqual(summary["matched records"], 2)
        self.assertEqual(summary["records only in file A (removed)"], 0)
        self.assertEqual(summary["records only in file B (added)"], 0)

    def test_detects_removed_and_added_records(self):
        df_a = pd.DataFrame({"Title": ["Paper A", "Paper B"], "DOI": ["10.1/a", "10.1/b"]})
        df_b = pd.DataFrame({"Title": ["Paper A", "Paper C"], "DOI": ["10.1/a", "10.1/c"]})
        changes, summary = tools.compare_dataframes(
            df_a, df_b, doi_column_a="DOI", title_column_a="Title",
            doi_column_b="DOI", title_column_b="Title")
        removed = changes_by_status(changes, tools.REMOVED)
        added = changes_by_status(changes, tools.ADDED)
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["title"], "Paper B")
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["title"], "Paper C")
        self.assertEqual(summary["records only in file A (removed)"], 1)
        self.assertEqual(summary["records only in file B (added)"], 1)

    def test_detects_changed_field_on_matched_record(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "Confidence": ["low"]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "Confidence": ["high"]})
        changes, summary = tools.compare_dataframes(
            df_a, df_b, doi_column_a="DOI", title_column_a="Title",
            doi_column_b="DOI", title_column_b="Title")
        changed = changes_by_status(changes, tools.CHANGED)
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["field"], "Confidence")
        self.assertEqual(changed[0]["old_value"], "low")
        self.assertEqual(changed[0]["new_value"], "high")
        self.assertEqual(summary["matched records with a changed field"], 1)

    def test_multi_tag_field_diffed_as_a_set_not_a_string(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "Tag": ["RELIG; ENV"]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "Tag": ["ENV; RELIG"]})
        changes, _summary = tools.compare_dataframes(
            df_a, df_b, doi_column_a="DOI", title_column_a="Title",
            doi_column_b="DOI", title_column_b="Title")
        # Same tags, different order/spacing - must NOT be reported as changed.
        self.assertEqual(changes_by_status(changes, tools.CHANGED), [])

    def test_multi_tag_field_reports_real_difference(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "Tag": ["RELIG"]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "Tag": ["RELIG; ENV"]})
        changes, _summary = tools.compare_dataframes(
            df_a, df_b, doi_column_a="DOI", title_column_a="Title",
            doi_column_b="DOI", title_column_b="Title")
        changed = changes_by_status(changes, tools.CHANGED)
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["new_value"], "ENV; RELIG")  # sorted for stable display

    def test_falls_back_to_title_matching_without_doi(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "Confidence": ["low"]})
        df_b = pd.DataFrame({"Title": ["paper a!"], "Confidence": ["high"]})
        changes, summary = tools.compare_dataframes(
            df_a, df_b, title_column_a="Title", title_column_b="Title")
        self.assertEqual(summary["matched records"], 1)
        # Matched by normalized title, but the raw Title text still differs
        # ("Paper A" vs "paper a!"), so both Title and Confidence are
        # legitimately reported as changed fields on this matched record.
        changed_fields = {c["field"] for c in changes_by_status(changes, tools.CHANGED)}
        self.assertEqual(changed_fields, {"Title", "Confidence"})

    def test_columns_only_in_one_file_reported_structurally(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "Notes": ["hi"]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "Extra": ["x"]})
        _changes, summary = tools.compare_dataframes(
            df_a, df_b, doi_column_a="DOI", title_column_a="Title",
            doi_column_b="DOI", title_column_b="Title")
        self.assertEqual(summary["columns only in file A"], ["Notes"])
        self.assertEqual(summary["columns only in file B"], ["Extra"])
        self.assertNotIn("Notes", [c["field"] for c in _changes])
        self.assertNotIn("Extra", [c["field"] for c in _changes])

    def test_empty_values_on_both_sides_are_not_a_change(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "Notes": [""]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "Notes": [None]})
        changes, _summary = tools.compare_dataframes(
            df_a, df_b, doi_column_a="DOI", title_column_a="Title",
            doi_column_b="DOI", title_column_b="Title")
        self.assertEqual(changes, [])


if __name__ == "__main__":
    unittest.main()
