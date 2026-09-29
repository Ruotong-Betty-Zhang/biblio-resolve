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
   - The exact module name found verbatim (as whole words) in the text.
     Ten of the twelve names count on their own. The other two
     (Citizenship, National Identity) are also everyday political-science
     vocabulary, so they only count here when they appear near an
     explicit "ISSP" mention - otherwise they are left to the keyword
     tier below.
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

import json
import os
import re
import threading
import time

import requests

import abstract_note_tools as abstract_tools

try:  # Package import: python -m system.test_issp_module_tags
    from . import lookup_core as core
except ImportError:  # Direct app/script import from inside system/
    import lookup_core as core

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
    "religion": "RELIG",
    "environment": "ENV",
    "social networks": "SOCNET",
}
# These two module names double as everyday academic vocabulary, so
# finding the bare word/phrase proves nothing on its own - it only counts
# as exact-name evidence when it appears within ISSP_PROXIMITY_CHARS of an
# explicit "ISSP" mention. Without that, they still get a fair shot via
# the ordinary keyword tier below (TOPIC_KEYWORDS), just not promoted to
# high confidence.
EXACT_MODULE_NAMES_NEEDS_ISSP_NEARBY = {
    "citizenship": "CIT",
    "national identity": "NATID",
}
ISSP_PROXIMITY_CHARS = 120

# Keyword tier: a module counts if it clears MIN_KEYWORD_SCORE distinct
# phrase hits; every module that clears it is kept, even if several tie
# - a record can use more than one module. A single incidental phrase
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
    """Return the (start, end) span of the first place `name` appears
    within `window` characters of an "ISSP" mention - the acronym or its
    spelled-out name, since many papers (especially formal reports) use the
    full name on first mention rather than the acronym - checked in both
    directions, or None. Used to gate the generic exact-module-names that
    are also everyday academic vocabulary."""
    name_re = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
    pattern = re.compile(
        ISSP_ANCHOR_RE + r".{0," + str(window) + "}" + name_re + "|" +
        name_re + r".{0," + str(window) + "}" + ISSP_ANCHOR_RE,
        re.IGNORECASE | re.DOTALL)
    match = pattern.search(text)
    return match.span() if match else None


# ---------------------------------------------------------------------------
# Sentence splitting - shared by the country tier (a country and its data
# phrase must be in the same sentence) and by the evidence quotes.
# ---------------------------------------------------------------------------

# A sentence ends at . ! or ? (optionally followed by a closing quote or
# bracket) when the next sentence starts with a capital letter or digit,
# or at any line break (the fields of a record are joined by newlines, so
# a title and an abstract are never treated as one sentence).
_SENTENCE_BREAK_RE = re.compile(r"([.!?]+[\"')\]”]*)\s+(?=[\"'(\[“]?[A-Z0-9])|\n+")
# ...unless the full stop belongs to an abbreviation: single-letter
# initials ("U.S.", "U.K.", "e.g.", "J. Smith") or a common short form.
_ABBREVIATION_RE = re.compile(
    r"(?:(?<![A-Za-z])(?:[A-Za-z]\.){1,3}|\b(?:al|vs|cf|ca|approx|fig|no|vol|dr|prof|st)\.)$",
    re.IGNORECASE)


def sentence_spans(text, start=0, end=None):
    """Split text[start:end] into sentences and return their (start, end)
    offsets in `text`."""
    end = len(text) if end is None else end
    spans, current = [], start
    for match in _SENTENCE_BREAK_RE.finditer(text, start, end):
        if match.group(1):
            if _ABBREVIATION_RE.search(text[max(start, match.start() - 12):match.start() + 1]):
                continue
            sentence_end = match.end(1)
        else:
            sentence_end = match.start()
        if text[current:sentence_end].strip():
            spans.append((current, sentence_end))
        current = match.end()
    if text[current:end].strip():
        spans.append((current, end))
    return spans


