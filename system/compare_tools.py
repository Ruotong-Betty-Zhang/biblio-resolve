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


# ---------------------------------------------------------------------------
# Column-pair comparison (Compare Documents page)
# ---------------------------------------------------------------------------
# The user chooses which column pairs identify a record (Title is required,
# others optional) and which column pairs to compare. Column names may differ
# between the two files, so everything is expressed as (column in A, column
# in B) pairs.

SAME = "Same"
DIFFERENT = "Different"
ONLY_A = "Only in A"
ONLY_B = "Only in B"
NO_TITLE = "No title"
STATUS_ORDER = (DIFFERENT, SAME, ONLY_A, ONLY_B, NO_TITLE)

_FLOAT_INTEGER_RE = re.compile(r"^-?\d+\.0+$")


def _clean(value):
    text = re.sub(r"\s+", " ", abstract_tools.clean_value(value)).strip()
    # 1999 and 1999.0 are the same year; spreadsheet exports disagree on it.
    return text[:text.index(".")] if _FLOAT_INTEGER_RE.match(text) else text


def normalize_match_value(value):
    """Loose key for an extra match column: a DOI in any spelling (URL form,
    doi: prefix, case) or otherwise case/punctuation-insensitive text."""
    text = _clean(value)
    if not text:
        return ""
    doi = core.normalize_doi(text)
    if doi and doi.startswith("10."):
        return "doi:" + doi.casefold()
    return normalize_title_key(text)


def pair_label(column_a, column_b):
    return column_a if column_a == column_b else f"{column_a} ↔ {column_b}"


