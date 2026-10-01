"""Build the Literature Lookup user guide PDF from the captured screenshots."""
import os

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, NextPageTemplate, PageBreak, PageTemplate, Frame,
                                Paragraph, Spacer, Table, TableStyle, BaseDocTemplate, CondPageBreak)
from reportlab.platypus.tableofcontents import TableOfContents

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, "shots")
OUT = os.path.join(HERE, "Literature Lookup - User Guide.pdf")
# DejaVu Sans ships with matplotlib (open licence, has arrows and other symbols).
import matplotlib  # noqa: E402
FONTS = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
pdfmetrics.registerFont(TTFont("Sans", os.path.join(FONTS, "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("Sans-Bold", os.path.join(FONTS, "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFont(TTFont("Sans-Italic", os.path.join(FONTS, "DejaVuSans-Oblique.ttf")))
pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans-Italic",
                              boldItalic="Sans-Bold")

BLUE = colors.HexColor("#0F6CBD")
DARK = colors.HexColor("#1B1B1B")
GREY = colors.HexColor("#5F6368")
LIGHT = colors.HexColor("#F3F6FA")
RED = colors.HexColor("#D62828")
VERSION = "Version 1.1 · October 2026"

body = ParagraphStyle("body", fontName="Sans", fontSize=9.6, leading=14, textColor=DARK, spaceAfter=5)
small = ParagraphStyle("small", parent=body, fontSize=8.6, leading=12, textColor=GREY)
bullet = ParagraphStyle("bullet", parent=body, leftIndent=13, bulletIndent=3, spaceAfter=2.5)
h1 = ParagraphStyle("h1", fontName="Sans-Bold", fontSize=19, leading=24, textColor=BLUE, spaceBefore=2,
                    spaceAfter=8, keepWithNext=1)
h2 = ParagraphStyle("h2", fontName="Sans-Bold", fontSize=13, leading=17, textColor=DARK, spaceBefore=10,
                    spaceAfter=5, keepWithNext=1)
h3 = ParagraphStyle("h3", fontName="Sans-Bold", fontSize=10.5, leading=14, textColor=DARK, spaceBefore=6,
                    spaceAfter=3, keepWithNext=1)
caption = ParagraphStyle("caption", parent=small, alignment=TA_CENTER, spaceBefore=3, spaceAfter=6)
cell = ParagraphStyle("cell", parent=body, fontSize=8.8, leading=12, spaceAfter=0)
cell_bold = ParagraphStyle("cell_bold", parent=cell, fontName="Sans-Bold")
note_style = ParagraphStyle("note", parent=body, fontSize=9.2, leading=13.2, spaceAfter=0)
toc_1 = ParagraphStyle("toc1", fontName="Sans", fontSize=10.5, leading=17, leftIndent=4)
toc_2 = ParagraphStyle("toc2", fontName="Sans", fontSize=9.2, leading=14, leftIndent=22, textColor=GREY)

CONTENT_WIDTH = A4[0] - 2 * 20 * mm


class GuideTemplate(BaseDocTemplate):
    def __init__(self, filename):
        super().__init__(filename, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                         topMargin=20 * mm, bottomMargin=18 * mm,
                         title="Literature Lookup - User Guide", author="Literature Lookup",
                         subject="User guide for the Literature Lookup desktop application")
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="main")
        self.addPageTemplates([PageTemplate("cover", [frame], onPage=self._cover_page),
                               PageTemplate("content", [frame], onPage=self._content_page)])

    @staticmethod
    def _cover_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(BLUE)
        canvas.rect(0, A4[1] - 95 * mm, A4[0], 95 * mm, fill=1, stroke=0)
        canvas.restoreState()

    @staticmethod
    def _content_page(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D9DEE5"))
        canvas.setLineWidth(0.6)
        canvas.line(20 * mm, A4[1] - 13 * mm, A4[0] - 20 * mm, A4[1] - 13 * mm)
        canvas.setFont("Sans", 8)
        canvas.setFillColor(GREY)
        canvas.drawString(20 * mm, A4[1] - 11 * mm, "Literature Lookup · User Guide")
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name in ("h1", "h2"):
            level = 0 if flowable.style.name == "h1" else 1
            text = flowable.getPlainText()
            key = f"h{self.seq.nextf('heading')}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=level > 0)
            self.notify("TOCEntry", (level, text, self.page, key))


story = []


def P(text, style=body):
    story.append(Paragraph(text, style))


def H1(text):
    story.append(CondPageBreak(60 * mm))
    story.append(Paragraph(text, h1))


def H2(text):
    story.append(CondPageBreak(35 * mm))
    story.append(Paragraph(text, h2))


def H3(text):
    story.append(Paragraph(text, h3))


def bullets(items):
    for item in items:
        story.append(Paragraph(item, bullet, bulletText="•"))
    story.append(Spacer(1, 3))


def steps(items):
    for number, item in enumerate(items, 1):
        story.append(Paragraph(item, bullet, bulletText=f"{number}."))
    story.append(Spacer(1, 3))


def callout_box(text, kind="Tip"):
    fill = {"Tip": colors.HexColor("#EAF3FC"), "Note": colors.HexColor("#F3F4F6"),
            "Important": colors.HexColor("#FDF1E6")}[kind]
    edge = {"Tip": BLUE, "Note": GREY, "Important": colors.HexColor("#C26A00")}[kind]
    table = Table([[Paragraph(f"<b>{kind}.</b> {text}", note_style)]], colWidths=[CONTENT_WIDTH])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), fill),
                               ("LINEBEFORE", (0, 0), (0, -1), 3, edge),
                               ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                               ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.append(Spacer(1, 3))
    story.append(table)
    story.append(Spacer(1, 7))


