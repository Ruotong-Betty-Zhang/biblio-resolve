"""
evaluation_tools.py
--------------------
Measure the ISSP module classifier's real precision/recall against an
existing, independent label source: GESIS/FDZ's own curator-assigned module
codes, which are already mixed in among many other keywords in the
*original, unprocessed* bibliography file's Keywords column - before this
app's own classifier ever writes anything into that column.

This is not a perfect gold standard: a missing GESIS code doesn't prove a
paper didn't use that module, since GESIS's own tagging can itself be
incomplete. But it is a real, independently-assigned label for a meaningful
subset of records, and computing against it costs nothing (no manual
review needed) - see issp_module_tags.py's module docstring for the tier
design this checks, and stats_tools.py for the column-classification ideas
this module borrows the "split on ';'" convention from.

IMPORTANT: this module exists partly to guard against a real hazard.
tag_issp_modules()'s tag-merging (see issp_module_tags._merge_tags) treats
ANY occurrence of one of its 12 module codes in a tag column as something
the app must have written previously, and replaces it. So once a file has
been run through the classifier with tag-writing enabled, it no longer
reliably contains GESIS's original codes for records the classifier
disagreed with (or missed). Ground truth must always come from a file that
predates the app's very first run, never from a "..._issp_module_tags*"
export.
"""
from __future__ import annotations

try:  # Package import: python -m system.test_evaluation_tools
    from . import abstract_note_tools as abstract_tools
    from . import compare_tools
    from . import issp_module_tags as issp_tags
except ImportError:  # Direct app/script import from inside system/
    import abstract_note_tools as abstract_tools
    import compare_tools
    import issp_module_tags as issp_tags


def true_tags_from_keywords(value, tag_codes=None):
    """Module codes GESIS/FDZ already assigned, found in a Keywords-style
    semicolon-separated field. `tag_codes` defaults to the app's own 12
    module codes (issp_module_tags.ISSP_MODULE_TAG_CODES)."""
    tag_codes = tag_codes if tag_codes is not None else issp_tags.ISSP_MODULE_TAG_CODES
    text = abstract_tools.clean_value(value)
    if not text:
        return set()
    return {part.strip() for part in text.split(";") if part.strip() in tag_codes}


def _split_joined(value):
    """Split one of tag_issp_modules' own "; "-joined output columns (ISSP
    Module Tag/Confidence/Method) back into its parts, in order."""
    text = abstract_tools.clean_value(value)
    return [part.strip() for part in text.split(";") if part.strip()] if text else []


def predicted_tags_from_row(row, tag_column="ISSP Module Tag",
                            confidence_column="ISSP Module Confidence",
                            method_column="ISSP Module Method"):
    """(tag, confidence, method) triples for one classified record. The
    three columns are written by tag_issp_modules in lockstep (same order,
    same length) - see its final per-record loop - so zipping them back up
    recovers each tag's own confidence and method."""
    tags = _split_joined(row.get(tag_column, ""))
    confidences = _split_joined(row.get(confidence_column, ""))
    methods = _split_joined(row.get(method_column, ""))
    if len(confidences) != len(tags):
        confidences = [""] * len(tags)
    if len(methods) != len(tags):
        methods = [""] * len(tags)
    return list(zip(tags, confidences, methods))


def _new_precision_stat():
    """A miss (false negative) has no confidence or method - it's a tag
    that was never predicted, so it can't be attributed to one of these
    buckets. Only precision is meaningful here; recall/f1 would silently
    read as a vacuous 1.0 if computed against an always-zero false_negative."""
    return {"true_positive": 0, "false_positive": 0, "predicted_count": 0}


def _new_full_stat():
    return {"true_positive": 0, "false_positive": 0, "false_negative": 0,
            "true_count": 0, "predicted_count": 0}


def _finalize_precision_stat(stat):
    tp, fp = stat["true_positive"], stat["false_positive"]
    stat["precision"] = (tp / (tp + fp)) if (tp + fp) else None
    return stat


def _finalize_full_stat(stat):
    tp, fp, fn = stat["true_positive"], stat["false_positive"], stat["false_negative"]
    stat["precision"] = (tp / (tp + fp)) if (tp + fp) else None
    stat["recall"] = (tp / (tp + fn)) if (tp + fn) else None
    stat["f1"] = (2 * stat["precision"] * stat["recall"] / (stat["precision"] + stat["recall"])
                 if stat["precision"] and stat["recall"] else None)
    return stat


