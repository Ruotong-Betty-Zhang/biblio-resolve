"""Measure how accurate ISSP Module Tags is on the whole bibliography.

The answers come from the bibliography itself: the original ISSP
Bibliography export already carries human-assigned module codes (RELIG,
ENV, ...) and country names in its keywords. The tagger is run WITHOUT
the keywords (only Title, Abstract, Notes), and its output is compared
with them.

Reported:
- Module tags, on records that have a module code in their keywords:
  tag-level precision and recall, how many records get any tag / a fully
  correct tag set / at least one correct tag, and precision broken down by
  confidence, by method, by module, and by whether an abstract exists.
- Records WITHOUT a module code in their keywords are not scored (an
  unlabelled record is not proof it used no module); how many of them the
  tagger tags is reported separately.
- Data-country tags against the country keywords, the same way.

Run:  python evaluate_module_tags.py [--input FILE] [--no-semantic]
"""

from __future__ import annotations

import argparse
import os
import re
from collections import Counter, defaultdict

import pandas as pd

import issp_module_tags as tagger

try:  # Package import
    from . import lookup_core as core
except ImportError:  # Direct script import from inside system/
    import lookup_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT = os.path.join(HERE, "..", "module", "ISSP Bibliography_final.ris")
DEFAULT_OUTPUT_DIR = os.path.join(HERE, "..", "outputs", "module_tag_evaluation")


def split_keywords(value):
    return [item.strip() for item in re.split(r"\s*(?:;|\n)\s*", tagger.clean_value(value))
            if item.strip()]


def gold_modules(keywords):
    return {item.upper() for item in keywords if item.upper() in tagger.ISSP_MODULE_TAG_CODES}


def gold_countries(keywords):
    """Keywords that name a country, as the tagger's canonical names
    ("United States--US" -> "USA", "Korea" -> "South Korea")."""
    found = set()
    for item in keywords:
        name = item.split("--")[0].strip().casefold()
        canonical = tagger.COUNTRY_NAME_TO_CANONICAL.get(name)
        if canonical:
            found.add(canonical)
    return found


def split_tags(value):
    return {item for item in tagger.clean_value(value).split("; ") if item}


def score_sets(pairs):
    """`pairs` is [(predicted set, gold set), ...] for labelled records."""
    true_positive = sum(len(predicted & gold) for predicted, gold in pairs)
    predicted_total = sum(len(predicted) for predicted, _ in pairs)
    gold_total = sum(len(gold) for _, gold in pairs)
    precision = true_positive / predicted_total if predicted_total else float("nan")
    recall = true_positive / gold_total if gold_total else float("nan")
    return {
        "records": len(pairs),
        "records_with_any_prediction": sum(1 for predicted, _ in pairs if predicted),
        "records_exactly_right": sum(1 for predicted, gold in pairs if predicted == gold),
        "records_at_least_one_right": sum(1 for predicted, gold in pairs if predicted & gold),
        "predicted_tags": predicted_total, "gold_tags": gold_total, "correct_tags": true_positive,
        "precision": precision, "recall": recall,
        "f1": (2 * precision * recall / (precision + recall)) if precision + recall else 0.0,
    }


