"""Tag bibliographic records with the ISSP topical module(s) they used.

The GUI imports this module, but the functions are deliberately independent
of the UI (same convention as abstract_note_tools.py) so they can be tested
or reused from a notebook.

A record can legitimately use more than one ISSP module (e.g. a trend
paper citing both a Work Orientations wave and a Social Inequality wave),
so classify_record() does not stop at the first tier that finds something -
it checks every tier and returns every distinct module it found evidence
for, each with its own confidence. If the same module is found by more
than one tier, it is only reported once, at the best confidence found.

Tiers, from most to least reliable:

1. High confidence - hard evidence, all collected (no early stop):
   - A GESIS ZA study number cited in the text (e.g. "ZA7570"). This is
     the permanent archive identifier GESIS assigns to a module release
     and does not change across data-file versions.
   - A GESIS dataset DOI (10.4232/1.xxxxx) cited in the text. Resolved
     live against the DataCite API to read the title (which names the
     module) - requires network access.
   - The exact module name found verbatim in the text. Seven of the
     twelve names are distinctive multi-word phrases ("Family and
     Changing Gender Roles", "Work Orientations", ...) unlikely to appear
     in an unrelated paper by coincidence, so they count on their own.
     The other five (Religion, Environment, Citizenship, National
     Identity, Social Networks) are also everyday academic vocabulary, so
     they only count here when they appear near an explicit "ISSP"
     mention - otherwise they are left to the keyword tier below.
2. Medium confidence - scored keyword/topic matching: each module has a
   list of topic phrases, and a module only counts if it clears a minimum
   number of distinct phrase hits (a single incidental phrase like "job
   satisfaction" in an unrelated labour-economics paper should not, by
   itself, tag it as ISSP Work Orientations). Every module that
   independently clears the bar is included - this is not an "only if
   unique" decision like the old version.
3. Low confidence - free, local semantic similarity: always runs, in
   addition to the tiers above, not only when they found nothing. Every
   module has a short reference description of what it covers; the
   record's text and all 12 descriptions are embedded with a small
   open-source sentence-embedding model (sentence-transformers,
   downloaded once from Hugging Face and then run entirely offline - no
   API key, no per-call cost, nothing sent anywhere after that first
   download) and matched by cosine similarity. Only its single best
   candidate is ever considered (unlike tiers 1-2, it does not report
   more than one module), and only added if that module was not already
   found above.

The ISSP-year tier ("ISSP 2018" -> Religion IV) that used to sit here has
been removed for now at the user's request (a paper's stated year does not
always match the year GESIS actually archived the dataset under, and it
was judged too unreliable to keep as a standing signal) - YEAR_TO_TAG and
extract_issp_years() are still defined below since they are useful
reference data, they are just not wired into classify_record() any more.
Re-enabling it is a matter of adding one more block back into the
function.

None of this replaces reading the paper's data/methods section. It is a
triage aid: sort a batch of records into "confidently tagged", "probably
this one, check it", and "no idea, look yourself".
"""

from __future__ import annotations

import re
import time

import requests

import abstract_note_tools as abstract_tools

clean_value = abstract_tools.clean_value
guess_column = abstract_tools.guess_column


# ---------------------------------------------------------------------------
# Reference data, transcribed from GESIS's own master study list
# (ISSP_Studienliste_gesamt, retrieved from gesis.org) plus the tag codes
# the user supplied. If GESIS revises a year's module assignment or adds a
# new one, this table is the only place that needs updating.
# ---------------------------------------------------------------------------

TOPIC_TAGS = {
    "Citizenship": "CIT",
    "National Identity": "NATID",
    "Digital Societies": "DIGSOC",
    "Environment": "ENV",
    "Family and Changing Gender Roles": "FAMGEN",
    "Health and Health Care": "HLTH",
    "Leisure Time and Sports": "LEISPORT",
    "Religion": "RELIG",
    "Role of Government": "ROG",
    "Social Inequality": "SOCINEQ",
    "Social Networks": "SOCNET",
    "Work Orientations": "WORKORI",
}
ISSP_MODULE_TAG_CODES = set(TOPIC_TAGS.values())