# Every country (and a few territories), one per line:
#   Canonical name | other names ; ... | nationality adjectives ; ...
# The canonical name is what goes into the "DATA - <country>" tag. The 41
# ISSP member countries keep ISSP's own spelling ("Great Britain", "USA",
# "South Korea", "Czech Republic"). Names are matched case-insensitively
# as whole words; when two overlap, the longest wins, so "Northern
# Ireland" is not also Ireland and "Papua New Guinea" is not also Guinea.
# Adjectives are weaker evidence ("German" is also a first name and
# surname) - see extract_data_country_evidence() for how they are used.
# "English" is deliberately not an adjective for England: it is far more
# often the language ("an English-language survey").
COUNTRY_TABLE = """
Afghanistan | | afghan
Albania | | albanian
Algeria | | algerian
Andorra | | andorran
Angola | | angolan
Antigua and Barbuda | |
Argentina | | argentine; argentinian; argentinean
Armenia | | armenian
Australia | | australian
Austria | | austrian
Azerbaijan | | azerbaijani
Bahamas | | bahamian
Bahrain | | bahraini
Bangladesh | | bangladeshi
Barbados | | barbadian
Belarus | | belarusian
Belgium | | belgian
Belize | | belizean
Benin | | beninese
Bhutan | | bhutanese
Bolivia | | bolivian
Bosnia and Herzegovina | bosnia | bosnian
Botswana | |
Brazil | | brazilian
Brunei | |
Bulgaria | | bulgarian
Burkina Faso | | burkinabe
Burundi | | burundian
Cabo Verde | cape verde | cape verdean
Cambodia | | cambodian
Cameroon | | cameroonian
Canada | | canadian
Central African Republic | |
Chad | | chadian
Chile | | chilean
China | people's republic of china; mainland china | chinese
Colombia | | colombian
Comoros | |
Congo | republic of the congo; congo-brazzaville | congolese
DR Congo | democratic republic of the congo; democratic republic of congo; congo-kinshasa |
Costa Rica | | costa rican
Cote d'Ivoire | côte d'ivoire; ivory coast | ivorian
Croatia | | croatian
Cuba | | cuban
Cyprus | | cypriot
Czech Republic | czechia | czech
Denmark | | danish
Djibouti | |
Dominica | |
Dominican Republic | | dominican
Ecuador | | ecuadorian
Egypt | | egyptian
El Salvador | | salvadoran
Equatorial Guinea | |
Eritrea | | eritrean
Estonia | | estonian
Eswatini | swaziland |
Ethiopia | | ethiopian
Fiji | | fijian
Finland | | finnish
France | | french
Gabon | | gabonese
Gambia | | gambian
Georgia | | georgian
Germany | west germany; east germany | german; west german; east german
Ghana | | ghanaian
Greece | | greek
Grenada | |
Guatemala | | guatemalan
Guinea | | guinean
Guinea-Bissau | |
Guyana | | guyanese
Haiti | | haitian
Honduras | | honduran
Hong Kong | |
Hungary | | hungarian
Iceland | | icelandic
India | | indian
Indonesia | | indonesian
Iran | | iranian
Iraq | | iraqi
Ireland | republic of ireland | irish
Israel | | israeli
Italy | | italian
Jamaica | | jamaican
Japan | | japanese
Jordan | | jordanian
Kazakhstan | | kazakh; kazakhstani
Kenya | | kenyan
Kiribati | |
Kosovo | | kosovar
Kuwait | | kuwaiti
Kyrgyzstan | kyrgyz republic | kyrgyz
Laos | lao pdr | laotian
Latvia | | latvian
Lebanon | | lebanese
Lesotho | |
Liberia | | liberian
Libya | | libyan
Liechtenstein | |
Lithuania | | lithuanian
Luxembourg | | luxembourgish
Macao | macau |
Madagascar | | malagasy
Malawi | | malawian
Malaysia | | malaysian
Maldives | | maldivian
Mali | | malian
Malta | | maltese
Marshall Islands | |
Mauritania | | mauritanian
Mauritius | | mauritian
Mexico | | mexican
Micronesia | |
Moldova | | moldovan
Monaco | |
Mongolia | | mongolian
Montenegro | | montenegrin
Morocco | | moroccan
Mozambique | | mozambican
Myanmar | burma | burmese
Namibia | | namibian
Nauru | |
Nepal | | nepalese; nepali
Netherlands | holland | dutch
New Zealand | | new zealander
Nicaragua | | nicaraguan
Niger | | nigerien
Nigeria | | nigerian
North Korea | democratic people's republic of korea | north korean
North Macedonia | macedonia | macedonian
Norway | | norwegian
Oman | | omani
Pakistan | | pakistani
Palau | |
Palestine | palestinian territories | palestinian
Panama | | panamanian
Papua New Guinea | |
Paraguay | | paraguayan
Peru | | peruvian
Philippines | | filipino; philippine
Poland | | polish
Portugal | | portuguese
Puerto Rico | | puerto rican
Qatar | | qatari
Romania | | romanian
Russia | russian federation | russian
Rwanda | | rwandan
Saint Kitts and Nevis | |
Saint Lucia | |
Saint Vincent and the Grenadines | |
Samoa | | samoan
San Marino | |
Sao Tome and Principe | são tomé and príncipe |
Saudi Arabia | | saudi
Senegal | | senegalese
Serbia | | serbian
Seychelles | |
Sierra Leone | | sierra leonean
Singapore | | singaporean
Slovakia | slovak republic | slovak
Slovenia | | slovenian; slovene
Solomon Islands | |
Somalia | | somali
South Africa | | south african
South Korea | republic of korea; korea | south korean; korean
South Sudan | | south sudanese
Spain | | spanish
Sri Lanka | | sri lankan
Sudan | | sudanese
Suriname | | surinamese
Sweden | | swedish
Switzerland | | swiss
Syria | | syrian
Taiwan | | taiwanese
Tajikistan | | tajik
Tanzania | | tanzanian
Thailand | | thai
Timor-Leste | east timor | timorese
Togo | | togolese
Tonga | | tongan
Trinidad and Tobago | | trinidadian
Tunisia | | tunisian
Turkey | türkiye; turkiye | turkish
Turkmenistan | | turkmen
Tuvalu | |
Uganda | | ugandan
Ukraine | | ukrainian
United Arab Emirates | | emirati
Great Britain | united kingdom; britain; u.k.; uk | british
England | |
Scotland | | scottish
Wales | | welsh
Northern Ireland | | northern irish
USA | united states; united states of america; u.s.; u.s.a.; america | american
Uruguay | | uruguayan
Uzbekistan | | uzbek
Vanuatu | |
Vatican City | holy see |
Venezuela | | venezuelan
Vietnam | viet nam | vietnamese
Yemen | | yemeni
Zambia | | zambian
Zimbabwe | | zimbabwean
"""
# Short codes that are also ordinary words in lower case ("us") are only
# accepted exactly as written here.
COUNTRY_CASE_SENSITIVE_NAMES = {"US": "USA", "UAE": "United Arab Emirates", "PRC": "China"}
# A single-word name or adjective right after one of these words is part
# of a larger region or group, not the country: "Latin America", "North
# American", "African American respondents", "East Asian".
_REGION_PREFIXES = ["latin", "north", "south", "central", "east", "west", "northern",
                    "southern", "eastern", "western", "african", "asian", "mexican",
                    "native", "anglo", "pan", "sub-saharan"]