def compare_by_column_pairs(df_a, df_b, title_pair, extra_match_pairs=(), compare_pairs=(),
                            ignore_case=False):
    """Match records of two tables and diff the chosen column pairs.

    Matching: a record in B is a candidate for a record in A when their
    normalized titles are equal. Each extra match pair must then agree
    *where both records have a value* - an empty cell on either side is
    ignored, so a missing DOI or year never prevents a match. Among several
    candidates the one agreeing on the most extra columns wins; remaining
    ties (true duplicates) are paired in file order. Every record is used
    at most once.

    ``ignore_case`` compares values case- and punctuation-insensitively;
    otherwise only surrounding/repeated whitespace is ignored. Columns that
    hold semicolon-separated tags are compared as sets.

    Returns (records, summary). Each record is a dict with ``status`` (one
    of STATUS_ORDER), ``index_a``/``index_b`` (None when absent), ``title``,
    ``match_note``, ``differences`` (list of pair labels that differ) and
    ``values`` {pair label: (value in A, value in B, differs)}.
    """
    title_a, title_b = title_pair
    labels = [pair_label(a, b) for a, b in compare_pairs]
    kinds = {}
    for (column_a, column_b), label in zip(compare_pairs, labels):
        kinds[label] = stats_tools.classify_column(
            pd.concat([df_a[column_a], df_b[column_b]], ignore_index=True), label)

    def row_keys(df, title_column, extra_columns):
        keys = []
        for _index, row in df.iterrows():
            keys.append((normalize_title_key(row.get(title_column, "")),
                         [normalize_match_value(row.get(column, "")) for column in extra_columns]))
        return keys

    keys_a = row_keys(df_a, title_a, [a for a, _b in extra_match_pairs])
    keys_b = row_keys(df_b, title_b, [b for _a, b in extra_match_pairs])
    b_by_title = {}
    for position, (title_key, _extras) in enumerate(keys_b):
        if title_key:
            b_by_title.setdefault(title_key, []).append(position)

    used_b, pairs = set(), []  # pairs: (position in A, position in B or None, note)
    for position_a, (title_key, extras_a) in enumerate(keys_a):
        if not title_key:
            pairs.append((position_a, None, "no title in file A"))
            continue
        best, best_score, tied = None, -1, 0
        for position_b in b_by_title.get(title_key, ()):
            if position_b in used_b:
                continue
            extras_b = keys_b[position_b][1]
            agree, conflict = 0, False
            for value_a, value_b in zip(extras_a, extras_b):
                if value_a and value_b:
                    if value_a == value_b:
                        agree += 1
                    else:
                        conflict = True
                        break
            if conflict:
                continue
            if agree > best_score:
                best, best_score, tied = position_b, agree, 1
            elif agree == best_score:
                tied += 1
        if best is None:
            pairs.append((position_a, None, ""))
            continue
        used_b.add(best)
        checked = sum(1 for value_a, value_b in zip(extras_a, keys_b[best][1]) if value_a and value_b)
        note = f"title + {best_score} of {len(extra_match_pairs)} extra key(s)" if extra_match_pairs else "title"
        if extra_match_pairs and checked < len(extra_match_pairs):
            note += " (others empty)"
        if tied > 1:
            note += f"; {tied} identical candidates, paired in file order"
        pairs.append((position_a, best, note))

    def compare_values(label, value_a, value_b):
        if kinds[label] == "multi_tag":
            return set(stats_tools.split_tags(value_a)) != set(stats_tools.split_tags(value_b))
        if ignore_case:
            return normalize_title_key(value_a) != normalize_title_key(value_b)
        return value_a != value_b

    records = []
    for position_a, position_b, note in pairs:
        row_a = df_a.iloc[position_a]
        row_b = df_b.iloc[position_b] if position_b is not None else None
        title = _clean(row_a.get(title_a, "")) or (_clean(row_b.get(title_b, "")) if row_b is not None else "")
        values, differences = {}, []
        for (column_a, column_b), label in zip(compare_pairs, labels):
            value_a = _clean(row_a.get(column_a, ""))
            value_b = _clean(row_b.get(column_b, "")) if row_b is not None else ""
            differs = row_b is not None and bool(value_a or value_b) and compare_values(label, value_a, value_b)
            values[label] = (value_a, value_b, differs)
            if differs:
                differences.append(label)
        if row_b is None:
            status = NO_TITLE if note.startswith("no title") else ONLY_A
        else:
            status = DIFFERENT if differences else SAME
        records.append({"status": status, "index_a": df_a.index[position_a],
                        "index_b": df_b.index[position_b] if position_b is not None else None,
                        "title": title, "match_note": note,
                        "differences": differences, "values": values})
    for position_b, (title_key, _extras) in enumerate(keys_b):
        if position_b in used_b:
            continue
        row_b = df_b.iloc[position_b]
        values = {label: ("", _clean(row_b.get(column_b, "")), False)
                  for (_column_a, column_b), label in zip(compare_pairs, labels)}
        records.append({"status": ONLY_B if title_key else NO_TITLE, "index_a": None,
                        "index_b": df_b.index[position_b], "title": _clean(row_b.get(title_b, "")),
                        "match_note": "" if title_key else "no title in file B",
                        "differences": [], "values": values})

    status_counts = {status: 0 for status in STATUS_ORDER}
    field_counts = {label: 0 for label in labels}
    for record in records:
        status_counts[record["status"]] += 1
        for label in record["differences"]:
            field_counts[label] += 1
    summary = {"records_a": len(df_a), "records_b": len(df_b),
               "status_counts": status_counts, "field_difference_counts": field_counts,
               "compared_labels": labels}
    return records, summary


def comparison_export_rows(records, labels):
    """Wide table for export: one row per record, A/B values side by side.
    Row numbers are spreadsheet rows (header = row 1)."""
    headers = ["Status", "Match note", "Row in A", "Row in B", "Title", "Differing columns"]
    for label in labels:
        headers += [f"{label} [A]", f"{label} [B]", f"{label} differs"]
    rows = []
    for record in records:
        row = [record["status"], record["match_note"],
               "" if record["index_a"] is None else record["index_a"] + 2,
               "" if record["index_b"] is None else record["index_b"] + 2,
               record["title"], "; ".join(record["differences"])]
        for label in labels:
            value_a, value_b, differs = record["values"][label]
            row += [value_a, value_b, "yes" if differs else ""]
        rows.append(row)
    return headers, rows