# Year of the main cross-national ("Integrated") release -> tag. 1985-2021
# is from GESIS's "Study List: Integrated ISSP Data Sets" master PDF;
# 2022-2024 is from ISSP's own "Data Download by topic" schedule page
# (issp.org), which is the authoritative, current source. 2025 (Work
# Orientations V) and 2026 (Role of Government VI) are listed there as
# "under development" with no data released yet, so they are deliberately
# left out of this table - a paper citing "ISSP 2025" today cannot
# actually be analysing that dataset. 2024 (Digital Societies) is included
# even though GESIS's own page says the data isn't expected until summer
# 2026 - the year-to-module mapping itself is confirmed, it is just early.
YEAR_TO_TAG = {
    1985: "ROG", 1986: "SOCNET", 1987: "SOCINEQ", 1988: "FAMGEN", 1989: "WORKORI",
    1990: "ROG", 1991: "RELIG", 1992: "SOCINEQ", 1993: "ENV", 1994: "FAMGEN",
    1995: "NATID", 1996: "ROG", 1997: "WORKORI", 1998: "RELIG", 1999: "SOCINEQ",
    2000: "ENV", 2001: "SOCNET", 2002: "FAMGEN", 2003: "NATID", 2004: "CIT",
    2005: "WORKORI", 2006: "ROG", 2007: "LEISPORT", 2008: "RELIG", 2009: "SOCINEQ",
    2010: "ENV", 2011: "HLTH", 2012: "FAMGEN", 2013: "NATID", 2014: "CIT",
    2015: "WORKORI", 2016: "ROG", 2017: "SOCNET", 2018: "RELIG", 2019: "SOCINEQ",
    2020: "ENV", 2021: "HLTH", 2022: "FAMGEN", 2023: "NATID", 2024: "DIGSOC",
}

# ZA study number -> tag. Covers all four sections of GESIS's master list:
# the main Integrated series, the two non-ISSP-member-country extensions,
# the cross-year Cumulated files (still a single topic even though they
# span several years), and the "Separate Country Data Sets - Not (yet)
# integrated" list (late/standalone national releases, resolved from
# whichever module+year they say in their own title, or from YEAR_TO_TAG
# when the entry only gives a bare year).
ZA_TO_TAG = {
    # Integrated ISSP Data Sets
    1490: "ROG", 1620: "SOCNET", 1680: "SOCINEQ", 1700: "FAMGEN", 1840: "WORKORI",
    1950: "ROG", 2150: "RELIG", 2310: "SOCINEQ", 2450: "ENV", 2620: "FAMGEN",
    2880: "NATID", 2900: "ROG", 3090: "WORKORI", 3190: "RELIG", 3430: "SOCINEQ",
    3440: "ENV", 3680: "SOCNET", 3880: "FAMGEN", 3910: "NATID", 3950: "CIT",
    4350: "WORKORI", 4700: "ROG", 4850: "LEISPORT", 4950: "RELIG", 5400: "SOCINEQ",
    5500: "ENV", 5800: "HLTH", 5900: "FAMGEN", 5950: "NATID", 6670: "CIT",
    6770: "WORKORI", 6900: "ROG", 6980: "SOCNET", 7570: "RELIG", 7600: "SOCINEQ",
    7650: "ENV", 8000: "HLTH",
    # Non-ISSP-member-country extensions
    5690: "RELIG", 7630: "RELIG",
    # Cumulated (multi-year, single topic) files
    4747: "ROG", 4748: "ROG", 5960: "NATID", 5961: "NATID",
    8790: "SOCINEQ", 8792: "RELIG", 8793: "ENV", 8797: "WORKORI",
    # Separate Country Data Sets - Not (yet) integrated
    1303: "SOCNET", 1306: "SOCINEQ", 1784: "SOCNET", 1977: "FAMGEN", 2496: "SOCINEQ",
    2793: "ENV", 3024: "ENV", 3293: "SOCINEQ", 3297: "SOCINEQ", 3351: "WORKORI",
    3375: "ENV", 3558: "SOCINEQ", 3562: "SOCINEQ", 3612: "WORKORI", 3613: "SOCINEQ",
    3916: "SOCNET", 4656: "CIT", 4674: "RELIG", 4831: "LEISPORT", 4861: "LEISPORT",
    4966: "RELIG", 5389: "SOCINEQ", 5517: "NATID", 5518: "CIT", 5519: "CIT",
    5520: "WORKORI", 5521: "SOCNET", 5522: "RELIG", 5962: "SOCNET", 5995: "SOCINEQ",
    7629: "RELIG", 7774: "RELIG", 7810: "SOCINEQ", 7811: "SOCINEQ", 7812: "SOCINEQ",
    7813: "SOCINEQ", 7845: "SOCINEQ", 7988: "ENV", 8783: "HLTH", 8784: "HLTH",
    8872: "HLTH",
}