def _parse_country_table(table):
    names, adjectives = {}, {}
    for line in table.strip().splitlines():
        canonical, other_names, adjective_list = (part.strip() for part in line.split("|"))
        for name in [canonical] + other_names.split(";"):
            if name.strip():
                names[name.strip().casefold()] = canonical
        for adjective in adjective_list.split(";"):
            if adjective.strip():
                adjectives[adjective.strip().casefold()] = canonical
    return names, adjectives


COUNTRY_NAME_TO_CANONICAL, COUNTRY_ADJECTIVE_TO_CANONICAL = _parse_country_table(COUNTRY_TABLE)
ALL_COUNTRIES = sorted(set(COUNTRY_NAME_TO_CANONICAL.values()) | set(COUNTRY_CASE_SENSITIVE_NAMES.values()))


def _mention_re(terms, flags):
    ordered = sorted(terms, key=len, reverse=True)  # longest wins
    not_after_region = "".join(f"(?<!{re.escape(prefix)} )" for prefix in _REGION_PREFIXES)
    return re.compile(not_after_region + r"(?<![\w-])(?:" +
                      "|".join(re.escape(term) for term in ordered) + r")(?![\w-])", flags)


_COUNTRY_MENTION_RE = _mention_re(
    list(COUNTRY_NAME_TO_CANONICAL) + list(COUNTRY_ADJECTIVE_TO_CANONICAL), re.IGNORECASE)
_COUNTRY_CASE_SENSITIVE_RE = _mention_re(list(COUNTRY_CASE_SENSITIVE_NAMES), 0)

# A country name counts as "the author used this country's data" when it
# is in the same sentence as one of these phrases, within
# COUNTRY_PROXIMITY_CHARS - a bare mention is very often just "prior
# research in Germany found X", citing someone else's study. Requiring the
# same sentence stops "Other work is based on Japan. This paper uses data
# from ..." from tagging Japan.
DATA_CONTEXT_PHRASES = [
    "data from", "sample from", "survey conducted in", "respondents from",
    "fieldwork in", "collected in", "data collected in", "survey data from",
    "sample of", "nationally representative sample", "cross-national sample",
    "country sample", "issp", "international social survey program",
    "international social survey programme",
]
COUNTRY_PROXIMITY_CHARS = 150
_DATA_CONTEXT_RE = re.compile(
    "|".join(r"\b" + re.escape(phrase) + r"\b" for phrase in DATA_CONTEXT_PHRASES), re.IGNORECASE)
