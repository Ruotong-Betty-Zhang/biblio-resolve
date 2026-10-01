"""
translate_tools.py
-------------------
Translate bibliographic text (titles, abstracts, ...) with one of three
machine translation APIs:

  - Azure Translator: the larger free quota (2,000,000 characters/month on
    the F0 tier), needs both an API key and a "region" (e.g. "eastus").
    Signing up needs an Azure account, which needs a card on file (even
    though the F0 tier itself never charges it).
  - DeepL: a smaller free quota (500,000 characters/month) but generally
    considered the higher-quality translator, especially for European
    languages. A free-tier key always ends in ":fx", which is how this
    module tells it apart from a paid key (different endpoint). Signing up
    also needs a card on file.
  - MyMemory: no signup, no key, no card at all - a plain HTTP GET anyone
    can call. Quota is much smaller (5,000 chars/day per IP, or 50,000/day
    if you pass a contact email - see MYMEMORY_MAX_REQUEST_BYTES below for
    its other big limitation, a hard 500-byte cap per request) and
    quality is noticeably behind the other two, but it's the only option
    here that costs nothing to even start using.

Azure and DeepL auto-detect the source language when none is given; MyMemory
does not - it rejects an "autodetect" source outright - so this module
falls back to its own langdetect-based guess for MyMemory specifically, and
simply skips a record it can't confidently guess a language for rather than
risk sending a wrong source language. See LANGUAGES below for the codes
each provider expects (they don't agree on spelling, e.g. DeepL wants
"EN-US"/"ZH" where Azure wants "en"/"zh-Hans").
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

import requests

# langdetect: used only to skip records that are already in the target
# language, so translating a mostly-English file to English doesn't spend
# API characters on every single row. Seeded the same way (and for the same
# reason - reproducible results) as doi_lookup_lib.py/find_doi_url_fast.py's
# own use of this library.
from langdetect import detect as _langdetect_detect, LangDetectException
from langdetect.detector_factory import DetectorFactory as _LangDetectorFactory
_LangDetectorFactory.seed = 0

try:  # Package import: python -m system.test_translate_tools
    from . import abstract_note_tools as abstract_tools
    from .app_paths import data_file
except ImportError:  # Direct app/script import from inside system/
    import abstract_note_tools as abstract_tools
    from app_paths import data_file

clean_value = abstract_tools.clean_value

PROVIDERS = ["azure", "deepl", "mymemory"]
PROVIDER_LABELS = {
    "azure": "Azure Translator",
    "deepl": "DeepL",
    "mymemory": "MyMemory (free, no signup)",
}

AZURE_ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"
DEEPL_FREE_ENDPOINT = "https://api-free.deepl.com/v2/translate"
DEEPL_PRO_ENDPOINT = "https://api.deepl.com/v2/translate"
MYMEMORY_ENDPOINT = "https://api.mymemory.translated.net/get"

# Azure allows up to 100 texts/request; DeepL allows up to 50. 25 keeps both
# comfortably under their limits while still batching most files in only a
# handful of requests. MyMemory has no batch endpoint at all - it's called
# once per text regardless of this - but the same number still caps how
# many of those individual calls happen before a progress update/pause.
DEFAULT_BATCH_SIZE = 25

# MyMemory's own documented cap on the "q" parameter is 500 bytes; this
# stays a little under that as a safety margin for UTF-8 multi-byte
# characters landing right on the boundary.
MYMEMORY_MAX_REQUEST_BYTES = 480

# ---------------------------------------------------------------------------
# Translation cache: a translation of the same text, to the same language,
# by the same provider is always the same answer, so this is a plain
# content-addressed cache with no expiry (unlike issp_module_tags.py's
# full-text cache, which does expire - a fetched web page can change, a
# translated sentence can't). Same append-only JSONL pattern as the rest of
# the project's caches, so a crash mid-run only loses the last unflushed
# line, not the whole file. Shared across every file/run/column, so the
# same title showing up twice (a common paper cited by many records, or
# translating the same file a second time from a fresh un-bracketed copy)
# never pays for a second API call.
# ---------------------------------------------------------------------------

TRANSLATE_CACHE_FILE = data_file("translate_cache.jsonl")
_TRANSLATE_CACHE_LOCK = threading.Lock()
TRANSLATE_CACHE_VERSION = "v1"


def _translate_cache_key(text, target_lang, provider):
    payload = {"v": TRANSLATE_CACHE_VERSION, "provider": provider,
              "target": _normalize_lang_code(target_lang), "text": text}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def load_translate_cache():
    with _TRANSLATE_CACHE_LOCK:
        try:
            with open(TRANSLATE_CACHE_FILE, "r", encoding="utf-8") as stream:
                cache = {}
                for line in stream:
                    try:
                        entry = json.loads(line)
                        cache[entry["key"]] = entry["translated"]
                    except (KeyError, TypeError, ValueError):
                        continue  # a final partial line can remain after a sudden shutdown
                return cache
        except OSError:
            return {}


def append_translate_cache_entry(key, translated):
    """Checkpoint one completed translation without rewriting the full cache."""
    entry = {"key": key, "translated": translated, "saved_at": time.time()}
    with _TRANSLATE_CACHE_LOCK:
        with open(TRANSLATE_CACHE_FILE, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()


def translate_cache_entry_count():
    return len(load_translate_cache())


def clear_translate_cache():
    """Delete the on-disk translation cache entirely. Returns how many
    entries were removed, for a confirmation message - the count is read
    before the delete, not after, since the file is simply gone afterward."""
    count = translate_cache_entry_count()
    with _TRANSLATE_CACHE_LOCK:
        try:
            os.remove(TRANSLATE_CACHE_FILE)
        except OSError:
            pass
    return count


# Below this length, langdetect's guess is unreliable (same threshold
# doi_lookup_lib.py uses for titles) - too short to skip translation on
# faith, so these are always sent to the API rather than risk skipping a
# genuinely foreign short title/abstract.
MIN_DETECT_LENGTH = 8

# Target-language options shown in the UI, with each provider's own spelling
# of the code. A language missing a provider's code (None) is skipped from
# that provider's dropdown rather than sent and failing at request time.
LANGUAGES = [
    {"label": "English", "azure": "en", "deepl": "EN-US", "mymemory": "en"},
    {"label": "German", "azure": "de", "deepl": "DE", "mymemory": "de"},
    {"label": "French", "azure": "fr", "deepl": "FR", "mymemory": "fr"},
    {"label": "Spanish", "azure": "es", "deepl": "ES", "mymemory": "es"},
    {"label": "Portuguese", "azure": "pt", "deepl": "PT-PT", "mymemory": "pt"},
    {"label": "Italian", "azure": "it", "deepl": "IT", "mymemory": "it"},
    {"label": "Dutch", "azure": "nl", "deepl": "NL", "mymemory": "nl"},
    {"label": "Polish", "azure": "pl", "deepl": "PL", "mymemory": "pl"},
    {"label": "Russian", "azure": "ru", "deepl": "RU", "mymemory": "ru"},
    {"label": "Swedish", "azure": "sv", "deepl": "SV", "mymemory": "sv"},
    {"label": "Turkish", "azure": "tr", "deepl": "TR", "mymemory": "tr"},
    {"label": "Japanese", "azure": "ja", "deepl": "JA", "mymemory": "ja"},
    {"label": "Korean", "azure": "ko", "deepl": "KO", "mymemory": "ko"},
    {"label": "Chinese (Simplified)", "azure": "zh-Hans", "deepl": "ZH", "mymemory": "zh-CN"},
    {"label": "Chinese (Traditional)", "azure": "zh-Hant", "deepl": "ZH", "mymemory": "zh-TW"},
    {"label": "Arabic", "azure": "ar", "deepl": "AR", "mymemory": "ar"},
]

# ISO 639-1 code -> human label, built from LANGUAGES above, for describing
# a *detected* source language in the run summary (langdetect returns bare
# codes like "de", not either provider's own spelling).
_ISO_TO_LABEL = {}
for _entry in LANGUAGES:
    for _provider_key in ("azure", "deepl", "mymemory"):
        _code = _entry.get(_provider_key)
        if _code:
            _ISO_TO_LABEL.setdefault(_code.strip().lower().split("-")[0], _entry["label"])
del _entry, _provider_key, _code


def language_label_for_code(iso_code):
    if not iso_code or iso_code == "unknown":
        return "Unknown/undetected"
    return _ISO_TO_LABEL.get(iso_code.strip().lower().split("-")[0], iso_code)


class TranslationError(Exception):
    """Raised when a translation request fails: bad key, quota, network, ...

    ``completed`` maps positions in the texts passed to translate_texts()
    to translations that did succeed before the failure, so the caller can
    keep (and cache) them instead of spending quota on them again.
    ``partial`` is set by translate_dataframe_fields() to its usual
    (frame, done, total, stats) result covering everything finished so far."""

    def __init__(self, message, completed=None):
        super().__init__(message)
        self.completed = completed or {}
        self.partial = None


def _normalize_lang_code(code):
    """"EN-US"/"zh-Hans"/"zh-cn"/"en" all collapse to their primary subtag
    ("en"/"zh"/"zh"/"en") so a provider's target-language spelling can be
    compared directly against langdetect's own output."""
    if not code:
        return ""
    return code.strip().lower().split("-")[0]


