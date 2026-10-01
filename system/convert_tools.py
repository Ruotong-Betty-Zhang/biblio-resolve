"""
convert_tools.py
----------------
Filtering and format-conversion logic used by the "Review & Convert" page
(which absorbed the former File Converter page): filter rows, keep only the
chosen columns, and write the result as CSV, RIS or BibTeX.

RIS and BibTeX only have tags for a fixed set of bibliographic fields. Any
kept column the target format has no tag for is carried as a portable
"Literature Lookup field" line in the record Note (the same mechanism the
other export paths use), so converting CSV -> RIS -> CSV keeps every column.
"""
from __future__ import annotations

import re

import pandas as pd

try:  # Package import: python -m system.test_convert_tools
    from . import lookup_core as core
except ImportError:  # Direct app/script import from inside system/
    import lookup_core as core

# label -> (lookup_core writer format, extension)
CONVERT_FORMATS = {
    "CSV (.csv)": ("csv", ".csv"),
    "RIS (.ris)": ("ris", ".ris"),
    "BibTeX (.bib)": ("bibtex", ".bib"),
}
INPUT_EXTENSIONS = (".csv", ".ris", ".bib", ".bibtex")

TEXT_OPERATORS = ["equals", "not equals", "contains", "does not contain", "is blank", "is not blank"]
NUMERIC_OPERATORS = [">", ">=", "<", "<=", "=", "!=", "between"]
NO_VALUE_OPERATORS = {"is blank", "is not blank"}

# Alias groups each writer reads, in lookup_core's priority order. Only the
# first column present from a group is written natively; the others would be
# shadowed, so they travel as portable Note fields instead.
_COMMON_GROUPS = [
    ["Title", "Matched Title"],
    ["Author", "Authors"],
    ["Publication Year", "Year"],
    ["Publication Title", "Journal"],
    ["Volume"], ["Issue", "Number"], ["Pages"], ["Publisher"],
    ["ISBN"], ["ISSN"],
    ["DOI"],
    ["Link", "Url", "URL"],
    ["Abstract Note", "Abstract", "Summary"],
    ["Tags", "Tag", "Keywords", "Keyword"],
    ["Notes", "Note"],
    ["Item Type"],
]
NATIVE_FIELD_GROUPS = {
    "ris": _COMMON_GROUPS,
    "bibtex": _COMMON_GROUPS + [["Key"]],
}


def is_numeric_column(series, threshold=0.8):
    values = series.dropna().astype(str).str.strip()
    values = values[values.ne("")]
    return bool(not values.empty and pd.to_numeric(values, errors="coerce").notna().mean() >= threshold)


def condition_mask(series, operator, value="", upper=""):
    """Boolean mask for one filter condition. Raises ValueError on bad input."""
    if operator in NUMERIC_OPERATORS:
        numeric, target = pd.to_numeric(series, errors="coerce"), float(value)
        if operator == "between":
            result = numeric.between(target, float(upper), inclusive="both")
        else:
            result = {">": numeric > target, ">=": numeric >= target, "<": numeric < target,
                      "<=": numeric <= target, "=": numeric == target,
                      "!=": numeric != target}[operator]
        return (result & numeric.notna()).fillna(False)
    if operator not in TEXT_OPERATORS:
        raise ValueError(f"Unknown operator: {operator}")
    text = series.fillna("").astype(str).str.strip()
    folded, target = text.str.casefold(), str(value).strip().casefold()
    if operator == "equals":
        return folded == target
    if operator == "not equals":
        return folded != target
    if operator == "contains":
        return folded.str.contains(re.escape(target), na=False)
    if operator == "does not contain":
        return ~folded.str.contains(re.escape(target), na=False)
    if operator == "is blank":
        return text.eq("")
    return text.ne("")


def filter_dataframe(df, conditions, mode="AND"):
    """Return rows matching ``conditions`` = [(column, operator, value, upper)]."""
    if not conditions:
        return df
    use_or = str(mode).upper() == "OR"
    mask = pd.Series(not use_or, index=df.index)
    for column, operator, value, upper in conditions:
        if column not in df.columns:
            raise KeyError(f"Column not found: {column}")
        result = condition_mask(df[column], operator, value, upper)
        mask = (mask | result) if use_or else (mask & result)
    return df[mask]


_VALUE_LIST_SPLIT = re.compile(r"\s*(?:;|\n)\s*")


def split_values(value):
    """A semicolon/line-separated cell as a list of items (keywords, tags)."""
    text = "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)
    return [item for item in _VALUE_LIST_SPLIT.split(text.strip()) if item]


def add_column_values(df, indices, source, target):
    """Append the items of ``source`` to ``target`` (as "; "-separated
    tags) for the rows in ``indices``; items already in ``target`` are not
    repeated and nothing is removed. ``target`` is created if missing.
    Returns the number of rows that changed."""
    if source not in df.columns:
        raise KeyError(f"Column not found: {source}")
    if target not in df.columns:
        df[target] = ""
    elif df[target].dtype != object:
        df[target] = df[target].astype(object)
    changed = 0
    for index in indices:
        current = split_values(df.at[index, target])
        additions = [item for item in split_values(df.at[index, source]) if item not in current]
        if additions:
            df.at[index, target] = "; ".join(current + additions)
            changed += 1
    return changed


def portable_columns_for(columns, fmt):
    """Columns the target format has no native tag for (kept via the Note)."""
    groups = NATIVE_FIELD_GROUPS.get(fmt)
    if groups is None:
        return []
    by_folded = {}
    for c in columns:
        by_folded.setdefault(str(c).strip().casefold(), c)
    native = set()
    for group in groups:
        for alias in group:
            # Same rule as lookup_core's writer: an exact name ("DOI") beats a
            # case-insensitive one ("doi"), which then travels in the Note.
            column = alias if alias in columns else by_folded.get(alias.casefold())
            if column is not None:
                native.add(column)
                break
    if fmt == "ris":
        # "RIS DA", "RIS LA", ... are written back under their own tag.
        native.update(c for c in columns if core.ris_extra_tag(c))
    return [c for c in columns if c not in native]


def prepare_output(df, columns, conditions=(), mode="AND"):
    """Filter rows, then keep ``columns`` in the given order."""
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise KeyError("Columns not found: " + ", ".join(map(str, missing)))
    return filter_dataframe(df, list(conditions), mode)[list(columns)].reset_index(drop=True)


def convert_file(df, path, fmt, columns, conditions=(), mode="AND"):
    """Write the filtered, column-selected records; returns (rows, portable)."""
    if fmt not in {f for f, _ext in CONVERT_FORMATS.values()}:
        raise ValueError(f"Unsupported output format: {fmt}")
    if not columns:
        raise ValueError("Select at least one column to keep.")
    output = prepare_output(df, columns, conditions, mode)
    portable = portable_columns_for(list(output.columns), fmt)
    core.write_records_file(output, path, fmt, portable_columns=portable)
    return len(output), portable