def table(rows, widths, header=True):
    data = [[Paragraph(str(c), cell_bold if (header and i == 0) else cell) for c in row]
            for i, row in enumerate(rows)]
    t = Table(data, colWidths=[w * CONTENT_WIDTH for w in widths], repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9DEE5")),
             ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
             ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), LIGHT))
    t.setStyle(TableStyle(style))
    story.append(t)
    story.append(Spacer(1, 8))


def figure(name, title, callouts=(), width=1.0):
    path = os.path.join(SHOTS, name + ".png")
    with PILImage.open(path) as im:
        w, h = im.size
    img_w = CONTENT_WIDTH * width
    img = Image(path, width=img_w, height=img_w * h / w)
    img.hAlign = "CENTER"
    frame = Table([[img]], colWidths=[img_w + 2])
    frame.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#C9CED6")),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    block = [frame, Paragraph(title, caption)]
    if callouts:
        rows = []
        for number, (label, text) in enumerate(callouts, 1):
            rows.append([Paragraph(f"<font color='white'><b>{number}</b></font>",
                                   ParagraphStyle("n", parent=cell, alignment=TA_CENTER, fontSize=8.4)),
                         Paragraph(f"<b>{label}</b>", cell), Paragraph(text, cell)])
        t = Table(rows, colWidths=[9 * mm, 0.27 * CONTENT_WIDTH, CONTENT_WIDTH - 9 * mm - 0.27 * CONTENT_WIDTH])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), RED),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LINEBELOW", (1, 0), (-1, -1), 0.3, colors.HexColor("#E3E7EC")),
                               ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                               ("LINEAFTER", (0, 0), (0, -1), 2, colors.white)]))
        block += [t, Spacer(1, 8)]
    # Pull the heading (and its short intro) that introduces this figure into
    # the same block, so a heading is never left alone at the bottom of a page.
    lead = []
    for index in range(len(story) - 1, max(-1, len(story) - 5), -1):
        item = story[index]
        if not isinstance(item, (Paragraph, Spacer)):
            break
        if isinstance(item, Paragraph) and item.style.name in ("h1", "h2", "h3"):
            lead = story[index:]
            del story[index:]
            break
    story.append(KeepTogether(lead + block))


# =============================================================================
# Cover
# =============================================================================
cover_title = ParagraphStyle("ct", fontName="Sans-Bold", fontSize=34, leading=40, textColor=colors.white)
cover_sub = ParagraphStyle("cs", fontName="Sans", fontSize=14, leading=20, textColor=colors.white)
story += [Spacer(1, 22 * mm), Paragraph("Literature Lookup", cover_title), Spacer(1, 5),
          Paragraph("User Guide", ParagraphStyle("cu", parent=cover_title, fontSize=22, leading=28)),
          Spacer(1, 8),
          Paragraph("Find, verify, enrich, review and convert bibliographic records", cover_sub),
          Spacer(1, 42 * mm)]
cover_img = os.path.join(SHOTS, "01_single_lookup.png")
with PILImage.open(cover_img) as im:
    cw, ch = im.size
img = Image(cover_img, width=CONTENT_WIDTH, height=CONTENT_WIDTH * ch / cw)
cover_frame = Table([[img]], colWidths=[CONTENT_WIDTH + 2])
cover_frame.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#C9CED6")),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
story += [cover_frame, Spacer(1, 10 * mm),
          Paragraph(f"<font color='#5F6368'>{VERSION} · Windows desktop application</font>", body),
          NextPageTemplate("content"), PageBreak()]

# =============================================================================
# Contents
# =============================================================================
story.append(Paragraph("Contents", ParagraphStyle("contents", parent=h1)))
toc = TableOfContents()
toc.levelStyles = [toc_1, toc_2]
toc.dotsMinLevel = 0
story += [toc, PageBreak()]

# =============================================================================
# 1 Introduction
# =============================================================================
H1("1. Introduction")
P("Literature Lookup is a desktop application for people who maintain a bibliography, typically a "
  "Zotero library, and need to fill in and check its links and metadata at scale. It finds missing DOIs and "
  "URLs, verifies that every link really points to the paper it claims to, fetches missing abstracts, tags "
  "records, cleans notes and keywords, and lets you review, compare and convert the results before you "
  "import them back into Zotero.")
P("Everything runs on your own computer. The app only goes online to query the public scholarly "
  "databases and translation services you enable. Your files are never uploaded anywhere, and the "
  "file you open is never overwritten: every page saves a <b>new copy</b>.")
H2("What each page is for")
table([
    ["Page", "Use it to…"],
    ["Single Lookup", "Look up one title and copy its DOI or link; verify the match."],
    ["Batch Import", "Find DOIs/URLs for a whole file of records. Its <i>Sources settings</i> sub-tab chooses "
                     "which databases every page searches."],
    ["Verification", "Check that each record's DOI/URL exists and describes that paper. Its "
                     "<i>Verification settings</i> sub-tab chooses which fields are compared."],
    ["Abstract Finder", "Fill in missing abstracts from scholarly sources or the record's own web page."],
    ["ISSP Module Tags", "Tag records with the ISSP survey module(s) they use, with evidence."],
    ["Note Link Recovery", "Move links typed into Notes into the URL field, and tidy the Notes."],
    ["Keyword Cleanup", "Keep your own uppercase keywords / country tags and drop imported ones."],
    ["Review & Convert", "Inspect, filter and manually review any result file; save it as CSV, Excel, RIS "
                         "or BibTeX (this page is also the file converter)."],
    ["Statistics", "Summarise columns with counts, percentages and charts."],
    ["Compare Documents", "Match the records of two files and show exactly which values differ."],
    ["Translate", "Add English (or another language) translations of titles and abstracts."],
], [0.24, 0.76])
H2("A typical workflow")
P("For a Zotero library that is missing links, the pages are usually used in this order. Each step "
  "reads the file saved by the previous one.")
