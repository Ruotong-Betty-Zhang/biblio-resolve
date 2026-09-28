"""Compare sentence-embedding models for the semantic (tier 3) ISSP
module matcher in issp_module_tags.py.

How the comparison works:

1. Answers ("gold" tags) come from records that tiers 1 already tagged
   with high confidence by hard evidence - a ZA study number or the exact
   module name. No manual labelling is needed.
2. That hard evidence is then removed from the text (ZA numbers, GESIS
   DOIs, exact module names), so a model cannot score well just by
   spotting "Religion" or "ZA7570" - it has to recognise the topic from
   the rest of the title/abstract. Records with fewer than
   MIN_READABLE_WORDS words left are dropped.
3. Every model embeds the masked texts and the 12 module descriptions,
   exactly as SemanticModuleMatcher does, and ranks the 12 modules by
   cosine similarity.

Reported per model: top-1 and top-3 accuracy (a prediction is right if it
is any of the record's gold modules), accuracy for English and
non-English texts separately, how many records the app's current
thresholds would tag and how often those tags are right, precision on
the half of records the model is most sure about, and speed.

The gold tags are "silver" labels: exact-name matching has some false
positives (e.g. an abstract that mentions religion only in passing), so
absolute numbers are a floor; the ranking between models is what matters.

Run:  python semantic_model_comparison.py [--input FILE] [--models N ...]
Models are downloaded from Hugging Face once and then run offline.
"""

from __future__ import annotations

import argparse
import os
import re
import time

import numpy as np
import pandas as pd

import issp_module_tags as tagger

try:  # Package import
    from . import lookup_core as core
except ImportError:  # Direct script import from inside system/
    import lookup_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT = os.path.join(HERE, "..", "module", "ISSP Bibliography_final_issp_module_tags_V4.csv")
DEFAULT_OUTPUT_DIR = os.path.join(HERE, "..", "outputs", "semantic_model_comparison")

# "prefix" is text some models expect in front of every input (the E5
# family is trained with "query: "/"passage: "; for a symmetric
# text-vs-description comparison both sides use "query: ").
MODELS = [
    {"name": "sentence-transformers/all-MiniLM-L6-v2", "label": "all-MiniLM-L6-v2 (current)",
     "prefix": ""},
    {"name": "sentence-transformers/all-mpnet-base-v2", "label": "all-mpnet-base-v2", "prefix": ""},
    {"name": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
     "label": "multilingual-MiniLM-L12-v2", "prefix": ""},
    {"name": "intfloat/multilingual-e5-base", "label": "multilingual-e5-base", "prefix": "query: "},
]

GOLD_METHODS = {"za_number", "exact_module_name"}


# ---------------------------------------------------------------------------
# Evaluation set
# ---------------------------------------------------------------------------

_EXPLICIT_NAME_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(name) for name in sorted(
        list(tagger.EXACT_MODULE_NAMES) + list(tagger.EXACT_MODULE_NAMES_NEEDS_ISSP_NEARBY),
        key=len, reverse=True)) + r")(?!\w)", re.IGNORECASE)