def _detect_language(text):
    """Best-guess ISO 639-1 code for `text`, or None if too short/undetectable.
    Same guard clause as doi_lookup_lib.detect_title_language: langdetect's
    n-gram model is unreliable below a handful of words."""
    if not text or len(text.strip()) < MIN_DETECT_LENGTH:
        return None
    try:
        return _langdetect_detect(text)
    except LangDetectException:
        return None


def already_in_target_language(text, target_lang):
    """True only when we're confident `text` is already in `target_lang` -
    used to skip sending it to the translation API at all. Anything we
    can't confidently place (blank, too short, detection failed) returns
    False so it still gets sent, rather than risk silently skipping a
    genuinely foreign record."""
    detected = _detect_language(text)
    if detected is None:
        return False
    return _normalize_lang_code(detected) == _normalize_lang_code(target_lang)


# Matches this tool's own "<original> [<translation>]" convention: a
# trailing, non-nested [...] with no unmatched brackets before it. Doesn't
# match a bare citation marker like "... previous studies [2]" - a
# translation is natural-language text, not just digits.
_BRACKET_SUFFIX_RE = re.compile(r"^(.*\S)\s\[([^\[\]]+)\]$", re.DOTALL)


def split_existing_translation(text):
    """If `text` already ends in this tool's own bracket convention from an
    earlier translation run, return (original, existing_translation);
    otherwise (text, None). Used both to avoid ever double-bracketing a
    field and to let a "don't overwrite" run skip already-translated rows
    without re-detecting/re-sending them."""
    text = (text or "").strip()
    match = _BRACKET_SUFFIX_RE.match(text)
    if not match or match.group(2).strip().isdigit():
        return text, None
    return match.group(1), match.group(2)