# Ordered longest/most-specific phrase first, since a DOI-resolved title or
# a plain-keyword scan is matched by first substring hit.
TOPIC_PHRASES = [
    ("family and changing gender roles", "FAMGEN"),
    ("changing gender roles", "FAMGEN"),
    ("social networks and support systems", "SOCNET"),
    ("social relations and support systems", "SOCNET"),
    ("social networks and social resources", "SOCNET"),
    ("social networks", "SOCNET"),
    ("social inequality", "SOCINEQ"),
    ("health and health care", "HLTH"),
    ("leisure time and sports", "LEISPORT"),
    ("role of government", "ROG"),
    ("national identity", "NATID"),
    ("digital societ", "DIGSOC"),
    ("citizenship", "CIT"),
    ("environment", "ENV"),
    ("religio", "RELIG"),
    ("work orientation", "WORKORI"),
]

# High-confidence "exact module name" tier. These names count as strong
# evidence on their own because a non-ISSP paper is very unlikely to use
# this exact multi-word phrasing by coincidence.
EXACT_MODULE_NAMES = {
    "family and changing gender roles": "FAMGEN",
    "work orientations": "WORKORI",
    "role of government": "ROG",
    "social inequality": "SOCINEQ",
    "health and health care": "HLTH",
    "leisure time and sports": "LEISPORT",
    "digital societies": "DIGSOC",
    "social networks and social resources": "SOCNET",
    "social networks and support systems": "SOCNET",
    "social relations and support systems": "SOCNET",
}
# These five module names double as everyday academic vocabulary, so
# finding the bare word/phrase proves nothing on its own - it only counts
# as exact-name evidence when it appears within ISSP_PROXIMITY_CHARS of an
# explicit "ISSP" mention. Without that, they still get a fair shot via
# the ordinary keyword tier below (TOPIC_KEYWORDS), just not promoted to
# high confidence.
EXACT_MODULE_NAMES_NEEDS_ISSP_NEARBY = {
    "religion": "RELIG",
    "environment": "ENV",
    "citizenship": "CIT",
    "national identity": "NATID",
    "social networks": "SOCNET",
}
ISSP_PROXIMITY_CHARS = 120

# Keyword tier: a module only wins if it clears MIN_KEYWORD_SCORE distinct
# phrase hits *and* no other module ties it - a single incidental phrase
# (e.g. "job satisfaction" in an unrelated labour-economics paper) should
# not by itself tag a record as ISSP Work Orientations. Broader/more
# numerous than a first pass would need, specifically so genuinely on-topic
# abstracts (which tend to use several related terms) clear the bar while
# passing mentions don't.
TOPIC_KEYWORDS = {
    "CIT": ["civic participation", "political participation", "civic engagement",
            "political engagement", "voter turnout", "voting behaviour", "voting behavior",
            "protest participation", "good citizen", "duties of citizens", "naturalization",
            "naturalisation"],
    "NATID": ["national identity", "nationalism", "national pride", "patriotism",
              "national belonging", "national attachment", "national sentiment", "chauvinism"],
    "DIGSOC": ["digital society", "digital societies", "digitalization", "digitalisation",
               "digital divide", "internet use", "social media use", "online privacy",
               "digital technology", "attitudes toward artificial intelligence",
               "automation attitudes"],
    "ENV": ["environmental attitude", "environmental behaviour", "environmental behavior",
            "environmental concern", "pro-environmental", "climate change attitude",
            "climate change belief", "environmental policy",
            "willingness to pay for environmental", "pollution concern", "green behavior",
            "green behaviour"],
    "FAMGEN": ["gender role attitude", "gender role attitudes", "division of household labor",
               "division of household labour", "changing gender roles", "women's employment",
               "womens employment", "attitudes toward working mothers", "housework division",
               "breadwinner", "work-family balance", "work family balance"],
    "HLTH": ["health care system", "healthcare system", "access to health care",
             "access to healthcare", "health inequalities", "health inequality",
             "health insurance", "quality of care", "satisfaction with health care",
             "self-rated health"],
    "LEISPORT": ["leisure time", "sports participation", "sport participation",
                 "physical activity", "leisure activities", "recreational activity",
                 "exercise participation"],
    "RELIG": ["religiosity", "religious belief", "religious beliefs", "secularization",
              "secularisation", "church attendance", "religious affiliation",
              "religious practice", "prayer frequency", "belief in god"],
    "ROG": ["role of government", "government intervention", "welfare state attitude",
            "welfare state attitudes", "government spending attitude", "state intervention",
            "attitudes toward taxation", "redistribution preferences",
            "social policy attitude"],
    "SOCINEQ": ["income inequality", "social stratification", "perceived inequality",
                "social inequality", "economic inequality", "distributive justice",
                "social class attitude", "social mobility", "wage inequality",
                "redistribution of wealth"],
    "SOCNET": ["social network", "social support network", "social resources",
               "social capital", "informal support", "kinship network", "social ties",
               "network of friends"],
    "WORKORI": ["work orientation", "job satisfaction", "work centrality",
                "work commitment", "work-life balance", "work life balance",
                "employment commitment", "organizational commitment",
                "organisational commitment", "attitudes toward work"],
}
MIN_KEYWORD_SCORE = 2

