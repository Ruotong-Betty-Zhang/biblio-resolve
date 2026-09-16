"""
stats_tools.py
--------------
Column-type detection and aggregation for the "Statistics" page: given an
arbitrary bibliography table (RIS/CSV/Excel, any columns), classify each
column so the GUI can offer the right chart types, then compute value
counts / cross-tabs on demand.

Column kinds:
- "multi_tag": semicolon-joined (or comma-joined) lists, e.g. the app's own
  "ISSP Module Tag" or a Zotero "Tags" column. Counted by splitting first,
  so a record tagged "RELIG; ENV" contributes to both categories rather
  than to a single combined bucket.
- "numeric": mostly parses as a number and isn't a plausible year.
- "year": mostly parses as a whole number in a plausible calendar-year range.
- "categorical": text with few distinct values relative to row count.
- "text": free text (titles, abstracts) — not useful to chart, only counted.
- "empty": column has no non-empty values at all.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

try:  # Package import: python -m system.test_stats_tools
    from . import abstract_note_tools as abstract_tools
except ImportError:  # Direct app/script import from inside system/
    import abstract_note_tools as abstract_tools

MULTI_TAG_SEPARATOR = ";"
MULTI_TAG_FALLBACK_SEPARATOR = ","
MULTI_TAG_MIN_FRACTION = 0.1  # >=10% of non-empty values contain the separator
CATEGORICAL_MAX_UNIQUE = 50
CATEGORICAL_MAX_UNIQUE_RATIO = 0.5
NUMERIC_MIN_FRACTION = 0.9
YEAR_MIN, YEAR_MAX = 1900, 2100

COLUMN_KINDS = ("multi_tag", "numeric", "year", "categorical", "text", "empty")


def split_tags(value):
    """Split a semicolon- (or comma-) joined tag string into clean parts."""
    text = abstract_tools.clean_value(value)
    if not text:
        return []
    if MULTI_TAG_SEPARATOR in text:
        separator = MULTI_TAG_SEPARATOR
    elif MULTI_TAG_FALLBACK_SEPARATOR in text:
        separator = MULTI_TAG_FALLBACK_SEPARATOR
    else:
        return [text]
    return [part.strip() for part in text.split(separator) if part.strip()]


def classify_column(series, column_name=""):
    """Return one of COLUMN_KINDS describing what a column mostly holds."""
    values = series.map(abstract_tools.clean_value)
    non_empty = values[values != ""]
    if len(non_empty) == 0:
        return "empty"

    multi_valued_fraction = non_empty.map(
        lambda v: MULTI_TAG_SEPARATOR in v or MULTI_TAG_FALLBACK_SEPARATOR in v).mean()
    if multi_valued_fraction >= MULTI_TAG_MIN_FRACTION:
        return "multi_tag"

    numeric = pd.to_numeric(non_empty, errors="coerce")
    if numeric.notna().mean() >= NUMERIC_MIN_FRACTION:
        numeric_values = numeric.dropna()
        looks_like_year = (
            numeric_values.between(YEAR_MIN, YEAR_MAX).mean() >= NUMERIC_MIN_FRACTION
            and (numeric_values % 1 == 0).mean() >= NUMERIC_MIN_FRACTION)
        return "year" if looks_like_year else "numeric"

    unique_count = non_empty.nunique()
    unique_ratio = unique_count / len(non_empty)
    if unique_count <= CATEGORICAL_MAX_UNIQUE and unique_ratio <= CATEGORICAL_MAX_UNIQUE_RATIO:
        return "categorical"
    return "text"


def summarize_columns(dataframe):
    """One row per column: kind, non-empty count/percentage, unique values."""
    total = len(dataframe)
    rows = []
    for column in dataframe.columns:
        series = dataframe[column]
        values = series.map(abstract_tools.clean_value)
        non_empty = values[values != ""]
        kind = classify_column(series, column)
        if kind == "multi_tag":
            unique_count = len({tag for value in non_empty for tag in split_tags(value)})
        else:
            unique_count = non_empty.nunique()
        rows.append({
            "column": column,
            "kind": kind,
            "non_empty": len(non_empty),
            "non_empty_pct": (len(non_empty) / total) if total else 0.0,
            "unique_values": unique_count,
        })
    return rows


def value_counts(dataframe, column, order="count"):
    """Counts for one column, splitting multi-tag values first.

    order="count" sorts by count descending (ties broken by value);
    order="key" sorts by the value itself (numerically when possible),
    which reads better for a year/date column shown as a trend."""
    series = dataframe[column].map(abstract_tools.clean_value)
    kind = classify_column(dataframe[column], column)
    counter = {}
    for value in series:
        if not value:
            continue
        for part in (split_tags(value) if kind == "multi_tag" else [value]):
            counter[part] = counter.get(part, 0) + 1

    if order == "key":
        def sort_key(item):
            try:
                return (0, float(item[0]))
            except ValueError:
                return (1, item[0])
        return sorted(counter.items(), key=sort_key)
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))


def cross_tab_counts(dataframe, row_column, col_column):
    """Counts of (row_value, col_value) pairs, splitting multi-tag columns
    on each side. Returns (row_values, col_values, matrix) sorted by
    descending total, where matrix[i][j] counts row_values[i] with
    col_values[j]. Records missing either value are skipped."""
    row_kind = classify_column(dataframe[row_column], row_column)
    col_kind = classify_column(dataframe[col_column], col_column)
    pair_counts, row_totals, col_totals = {}, {}, {}
    for _, record in dataframe.iterrows():
        row_raw = abstract_tools.clean_value(record[row_column])
        col_raw = abstract_tools.clean_value(record[col_column])
        if not row_raw or not col_raw:
            continue
        row_values = split_tags(row_raw) if row_kind == "multi_tag" else [row_raw]
        col_values = split_tags(col_raw) if col_kind == "multi_tag" else [col_raw]
        for row_value in row_values:
            row_totals[row_value] = row_totals.get(row_value, 0) + 1
            for col_value in col_values:
                pair_counts[(row_value, col_value)] = pair_counts.get((row_value, col_value), 0) + 1
        for col_value in col_values:
            col_totals[col_value] = col_totals.get(col_value, 0) + 1

    row_order = sorted(row_totals, key=lambda v: (-row_totals[v], v))
    col_order = sorted(col_totals, key=lambda v: (-col_totals[v], v))
    matrix = [[pair_counts.get((row_value, col_value), 0) for col_value in col_order]
              for row_value in row_order]
    return row_order, col_order, matrix


def numeric_histogram(dataframe, column, bins=20):
    """Bin edges and counts for a numeric column (non-numeric/empty dropped).
    Returns (bin_edges, counts) as plain lists, or ([], []) if nothing to bin."""
    numeric = pd.to_numeric(dataframe[column].map(abstract_tools.clean_value), errors="coerce").dropna()
    if numeric.empty:
        return [], []
    counts, edges = np.histogram(numeric.to_numpy(), bins=bins)
    return edges.tolist(), counts.tolist()


def suggested_chart_types(kind):
    """Chart types the GUI should offer for a column of this kind."""
    if kind in ("categorical", "multi_tag", "year"):
        return ["Bar chart", "Pie chart", "Table only"]
    if kind == "numeric":
        return ["Histogram", "Table only"]
    return ["Table only"]