def mask_explicit_evidence(text):
    """Remove the evidence tier 1 used (ZA numbers, GESIS dataset DOIs,
    exact module names), leaving the rest of the text for the models."""
    text = tagger.ZA_RE.sub(" ", text or "")
    text = tagger.GESIS_DOI_RE.sub(" ", text)
    text = _EXPLICIT_NAME_RE.sub(" ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


_ENGLISH_STOPWORDS = {
    "the", "of", "and", "in", "to", "a", "is", "that", "for", "on", "with", "as", "are", "by",
    "this", "we", "from", "be", "it", "an", "at", "which", "was", "these", "their", "or",
    "has", "have", "not", "between", "more", "than", "its", "they", "how", "our", "were",
}


def guess_language_group(text):
    """"English" if common English function words make up at least 12% of
    the words, else "Non-English". Rough, but enough to split the
    results for the multilingual models."""
    words = re.findall(r"[^\W\d_]+", (text or "").casefold())
    if not words:
        return "Non-English"
    share = sum(word in _ENGLISH_STOPWORDS for word in words) / len(words)
    return "English" if share >= 0.12 else "Non-English"


def build_evaluation_set(dataframe, text_columns):
    """One row per record that has gold tags and enough masked text left:
    columns row, gold (sorted list of tags), gold_method, text (masked),
    language."""
    rows = []
    for index, row in dataframe.iterrows():
        text = "\n".join(tagger.clean_value(row.get(column, "")) for column in text_columns)
        text = "\n".join(part for part in text.split("\n") if part)
        results = tagger.classify_record(text)
        gold = [item for item in results
                if item["confidence"] == "high" and item["method"] in GOLD_METHODS]
        if not gold:
            continue
        masked = mask_explicit_evidence(text)
        if not tagger.has_readable_text(masked):
            continue
        rows.append({
            "row": index,
            "gold": sorted({item["tag"] for item in gold}),
            "gold_method": "; ".join(sorted({item["method"] for item in gold})),
            "text": masked,
            "language": guess_language_group(masked),
        })
    return pd.DataFrame(rows, columns=["row", "gold", "gold_method", "text", "language"])


# ---------------------------------------------------------------------------
# Scoring (model-independent, so it can be tested with made-up numbers)
# ---------------------------------------------------------------------------

def score_similarities(similarities, tags, gold_lists, languages,
                       min_similarity=tagger.SemanticModuleMatcher.MIN_SIMILARITY,
                       min_margin=tagger.SemanticModuleMatcher.MIN_MARGIN):
    """`similarities` is an (n_records, n_tags) array. Returns (metrics
    dict, per-record predictions DataFrame)."""
    similarities = np.asarray(similarities, dtype=float)
    order = np.argsort(-similarities, axis=1)
    best = similarities[np.arange(len(order)), order[:, 0]]
    second = similarities[np.arange(len(order)), order[:, 1]]
    margin = best - second
    top1 = [tags[i] for i in order[:, 0]]
    top3 = [[tags[i] for i in ranked[:3]] for ranked in order]
    gold_sets = [set(gold) for gold in gold_lists]
    top1_ok = np.array([pred in gold for pred, gold in zip(top1, gold_sets)])
    top3_ok = np.array([bool(set(preds) & gold) for preds, gold in zip(top3, gold_sets)])
    tagged = (best >= min_similarity) & (margin >= min_margin)
    languages = np.asarray(languages)

    # Precision on the half of records the model is most sure about
    # (largest gap between its first and second choice) - comparable
    # across models even though their raw similarity scales differ.
    confident = np.argsort(-margin)[:max(1, len(margin) // 2)]

    def share(mask):
        return float(mask.mean()) if len(mask) else float("nan")

    metrics = {
        "records": len(top1),
        "top1_accuracy": share(top1_ok),
        "top3_accuracy": share(top3_ok),
        "top1_accuracy_english": share(top1_ok[languages == "English"]),
        "top1_accuracy_non_english": share(top1_ok[languages == "Non-English"]),
        "english_records": int((languages == "English").sum()),
        "non_english_records": int((languages == "Non-English").sum()),
        "current_thresholds_coverage": share(tagged),
        "current_thresholds_precision": share(top1_ok[tagged]),
        "precision_most_confident_half": share(top1_ok[confident]),
    }
    predictions = pd.DataFrame({
        "gold": ["; ".join(gold) for gold in gold_lists],
        "top1": top1, "top3": ["; ".join(preds) for preds in top3],
        "best_similarity": best.round(4), "margin": margin.round(4),
        "top1_correct": top1_ok, "top3_correct": top3_ok,
        "tagged_by_current_thresholds": tagged, "language": languages,
    })
    return metrics, predictions


def per_module_recall(predictions, label):
    """Top-1 recall per gold module (a record with two gold modules counts
    towards both)."""
    rows = []
    exploded = predictions.assign(gold=predictions["gold"].str.split("; ")).explode("gold")
    for tag, group in exploded.groupby("gold"):
        rows.append({"model": label, "module": tag, "records": len(group),
                     "top1_recall": float((group["top1"] == tag).mean())})
    return rows


# ---------------------------------------------------------------------------
# Running the models
# ---------------------------------------------------------------------------

def evaluate_model(model_config, evaluation_set, batch_size=32, model=None):
    """Embed and score one model. `model` can be injected for tests;
    otherwise the SentenceTransformer is loaded (downloaded once)."""
    if model is None:
        from sentence_transformers import SentenceTransformer  # deferred: optional dep
        model = SentenceTransformer(model_config["name"])
    prefix = model_config.get("prefix", "")
    tags = list(tagger.TOPIC_DESCRIPTIONS)
    descriptions = model.encode([prefix + tagger.TOPIC_DESCRIPTIONS[tag] for tag in tags],
                                normalize_embeddings=True)
    started = time.perf_counter()
    embeddings = model.encode([prefix + text for text in evaluation_set["text"]],
                              normalize_embeddings=True, batch_size=batch_size,
                              show_progress_bar=False)
    seconds = time.perf_counter() - started
    similarities = np.asarray(embeddings) @ np.asarray(descriptions).T
    metrics, predictions = score_similarities(
        similarities, tags, list(evaluation_set["gold"]), list(evaluation_set["language"]))
    metrics.update({
        "model": model_config["label"], "hf_name": model_config["name"],
        "max_tokens": getattr(model, "max_seq_length", None),
        "seconds": round(seconds, 1),
        "records_per_second": round(len(evaluation_set) / seconds, 1) if seconds else None,
    })
    predictions.insert(0, "model", model_config["label"])
    predictions.insert(1, "row", list(evaluation_set["row"]))
    return metrics, predictions


def plot_summary(summary, path):
    """Two panels on the same 0-100% scale: overall top-1/top-3 accuracy,
    and top-1 accuracy for English vs non-English texts."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    series_1, series_2 = "#2a78d6", "#eb6834"  # categorical slots 1-2
    ink, muted, grid, surface = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
    labels = list(summary["model"])[::-1]
    y = np.arange(len(labels))
    height = 0.36
    panels = [
        ("Overall accuracy", [("Top-1", "top1_accuracy", series_1),
                              ("Top-3", "top3_accuracy", series_2)]),
        ("Top-1 accuracy by language", [("English", "top1_accuracy_english", series_1),
                                        ("Non-English", "top1_accuracy_non_english", series_2)]),
    ]
    figure, axes = plt.subplots(1, 2, figsize=(12, 1.2 + 0.9 * len(labels)), sharey=True)
    figure.set_facecolor(surface)
    for axis, (title, series) in zip(axes, panels):
        axis.set_facecolor(surface)
        for offset, (name, column, color) in zip((height / 2, -height / 2), series):
            values = list(summary[column] * 100)[::-1]
            bars = axis.barh(y + offset, values, height=height - 0.04, color=color, label=name)
            for bar, value in zip(bars, values):
                if not np.isnan(value):
                    axis.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2,
                              f"{value:.0f}%", va="center", fontsize=8, color=muted)
        axis.set_xlim(0, 105)
        axis.set_title(title, loc="left", fontsize=11, color=ink)
        axis.xaxis.grid(True, color=grid, linewidth=0.8)
        axis.set_axisbelow(True)
        axis.tick_params(colors=muted, labelsize=9, length=0)
        for side in ("top", "right", "left"):
            axis.spines[side].set_visible(False)
        axis.spines["bottom"].set_color(grid)
        axis.legend(loc="lower right", bbox_to_anchor=(1, 1.0), ncol=2, frameon=False,
                    fontsize=9, labelcolor=muted)
    axes[0].set_yticks(y, labels, color=ink)
    figure.tight_layout()
    figure.savefig(path, dpi=160, facecolor=surface)
    plt.close(figure)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--models", nargs="*", type=int,
                        help="indices into MODELS to run (default: all)")
    parser.add_argument("--limit", type=int, help="only the first N evaluation records")
    args = parser.parse_args(argv)

    frame = core.read_records_file(args.input)
    columns = [core.guess_column(frame.columns, core.TITLE_ALIASES),
               core.guess_column(frame.columns, tagger.abstract_tools.ABSTRACT_ALIASES),
               core.guess_column(frame.columns, tagger.abstract_tools.NOTE_ALIASES)]
    columns = [column for column in columns if column]
    evaluation_set = build_evaluation_set(frame, columns)
    if args.limit:
        evaluation_set = evaluation_set.head(args.limit)
    print(f"Text columns: {columns}. Evaluation records: {len(evaluation_set)} "
          f"({(evaluation_set['language'] == 'English').sum()} English).")

    os.makedirs(args.output_dir, exist_ok=True)
    chosen = [MODELS[i] for i in args.models] if args.models else MODELS
    summaries, predictions, recalls = [], [], []
    for config in chosen:
        print(f"Running {config['name']} ...", flush=True)
        metrics, model_predictions = evaluate_model(config, evaluation_set)
        print(f"  top-1 {metrics['top1_accuracy']:.1%}, top-3 {metrics['top3_accuracy']:.1%}, "
              f"{metrics['seconds']} s", flush=True)
        summaries.append(metrics)
        predictions.append(model_predictions)
        recalls += per_module_recall(model_predictions, config["label"])

    first = ["model", "top1_accuracy", "top3_accuracy", "top1_accuracy_english",
             "top1_accuracy_non_english", "precision_most_confident_half",
             "current_thresholds_coverage", "current_thresholds_precision"]
    summary = pd.DataFrame(summaries)
    summary = summary[first + [column for column in summary.columns if column not in first]]
    summary.to_csv(os.path.join(args.output_dir, "summary.csv"), index=False)
    pd.concat(predictions).to_csv(os.path.join(args.output_dir, "predictions.csv"), index=False)
    evaluation_set.assign(gold=evaluation_set["gold"].str.join("; ")).to_csv(
        os.path.join(args.output_dir, "evaluation_set.csv"), index=False)
    recall_table = pd.DataFrame(recalls).pivot(index="module", columns="model", values="top1_recall")
    counts = pd.DataFrame(recalls).groupby("module")["records"].first()
    recall_table.insert(0, "records", counts)
    recall_table.to_csv(os.path.join(args.output_dir, "per_module_recall.csv"))
    plot_summary(summary, os.path.join(args.output_dir, "model_comparison.png"))
    print(f"Wrote results to {os.path.abspath(args.output_dir)}")
    return summary


if __name__ == "__main__":
    main()