def language_code(label, provider):
    for entry in LANGUAGES:
        if entry["label"] == label:
            return entry.get(provider)
    return None


def provider_language_labels(provider):
    """Labels this provider has a code for, in LANGUAGES' order."""
    return [entry["label"] for entry in LANGUAGES if entry.get(provider)]


def translate_texts(texts, target_lang, provider, api_key, region=None, source_lang=None, session=None):
    """Translate a list of strings in one batched request. Returns a list of
    the same length, one translated string per input - blank inputs pass
    through untouched rather than spending a request on them.

    For provider="mymemory", `api_key` is actually an optional contact
    email (MyMemory has no real API key) - see _translate_mymemory."""
    if provider == "azure":
        return _translate_azure(texts, target_lang, api_key, region, source_lang, session)
    if provider == "deepl":
        return _translate_deepl(texts, target_lang, api_key, source_lang, session)
    if provider == "mymemory":
        return _translate_mymemory(texts, target_lang, api_key, source_lang, session)
    raise TranslationError(f"Unknown translation provider: {provider!r}")


def _non_blank_indices(texts):
    return [i for i, text in enumerate(texts) if text and text.strip()]


def _translate_azure(texts, target_lang, api_key, region, source_lang, session):
    if not api_key:
        raise TranslationError("Azure Translator needs an API key (set it in Settings).")
    if not region:
        raise TranslationError("Azure Translator needs a region, e.g. \"eastus\" (set it in Settings).")
    indices = _non_blank_indices(texts)
    results = list(texts)
    if not indices:
        return results
    session = session or requests.Session()
    params = {"api-version": "3.0", "to": target_lang}
    if source_lang:
        params["from"] = source_lang
    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Ocp-Apim-Subscription-Region": region,
        "Content-Type": "application/json",
        "X-ClientTraceId": str(uuid.uuid4()),
    }
    body = [{"text": texts[i]} for i in indices]
    try:
        response = session.post(AZURE_ENDPOINT, params=params, headers=headers, json=body, timeout=20)
    except requests.RequestException as exc:
        raise TranslationError(f"Network error contacting Azure Translator: {exc}") from exc
    if response.status_code == 401:
        raise TranslationError("Azure Translator rejected the API key (401 Unauthorized). Check the key/region.")
    if response.status_code == 403:
        raise TranslationError("Azure Translator refused the request (403) - check your free-tier usage/region.")
    if not response.ok:
        raise TranslationError(f"Azure Translator error {response.status_code}: {response.text[:300]}")
    payload = response.json()
    for i, item in zip(indices, payload):
        translations = item.get("translations") or []
        if translations:
            results[i] = translations[0].get("text", texts[i])
    return results


