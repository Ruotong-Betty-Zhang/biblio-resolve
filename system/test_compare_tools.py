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


class CompareByColumnPairsTests(unittest.TestCase):
    def compare(self, df_a, df_b, extra=(), pairs=(), **kwargs):
        records, summary = tools.compare_by_column_pairs(
            df_a, df_b, ("Title", "Title"), extra, pairs, **kwargs)
        return {r["title"]: r for r in records}, summary

    def test_title_match_and_different_column_names(self):
        df_a = pd.DataFrame({"Title": ["Paper A", "Paper B"], "Url": ["http://a", "http://b"]})
        df_b = pd.DataFrame({"Title": ["paper a!", "Paper B"], "Link": ["http://a", "http://x"]})
        records, summary = self.compare(df_a, df_b, pairs=[("Url", "Link")])
        self.assertEqual(records["Paper A"]["status"], tools.SAME)
        self.assertEqual(records["Paper B"]["status"], tools.DIFFERENT)
        self.assertEqual(records["Paper B"]["differences"], ["Url ↔ Link"])
        self.assertEqual(records["Paper B"]["values"]["Url ↔ Link"], ("http://b", "http://x", True))
        self.assertEqual(summary["field_difference_counts"], {"Url ↔ Link": 1})

    def test_only_selected_columns_are_compared(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "Notes": ["one"], "Year": ["2001"]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "Notes": ["two"], "Year": ["2001"]})
        records, _summary = self.compare(df_a, df_b, pairs=[("Year", "Year")])
        self.assertEqual(records["Paper A"]["status"], tools.SAME)

    def test_extra_key_separates_same_title(self):
        df_a = pd.DataFrame({"Title": ["Survey", "Survey"], "Year": ["2001", "2010"], "V": ["a", "b"]})
        df_b = pd.DataFrame({"Title": ["Survey", "Survey"], "Year": ["2010", "2001.0"], "V": ["b", "a"]})
        records, summary = tools.compare_by_column_pairs(
            df_a, df_b, ("Title", "Title"), [("Year", "Year")], [("V", "V")])
        self.assertEqual([r["status"] for r in records], [tools.SAME, tools.SAME])
        self.assertEqual([r["index_b"] for r in records], [1, 0])

    def test_empty_extra_key_does_not_block_match(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "DOI": [""], "V": ["x"]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"], "V": ["y"]})
        records, _summary = self.compare(df_a, df_b, extra=[("DOI", "DOI")], pairs=[("V", "V")])
        self.assertEqual(records["Paper A"]["status"], tools.DIFFERENT)
        self.assertIn("others empty", records["Paper A"]["match_note"])

    def test_conflicting_extra_key_prevents_match(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/a"]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "DOI": ["https://doi.org/10.1/B"]})
        records, _summary = tools.compare_by_column_pairs(
            df_a, df_b, ("Title", "Title"), [("DOI", "DOI")], [])
        self.assertEqual(sorted(r["status"] for r in records), [tools.ONLY_A, tools.ONLY_B])

    def test_doi_spellings_agree(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "DOI": ["10.1/ABC"]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "DOI": ["https://doi.org/10.1/abc"]})
        records, _summary = self.compare(df_a, df_b, extra=[("DOI", "DOI")])
        self.assertEqual(records["Paper A"]["status"], tools.SAME)

    def test_duplicates_paired_in_order_and_leftover_reported(self):
        df_a = pd.DataFrame({"Title": ["Dup", "Dup", "Dup"]})
        df_b = pd.DataFrame({"Title": ["Dup", "Dup"]})
        records, summary = tools.compare_by_column_pairs(df_a, df_b, ("Title", "Title"))
        self.assertEqual([(r["index_a"], r["index_b"]) for r in records], [(0, 0), (1, 1), (2, None)])
        self.assertIn("paired in file order", records[0]["match_note"])
        self.assertEqual(summary["status_counts"][tools.ONLY_A], 1)

    def test_missing_title_reported_separately(self):
        df_a = pd.DataFrame({"Title": ["", "Paper A"]})
        df_b = pd.DataFrame({"Title": ["Paper A", None]})
        _records, summary = tools.compare_by_column_pairs(df_a, df_b, ("Title", "Title"))
        self.assertEqual(summary["status_counts"][tools.NO_TITLE], 2)
        self.assertEqual(summary["status_counts"][tools.SAME], 1)

    def test_ignore_case_option(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "Pub": ["SAGE Publications"]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "Pub": ["sage publications."]})
        strict, _ = self.compare(df_a, df_b, pairs=[("Pub", "Pub")])
        loose, _ = self.compare(df_a, df_b, pairs=[("Pub", "Pub")], ignore_case=True)
        self.assertEqual(strict["Paper A"]["status"], tools.DIFFERENT)
        self.assertEqual(loose["Paper A"]["status"], tools.SAME)

    def test_whitespace_and_float_year_not_a_difference(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "Year": ["1999"], "Pub": ["SAGE  Pub "]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "Year": [1999.0], "Pub": ["SAGE Pub"]})
        records, _ = self.compare(df_a, df_b, pairs=[("Year", "Year"), ("Pub", "Pub")])
        self.assertEqual(records["Paper A"]["status"], tools.SAME)

    def test_export_rows_are_wide(self):
        df_a = pd.DataFrame({"Title": ["Paper A"], "V": ["x"]})
        df_b = pd.DataFrame({"Title": ["Paper A"], "V": ["y"]})
        records, summary = tools.compare_by_column_pairs(df_a, df_b, ("Title", "Title"), (), [("V", "V")])
        headers, rows = tools.comparison_export_rows(records, summary["compared_labels"])
        self.assertEqual(headers[-3:], ["V [A]", "V [B]", "V differs"])
        self.assertEqual(rows[0][:4], [tools.DIFFERENT, "title", 2, 2])
        self.assertEqual(rows[0][-3:], ["x", "y", "yes"])


if __name__ == "__main__":
    unittest.main()