def evaluate_against_ground_truth(
        ground_truth_df, predicted_df, keywords_column,
        doi_column_truth=None, title_column_truth=None, year_column_truth=None,
        doi_column_pred=None, title_column_pred=None, year_column_pred=None,
        tag_column_pred="ISSP Module Tag", confidence_column_pred="ISSP Module Confidence",
        method_column_pred="ISSP Module Method", tag_codes=None):
    """Compare the classifier's output against GESIS/FDZ's own pre-existing
    module codes (see true_tags_from_keywords). Records are matched between
    the two tables the same way compare_tools.compare_dataframes does: DOI
    when available, else a normalized title (with year as a tiebreaker).

    Only records that (a) have at least one known code in `keywords_column`
    of `ground_truth_df` and (b) can be matched to a row in `predicted_df`
    are scored; every other record has no known answer to check against and
    is excluded rather than counted as wrong either way.

    Returns a dict:
    - "labeled_records": ground-truth records with >=1 known module code
    - "matched_records": of those, how many were found in predicted_df
    - "unmatched_records": labeled records not found in predicted_df at all
      (a matching/data problem worth checking, not a classifier failure)
    - "overall"/"by_tag": precision, recall and f1, micro-averaged over
      every (predicted tag, matched record) pair and every (true tag,
      matched record) pair
    - "by_confidence"/"by_method": precision only, over every (predicted
      tag, matched record) pair sliced by that tag's confidence/method. A
      missed tag has no confidence or method - it was never predicted - so
      recall isn't a meaningful thing to slice this way (it would read as
      an always-vacuous 1.0)
    - "false_positives": [{key, title, tag, confidence, method}] - tags the
      classifier predicted that GESIS's own tags don't confirm (may be a
      real error, or a case GESIS itself under-tagged)
    - "false_negatives": [{key, title, tag}] - GESIS-tagged modules the
      classifier didn't predict at any confidence for that record
    """
    tag_codes = tag_codes if tag_codes is not None else issp_tags.ISSP_MODULE_TAG_CODES
    index_truth, _dup_truth = compare_tools.build_key_index(
        ground_truth_df, doi_column_truth, title_column_truth, year_column_truth)
    index_pred, _dup_pred = compare_tools.build_key_index(
        predicted_df, doi_column_pred, title_column_pred, year_column_pred)

    columns_truth = list(ground_truth_df.columns)
    display_title_truth = title_column_truth or (columns_truth[0] if columns_truth else None)

    overall = _new_full_stat()
    by_confidence, by_method, by_tag = {}, {}, {}
    false_positives, false_negatives = [], []
    labeled_records = matched_records = unmatched_records = 0

    for key, truth_index in index_truth.items():
        truth_row = ground_truth_df.loc[truth_index]
        true_tags = true_tags_from_keywords(truth_row.get(keywords_column, ""), tag_codes)
        if not true_tags:
            continue
        labeled_records += 1
        title = abstract_tools.clean_value(truth_row.get(display_title_truth, "")) if display_title_truth else ""

        pred_index = index_pred.get(key)
        if pred_index is None:
            unmatched_records += 1
            continue
        matched_records += 1

        predictions = predicted_tags_from_row(
            predicted_df.loc[pred_index], tag_column_pred, confidence_column_pred, method_column_pred)
        predicted_tags = {tag for tag, _confidence, _method in predictions}

        for tag, confidence, method in predictions:
            stats_for_prediction = (overall, by_tag.setdefault(tag, _new_full_stat()),
                                    by_confidence.setdefault(confidence, _new_precision_stat()),
                                    by_method.setdefault(method, _new_precision_stat()))
            for stat in stats_for_prediction:
                stat["predicted_count"] += 1
                stat["true_positive" if tag in true_tags else "false_positive"] += 1
            if tag not in true_tags:
                false_positives.append({"key": key[1], "title": title, "tag": tag,
                                        "confidence": confidence, "method": method})

        for tag in true_tags:
            stat_tag = by_tag.setdefault(tag, _new_full_stat())
            overall["true_count"] += 1
            stat_tag["true_count"] += 1
            if tag not in predicted_tags:
                overall["false_negative"] += 1
                stat_tag["false_negative"] += 1
                false_negatives.append({"key": key[1], "title": title, "tag": tag})

    for stat in [overall, *by_tag.values()]:
        _finalize_full_stat(stat)
    for stat in [*by_confidence.values(), *by_method.values()]:
        _finalize_precision_stat(stat)

    return {
        "labeled_records": labeled_records,
        "matched_records": matched_records,
        "unmatched_records": unmatched_records,
        "overall": overall,
        "by_confidence": by_confidence,
        "by_method": by_method,
        "by_tag": by_tag,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
    }
