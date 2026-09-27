import os
import tempfile
import unittest

import pandas as pd

try:
    from . import convert_tools as tools
    from . import lookup_core as core
except ImportError:
    import convert_tools as tools
    import lookup_core as core


def sample_frame():
    return pd.DataFrame({
        "Item Type": ["journalArticle", "book", "journalArticle"],
        "Title": ["Religion and trust", "Digital society", "Environmental attitudes"],
        "Author": ["Smith, Jane; Doe, John", "Lee, Kim", "Ng, Ana"],
        "Publication Year": ["2019", "2021", "2015"],
        "Publication Title": ["Social Forces", "", "Env. Politics"],
        "Volume": ["12", "", "4"],
        "Issue": ["3", "", "1"],
        "Pages": ["10-25", "", "100-120"],
        "DOI": ["10.1000/a1", "", "10.1000/c3"],
        "Url": ["", "https://example.org/b", ""],
        "Module": ["RELIG", "DIGSOC", "ENV"],
    })


class FilterTests(unittest.TestCase):
    def test_text_contains_is_case_insensitive(self):
        result = tools.filter_dataframe(sample_frame(), [("Title", "contains", "SOCIETY", "")])
        self.assertEqual(result["Title"].tolist(), ["Digital society"])

    def test_numeric_between(self):
        result = tools.filter_dataframe(
            sample_frame(), [("Publication Year", "between", "2016", "2021")])
        self.assertEqual(result["Publication Year"].tolist(), ["2019", "2021"])

    def test_and_versus_or(self):
        conditions = [("Module", "equals", "relig", ""), ("Publication Year", ">=", "2020", "")]
        self.assertEqual(len(tools.filter_dataframe(sample_frame(), conditions, "AND")), 0)
        self.assertEqual(len(tools.filter_dataframe(sample_frame(), conditions, "OR")), 2)

    def test_blank_operators(self):
        frame = sample_frame()
        self.assertEqual(len(tools.filter_dataframe(frame, [("DOI", "is blank", "", "")])), 1)
        self.assertEqual(len(tools.filter_dataframe(frame, [("DOI", "is not blank", "", "")])), 2)

    def test_no_conditions_keeps_everything(self):
        self.assertEqual(len(tools.filter_dataframe(sample_frame(), [])), 3)

    def test_unknown_column_raises(self):
        with self.assertRaises(KeyError):
            tools.filter_dataframe(sample_frame(), [("Nope", "equals", "x", "")])


class PortableColumnTests(unittest.TestCase):
    def test_custom_columns_are_portable_for_ris(self):
        portable = tools.portable_columns_for(["Title", "DOI", "Module"], "ris")
        self.assertEqual(portable, ["Module"])

    def test_shadowed_alias_is_portable(self):
        portable = tools.portable_columns_for(["Title", "Matched Title"], "bibtex")
        self.assertEqual(portable, ["Matched Title"])

    def test_csv_needs_no_portable_columns(self):
        self.assertEqual(tools.portable_columns_for(["Module"], "csv"), [])


class ConvertRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_column_selection_and_filter_applied(self):
        path = self._path("out.csv")
        count, _portable = tools.convert_file(
            sample_frame(), path, "csv", ["Title", "DOI"],
            [("DOI", "is not blank", "", "")])
        loaded = core.read_records_file(path)
        self.assertEqual(count, 2)
        self.assertEqual(list(loaded.columns), ["Title", "DOI"])

    def test_every_pair_of_formats_keeps_fields(self):
        columns = ["Title", "Author", "Publication Year", "Publication Title",
                   "Volume", "Issue", "Pages", "DOI", "Module"]
        for source_fmt, source_ext in [("csv", ".csv"), ("ris", ".ris"), ("bibtex", ".bib")]:
            source = self._path("source" + source_ext)
            tools.convert_file(sample_frame(), source, source_fmt, columns)
            loaded = core.read_records_file(source, as_text=True)
            for target_fmt, target_ext in [("csv", ".csv"), ("ris", ".ris"), ("bibtex", ".bib")]:
                with self.subTest(source=source_fmt, target=target_fmt):
                    target = self._path("target" + target_ext)
                    tools.convert_file(loaded, target, target_fmt, columns)
                    result = core.read_records_file(target, as_text=True).fillna("")
                    self.assertEqual(result["Title"].tolist(), sample_frame()["Title"].tolist())
                    self.assertEqual(result["Module"].tolist(), ["RELIG", "DIGSOC", "ENV"])
                    self.assertEqual(result["Pages"].tolist(), ["10-25", "", "100-120"])
                    self.assertEqual(result["Volume"].tolist(), ["12", "", "4"])
                    self.assertEqual(result["DOI"].tolist(), ["10.1000/a1", "", "10.1000/c3"])

    def test_rejects_empty_column_selection(self):
        with self.assertRaises(ValueError):
            tools.convert_file(sample_frame(), self._path("x.csv"), "csv", [])


if __name__ == "__main__":
    unittest.main()