# Reference description per module for the free, local semantic-similarity
# tier. These are not verbatim ISSP text (ISSP does not publish a single
# official paragraph per topic) - they are accurate summaries of each
# module's established subject matter, used only to compute similarity
# against a record's abstract, not asserted anywhere as an exact quote.
TOPIC_DESCRIPTIONS = {
    "CIT": ("Citizenship: political and social participation, civic engagement, voting "
            "behaviour, protest activity, trust in political institutions, what makes a "
            "good citizen, and the rights and duties of citizenship."),
    "NATID": ("National Identity: national pride and attachment, nationalism versus "
              "patriotism, who should be considered a citizen of the nation, attitudes "
              "toward immigrants and immigration, and belonging to one's country versus "
              "the wider world."),
    "DIGSOC": ("Digital Societies: use of the internet and digital technology, social "
               "media use, the digital divide, online privacy concerns, and attitudes "
               "toward the impact of digitalization and automation on work and social "
               "life."),
    "ENV": ("Environment: environmental attitudes and behaviour, concern about pollution "
            "and climate change, willingness to pay for environmental protection, trust "
            "in science, and the trade-off between economic growth and environmental "
            "protection."),
    "FAMGEN": ("Family and Changing Gender Roles: attitudes toward women's employment, "
               "marriage, and childbearing, division of housework and household income "
               "management between partners, working parents and childcare, and gender "
               "roles within the family and workplace."),
    "HLTH": ("Health and Health Care: self-rated health, access to and satisfaction with "
             "health care systems, health inequalities, health insurance coverage, and "
             "out-of-pocket health care costs."),
    "LEISPORT": ("Leisure Time and Sports: how people spend their free time, participation "
                 "in sports and physical activity, hobbies and recreational activities, and "
                 "the balance between work and leisure."),
    "RELIG": ("Religion: religious beliefs and practices, religious affiliation, frequency "
              "of church attendance and prayer, belief in God and the afterlife, and the "
              "role of religion in public and political life."),
    "ROG": ("Role of Government: attitudes toward the size and scope of government, "
            "government spending and social policy, taxation and income redistribution, "
            "and civil rights and government intervention in the economy."),
    "SOCINEQ": ("Social Inequality: beliefs about income and wealth inequality, perceptions "
                "of social class and social mobility, attitudes toward redistribution and "
                "taxation, and what determines a person's pay and social position."),
    "SOCNET": ("Social Networks: the structure and content of people's personal "
               "relationships and social support, contact with family, friends, "
               "neighbours, and colleagues, reliance on personal networks versus formal "
               "institutions for help, and social capital."),
    "WORKORI": ("Work Orientations: attitudes toward work and its importance in life, job "
                "satisfaction, work commitment and organizational commitment, working "
                "conditions, and the balance between work and private life."),
}

ZA_RE = re.compile(r"\bZA[\s-]?(\d{3,5})\b", re.IGNORECASE)
GESIS_DOI_RE = re.compile(r"10\.4232/1\.\d+", re.IGNORECASE)
ISSP_YEAR_RE = re.compile(
    r"\bISSP\b[^0-9]{0,15}(19[89]\d|20[0-2]\d)|(19[89]\d|20[0-2]\d)[^0-9]{0,15}\bISSP\b",
    re.IGNORECASE)


def extract_za_numbers(text):
    return {int(match) for match in ZA_RE.findall(text or "")}


def extract_gesis_dois(text):
    return {match.casefold() for match in GESIS_DOI_RE.findall(text or "")}


def extract_issp_years(text):
    years = set()
    for first, second in ISSP_YEAR_RE.findall(text or ""):
        years.add(int(first or second))
    return years


ISSP_ANCHOR_RE = r"(?:\bissp\b|international social survey program(?:me)?)"


def _name_near_issp(text, name, window=ISSP_PROXIMITY_CHARS):
    """True if `name` appears within `window` characters of an "ISSP"
    mention - the acronym or its spelled-out name, since many papers
    (especially formal reports) use the full name on first mention rather
    than the acronym - checked in both directions. Used to gate the five
    generic exact-module-names that are also everyday academic vocabulary."""
    pattern = re.compile(
        ISSP_ANCHOR_RE + r".{0," + str(window) + "}" + re.escape(name) + "|" +
        re.escape(name) + r".{0," + str(window) + "}" + ISSP_ANCHOR_RE,
        re.IGNORECASE | re.DOTALL)
    return bool(pattern.search(text))