# A name or adjective also counts when followed (at most two words in
# between) by one of these: "UK data", "the UK's survey", "German
# respondents", "Chinese General Social Survey".
DATA_NOUN_RE = (r"(?:data|dataset|datasets|microdata|sample|samples|survey|surveys|respondents|"
                r"adults|households|panel|census)")
_NAME_FOLLOWED_BY_NOUN_RE = re.compile(
    r"(?:['’]s)?(?:[\s-]+[\w'-]+){0,2}?[\s-]+" + DATA_NOUN_RE + r"\b", re.IGNORECASE)
# For adjectives the words in between may not be possessives, so a person
# called German is not a country: "German Lopez's survey" does not count.
_ADJECTIVE_FOLLOWED_BY_NOUN_RE = re.compile(
    r"(?:[\s-]+[\w-]+){0,2}?[\s-]+" + DATA_NOUN_RE + r"\b", re.IGNORECASE)


def _country_mentions(sentence):
    """[(canonical, is_adjective, start, end), ...] in `sentence`."""
    mentions = []
    for match in _COUNTRY_MENTION_RE.finditer(sentence):
        term = match.group(0).casefold()
        if term in COUNTRY_NAME_TO_CANONICAL:
            mentions.append((COUNTRY_NAME_TO_CANONICAL[term], False, match.start(), match.end()))
        else:
            mentions.append((COUNTRY_ADJECTIVE_TO_CANONICAL[term], True, match.start(), match.end()))
    for match in _COUNTRY_CASE_SENSITIVE_RE.finditer(sentence):
        mentions.append((COUNTRY_CASE_SENSITIVE_NAMES[match.group(0)], False,
                         match.start(), match.end()))
    return mentions


def _mention_in_data_context(sentence, is_adjective, start, end):
    if is_adjective:
        return bool(_ADJECTIVE_FOLLOWED_BY_NOUN_RE.match(sentence, end))
    if _NAME_FOLLOWED_BY_NOUN_RE.match(sentence, end):
        return True
    return bool(_DATA_CONTEXT_RE.search(sentence, max(0, start - COUNTRY_PROXIMITY_CHARS), start) or
                _DATA_CONTEXT_RE.search(sentence, end, end + COUNTRY_PROXIMITY_CHARS))


def extract_data_country_evidence(text):
    """Return {canonical country: (start, end) of the first sentence that
    shows the author used that country's data}. In one sentence:
    - a country name counts next to a data-source phrase ("data from
      Germany and France", "ISSP ... Poland") or when followed by a data
      noun ("UK data", "Japan survey");
    - a nationality adjective counts only when followed by a data noun
      ("German respondents", "Chinese General Social Survey"), never next
      to a phrase alone, since "German" is also a personal name.
    Papers routinely mention other countries' unrelated prior research in
    passing, hence the same-sentence rule."""
    text = text or ""
    found = {}
    if not (_COUNTRY_MENTION_RE.search(text) or _COUNTRY_CASE_SENSITIVE_RE.search(text)):
        return found
    for start, end in sentence_spans(text):
        sentence = text[start:end]
        for canonical, is_adjective, mention_start, mention_end in _country_mentions(sentence):
            if canonical not in found and _mention_in_data_context(
                    sentence, is_adjective, mention_start, mention_end):
                found[canonical] = (start, end)
    return found


def extract_data_countries(text):
    """Set of countries from extract_data_country_evidence()."""
    return set(extract_data_country_evidence(text))


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