steps([
    "<b>Export</b> your library from Zotero as RIS (or CSV).",
    "<b>Note Link Recovery</b>: recover links already written in Notes.",
    "<b>Batch Import</b>: find DOIs/URLs for the records that still have none.",
    "<b>Verification</b>: check every DOI/URL against the paper's metadata.",
    "<b>Review & Convert</b>: review the doubtful records by hand, then save.",
    "<b>Abstract Finder</b>, <b>ISSP Module Tags</b>, <b>Keyword Cleanup</b>, <b>Translate</b>: optional "
    "enrichment and clean-up steps.",
    "<b>Import</b> the final file back into Zotero.",
])

# =============================================================================
# 2 Getting started
# =============================================================================
H1("2. Getting started")
H2("Requirements")
bullets(["Windows 10 or Windows 11 (64-bit).",
         "An internet connection for the pages that search databases, verify links, fetch abstracts or "
         "translate. Review & Convert, Statistics, Compare Documents, Keyword Cleanup and Note Link "
         "Recovery work completely offline.",
         "No installation, no Python and no administrator rights are needed."])
H2("Starting the app for the first time")
steps(["Save <b>Literature Lookup.exe</b> anywhere, for example your Desktop or Documents folder.",
       "Double-click it. The first start takes about 5–10 seconds while the app unpacks itself; "
       "this happens on every start and is normal.",
       "Because the program is not digitally signed, Windows may show <i>“Windows protected your "
       "PC”</i>. Click <b>More info</b>, then <b>Run anyway</b>. You only need to do this once."])
callout_box("If your antivirus quarantines the file, add an exception for Literature Lookup.exe. "
            "Single-file applications built with Python are sometimes flagged by mistake.", "Note")
H2("Where your settings are stored")
P("Your settings (the sources you enabled, your contact email and any API keys) and the app's caches are "
  "saved automatically in <b>%APPDATA%\\Literature Lookup</b> (type that into the File Explorer "
  "address bar to open it). They are restored every time you start the app. To reset the app completely, "
  "close it and delete that folder.")
H2("Things that work the same on every page")
bullets([
    "<b>Choose file…</b> opens CSV, Excel (.xlsx/.xls), RIS, BibTeX (.bib) and CSL JSON files, "
    "including Zotero exports. Non-UTF-8 CSV files are detected automatically; the detected encoding is "
    "shown next to the file name.",
    "<b>Column mapping</b> menus are filled in automatically from the column names (Title, Author, "
    "Publication Year, DOI, Url, …). Check them and change any that were guessed wrongly.",
    "<b>Export format</b> defaults to <i>Same as source</i>; you can choose CSV, Excel, CSL JSON, RIS or "
    "BibTeX instead. Exports never overwrite the file you opened.",
    "<b>Stop</b> finishes the records currently in progress and keeps everything done so far.",
    "Results are <b>cached</b>: running the same file again skips records that were already processed "
    "and only queries the databases for new or changed ones.",
    "Every results table can be copied: right-click a cell for <i>Copy cell</i> / <i>Copy row</i>, or "
    "press Ctrl+C. Blue link cells open in your browser when clicked.",
])
H2("Zotero round trip")
P("Export from Zotero with <i>File → Export Library…</i> (or right-click a collection → "
  "<i>Export Collection…</i>) and choose <b>RIS</b> or <b>CSV</b>. After processing, import the saved file "
  "with <i>File → Import…</i>. Standard fields (DOI, URL, abstract, tags, notes) map to Zotero's own "
  "fields. Extra columns the RIS/BibTeX formats have no field for, such as verification details, are "
  "stored in each record's Note and are restored as columns when this app opens the file again. Before "
  "saving such columns to RIS or BibTeX, the app lists them and asks: <b>Yes</b> keeps them in the Note, "
  "<b>No</b> leaves them out of the file, <b>Cancel</b> does not save. CSV and Excel keep every column "
  "as an ordinary column.")
callout_box("When a cleanup page (Note Link Recovery or Keyword Cleanup) saves a RIS file as RIS, only "
            "the lines it changed are rewritten. Every other field of your original file, including "
            "volume, issue, pages, ISSN, dates and language, is kept exactly as it was.", "Tip")

# =============================================================================
# 3 Main window
# =============================================================================
H1("3. The main window")
figure("00_main_window", "The main window, opened on the Single Lookup page.",
       [("Page tabs", "Click a tab to switch page. Each page keeps its own file and results while you "
                      "work on another page. The tab labels shrink automatically to fit narrow windows.")])
P("Batch Import and Verification have two sub-tabs each, shown as a small switch below the page tabs: "
  "one for running the task and one for its settings.")

# =============================================================================
# 4 Single Lookup
# =============================================================================
H1("4. Single Lookup")
P("Use Single Lookup to find the DOI or link of one paper, for example while writing, or to check one "
  "record by hand.")
