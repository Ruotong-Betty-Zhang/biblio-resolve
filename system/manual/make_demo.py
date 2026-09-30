"""Create the small sample files the screenshot scripts load (manual/demo/).

A varied handful of real, public records from the project's ISSP bibliography:
some with a DOI, some with no link at all, some whose link is only in the
Notes, and some non-English titles. Also takes the first 40 rows of a lookup
result (before and after verification) for Review & Convert and Compare.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM = os.path.dirname(HERE)
PROJECT = os.path.dirname(SYSTEM)
DEMO = os.path.join(HERE, "demo")
sys.path.insert(0, SYSTEM)

import pandas as pd  # noqa: E402

import lookup_core as core  # noqa: E402
import translate_tools as tt  # noqa: E402

os.makedirs(DEMO, exist_ok=True)
df = core.read_records_file(os.path.join(PROJECT, "dataset", "ISSP Bibliography.ris"), as_text=True).fillna("")


def has(column):
    return df[column].astype(str).str.strip().ne("") if column in df else pd.Series(False, index=df.index)


language = df["Title"].map(lambda title: tt._detect_language(title) or "")  # slow: ~1-2 min
picks = []
picks += df[has("DOI") & (df["Item Type"] == "JOUR") & has("Abstract")].sample(6, random_state=3).index.tolist()
picks += df[~has("DOI") & ~has("Url") & (df["Item Type"] == "JOUR") & ~has("Notes")].sample(
    5, random_state=4).index.tolist()
link_in_note = df["Notes"].str.contains(r"https?://", regex=True) & ~has("DOI") & ~has("Url")
picks += df[link_in_note].sample(3, random_state=5).index.tolist()
picks += df[language.isin(["de", "es", "fr", "sv", "nl"])].sample(4, random_state=6).index.tolist()
demo = df.loc[list(dict.fromkeys(picks))].reset_index(drop=True)

core.write_records_file(demo, os.path.join(DEMO, "ISSP_sample.ris"), "ris")
linked = demo[demo["DOI"].ne("") | demo["Url"].ne("")].reset_index(drop=True)
core.write_records_file(linked, os.path.join(DEMO, "ISSP_sample_linked.ris"), "ris")

zotero_types = {"JOUR": "journalArticle", "THES": "thesis", "BOOK": "book", "CHAP": "bookSection",
                "RPRT": "report"}
pd.DataFrame({
    "Key": [f"DEMO{i:03d}" for i in range(len(demo))],
    "Item Type": demo["Item Type"].map(zotero_types).fillna("journalArticle"),
    "Publication Year": demo["Publication Year"], "Author": demo["Author"], "Title": demo["Title"],
    "Publication Title": demo.get("Publication Title", ""), "Volume": demo.get("Volume", ""),
    "Issue": demo.get("Issue", ""), "Pages": demo.get("Pages", ""), "Publisher": demo.get("Publisher", ""),
    "ISSN": demo.get("ISSN", ""), "DOI": demo.get("DOI", ""), "Url": demo.get("Url", ""),
    "Abstract Note": demo.get("Abstract", ""), "Notes": demo.get("Notes", ""),
    "Manual Tags": demo.get("Keywords", ""),
}).fillna("").to_csv(os.path.join(DEMO, "ISSP_sample.csv"), index=False, encoding="utf-8-sig")

for source, target in (("linked_records_combined_verified.csv", "lookup_results_verified.csv"),
                       ("linked_records_combined.csv", "lookup_results.csv")):
    core.read_records_file(os.path.join(PROJECT, "AI URL", source), as_text=True).head(40).to_csv(
        os.path.join(DEMO, target), index=False, encoding="utf-8-sig")
print(f"{len(demo)} demo records written to {DEMO}")