def _translate_deepl(texts, target_lang, api_key, source_lang, session):
    if not api_key:
        raise TranslationError("DeepL needs an API key (set it in Settings).")
    indices = _non_blank_indices(texts)
    results = list(texts)
    if not indices:
        return results
    session = session or requests.Session()
    api_key = api_key.strip()
    free_key = api_key.endswith(":fx")
    endpoint = DEEPL_FREE_ENDPOINT if free_key else DEEPL_PRO_ENDPOINT
    # DeepL retired the auth_key form/query parameter; the key now has to be
    # sent in the Authorization header, otherwise every request gets 403.
    headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}
    data = [("target_lang", target_lang.upper())]
    data += [("text", texts[i]) for i in indices]
    if source_lang:
        data.append(("source_lang", source_lang.upper()))
    try:
        response = session.post(endpoint, data=data, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise TranslationError(f"Network error contacting DeepL: {exc}") from exc
    if response.status_code == 403:
        plan = "Free (ends in :fx, sent to api-free.deepl.com)" if free_key else \
            "Pro (no :fx suffix, sent to api.deepl.com)"
        raise TranslationError(
            f"DeepL rejected the API key (403 Forbidden). The key was treated as a {plan} key. "
            "Check that it is copied completely from your DeepL account's API Keys page, that "
            "the key hasn't been deleted or regenerated, and that your DeepL API subscription "
            "is active.")
    if response.status_code == 456:
        raise TranslationError("DeepL free-tier quota exceeded (456) for this billing period.")
    if not response.ok:
        raise TranslationError(f"DeepL error {response.status_code}: {response.text[:300]}")
    payload = response.json()
    for i, item in zip(indices, payload.get("translations", [])):
        results[i] = item.get("text", texts[i])
    return results


def _hard_split_by_bytes(text, max_bytes):
    """Byte-safe fallback split for a chunk _mymemory_chunks() couldn't
    break on a sentence boundary (e.g. one very long run-on sentence) -
    slices on UTF-8 boundaries so a multi-byte character is never cut in
    half mid-character."""
    encoded = text.encode("utf-8")
    parts = []
    start = 0
    while start < len(encoded):
        end = min(start + max_bytes, len(encoded))
        while end < len(encoded) and end > start and (encoded[end] & 0xC0) == 0x80:  # continuation byte - back up
            end -= 1
        parts.append(encoded[start:end].decode("utf-8", errors="ignore"))
        start = end
    return parts


def _mymemory_chunks(text):
    """Split `text` into pieces that each fit MyMemory's ~500-byte cap on
    a single request, breaking on sentence boundaries so a split doesn't
    land mid-sentence wherever that's possible."""
    if len(text.encode("utf-8")) <= MYMEMORY_MAX_REQUEST_BYTES:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate.encode("utf-8")) <= MYMEMORY_MAX_REQUEST_BYTES:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(sentence.encode("utf-8")) > MYMEMORY_MAX_REQUEST_BYTES:
            chunks.extend(_hard_split_by_bytes(sentence, MYMEMORY_MAX_REQUEST_BYTES))
        else:
            current = sentence
    if current:
        chunks.append(current)
    return chunks