figure("01_single_lookup", "Searching for one title, with the best match selected and verified.", [
    ("Title (required)", "Type or paste the paper's title. Press Enter to search."),
    ("Author surname, Year", "Optional. They make the match score more reliable, especially for short "
                             "or common titles."),
    ("Search", "Queries every source enabled under Batch Import → Sources settings."),
    ("Candidates", "Matches from all sources, sorted by match score (0–100). The score combines title "
                   "similarity with author and year agreement. Click a row to select it."),
    ("DOI and Link", "The selected candidate's DOI and link, with Copy and Open in browser buttons."),
    ("Verify result", "Independently checks the DOI/link: the DOI must be registered and its metadata "
                      "must match your title, author and year. The verdict appears below."),
])
callout_box("The note above the table tells you when a source did not respond (for example “Semantic "
            "Scholar (HTTP 429)”, meaning it is rate-limited). The other sources' results are still "
            "shown; search again later for a complete list.", "Note")

# =============================================================================
# 5 Batch Import
# =============================================================================
H1("5. Batch Import")
P("Batch Import finds DOIs and URLs for every record in a file. It has two sub-tabs: "
  "<b>Batch Lookup</b>, where you run the search, and <b>Sources settings</b>, where you choose which "
  "databases are searched. The source selection is shared by Single Lookup, Verification and Abstract "
  "Finder.")
H2("5.1 Sources settings")
figure("02_sources_settings", "Batch Import → Sources settings.", [
    ("Contact email", "Optional. Crossref, PubMed and Unpaywall use it to identify polite users and give "
                      "them a more generous rate limit."),
    ("Semantic Scholar API key", "Optional and free. Raises Semantic Scholar's shared limit (about 100 "
                                 "requests per 5 minutes) to a dedicated 1 request per second."),
    ("CORE / Lens.org keys", "Required only if you enable CORE or Lens.org. Without a key those two "
                             "sources return nothing."),
    ("Sources to search", "Tick the databases to query. Mainstream sources are listed first, then "
                          "specialised and regional ones."),
])
P("All settings are saved automatically as you change them. The available sources:")
table([
    ["Source", "Coverage", "Key"],
    ["Crossref", "Journal articles, books, chapters (the main DOI registry).", "No (email optional)"],
    ["OpenAlex", "Broad coverage including theses and working papers.", "No"],
    ["Semantic Scholar", "About 200 million papers across all fields.", "Optional"],
    ["DataCite", "DOIs for theses, datasets and working papers.", "No"],
    ["OpenAIRE", "European research output.", "No"],
    ["Unpaywall", "Not a title search: adds an open-access URL to records that already have a DOI.",
     "No (email optional)"],
    ["DOAJ", "Open-access journal articles.", "No"],
    ["GESIS", "German social-science data archive (ISSP, SSOAR).", "No"],
    ["CORE", "300M+ records from institutional repositories.", "Required"],
    ["Lens.org", "Scholarly and patent literature.", "Required"],
    ["DNB, HAL, CiNii, swisscovery, Libris", "National catalogues of Germany, France, Japan, "
     "Switzerland and Sweden. Queried only for titles detected in that country's language(s).", "No"],
    ["Europe PMC, PubMed", "Biomedical and public-health literature.", "No"],
    ["Google Books", "Queried only for records whose Item Type is Book or Book Section.", "No"],
    ["arXiv", "Physics, maths, computer science and economics preprints.", "No"],
], [0.25, 0.55, 0.20])
callout_box("More sources find more links but take longer and are more likely to hit rate limits. "
            "Crossref, OpenAlex, DataCite and Unpaywall are a good default for most libraries; add GESIS "
            "for social-science survey literature and the national catalogues for non-English titles.", "Tip")

H2("5.2 Running a batch lookup")
figure("03_batch_loaded", "A Zotero CSV loaded and ready to run.", [
    ("Choose file…", "Open the file containing your records."),
    ("Title / Author / Year columns", "Which columns hold the search terms. Detected automatically."),
    ("DOI / URL / Item type columns", "Tell the app which records already have a link. Item type lets "
                                      "type-restricted sources (such as Google Books) run."),
    ("Which records to look up", "<i>Only records missing a DOI or a URL</i> (recommended); <i>only "
                                 "records missing both</i> (never touches a record that already has either); or "
                                 "<i>all records</i>. A value that is already present is only ever replaced by "
                                 "a newly found one, never blanked."),
    ("Concurrent workers", "How many records are searched at the same time. 4–6 is a good default; "
                           "lower it if you see many timeouts."),
    ("Run batch lookup", "Starts the search. Tick <i>Clear cached results before running</i> above it to "
                         "force every record to be searched again (for example after enabling new "
                         "sources)."),
])
figure("04_batch_results", "Finished batch lookup.", [
    ("Progress", "Shows progress, elapsed time and the estimated time remaining."),
    ("Results", "Title, status, match score, source, DOI and link of each record (first 200 shown; the "
                "export contains all). Scroll right for all columns."),
    ("Export format", "Format of the saved file. <i>Same as source</i> keeps the format you opened."),
    ("Export results…", "Saves a copy with the found DOIs/links filled in, plus the columns Lookup "
                        "Status, Match Score, Match Source, Matched Title and Failed Sources."),
])
H3("Lookup statuses")
table([
    ["Status", "Meaning"],
    ["Found", "A strong match: DOI/link filled in automatically."],
    ["Please verify", "A plausible match. Its DOI/link is filled in, but should be checked (use "
                      "Verification or Review & Convert)."],
    ["Low confidence", "Only a weak match was found; it is not written into the DOI/link fields."],
    ["matched_no_link", "A matching record was found, but the source has no DOI or link for it."],
    ["Not found", "No source returned a matching record."],
    ["Not found — search incomplete", "Nothing found, but at least one source failed to respond. "
                                      "Run again later."],
    ["URL added (Unpaywall)", "The record had a DOI; Unpaywall supplied an open-access URL."],
    ["Already had a DOI/URL — skipped", "Excluded by the “which records” setting."],
    ["Missing title", "The record has no title to search for."],
], [0.32, 0.68])