# ISSP's 41 member countries (issp.org/members/member-states/), spelled
# the way ISSP itself spells them (e.g. "Great Britain", "South Korea") -
# a DATA tag uses this exact spelling regardless of which alias matched.
# Aliases are deliberately conservative (no bare 2-letter codes like "UK"
# or "US") since these are matched with only a word-boundary, not a full
# name-detector, and the context-proximity check below is the main guard
# against false positives, not the alias list itself.
DATA_COUNTRY_ALIASES = {
    "Canada": ["canada"], "Mexico": ["mexico"], "USA": ["usa", "united states of america",
    "united states"], "Chile": ["chile"], "Suriname": ["suriname"],
    "South Africa": ["south africa"], "Austria": ["austria"], "Bulgaria": ["bulgaria"],
    "Croatia": ["croatia"], "Czech Republic": ["czech republic", "czechia"],
    "Denmark": ["denmark"], "Estonia": ["estonia"], "Finland": ["finland"],
    "France": ["france"], "Germany": ["germany"],
    "Great Britain": ["great britain", "united kingdom"], "Greece": ["greece"],
    "Hungary": ["hungary"], "Iceland": ["iceland"], "Israel": ["israel"], "Italy": ["italy"],
    "Lithuania": ["lithuania"], "Netherlands": ["netherlands"], "Norway": ["norway"],
    "Poland": ["poland"], "Russia": ["russia", "russian federation"],
    "Slovakia": ["slovakia"], "Slovenia": ["slovenia"], "Spain": ["spain"],
    "Sweden": ["sweden"], "Switzerland": ["switzerland"], "Turkey": ["turkey", "türkiye"],
    "Ukraine": ["ukraine"], "India": ["india"], "Japan": ["japan"],
    "Philippines": ["philippines"], "South Korea": ["south korea", "republic of korea"],
    "Taiwan": ["taiwan"], "Thailand": ["thailand"], "Australia": ["australia"],
    "New Zealand": ["new zealand"],
}
# Only a country appearing near one of these counts as "the author used
# this country's data" - a bare mention is very often just "prior research
# in Germany found X", i.e. citing someone else's study, not the author's
# own data source.
DATA_CONTEXT_PHRASES = [
    "data from", "sample from", "survey conducted in", "respondents from",
    "fieldwork in", "collected in", "data collected in", "survey data from",
    "sample of", "nationally representative sample", "cross-national sample",
    "country sample", "issp", "international social survey program",
    "international social survey programme",
]
COUNTRY_PROXIMITY_CHARS = 150


def _near_any_phrase(text, name, phrases, window):
    """True if `name` appears within `window` characters of any of
    `phrases` (checked in both directions)."""
    anchor = "(?:" + "|".join(re.escape(phrase) for phrase in phrases) + ")"
    pattern = re.compile(
        anchor + r".{0," + str(window) + r"}\b" + re.escape(name) + r"\b|"
        r"\b" + re.escape(name) + r"\b.{0," + str(window) + "}" + anchor,
        re.IGNORECASE | re.DOTALL)
    return bool(pattern.search(text))


def extract_data_countries(text):
    """Return the set of canonical ISSP member-country names mentioned
    near a data-source context phrase (e.g. "data from Germany and
    France") - not just mentioned anywhere, since papers routinely cite
    other countries' unrelated prior research in passing."""
    text = text or ""
    found = set()
    for canonical, aliases in DATA_COUNTRY_ALIASES.items():
        for alias in aliases:
            if re.search(r"\b" + re.escape(alias) + r"\b", text, re.IGNORECASE) and \
                    _near_any_phrase(text, alias, DATA_CONTEXT_PHRASES, COUNTRY_PROXIMITY_CHARS):
                found.add(canonical)
                break
    return found


def data_country_tag(country):
    return f"DATA - {country}"


def topic_from_title(title):
    """Match a GESIS study title (e.g. 'International Social Survey
    Programme: Religion IV - ISSP 2018') to a tag via TOPIC_PHRASES."""
    folded = clean_value(title).casefold()
    for phrase, tag in TOPIC_PHRASES:
        if phrase in folded:
            return tag
    return None


