"""
compare_tools.py
-----------------
Row-level diff between two bibliography tables (RIS/CSV/Excel) that may not
share the exact same columns, row order, or row count.

Records are matched between the two files by DOI when both sides have one
for that row; otherwise a normalized title (optionally combined with a year,
when a year column is given, to reduce accidental collisions between
unrelated papers that happen to share a title) is used as a fallback key.
This is a coarser match than the multi-source scoring used elsewhere in the
app for verifying a link against a live database result — it only needs to
answer "is this the same row in both spreadsheets", which DOI/title equality
already does well for two exports of the same-ish library.
"""
from __future__ import annotations

import os
import re

import pandas as pd

try:  # Package import: python -m system.test_compare_tools
    from . import abstract_note_tools as abstract_tools
    from . import lookup_core as core
    from . import stats_tools
except ImportError:  # Direct app/script import from inside system/
    import abstract_note_tools as abstract_tools
    import lookup_core as core
    import stats_tools

ADDED = "Added"
REMOVED = "Removed"
CHANGED = "Changed"
NEW_RECORD_FIELD = "(new record)"
REMOVED_RECORD_FIELD = "(removed record)"

_TITLE_PUNCTUATION_RE = re.compile(r"[^a-z0-9]+")


def normalize_title_key(title):
    """Lowercase, punctuation-insensitive key used to match records by title."""
    text = _TITLE_PUNCTUATION_RE.sub(" ", abstract_tools.clean_value(title).casefold())
    return re.sub(r"\s+", " ", text).strip()


def record_key(row, doi_column, title_column, year_column=None):
    """A DOI-based key when available, else a normalized-title key (with a
    year tiebreaker when a year column is given). None when neither exists."""
    if doi_column:
        doi = core.normalize_doi(abstract_tools.clean_value(row.get(doi_column, "")))
        if doi:
            return ("doi", doi, "")
    if title_column:
        title_key = normalize_title_key(row.get(title_column, ""))
        if title_key:
            year = abstract_tools.clean_value(row.get(year_column, "")) if year_column else ""
            return ("title", title_key, year)
    return None


def build_key_index(dataframe, doi_column, title_column, year_column=None):
    """Map record_key -> row index for the first row seen with that key.
    Later rows sharing an already-seen key are reported as duplicates and
    excluded from the comparison (rather than silently overwriting the
    first match), since we can't tell which one the other file meant."""
    index_by_key, duplicate_keys = {}, set()
    for index, row in dataframe.iterrows():
        key = record_key(row, doi_column, title_column, year_column)
        if key is None:
            continue
        if key in index_by_key:
            duplicate_keys.add(key)
        else:
            index_by_key[key] = index
    for key in duplicate_keys:
        index_by_key.pop(key, None)
    return index_by_key, duplicate_keys


def _field_value(row, column):
    return abstract_tools.clean_value(row.get(column, "")) if column else ""


def _values_differ(kind, value_a, value_b):
    if kind == "multi_tag":
        return set(stats_tools.split_tags(value_a)) != set(stats_tools.split_tags(value_b))
    return value_a != value_b


def _display_value(kind, value):
    if kind == "multi_tag":
        return "; ".join(sorted(stats_tools.split_tags(value)))
    return value


def compare_dataframes(df_a, df_b, doi_column_a=None, title_column_a=None, year_column_a=None,
                        doi_column_b=None, title_column_b=None, year_column_b=None):
    """Diff two bibliography tables.

    Returns (changes, summary):
    - changes: a list of dicts, one row per added/removed record and one
      row per changed field for records present in both files:
      {"key": str, "title": str, "status": Added|Removed|Changed,
       "field": column name (or a placeholder for whole-record rows),
       "old_value": str, "new_value": str}
    - summary: dict of counts and the column-structure difference, for an
      overview panel.
    """
    index_a, duplicates_a = build_key_index(df_a, doi_column_a, title_column_a, year_column_a)
    index_b, duplicates_b = build_key_index(df_b, doi_column_b, title_column_b, year_column_b)

    columns_a, columns_b = list(df_a.columns), list(df_b.columns)
    common_columns = [column for column in columns_a if column in columns_b]
    only_in_a = [column for column in columns_a if column not in columns_b]
    only_in_b = [column for column in columns_b if column not in columns_a]
    column_kinds = {
        column: stats_tools.classify_column(
            pd.concat([df_a[column], df_b[column]], ignore_index=True), column)
        for column in common_columns
    }

    display_title_a = title_column_a or (columns_a[0] if columns_a else None)
    display_title_b = title_column_b or (columns_b[0] if columns_b else None)

    keys_a, keys_b = set(index_a), set(index_b)
    changes = []

    for key in sorted(keys_a - keys_b, key=str):
        title = _field_value(df_a.loc[index_a[key]], display_title_a)
        changes.append({"key": key[1], "title": title, "status": REMOVED,
                        "field": REMOVED_RECORD_FIELD, "old_value": title, "new_value": ""})

    for key in sorted(keys_b - keys_a, key=str):
        title = _field_value(df_b.loc[index_b[key]], display_title_b)
        changes.append({"key": key[1], "title": title, "status": ADDED,
                        "field": NEW_RECORD_FIELD, "old_value": "", "new_value": title})

    matched_keys = sorted(keys_a & keys_b, key=str)
    changed_record_keys = set()
    for key in matched_keys:
        row_a, row_b = df_a.loc[index_a[key]], df_b.loc[index_b[key]]
        title = _field_value(row_a, display_title_a) or _field_value(row_b, display_title_b)
        for column in common_columns:
            value_a, value_b = _field_value(row_a, column), _field_value(row_b, column)
            if not value_a and not value_b:
                continue
            kind = column_kinds[column]
            if _values_differ(kind, value_a, value_b):
                changed_record_keys.add(key)
                changes.append({
                    "key": key[1], "title": title, "status": CHANGED, "field": column,
                    "old_value": _display_value(kind, value_a), "new_value": _display_value(kind, value_b),
                })

    summary = {
        "records in file A": len(df_a),
        "records in file B": len(df_b),
        "matched records": len(matched_keys),
        "records only in file A (removed)": len(keys_a - keys_b),
        "records only in file B (added)": len(keys_b - keys_a),
        "matched records with a changed field": len(changed_record_keys),
        "duplicate keys ignored in file A": len(duplicates_a),
        "duplicate keys ignored in file B": len(duplicates_b),
        "columns only in file A": only_in_a,
        "columns only in file B": only_in_b,
        "common columns compared": len(common_columns),
    }
    return changes, summary