# =============================================================================
# 6 Verification
# =============================================================================
H1("6. Verification")
P("Verification checks, record by record, that each DOI or URL exists <b>and</b> belongs to that paper. "
  "It needs no previous lookup: any file with titles and DOIs/URLs can be verified. A merely similar "
  "title with a different identifier is never accepted as verification.")
H2("6.1 Verification settings")
figure("05_verification_settings", "Verification → Verification settings.", [
    ("Title (required)", "Always compared; the main title and title + subtitle are both tested."),
    ("Authors", "Compares author names, with and without accents."),
    ("Publication Year", "Off by default so that online-first and print years cannot cause false "
                         "mismatches. When on, a difference of ±1 year is accepted."),
])
P("Publisher, Item Type, Publication Title, Volume, Issue, Pages, ISBN and ISSN can also be switched on. "
  "Fields that are off are still kept in the exported file; they just do not affect the score or decision.")
H2("6.2 Verifying a file")
figure("06_verification_results", "A verified RIS file with one result selected.", [
    ("Column mapping", "Title, Author, Year, DOI and URL columns. Detected automatically."),
    ("DOI / URL columns", "The identifiers to check. Records with neither are reported as invalid."),
    ("Mode", "<b>Fast</b>: checks the DOI registry or the page/PDF metadata and stops at the first "
             "match. <b>High confidence</b>: additionally requires a second, independent source to "
             "confirm the same identifier. Slower, but stronger evidence."),
    ("Verify file / Stop", "Start or stop. Progress is saved after every record, so a stopped or "
                           "interrupted run resumes where it left off."),
    ("Results", "Each record's verification status, score, the title found in the metadata, and the "
                "resolved URL."),
    ("Selected result details", "The full reasoning for the selected record: status, score, decision "
                                "rule, conflicts, warnings and message."),
    ("Export verified file…", "Saves a copy with the verification columns added."),
])
H3("Verification statuses")
table([
    ["Status", "Meaning"],
    ["Verified", "The identifier exists and its metadata matches the record."],
    ["Verified with warning", "The core identity (title/authors) matches, but a secondary field such as "
                              "the publisher differs. Worth a quick look."],
    ["Verified via publisher page", "The registry metadata differed (e.g. a translated title), but the "
                                    "publisher's own page shows a matching title and author."],
    ["Verified via container page", "A chapter whose DOI belongs to the whole book; the book's page lists "
                                    "the chapter and its authors."],
    ["Container match - not verified", "The DOI identifies the parent book/journal, not this item."],
    ["Mismatch", "The identifier exists but describes a different paper."],
    ["Invalid identifier", "Malformed or unregistered DOI, a dead link, or no DOI/URL at all."],
    ["Unverifiable - insufficient metadata", "Reachable, but the page/PDF gives no usable metadata."],
    ["Temporarily unavailable", "The site blocked or rate-limited the request (e.g. HTTP 403/429). "
                                "Try again later; the check is not a verdict on the link."],
], [0.34, 0.66])
H2("6.3 Choosing which columns to export")
P("When you export, you can choose which of the added verification columns to keep:")
figure("07_verification_remove_columns", "Removing verification columns from the exported copy.", [
    ("Select all / Keep all", "Ticked columns are removed from the exported copy. All are ticked by "
                              "default; untick the ones you want to keep."),
    ("Continue to save…", "Opens the save dialog (the button to the left of the marker). Cancel returns "
                          "without saving."),
], width=0.62)

# =============================================================================
# 7 Abstract Finder
# =============================================================================
H1("7. Abstract Finder")
P("Abstract Finder fills in missing abstracts. It first asks the enabled scholarly sources, using title, "
  "author and year to make sure it has the right paper, and otherwise reads the record's own web page or "
  "PDF (from its URL or DOI). Existing abstracts are never replaced.")
figure("08_abstract_finder", "Abstract Finder after a run.", [
    ("Title / Author / Year columns", "Used to confirm that a found abstract belongs to this record."),
    ("URL / DOI columns", "Used for the web-page fallback."),
    ("Abstract column", "Where abstracts are written (the RIS <i>AB</i> field / Zotero's Abstract)."),
    ("Find missing abstracts", "Starts the search; Stop keeps what was found so far."),
    ("Coverage", "How many records have links and abstracts, before and after the run."),
    ("Results", "Fetch status, review tag, page title, abstract source and the abstract itself."),
])
P("A found abstract is always kept. If the page's title/author/year evidence does not clearly match the "
  "record, the record receives a tag you can filter on in Zotero:")
bullets(["<b>ABSTRACT_FOUND_POSSIBLE_MISMATCH</b>: the page appears to describe a different paper.",
         "<b>ABSTRACT_FOUND_NEEDS_REVIEW</b>: not enough evidence either way."])
P("The evidence (page title, authors, year, DOI and match scores) is written to extra columns in "
  "CSV/Excel exports and to the record Note in RIS/BibTeX exports.")

# =============================================================================
# 8 ISSP Module Tags
# =============================================================================
H1("8. ISSP Module Tags")
P("This page identifies which ISSP (International Social Survey Programme) module or modules a publication "
  "uses, with the evidence and a confidence level for every tag. The module tags are written into three "
  "columns, one per confidence level, so you can check them before any of them reach the keywords.")