class SemanticModuleMatcher:
    """Local, offline sentence-embedding similarity classifier. Free: the
    model is a small open-source one from sentence-transformers, downloaded
    once from Hugging Face on first use and cached locally; every
    classification after that runs entirely on this machine with no network
    access, no API key, and no per-call cost."""

    MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
    MIN_SIMILARITY = 0.35
    MIN_MARGIN = 0.05

    def __init__(self, model=None):
        if model is None:
            from sentence_transformers import SentenceTransformer  # deferred: optional dep
            model = SentenceTransformer(self.MODEL_NAME)
        self.model = model
        self.tags = list(TOPIC_DESCRIPTIONS.keys())
        self.topic_embeddings = self.model.encode(
            [TOPIC_DESCRIPTIONS[tag] for tag in self.tags], normalize_embeddings=True)

    def classify_batch(self, texts):
        """Return a [(tag_or_None, best_similarity), ...] list, one entry
        per input text, in order."""
        if not texts:
            return []
        embeddings = self.model.encode(
            list(texts), normalize_embeddings=True, batch_size=64, show_progress_bar=False)
        results = []
        for embedding in embeddings:
            similarities = self.topic_embeddings @ embedding
            order = sorted(range(len(similarities)), key=lambda i: similarities[i], reverse=True)
            best, second = order[0], order[1]
            best_score, second_score = float(similarities[best]), float(similarities[second])
            if best_score >= self.MIN_SIMILARITY and (best_score - second_score) >= self.MIN_MARGIN:
                results.append((self.tags[best], best_score))
            else:
                results.append((None, best_score))
        return results

    def classify_one(self, text):
        return self.classify_batch([text])[0]


def build_semantic_matcher():
    """Return a ready-to-use SemanticModuleMatcher, or None if
    sentence-transformers isn't installed or the model can't be loaded
    (e.g. no internet for the one-time download) - callers should treat
    None as "skip this tier", not an error."""
    try:
        return SemanticModuleMatcher()
    except Exception:
        return None


