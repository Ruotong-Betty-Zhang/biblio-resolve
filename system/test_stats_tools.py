import unittest

import pandas as pd

try:
    from . import stats_tools as tools
except ImportError:
    import stats_tools as tools


class SplitTagsTests(unittest.TestCase):
    def test_splits_on_semicolon(self):
        self.assertEqual(tools.split_tags("RELIG; ENV; DIGSOC"), ["RELIG", "ENV", "DIGSOC"])

    def test_falls_back_to_comma_when_no_semicolon(self):
        self.assertEqual(tools.split_tags("a, b,c"), ["a", "b", "c"])

    def test_single_value_returned_as_one_item(self):
        self.assertEqual(tools.split_tags("RELIG"), ["RELIG"])

    def test_empty_value_returns_empty_list(self):
        self.assertEqual(tools.split_tags(""), [])
        self.assertEqual(tools.split_tags(None), [])
        self.assertEqual(tools.split_tags(float("nan")), [])


class ClassifyColumnTests(unittest.TestCase):
    def test_empty_column(self):
        self.assertEqual(tools.classify_column(pd.Series(["", None, ""])), "empty")

    def test_multi_tag_column(self):
        series = pd.Series(["RELIG; ENV", "NATID", "DIGSOC; ENV", "RELIG"])
        self.assertEqual(tools.classify_column(series), "multi_tag")

    def test_numeric_column(self):
        series = pd.Series(["1.5", "2.75", "3.0", "4.2", "5.9"])
        self.assertEqual(tools.classify_column(series), "numeric")

    def test_year_column(self):
        series = pd.Series(["2001", "2002", "2001", "2003", "1999"])
        self.assertEqual(tools.classify_column(series), "year")

    def test_year_out_of_range_is_numeric_not_year(self):
        series = pd.Series(["1500", "1600", "1700"])
        self.assertEqual(tools.classify_column(series), "numeric")

    def test_categorical_column(self):
        series = pd.Series(["A"] * 40 + ["B"] * 40 + ["C"] * 20)
        self.assertEqual(tools.classify_column(series), "categorical")

    def test_text_column_high_cardinality(self):
        series = pd.Series([f"Unique title {i}" for i in range(100)])
        self.assertEqual(tools.classify_column(series), "text")


class SummarizeColumnsTests(unittest.TestCase):
    def test_reports_kind_and_counts_per_column(self):
        df = pd.DataFrame({
            "Title": [f"Paper {i}" for i in range(10)],
            "Tag": ["RELIG; ENV"] * 5 + ["NATID"] * 5,
            "Year": ["2001"] * 10,
            "Empty": [""] * 10,
        })
        summary = {row["column"]: row for row in tools.summarize_columns(df)}
        self.assertEqual(summary["Title"]["kind"], "text")
        self.assertEqual(summary["Tag"]["kind"], "multi_tag")
        self.assertEqual(summary["Tag"]["unique_values"], 3)  # RELIG, ENV, NATID
        self.assertEqual(summary["Year"]["kind"], "year")
        self.assertEqual(summary["Empty"]["kind"], "empty")
        self.assertEqual(summary["Empty"]["non_empty"], 0)
        self.assertAlmostEqual(summary["Tag"]["non_empty_pct"], 1.0)


class ValueCountsTests(unittest.TestCase):
    def test_counts_multi_tag_column_by_split_value(self):
        df = pd.DataFrame({"Tag": ["RELIG; ENV", "ENV", "RELIG", "", "DIGSOC"]})
        counts = dict(tools.value_counts(df, "Tag"))
        self.assertEqual(counts, {"RELIG": 2, "ENV": 2, "DIGSOC": 1})

    def test_counts_sorted_by_count_desc_then_value(self):
        df = pd.DataFrame({"Kind": ["b", "a", "a", "c", "b", "a"]})
        self.assertEqual(tools.value_counts(df, "Kind"), [("a", 3), ("b", 2), ("c", 1)])

    def test_order_key_sorts_numerically(self):
        df = pd.DataFrame({"Year": ["2003", "2001", "2001", "1999"]})
        self.assertEqual(
            tools.value_counts(df, "Year", order="key"),
            [("1999", 1), ("2001", 2), ("2003", 1)])


class CrossTabCountsTests(unittest.TestCase):
    def test_basic_crosstab(self):
        df = pd.DataFrame({
            "Module": ["RELIG", "RELIG", "ENV"],
            "Country": ["Germany", "France", "Germany"],
        })
        rows, cols, matrix = tools.cross_tab_counts(df, "Module", "Country")
        self.assertEqual(rows, ["RELIG", "ENV"])
        self.assertIn("Germany", cols)
        self.assertIn("France", cols)
        row_idx, col_idx = rows.index("RELIG"), cols.index("Germany")
        self.assertEqual(matrix[row_idx][col_idx], 1)

    def test_multi_tag_columns_contribute_to_every_combination(self):
        df = pd.DataFrame({
            "Module": ["RELIG; ENV"],
            "Country": ["Germany; France"],
        })
        rows, cols, matrix = tools.cross_tab_counts(df, "Module", "Country")
        self.assertEqual(set(rows), {"RELIG", "ENV"})
        self.assertEqual(set(cols), {"Germany", "France"})
        total = sum(sum(row) for row in matrix)
        self.assertEqual(total, 4)  # 2 modules x 2 countries

    def test_rows_missing_either_value_are_skipped(self):
        df = pd.DataFrame({"Module": ["RELIG", ""], "Country": ["", "Germany"]})
        rows, cols, matrix = tools.cross_tab_counts(df, "Module", "Country")
        self.assertEqual(rows, [])
        self.assertEqual(cols, [])


class NumericHistogramTests(unittest.TestCase):
    def test_bins_numeric_values(self):
        df = pd.DataFrame({"Score": [str(v) for v in range(1, 21)]})
        edges, counts = tools.numeric_histogram(df, "Score", bins=4)
        self.assertEqual(len(edges), 5)
        self.assertEqual(sum(counts), 20)

    def test_empty_when_nothing_numeric(self):
        df = pd.DataFrame({"Score": ["", "n/a", None]})
        edges, counts = tools.numeric_histogram(df, "Score")
        self.assertEqual(edges, [])
        self.assertEqual(counts, [])


class SuggestedChartTypesTests(unittest.TestCase):
    def test_categorical_offers_bar_and_pie(self):
        self.assertEqual(tools.suggested_chart_types("categorical"),
                         ["Bar chart", "Pie chart", "Table only"])

    def test_numeric_offers_histogram_only(self):
        self.assertEqual(tools.suggested_chart_types("numeric"), ["Histogram", "Table only"])

    def test_text_offers_table_only(self):
        self.assertEqual(tools.suggested_chart_types("text"), ["Table only"])


if __name__ == "__main__":
    unittest.main()