figure("09_issp_module_tags", "ISSP Module Tags after classifying a file.", [
    ("Title / Abstract / Notes columns", "The text that is searched for module evidence."),
    ("Tags/Keywords column", "Where “DATA - &lt;country&gt;” tags (and module tags, if chosen in 5) are "
                             "added. Existing keywords are never removed."),
    ("Resolve GESIS DOIs online", "Looks up GESIS dataset DOIs (10.4232/1.xxxxx) to find the study they "
                                  "cite. Needed when a record cites only a DOI, not a ZA study number."),
    ("Search the full text", "Also downloads each linked page/PDF and searches it, reaching Methods/Data "
                             "sections an abstract misses. Much slower. It also tags which countries' "
                             "data were used, as “DATA - &lt;country&gt;”."),
    ("Module tags into the keywords", "<i>None (review later)</i>, the default, keeps module tags only "
                                      "in the confidence columns. <i>High</i>, <i>High + medium</i> or "
                                      "<i>All (incl. low)</i> also adds the tags at or above that level "
                                      "to the keywords."),
    ("Classify records", "Starts the classification."),
    ("Results", "Tag, confidence, status, data countries, where the evidence was found and the exact "
                "quote or reason."),
])
P("A record can receive more than one module tag. Each tag gets one of three confidence levels:")
table([
    ["Confidence", "Evidence", "Column"],
    ["High", "A ZA study number, a resolved GESIS DOI (10.4232/…), or an exact ISSP module name.",
     "ISSP Tags (high)"],
    ["Medium", "At least two topic phrases typical of one module.", "ISSP Tags (medium)"],
    ["Low", "Similar in meaning only (one best tag per record). Check these by hand.", "ISSP Tags (low)"],
], [0.16, 0.56, 0.28])
P("Each record also gets a status: <i>Tagged</i>, <i>Not reported</i> (there was readable text but no "
  "ISSP module was found) or <i>Unavailable</i> (no abstract or full text to analyse).")
P("When you export to RIS or BibTeX, the three columns are stored in each record's Note (the app asks "
  "first; see <i>Zotero round trip</i>). Open the exported file in <b>Review &amp; Convert</b> to see them "
  "as columns again and to add the tags you accept to the keywords with <b>Add values</b> "
  "(section 11.1).")
callout_box("The option “Match by meaning” is not included in this version of the app. It needs a "
            "large machine-learning component, so it is shown greyed out. All other matching steps run "
            "normally.", "Note")

# =============================================================================
# 9 Note Link Recovery
# =============================================================================
H1("9. Note Link Recovery")
P("Links are often pasted into a record's Notes instead of its URL field. This page finds them and, for "
  "records that have neither a URL nor a DOI, copies the first link into the URL field. It works "
  "entirely offline.")
figure("10_note_link_recovery", "Analysing notes in a RIS export.", [
    ("Notes / URL / DOI columns", "Detected automatically."),
    ("Analyze notes & add missing links", "Runs the analysis and fills the table."),
    ("Remove links from Notes", "Optional: deletes a link from the Note once it is in the URL/DOI field."),
    ("Clear Notes that contain only “ISSP”", "Optional clean-up of placeholder notes."),
    ("Statistics", "Records with notes, with links, and with links only in their notes."),
    ("Results", "Each record's existing or recovered link and the URLs found in its Note."),
    ("Export enriched copy…", "Saves the result."),
])
callout_box("A RIS file saved as RIS is patched rather than rewritten: only the Note (N1) and URL (UR) "
            "lines that changed are replaced. Every other line of each record stays exactly as it was.",
            "Tip")

# =============================================================================
# 10 Keyword Cleanup
# =============================================================================
H1("10. Keyword Cleanup")
P("Imported records often carry many automatically assigned keywords. This page keeps only your own "
  "curated keywords, recognised by being written in capitals (for example <i>SOCNET</i>, "
  "<i>SURVEY DESIGN</i>) and/or by naming a country (<i>Germany</i>, <i>DATA - Japan</i>), and removes "
  "the rest.")
figure("11_keyword_cleanup", "Previewing a keyword cleanup.", [
    ("Keywords field", "The column holding keywords. For Zotero CSV files <i>Manual Tags</i> is chosen "
                       "rather than <i>Automatic Tags</i>."),
    ("Keyword separator", "How keywords are separated. <i>Auto</i> detects semicolons, new lines or commas."),
    ("Preview cleanup", "Shows what would be kept and removed, without changing anything."),
    ("Keep rules", "Keep keywords that are all uppercase, and/or keywords that contain a country name."),
    ("Preview", "Original, kept and removed keywords for each record."),
    ("Export cleaned copy…", "Saves the cleaned file. RIS saved as RIS only rewrites the keyword (KW) lines."),
])

# =============================================================================
# 11 Review & Convert
# =============================================================================
H1("11. Review & Convert")
P("Review & Convert opens any result file for inspection and manual review, and saves it in any format. "
  "It makes no internet requests. Use it to check the records other pages flagged, to filter a file down "
  "to the records you need, or simply to convert between CSV, Excel, RIS and BibTeX.")