# Substrings MyMemory embeds in `translatedText` itself - with an
# unchanged 200 status - when the daily quota is used up, instead of a
# distinct error response. Checked case-insensitively so this isn't
# mistaken for a real translation and bracketed into the data.
_MYMEMORY_QUOTA_MARKERS = ("mymemory warning", "quota exceeded", "you used all available free translations")


def _mymemory_quota_message(response_text, email):
    """Readable quota message, including MyMemory's own reset countdown."""
    wait = re.search(r"NEXT AVAILABLE IN\s+(\d+)\s+HOURS?\s+(\d+)\s+MINUTES?", response_text or "", re.I)
    when = (f" It resets in about {int(wait.group(1))} h {int(wait.group(2))} min." if wait else "")
    advice = ("switch to Azure or DeepL" if email else
              "add a contact email under Settings (raises the limit to 50,000 characters/day), "
              "or switch to Azure or DeepL")
    return ("MyMemory's free daily quota is used up (5,000 characters/day anonymously, 50,000/day "
            f"with a contact email).{when} Records translated before this point are kept and cached, "
            f"so running again later continues where it stopped. To continue now, {advice}.")


def _translate_mymemory(texts, target_lang, email, source_lang, session):
    indices = _non_blank_indices(texts)
    results = list(texts)
    if not indices:
        return results
    session = session or requests.Session()
    finished = []  # positions fully translated so far, reported if a later one fails
    for i in indices:
        text = texts[i]
        lang_from = source_lang or _detect_language(text)
        if not lang_from:
            # MyMemory has no auto-detect - without a confident guess at
            # the source language there's no safe langpair to send, so
            # this one is left untranslated rather than risk a wrong pair.
            continue
        translated_parts = []
        for chunk in _mymemory_chunks(text):
            params = {"q": chunk, "langpair": f"{lang_from}|{target_lang}"}
            if email:
                params["de"] = email
            completed = {j: results[j] for j in finished}
            try:
                response = session.get(MYMEMORY_ENDPOINT, params=params, timeout=20)
            except requests.RequestException as exc:
                raise TranslationError(f"Network error contacting MyMemory: {exc}", completed) from exc
            # The quota answer can arrive as HTTP 429 or as a 200 whose
            # "translation" is the warning text; check before anything else.
            body = response.text or ""
            if response.status_code == 429 or any(marker in body.lower() for marker in _MYMEMORY_QUOTA_MARKERS):
                raise TranslationError(_mymemory_quota_message(body, email), completed)
            if not response.ok:
                raise TranslationError(f"MyMemory error {response.status_code}: {body[:300]}", completed)
            payload = response.json()
            if payload.get("responseStatus") not in (200, "200"):
                raise TranslationError(
                    f"MyMemory couldn't translate this text: "
                    f"{payload.get('responseDetails', 'unknown error')}", completed)
            translated = payload.get("responseData", {}).get("translatedText", chunk)
            if any(marker in translated.lower() for marker in _MYMEMORY_QUOTA_MARKERS):
                raise TranslationError(_mymemory_quota_message(translated, email), completed)
            translated_parts.append(translated)
        results[i] = " ".join(translated_parts)
        finished.append(i)
    return results