def semantic_matching_available():
    """Whether sentence-transformers is installed. The packaged exe leaves it
    (and PyTorch, ~2 GB) out, so the page can explain that up front."""
    import importlib.util
    return importlib.util.find_spec("sentence_transformers") is not None


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
    """Return a list of {"tag", "confidence", "method", "evidence", "spans"}
    dicts ("spans" = (start, end) offsets in `text` of what was matched,
    empty for the semantic tier),
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

    def add(tag, confidence, method, evidence, spans=()):
        if tag is None:
            return
        existing = found.get(tag)
        if existing is None or _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[existing["confidence"]]:
            found[tag] = {"tag": tag, "confidence": confidence, "method": method,
                          "evidence": evidence, "spans": list(spans)}

    # Tier 1a: ZA study numbers - every one that resolves.
    za_spans = {}
    for match in ZA_RE.finditer(text):
        za_spans.setdefault(int(match.group(1)), match.span())
    for za in sorted(za_spans):
        if za in ZA_TO_TAG:
            add(ZA_TO_TAG[za], "high", "za_number", f"ZA{za} cited in text", [za_spans[za]])

    # Tier 1b: GESIS dataset DOIs - every one that resolves to a real title.
    if doi_resolver is not None:
        doi_spans = {}
        for match in GESIS_DOI_RE.finditer(text):
            doi_spans.setdefault(match.group(0).casefold(), match.span())
        for doi in sorted(doi_spans):
            try:
                title = doi_resolver(doi)
            except Exception:
                continue
            tag = topic_from_title(title)
            if tag:
                add(tag, "high", "gesis_doi", f"DOI {doi} resolved to '{title}'", [doi_spans[doi]])

    # Tier 1c: exact module name found verbatim, as whole words (so
    # "environmental" does not count as the module name "environment").
    for name, tag in EXACT_MODULE_NAMES.items():
        match = re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE)
        if match:
            add(tag, "high", "exact_module_name", f"Exact module name '{name}' found in text",
                [match.span()])
    for name, tag in EXACT_MODULE_NAMES_NEEDS_ISSP_NEARBY.items():
        span = _name_near_issp(text, name) if name in folded else None
        if span:
            add(tag, "high", "exact_module_name",
                f"Exact module name '{name}' found near an ISSP mention", [span])

    # Tier 2: scored keywords - every module that independently clears the
    # minimum score, not only a unique winner.
    for tag, phrases in TOPIC_KEYWORDS.items():
        hits = [phrase for phrase in phrases if phrase in folded]
        if len(hits) >= MIN_KEYWORD_SCORE:
            spans = [match.span() for match in
                     (re.search(re.escape(phrase), text, re.IGNORECASE) for phrase in hits) if match]
            add(tag, "medium", "keyword",
                f"Matched {len(hits)} topic keyword(s) for {tag} ({', '.join(hits)}), no "
                f"explicit ISSP citation found", spans)

    # Tier 3: semantic similarity - always runs; adds its single best
    # candidate only if that module wasn't already found above. It judges
    # the whole text, so it has no single quotable span.
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
                 "evidence": "No ISSP module evidence found", "spans": []}]
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


# ---------------------------------------------------------------------------
# Full-text fetch cache: a separate file from abstract_cache.jsonl (Abstract
# Finder's own cache) since this stores plain extracted text keyed only by
# URL + page count, not the title/author/year-aware, source-lookup-aware
# entries that feature uses. Same append-only JSONL pattern as the rest of
# the project's caches so a crash mid-run only loses the last unflushed
# line, not the whole file.
# ---------------------------------------------------------------------------

FULL_TEXT_CACHE_FILE = core.data_file("full_text_cache.jsonl")
_FULL_TEXT_CACHE_LOCK = threading.Lock()
FULL_TEXT_CACHE_VERSION = "v1"
FULL_TEXT_SUCCESS_TTL = 180 * 86400   # successfully fetched text rarely changes
FULL_TEXT_FAILURE_TTL = 1 * 86400     # network hiccups/paywalls are worth retrying sooner


def full_text_cache_key(link, max_pdf_pages):
    payload = {"v": FULL_TEXT_CACHE_VERSION, "url": core._canonical_url(link),
              "max_pdf_pages": max_pdf_pages}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def load_full_text_cache():
    with _FULL_TEXT_CACHE_LOCK:
        try:
            with open(FULL_TEXT_CACHE_FILE, "r", encoding="utf-8") as stream:
                cache = {}
                for line in stream:
                    try:
                        entry = json.loads(line)
                        cache[entry["key"]] = {"saved_at": entry["saved_at"], "payload": entry["payload"]}
                    except (KeyError, TypeError, ValueError):
                        continue  # a final partial line can remain after a sudden shutdown
                return cache
        except OSError:
            return {}


def cached_full_text(cache, key):
    entry = cache.get(key)
    if not entry:
        return None
    payload = entry.get("payload", {})
    age = time.time() - float(entry.get("saved_at", 0))
    ttl = FULL_TEXT_SUCCESS_TTL if payload.get("status") in {"abstract_found", "no_abstract_found"} \
        else FULL_TEXT_FAILURE_TTL
    return payload if age <= ttl else None


def append_full_text_cache_entry(key, payload, saved_at=None):
    """Checkpoint one completed fetch without rewriting the full cache."""
    entry = {"key": key, "saved_at": saved_at or time.time(), "payload": payload}
    with _FULL_TEXT_CACHE_LOCK:
        with open(FULL_TEXT_CACHE_FILE, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()


# ---------------------------------------------------------------------------
# Evidence location and status helpers for tag_issp_modules().
# ---------------------------------------------------------------------------

STATUS_TAGGED = "Tagged"
STATUS_NOT_REPORTED = "Not reported"
STATUS_UNAVAILABLE = "Unavailable"
STATUS_CANCELLED = "Cancelled"
FULL_TEXT_LABEL = "Full text"
SEMANTIC_LOCATION = "Whole searched text (meaning-based match)"
MAX_QUOTE_CHARS = 300
MAX_QUOTES_PER_TAG = 2
FULL_TEXT_OK_STATUSES = {"abstract_found", "no_abstract_found"}
FETCH_STATUS_TEXT = {
    "no_link": "no URL/DOI to download",
    "fetch_failed": "download failed",
    "parse_failed": "the page/PDF could not be read",
    "pdf_too_large": "PDF over the size limit",
}


# What does not count as readable text when deciding "Not reported" vs
# "Unavailable": HTML tags, links, bracketed dataset markers such as
# "(ISSP) (EVS)", and database export stamps ("Export Date: ...; Cited By: 3").
_NON_PROSE_RE = re.compile(
    r"<[^>]+>|&\w+;|\b(?:https?://|www\.)\S+|\(\s*[A-Z][A-Za-z0-9-]{1,15}\s*\)|"
    r"\bexport date:[^;\n]*;?|\bcited by:?\s*\d+", re.IGNORECASE)
MIN_READABLE_WORDS = 15


def has_readable_text(value):
    """True if `value` still has MIN_READABLE_WORDS words once markup,
    links, dataset markers, and export stamps are removed - i.e. it is an
    actual abstract/note/full text a person could have read for a module."""
    return len(re.findall(r"[^\W\d_]{2,}", _NON_PROSE_RE.sub(" ", value or ""))) >= MIN_READABLE_WORDS


def _join_labelled_parts(parts):
    """`parts` is [(label, value, is_body), ...]. Join the non-empty values
    with newlines and return (text, segments), where each segment is
    (label, start, end, is_body) - the offsets of that value in `text`."""
    values, segments, position = [], [], 0
    for label, value, is_body in parts:
        if not value:
            continue
        if values:
            position += 1  # the "\n" separator
        segments.append((label, position, position + len(value), is_body))
        values.append(value)
        position += len(value)
    return "\n".join(values), segments


def _segment_at(segments, offset):
    for segment in segments:
        if segment[1] <= offset < segment[2]:
            return segment
    return None


def _quote(text, span, segment):
    """The sentence(s) around `span`, never crossing out of its segment,
    shortened to about MAX_QUOTE_CHARS around the match if very long."""
    _label, seg_start, seg_end, _is_body = segment
    start, end = span
    sentences = sentence_spans(text, seg_start, seg_end)
    quote_start = next((s for s, e in sentences if s <= start < e), seg_start)
    quote_end = next((e for s, e in sentences if s < end <= e), seg_end)
    prefix = suffix = ""
    if quote_end - quote_start > MAX_QUOTE_CHARS:
        room = max(0, (MAX_QUOTE_CHARS - (end - start)) // 2)
        if start - room > quote_start:
            quote_start, prefix = start - room, "…"
        if end + room < quote_end:
            quote_end, suffix = end + room, "…"
    return prefix + re.sub(r"\s+", " ", text[quote_start:quote_end]).strip() + suffix


def _evidence_location_and_quotes(text, segments, spans):
    """Return (ordered unique segment labels, list of quotes) for spans."""
    labels, quotes = [], []
    for span in spans:
        segment = _segment_at(segments, span[0])
        if segment is None:
            continue
        if segment[0] not in labels:
            labels.append(segment[0])
        quote = _quote(text, span, segment)
        if quote not in quotes and len(quotes) < MAX_QUOTES_PER_TAG:
            quotes.append(quote)
    return labels, quotes


def _status_for(matched, segments, fetch_status):
    """Tagged / Not reported / Unavailable, plus a short reason.
    Not reported = there was real text to read (an abstract, notes, or
    downloaded full text - see has_readable_text) and it named no module.
    Unavailable = there was only a title/identifier (or a note like
    "(ISSP)") to go on, so "no module" is not a finding."""
    body = [segment[0] for segment in segments if segment[3]]
    if matched:
        return STATUS_TAGGED, ""
    if body:
        reason = f"Searched {', '.join(body)}; no module named or implied"
        if fetch_status and fetch_status not in FULL_TEXT_OK_STATUSES:
            reason += f" (full text not searched: {FETCH_STATUS_TEXT.get(fetch_status, fetch_status)})"
        return STATUS_NOT_REPORTED, reason
    reason = "Only a title/identifier to search - no readable abstract or notes text"
    if fetch_status is None:
        reason += ", full-text download not enabled"
    else:
        reason += f", full text: {FETCH_STATUS_TEXT.get(fetch_status, fetch_status)}"
    return STATUS_UNAVAILABLE, reason


def tag_issp_modules(dataframe, text_columns, url_column=None, doi_column=None, tag_column=None,
                     title_column=None, use_network_doi_lookup=True, use_semantic_matching=True,
                     fetch_full_text=False, full_text_pdf_pages=15, request_delay=0.5,
                     use_full_text_cache=True, semantic_matcher=None, session=None,
                     progress_callback=None, cancel_event=None):
    """Classify every record - possibly with more than one module tag each
    - and write the result into columns (semicolon-separated when there is
    more than one) plus merge every tag found into `tag_column`.
    `text_columns` is an iterable of column names whose values are
    concatenated as the text to search (typically Title, Abstract, and/or
    Notes/Extra). `title_column`, if given, is the one of those that holds
    only the title: a record whose only text is its title (or notes with
    no readable prose, e.g. just "(ISSP)" or a link) is reported as
    "Unavailable" rather than "Not reported" when nothing is found.

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
    a large batch - so it is opt-in and fully respects `cancel_event`. A
    successful fetch is cached (full_text_cache.jsonl, keyed by URL and
    `full_text_pdf_pages`) for 180 days, and a failed one for 1 day, so a
    second run - e.g. after tweaking the classifier - does not re-download
    everything. Pass use_full_text_cache=False to force a fresh fetch.

    Independently of module classification, every record's search text
    (including any fetched full text) is scanned for ISSP member
    countries named in the same sentence as a data-source phrase (e.g.
    "data from Germany and France") and tagged "DATA - <country>" in
    `tag_column` and the "ISSP Data Countries" column - a paper can of
    course draw on more than one country's data.

    Every module tag and country comes with where it was found (which
    column, or "Full text") and the sentence it was found in, so a reviewer
    can check it without reopening the paper. A record with no module gets
    a status that separates "Not reported" (there was an abstract/notes/
    full text and it named no module) from "Unavailable" (there was
    nothing but a title to read).

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
    full_text_cache = None
    if fetch_full_text:
        session = session or requests.Session()
        if use_full_text_cache:
            full_text_cache = load_full_text_cache()

    if tag_column is None:
        tag_column = guess_column(frame.columns, abstract_tools.TAG_ALIASES)
    if tag_column is None:
        tag_column = "Manual Tags"
        frame[tag_column] = ""

    for column in ("ISSP Module Tag", "ISSP Module Confidence", "ISSP Module Status",
                   "ISSP Module Status Reason", "ISSP Module Method", "ISSP Module Evidence",
                   "ISSP Module Evidence Location", "ISSP Module Evidence Quote",
                   "ISSP Data Countries", "ISSP Data Country Evidence",
                   "ISSP Full Text Fetch Status"):
        frame[column] = ""

    counts = {"Total records": len(frame), "high confidence": 0, "medium confidence": 0,
              "low confidence": 0, "not reported": 0,
              "unavailable": 0, "Cancelled records": 0,
              "full text served from cache": 0, "full text freshly fetched": 0}
    total = len(frame)
    indices = list(frame.index)
    texts, segments_by_index, fetch_statuses, results, countries = {}, {}, {}, {}, {}
    finalize_indices = indices

    for completed, index in enumerate(indices, start=1):
        if cancel_event is not None and cancel_event.is_set():
            counts["Cancelled records"] += total - completed + 1
            remaining = indices[completed - 1:]
            frame.loc[remaining, "ISSP Module Confidence"] = "cancelled"
            frame.loc[remaining, "ISSP Module Status"] = STATUS_CANCELLED
            finalize_indices = indices[:completed - 1]
            break
        row = frame.loc[index]
        parts = []
        for column in text_columns:
            value = clean_value(row.get(column, ""))
            parts.append((column, value, column != title_column and has_readable_text(value)))
        if doi_column:
            parts.append((doi_column, clean_value(row.get(doi_column, "")), False))
        fetch_status = None
        if fetch_full_text:
            link = abstract_tools.record_link(row, url_column, doi_column)
            if link:
                cache_key = full_text_cache_key(link, full_text_pdf_pages) if full_text_cache is not None else None
                cached = cached_full_text(full_text_cache, cache_key) if cache_key else None
                if cached is not None:
                    fetch_result = cached
                    counts["full text served from cache"] += 1
                else:
                    fetch_result = abstract_tools.fetch_abstract(
                        link, session=session, max_pdf_pages=full_text_pdf_pages)
                    counts["full text freshly fetched"] += 1
                    if request_delay:
                        time.sleep(request_delay)
                    if full_text_cache is not None:
                        full_text_cache[cache_key] = {"saved_at": time.time(), "payload": fetch_result}
                        append_full_text_cache_entry(cache_key, fetch_result)
                fetch_status = fetch_result.get("status", "")
                full_text = clean_value(fetch_result.get("full_text", ""))
                parts.append((FULL_TEXT_LABEL, full_text, has_readable_text(full_text)))
            else:
                fetch_status = "no_link"
            frame.at[index, "ISSP Full Text Fetch Status"] = fetch_status
        text, segments = _join_labelled_parts(parts)
        texts[index], segments_by_index[index], fetch_statuses[index] = text, segments, fetch_status
        results[index] = classify_record(text, doi_resolver=doi_resolver)
        countries[index] = extract_data_country_evidence(text)
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
                            "spans": [],
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
        text, segments = texts[index], segments_by_index[index]
        items = results[index]
        matched = [item for item in items if item["tag"]]
        frame.at[index, "ISSP Module Tag"] = "; ".join(item["tag"] for item in matched)
        frame.at[index, "ISSP Module Confidence"] = "; ".join(item["confidence"] for item in matched)
        status, reason = _status_for(matched, segments, fetch_statuses[index])
        frame.at[index, "ISSP Module Status"] = status
        frame.at[index, "ISSP Module Status Reason"] = reason
        if matched:
            frame.at[index, "ISSP Module Method"] = "; ".join(item["method"] for item in matched)
            frame.at[index, "ISSP Module Evidence"] = " | ".join(item["evidence"] for item in matched)
            locations, quotes = [], []
            for item in matched:
                if item.get("spans"):
                    labels, item_quotes = _evidence_location_and_quotes(text, segments, item["spans"])
                    locations.append(f"{item['tag']}: {', '.join(labels)}")
                    quotes.append(f"{item['tag']}: " + " / ".join(f"“{q}”" for q in item_quotes))
                else:
                    locations.append(f"{item['tag']}: {SEMANTIC_LOCATION}")
            frame.at[index, "ISSP Module Evidence Location"] = " | ".join(locations)
            frame.at[index, "ISSP Module Evidence Quote"] = " | ".join(quotes)
            counts[f"{matched[0]['confidence']} confidence"] += 1
        else:
            frame.at[index, "ISSP Module Method"] = items[0]["method"]
            frame.at[index, "ISSP Module Evidence"] = items[0]["evidence"]
            if status == STATUS_NOT_REPORTED:
                counts["not reported"] += 1
            else:
                counts["unavailable"] += 1

        found_countries = sorted(countries.get(index, {}))
        frame.at[index, "ISSP Data Countries"] = "; ".join(found_countries)
        country_evidence = []
        for country in found_countries:
            sentence = countries[index][country]
            segment = _segment_at(segments, sentence[0])
            if segment is not None:
                country_evidence.append(
                    f"{country} [{segment[0]}]: “{_quote(text, sentence, segment)}”")
        frame.at[index, "ISSP Data Country Evidence"] = " | ".join(country_evidence)

        current_tags = frame.at[index, tag_column]
        current_tags = replace_issp_module_tags(current_tags, [item["tag"] for item in matched])
        current_tags = replace_data_country_tags(
            current_tags, [data_country_tag(country) for country in found_countries])
        frame.at[index, tag_column] = current_tags
    counts["records with a data country tag"] = sum(1 for value in countries.values() if value)
    return frame, counts, tag_column