figure("12_review_convert", "A verification result filtered to the records that need attention.", [
    ("Choose existing file…", "Opens CSV, Excel, RIS, BibTeX or CSL JSON."),
    ("Save as", "Output format: CSV, Excel, RIS or BibTeX. Preset to the source file's format."),
    ("Records", "<i>All</i> records, or only the <i>Filtered</i> ones (selected automatically after you "
                "apply a filter)."),
    ("Columns", "<i>All</i> columns, or <i>Ticked only</i> (the columns ticked on the left). The summary "
                "next to the button shows what will be saved."),
    ("Save / convert…", "Saves the copy. For RIS/BibTeX, the app first lists any columns the format has "
                        "no field for and asks whether to keep them in the record Note."),
    ("Columns", "Tick the columns to display (and to keep with <i>Ticked only</i>). All / None tick every "
                "column; <i>Add manual field…</i> creates a new column for your own notes."),
    ("Filter records", "Build conditions (equals, contains, is blank, &gt;, between, …), combine them "
                       "with AND or OR, then click Apply. Clear removes all filters."),
    ("Open review queue…", "Opens the record-by-record review window for the current (filtered) records."),
    ("Records", "Paged table (50–1,000 rows per page). Click a DOI/URL to open it; double-click a row to "
                "review it."),
    ("Manual decision / Notes", "Quick decision for the selected row: Approved, Rejected, Needs review or "
                                "Unverifiable, plus a note. Click <i>Apply to selected row</i>."),
])
H2("11.1 Adding one column's values to another")
P("<b>Add values</b> copies the items of one column into another column, for example the module tags you "
  "accept from <i>ISSP Tags (low)</i> into <i>Keywords</i>. Items are separated by semicolons or new "
  "lines; an item already in the target is not added twice, and nothing is removed.")
figure("12b_add_values", "Adding a record's checked module tags to its keywords.", [
    ("Add values of", "The column to copy from. After an ISSP Module Tags run this is preset to "
                      "<i>ISSP Tags (low)</i>."),
    ("to", "The column to add to, normally the keywords column. A new column name creates it."),
    ("for", "<i>Selected record</i>, the <i>Filtered records</i>, or <i>All records</i>."),
    ("Add", "Adds the values and shows how many records changed. Save with <b>Save / convert…</b>."),
    ("Records", "Tick the ISSP Tags columns on the left to see them next to the keywords."),
])
callout_box("A typical workflow: add <i>ISSP Tags (high)</i> to <i>Keywords</i> for <b>All records</b>, "
            "then filter on <i>ISSP Tags (medium)</i> or <i>ISSP Tags (low)</i> “is not blank”, check each "
            "record, and add the tags you agree with for the <b>Selected record</b>.", "Tip")
H2("11.2 The review queue")
P("The review window shows one record at a time and compares the record's own values with the values "
  "found by lookup and verification.")
figure("13_review_dialog", "Reviewing one record: the input record (left) against the retrieved "
                           "metadata (right).", [
    ("Show comparison", "Switch the right-hand comparison on or off."),
    ("Key fields / All fields", "Key fields: Title, Authors, Year, Item type, Publisher, Publication, "
                                "DOI, URL and Abstract. All fields: every column of the file."),
    ("Edit fields", "Fields open read-only. Switch this on to correct a value; changes are written into "
                    "the file you save afterwards."),
    ("Previous / Next", "Move through the queue. Keep on top keeps the window above other windows."),
    ("Input record", "The record's own value. When the record's own cell is empty, the value found by the "
                     "lookup is shown in grey with the note “empty in record · showing lookup column”; it is "
                     "not saved unless you edit it."),
    ("Comparison", "The outcome for each field, e.g. Same after normalization, Year differs by 2, "
                   "Different."),
    ("Retrieved metadata", "The value found by verification or lookup, with the column it came from. Blue "
                           "DOIs and URLs open in the browser."),
    ("Verification message", "Why the record received its verification status."),
    ("Review state / Notes", "Mark the record Accepted or Not accepted and add a note."),
    ("Save & Next", "Saves the decision and moves to the next record. Save stays on this record; Close "
                    "ends the review."),
])
table([
    ["Colour", "Meaning"],
    ["Green", "The two values match (after ignoring case, punctuation and spacing where appropriate)."],
    ["Yellow", "Close but not identical: review recommended."],
    ["Red", "The values differ."],
    ["Grey", "One or both sides have no value, or the comparison is switched off."],
], [0.18, 0.82])
callout_box("Decisions are kept in memory while you work. Remember to click <b>Save / convert…</b> on "
            "the main page to write them to a file.", "Important")

# =============================================================================
# 12 Statistics
# =============================================================================
H1("12. Statistics")
P("Statistics summarises a file: which columns are filled in and how values are distributed. It works "
  "offline.")
figure("14_statistics", "Item types of a 12,000-record library.", [
    ("Column", "The column to summarise."),
    ("Group by", "Optional second column, for example Item Type by Publication Year."),
    ("Chart type", "Bar chart, pie chart or histogram, depending on the kind of column; or Table only."),
    ("Generate", "Builds the table and chart."),
    ("Column overview", "Every column with its detected kind, how many records fill it, and its number "
                        "of distinct values."),
    ("Chart", "The chart for the chosen column."),
    ("Counts", "Each value with its count and percentage. Keyword-type columns are counted per "
               "individual keyword."),
    ("Export table… / Save chart…", "Save the counts as CSV/Excel, or the chart as an image."),
    ("Resize", "Drag the grey bar between the upper area and the counts table to make either taller, "
               "and the bar between the overview and the chart to change their widths."),
])

# =============================================================================
# 13 Compare Documents
# =============================================================================
H1("13. Compare Documents")
P("Compare Documents matches the records of two files, which may differ in order, size and even column "
  "names, and shows exactly which values changed. Typical uses: before vs after a processing step, or "
  "your library vs a colleague's copy.")