def _combine_original_and_translation(original, translated):
    """"<original> [<translated>]", the convention this bibliography's
    Title field already uses for non-English titles. Skipped in favor of
    the bare original when there's nothing to add - a blank source, a
    translation that failed to return anything, or a source already in the
    target language (the API echoes it back unchanged/near-unchanged) -
    so records don't end up with the same sentence doubled in brackets."""
    original = (original or "").strip()
    translated = (translated or "").strip()
    if not translated or translated.casefold() == original.casefold():
        return original
    return f"{original} [{translated}]"


def _new_column_stats(total_rows):
    return {
        "total": total_rows,
        "translated": 0,
        "served_from_cache": 0,
        "freshly_translated": 0,
        "skipped_already_target_language": 0,
        "skipped_existing_translation": 0,
        "skipped_empty": 0,
        "by_source_language": {},
    }


def translate_dataframe_fields(dataframe, columns, target_lang, provider, api_key, region=None,
                                source_lang=None, batch_size=DEFAULT_BATCH_SIZE, request_delay=0.3,
                                overwrite_existing=True, use_cache=True,
                                progress_callback=None, cancel_event=None):
    """Translate one or more columns *in place*: each selected column is
    overwritten with "<original text> [<translated text>]" rather than
    written into a separate column, so the original is never lost but also
    never needs a second column to find it. Columns are processed one at a
    time, each in batches of `batch_size`; `progress_callback(done, total)`
    counts rows across all columns combined. Stopping early via
    `cancel_event` leaves not-yet-reached rows exactly as they were.

    A row already detected as being in `target_lang` is left untouched
    without ever being sent to the API - free character quota is limited,
    and there's nothing to translate anyway.

    `overwrite_existing` decides what happens to a field that already
    carries this tool's own bracket from an earlier run: True re-translates
    it from the bare original (the old bracket is dropped first, so it's
    never doubled); False leaves the whole field - bracket included -
    exactly as it is, spending no quota on it at all.

    `use_cache` looks up (and, on a fresh translation, saves into) the
    on-disk translation cache (see TRANSLATE_CACHE_FILE above) - a repeat
    of the same text/target/provider is served locally instead of spending
    another API call.

    Returns (frame, done, total, stats). `stats` is {column: {...}} with a
    "total"/"translated"/"served_from_cache"/"freshly_translated"/
    "skipped_already_target_language"/"skipped_existing_translation" count
    and a "by_source_language" dict of {iso_code: count} - a per-run
    breakdown suitable for a "translated N records, M were non-English:
    ..." summary."""
    frame = dataframe.copy()
    columns = [column for column in columns if column]
    total = len(frame) * len(columns)
    session = requests.Session()
    cache = load_translate_cache() if use_cache else None
    stats = {column: _new_column_stats(len(frame)) for column in columns}
    done = 0
    for column in columns:
        if cancel_event is not None and cancel_event.is_set():
            break
        col_stats = stats[column]
        raw_values = [clean_value(v) for v in frame[column]]
        parsed = [split_existing_translation(v) for v in raw_values]
        col_loc = frame.columns.get_loc(column)
        for start in range(0, len(raw_values), max(1, batch_size)):
            if cancel_event is not None and cancel_event.is_set():
                break
            chunk_raw = raw_values[start:start + batch_size]
            chunk_parsed = parsed[start:start + batch_size]
            chunk_originals = [original for original, _ in chunk_parsed]
            keep_as_is = [not overwrite_existing and existing is not None for _, existing in chunk_parsed]
            col_stats["skipped_existing_translation"] += sum(keep_as_is)

            already_target = [
                (not keep_as_is[i]) and already_in_target_language(chunk_originals[i], target_lang)
                for i in range(len(chunk_originals))
            ]
            col_stats["skipped_already_target_language"] += sum(already_target)

            empty = [not keep_as_is[i] and not chunk_originals[i].strip()
                     for i in range(len(chunk_originals))]
            col_stats["skipped_empty"] += sum(empty)
            pending = [i for i in range(len(chunk_originals))
                       if not keep_as_is[i] and not already_target[i] and not empty[i]]
            for i in pending:
                lang = _detect_language(chunk_originals[i]) or "unknown"
                col_stats["by_source_language"][lang] = col_stats["by_source_language"].get(lang, 0) + 1
            col_stats["translated"] += len(pending)

            translated_chunk = list(chunk_originals)
            to_fetch = []
            for i in pending:
                if cache is None:
                    to_fetch.append(i)
                    continue
                key = _translate_cache_key(chunk_originals[i], target_lang, provider)
                if key in cache:
                    translated_chunk[i] = cache[key]
                    col_stats["served_from_cache"] += 1
                else:
                    to_fetch.append(i)
            if to_fetch:
                try:
                    translated_fresh = translate_texts(
                        [chunk_originals[i] for i in to_fetch], target_lang, provider, api_key,
                        region=region, source_lang=source_lang, session=session)
                except TranslationError as exc:
                    # Keep what this batch did finish (and cache it, so the
                    # quota it cost isn't spent again), then hand the caller
                    # everything completed so far.
                    for position, translated in exc.completed.items():
                        i = to_fetch[position]
                        col_stats["freshly_translated"] += 1
                        if cache is not None:
                            key = _translate_cache_key(chunk_originals[i], target_lang, provider)
                            cache[key] = translated
                            append_translate_cache_entry(key, translated)
                        frame.iat[start + i, col_loc] = _combine_original_and_translation(
                            chunk_originals[i], translated)
                    for i in range(len(chunk_originals)):
                        if i not in to_fetch and i in pending:  # served from cache
                            frame.iat[start + i, col_loc] = _combine_original_and_translation(
                                chunk_originals[i], translated_chunk[i])
                    exc.partial = (frame, done, total, stats)
                    raise
                for i, translated in zip(to_fetch, translated_fresh):
                    translated_chunk[i] = translated
                    col_stats["freshly_translated"] += 1
                    if cache is not None:
                        key = _translate_cache_key(chunk_originals[i], target_lang, provider)
                        cache[key] = translated
                        append_translate_cache_entry(key, translated)

            for offset in range(len(chunk_raw)):
                if keep_as_is[offset]:
                    frame.iat[start + offset, col_loc] = chunk_raw[offset]
                else:
                    frame.iat[start + offset, col_loc] = _combine_original_and_translation(
                        chunk_originals[offset], translated_chunk[offset])
            done += len(chunk_raw)
            if progress_callback:
                progress_callback(done, total)
            if request_delay and to_fetch and done < total:
                time.sleep(request_delay)
    return frame, done, total, stats