def module_breakdowns(output_df, gold_by_row):
    """Precision per confidence level and per method, and precision/recall
    per module, over labelled records."""
    by_confidence, by_method = defaultdict(Counter), defaultdict(Counter)
    per_module = defaultdict(Counter)
    for index, gold in gold_by_row.items():
        row = output_df.loc[index]
        tags = [t for t in tagger.clean_value(row["ISSP Module Tag"]).split("; ") if t]
        confidences = tagger.clean_value(row["ISSP Module Confidence"]).split("; ")
        methods = tagger.clean_value(row["ISSP Module Method"]).split("; ")
        for tag, confidence, method in zip(tags, confidences, methods):
            correct = tag in gold
            by_confidence[confidence]["predicted"] += 1
            by_confidence[confidence]["correct"] += correct
            by_method[method]["predicted"] += 1
            by_method[method]["correct"] += correct
            per_module[tag]["predicted"] += 1
            per_module[tag]["correct"] += correct
        for tag in gold:
            per_module[tag]["gold"] += 1
            per_module[tag]["found"] += tag in tags

    def table(counter_map, key):
        rows = []
        for name, counts in sorted(counter_map.items()):
            rows.append({key: name, "tags_predicted": counts["predicted"],
                         "correct": counts["correct"],
                         "precision": counts["correct"] / counts["predicted"] if counts["predicted"] else float("nan")})
        return pd.DataFrame(rows)

    module_rows = []
    for tag, counts in sorted(per_module.items()):
        module_rows.append({
            "module": tag, "gold_records": counts["gold"], "predicted": counts["predicted"],
            "correct": counts["correct"],
            "precision": counts["correct"] / counts["predicted"] if counts["predicted"] else float("nan"),
            "recall": counts["found"] / counts["gold"] if counts["gold"] else float("nan"),
        })
    return table(by_confidence, "confidence"), table(by_method, "method"), pd.DataFrame(module_rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--no-semantic", action="store_true", help="skip the semantic tier")
    args = parser.parse_args(argv)

    frame = core.read_records_file(args.input).reset_index(drop=True)
    keyword_column = core.guess_column(frame.columns, tagger.abstract_tools.TAG_ALIASES)
    keywords = frame[keyword_column].map(split_keywords)
    title = core.guess_column(frame.columns, core.TITLE_ALIASES)
    abstract = core.guess_column(frame.columns, tagger.abstract_tools.ABSTRACT_ALIASES)
    notes = core.guess_column(frame.columns, tagger.abstract_tools.NOTE_ALIASES)

    # The tagger must not see the answers: drop the keyword column and let
    # it write its tags into a fresh column instead.
    blind = frame.drop(columns=[keyword_column])
    output_df, counts, _tag_column = tagger.tag_issp_modules(
        blind, text_columns=[title, abstract, notes], title_column=title,
        doi_column=core.guess_column(frame.columns, tagger.abstract_tools.DOI_ALIASES),
        tag_column="Predicted Tags", use_network_doi_lookup=False,
        use_semantic_matching=not args.no_semantic)

    has_abstract = frame[abstract].map(tagger.has_readable_text) if abstract else pd.Series(False, index=frame.index)
    module_gold = {i: gold_modules(k) for i, k in keywords.items() if gold_modules(k)}
    country_gold = {i: gold_countries(k) for i, k in keywords.items() if gold_countries(k)}
    predicted_modules = output_df["ISSP Module Tag"].map(split_tags)
    predicted_countries = output_df["ISSP Data Countries"].map(split_tags)

    summary_rows = []

    def add(name, pairs):
        summary_rows.append({"evaluation": name, **score_sets(pairs)})

    add("Modules - all labelled records", [(predicted_modules[i], g) for i, g in module_gold.items()])
    add("Modules - labelled, with abstract",
        [(predicted_modules[i], g) for i, g in module_gold.items() if has_abstract[i]])
    add("Modules - labelled, no abstract",
        [(predicted_modules[i], g) for i, g in module_gold.items() if not has_abstract[i]])
    high_only = predicted_modules.copy()
    for i in output_df.index:
        tags = [t for t in tagger.clean_value(output_df.at[i, "ISSP Module Tag"]).split("; ") if t]
        confidences = tagger.clean_value(output_df.at[i, "ISSP Module Confidence"]).split("; ")
        high_only[i] = {t for t, c in zip(tags, confidences) if c == "high"}
    add("Modules - high confidence tags only", [(high_only[i], g) for i, g in module_gold.items()])
    add("Countries - all records with country keywords",
        [(predicted_countries[i], g) for i, g in country_gold.items()])

    summary = pd.DataFrame(summary_rows)
    unlabelled = [i for i in output_df.index if i not in module_gold]
    unlabelled_tagged = sum(1 for i in unlabelled if predicted_modules[i])
    by_confidence, by_method, per_module = module_breakdowns(output_df, module_gold)

    os.makedirs(args.output_dir, exist_ok=True)
    summary.to_csv(os.path.join(args.output_dir, "summary.csv"), index=False)
    by_confidence.to_csv(os.path.join(args.output_dir, "precision_by_confidence.csv"), index=False)
    by_method.to_csv(os.path.join(args.output_dir, "precision_by_method.csv"), index=False)
    per_module.to_csv(os.path.join(args.output_dir, "per_module.csv"), index=False)
    detail = output_df[[title, "ISSP Module Tag", "ISSP Module Confidence", "ISSP Module Method",
                        "ISSP Module Status", "ISSP Module Evidence Quote", "ISSP Data Countries"]].copy()
    detail.insert(1, "Gold modules", ["; ".join(sorted(module_gold.get(i, ()))) for i in detail.index])
    detail.insert(2, "Module result", [
        "" if i not in module_gold else
        ("exact" if predicted_modules[i] == module_gold[i] else
         "partly right" if predicted_modules[i] & module_gold[i] else
         "missed" if not predicted_modules[i] else "wrong")
        for i in detail.index])
    detail.insert(3, "Gold countries", ["; ".join(sorted(country_gold.get(i, ()))) for i in detail.index])
    detail.to_csv(os.path.join(args.output_dir, "records.csv"), index_label="row")

    pd.set_option("display.width", 200)
    print(summary.round(3).to_string(index=False))
    print(f"\nUnlabelled records: {len(unlabelled)}, of which the tagger tagged {unlabelled_tagged}")
    print("\nPrecision by confidence:\n" + by_confidence.round(3).to_string(index=False))
    print("\nPrecision by method:\n" + by_method.round(3).to_string(index=False))
    print("\nPer module:\n" + per_module.round(3).to_string(index=False))
    print(f"\nTagger counts: {counts}")
    print(f"Wrote results to {os.path.abspath(args.output_dir)}")
    return summary


if __name__ == "__main__":
    main()