H2("13.1 Setting up the comparison")
figure("15_compare_setup", "Choosing how records are matched and which columns are compared.", [
    ("Choose file A / file B", "The two files to compare."),
    ("Match records by: Title", "Required. Titles are compared ignoring case and punctuation."),
    ("+ Add match column", "Optional extra keys such as DOI, Year or Author. They only have to agree when "
                           "both records have a value, so an empty cell never prevents a match. Among "
                           "several records with the same title, the one agreeing on most keys is used."),
    ("Columns to compare", "Columns with the same name in both files. Click a row to tick it; the filter "
                           "box, Select shown and Clear help with long lists."),
    ("Pair differently named columns", "Compare two columns with different names, for example "
                                       "<i>url</i> in A with <i>Resolved URL</i> in B."),
    ("Compare", "Runs the comparison. Tick <i>Ignore case &amp; punctuation</i> to treat “SAGE” and "
                "“sage.” as equal."),
    ("Hide / Show setup", "The setup collapses after comparing, to give the results more room."),
])
H2("13.2 Reading the results")
figure("16_compare_results", "Comparison results with one record selected.", [
    ("Status filter", "All, Different, Same, Only in A, Only in B, No title, each with its count."),
    ("Column filter", "Show only records where one particular column differs."),
    ("Search title", "Narrow the list by title."),
    ("Records", "One row per record, coloured by status; each compared column shows “A value → B value” "
                "where the two differ."),
    ("Side-by-side detail", "Every match key (blue) and compared column (red where different) of the "
                            "selected record, from both files, with its row number in each file."),
    ("Export shown records…", "Saves the records currently shown, with A and B values and a “differs” "
                              "flag for each compared column."),
])

# =============================================================================
# 14 Translate
# =============================================================================
H1("14. Translate")
P("Translate adds translations of titles and/or abstracts, written as <b>original [translation]</b>, so "
  "the original text is always kept. The source language is detected automatically; text already in the "
  "target language is skipped and costs no quota.")
figure("17_translate", "The Translate page with a file loaded.", [
    ("Title column", "Titles to translate (choose (none) to skip)."),
    ("Abstract column", "Abstracts to translate (choose (none) to skip)."),
    ("Provider", "Azure Translator, DeepL or MyMemory (see below)."),
    ("Translate to", "The target language."),
    ("Re-translate rows", "When off (recommended), fields that already contain a bracketed translation "
                          "are skipped to save quota."),
    ("Translate / Stop", "Start or stop. Everything translated before a stop or error is kept and can be "
                         "exported."),
    ("Settings… / Clear cache…", "Enter API keys; clear the saved translations."),
])
figure("18_translate_settings", "Translation API keys (Settings…).", [
    ("Azure Translator key and region", "Free tier: 2,000,000 characters per month. Create a "
                                        "“Translator” resource at portal.azure.com."),
    ("DeepL API key", "Free tier: 500,000 characters per month, usually the best quality. Sign up at "
                      "deepl.com/pro-api (a free key ends in “:fx”)."),
    ("MyMemory contact email", "No account needed. 5,000 characters per day, or 50,000 per day with a "
                               "contact email."),
], width=0.6)
callout_box("Every translation is cached, so running the same file again, or continuing after a quota "
            "or network error, never pays for the same text twice.", "Tip")

# =============================================================================
# 15 Troubleshooting
# =============================================================================
H1("15. Troubleshooting and FAQ")
table([
    ["Problem", "What to do"],
    ["Windows says “Windows protected your PC”.", "Click More info → Run anyway. The app is not "
                                                   "digitally signed; this appears once."],
    ["The app takes a few seconds to open.", "Normal: the single-file app unpacks itself on each start."],
    ["A source shows HTTP 429 or “rate limited”.", "That database is limiting request speed. The app "
                                                   "retries automatically; for large files add a contact "
                                                   "email / API key, lower Concurrent workers, or run again "
                                                   "later. Cached records are not searched again."],
    ["“Not found — search incomplete”.", "A source did not respond. Run the same file again later; only "
                                         "the incomplete records are searched."],
    ["Verification says “Temporarily unavailable”.", "The publisher's site blocked the check (often HTTP "
                                                     "403). The link may be fine; open it in your "
                                                     "browser or verify again later."],
    ["DeepL: “rejected the API key (403)”.", "Check that the whole key was pasted into Settings, that it "
                                             "has not been regenerated, and that the DeepL API "
                                             "subscription is active."],
    ["MyMemory: “free daily quota is used up”.", "Wait for the reset time shown, add a contact email in "
                                                 "Settings (10× the quota), or switch provider. Work done "
                                                 "so far is kept."],
    ["A cached result looks wrong or outdated.", "Batch Import: tick “Clear cached results before "
                                                 "running”. Translate: Clear cache…"],
    ["Scanned PDFs are not read.", "Text recognition (OCR) for scanned PDFs is not included in this "
                                   "version; PDFs with a text layer are read normally."],
    ["I want to start over with default settings.", "Close the app and delete the folder "
                                                    "%APPDATA%\\Literature Lookup."],
], [0.36, 0.64])
H2("Privacy")
bullets(["Your files stay on your computer; nothing is uploaded.",
         "Only the title, author, year, DOI or URL needed for a search or check is sent to the databases "
         "you enabled. Text to be translated is sent to the translation provider you chose.",
         "API keys and your contact email are stored only in %APPDATA%\\Literature Lookup on your computer."])

doc = GuideTemplate(OUT)
doc.multiBuild(story)
print("wrote", OUT)