def format_stats_summary(stats):
    """Render translate_dataframe_fields()'s `stats` dict as the plain-text
    report the Translate page shows after a run: how many records, how many
    were already in the target language, and a breakdown of which source
    languages actually got translated."""
    lines = []
    for column, col_stats in stats.items():
        lines.append(f"{column}: {col_stats['total']:,} records")
        if col_stats.get("skipped_empty"):
            lines.append(f"  Empty (nothing to translate): {col_stats['skipped_empty']:,}")
        lines.append(
            f"  Already in the target language (skipped): {col_stats['skipped_already_target_language']:,}")
        if col_stats["skipped_existing_translation"]:
            lines.append(
                f"  Already had a bracketed translation (left as-is): "
                f"{col_stats['skipped_existing_translation']:,}")
        lines.append(
            f"  Translated (non-target-language records): {col_stats['translated']:,}"
            f" — {col_stats['served_from_cache']:,} from cache, "
            f"{col_stats['freshly_translated']:,} freshly translated")
        if col_stats["by_source_language"]:
            breakdown = ", ".join(
                f"{language_label_for_code(lang)}: {count:,}"
                for lang, count in sorted(col_stats["by_source_language"].items(), key=lambda kv: -kv[1]))
            lines.append(f"  By source language: {breakdown}")
    return "\n".join(lines)