def resolve_gesis_doi_title(doi, session=None, timeout=15):
    """Look up a 10.4232/1.xxxxx DOI on DataCite and return its title, or
    "" if the lookup fails. Network call - inject a fake `session` in
    tests instead of hitting the real API."""
    session = session or requests.Session()
    response = session.get(f"https://api.datacite.org/dois/{doi}", timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    titles = payload.get("data", {}).get("attributes", {}).get("titles", [])
    return clean_value(titles[0].get("title")) if titles else ""


_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def classify_record(text, doi_resolver=None, embedding_matcher=None):
    """Return a list of {"tag", "confidence", "method", "evidence"} dicts,
    one per distinct module this record has evidence for (never more than
    one entry per tag - if several tiers find the same module, only the
    best-confidence one is kept). An empty-evidence record still gets a
    single-item list: [{"tag": None, "confidence": "none",
    "method": "no_evidence", ...}].

    `doi_resolver(doi) -> title` is optional and only needed for the GESIS
    DOI half of tier 1; pass None to skip it (e.g. no network access).
    `embedding_matcher(text) -> (tag_or_None, similarity)` is optional and
    only needed for tier 3 (semantic similarity); pass a
    SemanticModuleMatcher.classify_one, a fake for tests, or None to skip
    it. tag_issp_modules() calls the semantic tier itself in an efficient
    batch instead of through this parameter - it exists here mainly so the
    full tier chain can be exercised and tested one record at a time."""
    text = text or ""
    folded = text.casefold()
    found = {}

    def add(tag, confidence, method, evidence):
        if tag is None:
            return
        existing = found.get(tag)
        if existing is None or _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[existing["confidence"]]:
            found[tag] = {"tag": tag, "confidence": confidence, "method": method,
                          "evidence": evidence}

    # Tier 1a: ZA study numbers - every one that resolves.
    for za in sorted(extract_za_numbers(text)):
        if za in ZA_TO_TAG:
            add(ZA_TO_TAG[za], "high", "za_number", f"ZA{za} cited in text")

    # Tier 1b: GESIS dataset DOIs - every one that resolves to a real title.
    if doi_resolver is not None:
        for doi in sorted(extract_gesis_dois(text)):
            try:
                title = doi_resolver(doi)
            except Exception:
                continue
            tag = topic_from_title(title)
            if tag:
                add(tag, "high", "gesis_doi", f"DOI {doi} resolved to '{title}'")

    # Tier 1c: exact module name found verbatim.
    for name, tag in EXACT_MODULE_NAMES.items():
        if name in folded:
            add(tag, "high", "exact_module_name", f"Exact module name '{name}' found in text")
    for name, tag in EXACT_MODULE_NAMES_NEEDS_ISSP_NEARBY.items():
        if name in folded and _name_near_issp(text, name):
            add(tag, "high", "exact_module_name",
                f"Exact module name '{name}' found near an ISSP mention")

    # Tier 2: scored keywords - every module that independently clears the
    # minimum score, not only a unique winner.
    scores = {tag: sum(1 for phrase in phrases if phrase in folded)
              for tag, phrases in TOPIC_KEYWORDS.items()}
    for tag, score in scores.items():
        if score >= MIN_KEYWORD_SCORE:
            add(tag, "medium", "keyword",
                f"Matched {score} topic keyword(s) for {tag}, no explicit ISSP citation found")

    # Tier 3: semantic similarity - always runs; adds its single best
    # candidate only if that module wasn't already found above.
    if embedding_matcher is not None:
        try:
            tag, similarity = embedding_matcher(text)
        except Exception:
            tag, similarity = None, 0.0
        if tag and tag not in found:
            add(tag, "low", "semantic_similarity",
                f"Semantically similar to the {tag} module description (similarity "
                f"{similarity:.2f}), no stronger evidence found")

    if not found:
        return [{"tag": None, "confidence": "none", "method": "no_evidence",
                 "evidence": "No ISSP module evidence found"}]
    return sorted(found.values(), key=lambda r: (-_CONFIDENCE_RANK[r["confidence"]], r["tag"]))


def _merge_tags(value, new_tags, is_managed):
    """Replace only tags this feature previously assigned (per
    `is_managed`), preserving every other user tag (same convention as
    _replace_abstract_review_tag), then append `new_tags`."""
    tags = [item.strip() for item in re.split(r"\s*(?:;|\n|,)\s*", clean_value(value))
            if item.strip()]
    tags = [item for item in tags if not is_managed(item)]
    for tag in new_tags:
        if tag and tag not in tags:
            tags.append(tag)
    return "; ".join(tags)


def replace_issp_module_tags(value, new_tags):
    """`new_tags` is an iterable of zero or more module tag codes to add."""
    return _merge_tags(value, new_tags, lambda item: item in ISSP_MODULE_TAG_CODES)


def replace_data_country_tags(value, new_tags):
    """`new_tags` is an iterable of zero or more "DATA - <country>" tags."""
    return _merge_tags(value, new_tags, lambda item: item.startswith("DATA - "))


def tag_issp_modules(dataframe, text_columns, url_column=None, doi_column=None, tag_column=None,
                     use_network_doi_lookup=True, use_semantic_matching=True,
                     fetch_full_text=False, full_text_pdf_pages=15, request_delay=0.5,
                     semantic_matcher=None, session=None,
                     progress_callback=None, cancel_event=None):
    """Classify every record - possibly with more than one module tag each
    - and write the result into columns (semicolon-separated when there is
    more than one) plus merge every tag found into `tag_column`.
    `text_columns` is an iterable of column names whose values are
    concatenated as the text to search (typically Title, Abstract, and/or
    Notes/Extra).

    Tier 1 (ZA number / GESIS DOI / exact module name) and tier 2 (scored
    keywords) run per-record first. Tier 3 (semantic similarity) then runs
    over every record in batches regardless of what tiers 1-2 found -
    encoding is much faster in bulk than one text at a time, and a record
    can use more than one module. Pass a pre-built `semantic_matcher`
    (e.g. a fake in tests) to skip loading the real model, or
    use_semantic_matching=False to disable tier 3 entirely.

    If `fetch_full_text` is True and a record has a URL or DOI (via
    `url_column`/`doi_column`), the linked page/PDF is downloaded (reusing
    abstract_note_tools.fetch_abstract - same PDF-vs-HTML handling, OCR
    fallback for scanned PDFs, and 15 MB size limit) and its extracted
    text is added to the search text for that one record before
    classification and country extraction, in addition to `text_columns`.
    This is a real network request per record with a URL/DOI - slow over
    a large batch - so it is opt-in and fully respects `cancel_event`.

    Independently of module classification, every record's search text
    (including any fetched full text) is scanned for ISSP member
    countries mentioned near a data-source phrase (e.g. "data from
    Germany and France") and tagged "DATA - <country>" in `tag_column`
    and the "ISSP Data Countries" column - a paper can of course draw on
    more than one country's data.

    The summary `counts` bucket each record by the single best confidence
    among the module tags it received (a record with both a high- and a
    low-confidence tag counts as "high confidence" in the summary), even
    though every tag it received is preserved in the detail columns."""
    frame = dataframe.copy()
    text_columns = [column for column in text_columns if column]
    doi_resolver = None
    if use_network_doi_lookup:
        session = session or requests.Session()
        doi_resolver = lambda doi: resolve_gesis_doi_title(doi, session=session)
    if fetch_full_text:
        session = session or requests.Session()

    if tag_column is None:
        tag_column = guess_column(frame.columns, abstract_tools.TAG_ALIASES)
    if tag_column is None:
        tag_column = "Manual Tags"
        frame[tag_column] = ""

    for column in ("ISSP Module Tag", "ISSP Module Confidence", "ISSP Module Method",
                   "ISSP Module Evidence", "ISSP Data Countries", "ISSP Full Text Fetch Status"):
        frame[column] = ""

    counts = {"Total records": len(frame), "high confidence": 0, "medium confidence": 0,
              "low confidence": 0, "no match": 0, "Cancelled records": 0}
    total = len(frame)
    indices = list(frame.index)
    texts, results, countries = {}, {}, {}
    finalize_indices = indices

    for completed, index in enumerate(indices, start=1):
        if cancel_event is not None and cancel_event.is_set():
            counts["Cancelled records"] += total - completed + 1
            remaining = indices[completed - 1:]
            frame.loc[remaining, "ISSP Module Confidence"] = "cancelled"
            finalize_indices = indices[:completed - 1]
            break
        row = frame.loc[index]
        parts = [clean_value(row.get(column, "")) for column in text_columns]
        if doi_column:
            parts.append(clean_value(row.get(doi_column, "")))
        if fetch_full_text:
            link = abstract_tools.record_link(row, url_column, doi_column)
            if link:
                fetch_result = abstract_tools.fetch_abstract(
                    link, session=session, max_pdf_pages=full_text_pdf_pages)
                frame.at[index, "ISSP Full Text Fetch Status"] = fetch_result.get("status", "")
                parts.append(clean_value(fetch_result.get("full_text", "")))
                if request_delay:
                    time.sleep(request_delay)
            else:
                frame.at[index, "ISSP Full Text Fetch Status"] = "no_link"
        text = "\n".join(part for part in parts if part)
        texts[index] = text
        results[index] = classify_record(text, doi_resolver=doi_resolver)
        countries[index] = extract_data_countries(text)
        if progress_callback:
            best = results[index][0]["confidence"]
            progress_callback(completed, total, index, best)

    still_running = cancel_event is None or not cancel_event.is_set()
    if use_semantic_matching and still_running:
        matcher = semantic_matcher if semantic_matcher is not None else build_semantic_matcher()
        if matcher is not None:
            batch_size = 200
            for start in range(0, len(finalize_indices), batch_size):
                if cancel_event is not None and cancel_event.is_set():
                    break
                chunk = finalize_indices[start:start + batch_size]
                for index, (tag, similarity) in zip(
                        chunk, matcher.classify_batch([texts[i] for i in chunk])):
                    existing_tags = {item["tag"] for item in results[index]}
                    if tag and tag not in existing_tags:
                        addition = {
                            "tag": tag, "confidence": "low", "method": "semantic_similarity",
                            "evidence": (f"Semantically similar to the {tag} module "
                                         f"description (similarity {similarity:.2f}), no "
                                         f"stronger evidence found"),
                        }
                        if len(results[index]) == 1 and results[index][0]["tag"] is None:
                            results[index] = [addition]
                        else:
                            results[index] = sorted(
                                results[index] + [addition],
                                key=lambda r: (-_CONFIDENCE_RANK[r["confidence"]], r["tag"]))
                if progress_callback:
                    progress_callback(total, total, chunk[-1] if chunk else None,
                                      f"semantic pass {start + len(chunk)}/{len(finalize_indices)}")

    for index in finalize_indices:
        items = results[index]
        matched = [item for item in items if item["tag"]]
        frame.at[index, "ISSP Module Tag"] = "; ".join(item["tag"] for item in matched)
        frame.at[index, "ISSP Module Confidence"] = "; ".join(item["confidence"] for item in matched)
        if matched:
            frame.at[index, "ISSP Module Method"] = "; ".join(item["method"] for item in matched)
            frame.at[index, "ISSP Module Evidence"] = " | ".join(item["evidence"] for item in matched)
            counts[f"{matched[0]['confidence']} confidence"] += 1
        else:
            frame.at[index, "ISSP Module Method"] = items[0]["method"]
            frame.at[index, "ISSP Module Evidence"] = items[0]["evidence"]
            counts["no match"] += 1

        found_countries = sorted(countries.get(index, ()))
        frame.at[index, "ISSP Data Countries"] = "; ".join(found_countries)

        current_tags = frame.at[index, tag_column]
        current_tags = replace_issp_module_tags(current_tags, [item["tag"] for item in matched])
        current_tags = replace_data_country_tags(
            current_tags, [data_country_tag(country) for country in found_countries])
        frame.at[index, tag_column] = current_tags
    counts["records with a data country tag"] = sum(1 for value in countries.values() if value)
    return frame, counts, tag_column
