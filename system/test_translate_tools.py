import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

try:
    from . import translate_tools as tools
except ImportError:
    import translate_tools as tools


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    """Records every call and replays a canned response, in call order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class LanguageCodeTests(unittest.TestCase):
    def test_known_label_returns_provider_code(self):
        self.assertEqual(tools.language_code("German", "azure"), "de")
        self.assertEqual(tools.language_code("German", "deepl"), "DE")

    def test_unknown_label_returns_none(self):
        self.assertIsNone(tools.language_code("Klingon", "azure"))

    def test_provider_language_labels_skips_missing_codes(self):
        labels = tools.provider_language_labels("azure")
        self.assertIn("English", labels)


class TranslateAzureTests(unittest.TestCase):
    def test_translates_non_blank_texts_only(self):
        session = FakeSession([FakeResponse(200, [
            {"translations": [{"text": "Hallo"}]},
            {"translations": [{"text": "Welt"}]},
        ])])
        result = tools.translate_texts(
            ["Hello", "", "World"], "de", "azure", api_key="key", region="eastus", session=session)
        self.assertEqual(result, ["Hallo", "", "Welt"])
        # Only the two non-blank texts were sent.
        body = session.calls[0][1]["json"]
        self.assertEqual([item["text"] for item in body], ["Hello", "World"])

    def test_missing_key_raises_without_network_call(self):
        session = FakeSession([])
        with self.assertRaises(tools.TranslationError):
            tools.translate_texts(["Hello"], "de", "azure", api_key=None, region="eastus", session=session)
        self.assertEqual(session.calls, [])

    def test_missing_region_raises_without_network_call(self):
        session = FakeSession([])
        with self.assertRaises(tools.TranslationError):
            tools.translate_texts(["Hello"], "de", "azure", api_key="key", region=None, session=session)
        self.assertEqual(session.calls, [])

    def test_401_raises_translation_error(self):
        session = FakeSession([FakeResponse(401, text="bad key")])
        with self.assertRaises(tools.TranslationError):
            tools.translate_texts(["Hello"], "de", "azure", api_key="bad", region="eastus", session=session)


class TranslateDeeplTests(unittest.TestCase):
    def test_translates_and_uses_free_endpoint_for_fx_key(self):
        session = FakeSession([FakeResponse(200, {"translations": [{"text": "Bonjour"}]})])
        result = tools.translate_texts(
            ["Hello"], "FR", "deepl", api_key="abc:fx", session=session)
        self.assertEqual(result, ["Bonjour"])
        self.assertEqual(session.calls[0][0], tools.DEEPL_FREE_ENDPOINT)

    def test_key_goes_in_authorization_header_not_the_body(self):
        session = FakeSession([FakeResponse(200, {"translations": [{"text": "Bonjour"}]})])
        tools.translate_texts(["Hello"], "FR", "deepl", api_key=" abc:fx\n", session=session)
        kwargs = session.calls[0][1]
        self.assertEqual(kwargs["headers"]["Authorization"], "DeepL-Auth-Key abc:fx")
        self.assertNotIn("auth_key", [name for name, _value in kwargs["data"]])

    def test_uses_pro_endpoint_for_non_fx_key(self):
        session = FakeSession([FakeResponse(200, {"translations": [{"text": "Bonjour"}]})])
        tools.translate_texts(["Hello"], "FR", "deepl", api_key="abcnotfree", session=session)
        self.assertEqual(session.calls[0][0], tools.DEEPL_PRO_ENDPOINT)

    def test_quota_exceeded_raises_translation_error(self):
        session = FakeSession([FakeResponse(456, text="quota")])
        with self.assertRaises(tools.TranslationError):
            tools.translate_texts(["Hello"], "FR", "deepl", api_key="abc:fx", session=session)

    def test_missing_key_raises_without_network_call(self):
        session = FakeSession([])
        with self.assertRaises(tools.TranslationError):
            tools.translate_texts(["Hello"], "FR", "deepl", api_key=None, session=session)
        self.assertEqual(session.calls, [])


class TranslateMyMemoryTests(unittest.TestCase):
    def test_translates_using_detected_source_language_no_key_needed(self):
        session = FakeSession([FakeResponse(200, {
            "responseData": {"translatedText": "Hallo Welt"}, "responseStatus": 200})])
        result = tools.translate_texts(
            ["Dies ist ein deutscher Satz, der übersetzt werden muss."], "en", "mymemory",
            api_key=None, session=session)
        self.assertEqual(result, ["Hallo Welt"])
        params = session.calls[0][1]["params"]
        self.assertTrue(params["langpair"].startswith("de|"))
        self.assertNotIn("de", params)  # no email passed -> no "de" contact param

    def test_passes_email_as_the_de_param_when_given(self):
        session = FakeSession([FakeResponse(200, {
            "responseData": {"translatedText": "Hallo"}, "responseStatus": 200})])
        tools.translate_texts(
            ["Dies ist ein deutscher Satz, der übersetzt werden muss."], "en", "mymemory",
            api_key="me@example.com", session=session)
        self.assertEqual(session.calls[0][1]["params"]["de"], "me@example.com")

    def test_skips_text_with_no_confidently_detected_language(self):
        session = FakeSession([])
        result = tools.translate_texts(["Hi"], "en", "mymemory", api_key=None, session=session)
        self.assertEqual(result, ["Hi"])  # left untouched, no network call at all
        self.assertEqual(session.calls, [])

    def test_explicit_source_lang_skips_detection(self):
        session = FakeSession([FakeResponse(200, {
            "responseData": {"translatedText": "Bonjour"}, "responseStatus": 200})])
        tools.translate_texts(
            ["Hi"], "fr", "mymemory", api_key=None, source_lang="en", session=session)
        self.assertEqual(session.calls[0][1]["params"]["langpair"], "en|fr")

    def test_non_200_response_status_raises(self):
        session = FakeSession([FakeResponse(200, {"responseStatus": 403, "responseDetails": "nope"})])
        with self.assertRaises(tools.TranslationError):
            tools.translate_texts(
                ["Dies ist ein deutscher Satz, der übersetzt werden muss."], "en", "mymemory",
                api_key=None, session=session)

    def test_quota_warning_embedded_in_translated_text_raises_instead_of_being_used(self):
        session = FakeSession([FakeResponse(200, {
            "responseData": {
                "translatedText": "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS FOR TODAY"},
            "responseStatus": 200})])
        with self.assertRaises(tools.TranslationError):
            tools.translate_texts(
                ["Dies ist ein deutscher Satz, der übersetzt werden muss."], "en", "mymemory",
                api_key=None, session=session)

    def test_http_429_quota_gives_readable_message_with_reset_time(self):
        body = ('{"responseData":{"translatedText":"MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE '
                'TRANSLATIONS FOR TODAY. NEXT AVAILABLE IN  03 HOURS 54 MINUTES 52 SECONDS"}}')
        session = FakeSession([FakeResponse(429, None, text=body)])
        with self.assertRaises(tools.TranslationError) as caught:
            tools.translate_texts(["Hallo Welt, das ist ein Satz."], "en", "mymemory",
                                  api_key=None, source_lang="de", session=session)
        message = str(caught.exception)
        self.assertIn("quota is used up", message)
        self.assertIn("3 h 54 min", message)
        self.assertNotIn("responseData", message)

    def test_failure_reports_texts_already_translated(self):
        ok = FakeResponse(200, {"responseData": {"translatedText": "One"}, "responseStatus": 200})
        session = FakeSession([ok, FakeResponse(429, None, text="MYMEMORY WARNING")])
        with self.assertRaises(tools.TranslationError) as caught:
            tools.translate_texts(["Eins", "Zwei"], "en", "mymemory", api_key=None,
                                  source_lang="de", session=session)
        self.assertEqual(caught.exception.completed, {0: "One"})

    def test_long_text_is_split_into_multiple_requests_and_rejoined(self):
        long_text = "Dies ist ein deutscher Satz. " * 40  # well over 480 bytes
        self.assertGreater(len(long_text.encode("utf-8")), tools.MYMEMORY_MAX_REQUEST_BYTES)
        responses = []
        chunks = tools._mymemory_chunks(long_text)
        self.assertGreater(len(chunks), 1)
        for _ in chunks:
            responses.append(FakeResponse(200, {
                "responseData": {"translatedText": "This is a German sentence."},
                "responseStatus": 200}))
        session = FakeSession(responses)
        result = tools.translate_texts(
            [long_text], "en", "mymemory", api_key=None, source_lang="de", session=session)
        self.assertEqual(len(session.calls), len(chunks))
        self.assertEqual(result[0].count("This is a German sentence."), len(chunks))


class MyMemoryChunkingTests(unittest.TestCase):
    def test_short_text_is_not_split(self):
        self.assertEqual(tools._mymemory_chunks("Short title"), ["Short title"])

    def test_splits_on_sentence_boundaries(self):
        text = ("A. " * 1) + "B" * 460 + ". " + "C" * 460 + "."
        chunks = tools._mymemory_chunks(text)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-8")), tools.MYMEMORY_MAX_REQUEST_BYTES)
        self.assertEqual("".join(chunks).replace(" ", ""), text.replace(" ", ""))

    def test_hard_splits_a_single_run_on_sentence_without_breaking_multibyte_chars(self):
        text = "中" * 300  # each character is 3 bytes in UTF-8 - well over the byte cap
        chunks = tools._mymemory_chunks(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-8")), tools.MYMEMORY_MAX_REQUEST_BYTES)
            chunk.encode("utf-8").decode("utf-8")  # raises if a character got split in half
        self.assertEqual("".join(chunks), text)


class SplitExistingTranslationTests(unittest.TestCase):
    def test_splits_a_previously_translated_field(self):
        original, existing = tools.split_existing_translation(
            "香港社會動態追蹤調查 [Hong Kong Panel Study]")
        self.assertEqual(original, "香港社會動態追蹤調查")
        self.assertEqual(existing, "Hong Kong Panel Study")

    def test_plain_text_has_no_existing_translation(self):
        original, existing = tools.split_existing_translation("Just a normal title")
        self.assertEqual(original, "Just a normal title")
        self.assertIsNone(existing)

    def test_trailing_citation_marker_is_not_mistaken_for_a_translation(self):
        original, existing = tools.split_existing_translation(
            "as shown in previous studies [2]")
        self.assertEqual(original, "as shown in previous studies [2]")
        self.assertIsNone(existing)

    def test_blank_text_round_trips(self):
        self.assertEqual(tools.split_existing_translation(""), ("", None))
        self.assertEqual(tools.split_existing_translation(None), ("", None))


class CombineOriginalAndTranslationTests(unittest.TestCase):
    def test_appends_translation_in_brackets(self):
        self.assertEqual(
            tools._combine_original_and_translation("香港社會動態追蹤調查", "Hong Kong Panel Study"),
            "香港社會動態追蹤調查 [Hong Kong Panel Study]")

    def test_already_target_language_keeps_bare_original(self):
        self.assertEqual(
            tools._combine_original_and_translation("Hello world", "Hello world"), "Hello world")
        self.assertEqual(
            tools._combine_original_and_translation("Hello World", "hello world"), "Hello World")

    def test_blank_translation_keeps_bare_original(self):
        self.assertEqual(tools._combine_original_and_translation("Some title", ""), "Some title")


class AlreadyInTargetLanguageTests(unittest.TestCase):
    def test_english_text_matches_english_target(self):
        self.assertTrue(tools.already_in_target_language(
            "This is a sample sentence in English.", "en"))

    def test_german_text_does_not_match_english_target(self):
        self.assertFalse(tools.already_in_target_language(
            "Das ist ein Beispieltext auf Deutsch.", "en"))

    def test_matches_regardless_of_region_suffix_spelling(self):
        # DeepL spells English targets "EN-US"; Azure spells Chinese "zh-Hans".
        self.assertTrue(tools.already_in_target_language(
            "This is a sample sentence in English.", "EN-US"))

    def test_short_text_is_never_treated_as_already_translated(self):
        self.assertFalse(tools.already_in_target_language("Hi", "en"))

    def test_blank_text_is_never_treated_as_already_translated(self):
        self.assertFalse(tools.already_in_target_language("", "en"))


class TranslateDataframeFieldsSkipsTargetLanguageTests(unittest.TestCase):
    def test_english_rows_are_never_sent_to_the_api(self):
        df = pd.DataFrame({"Title": [
            "This is already a perfectly normal English sentence.",
            "Dies ist ein deutscher Satz, der übersetzt werden muss.",
        ]})
        sent = []

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            sent.extend(texts)
            return [f"[{target_lang}] {t}" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            result, done, total, stats = tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus", request_delay=0, use_cache=False)

        self.assertEqual(sent, ["Dies ist ein deutscher Satz, der übersetzt werden muss."])
        self.assertEqual(result.loc[0, "Title"],
                          "This is already a perfectly normal English sentence.")
        self.assertIn("[en]", result.loc[1, "Title"])
        self.assertEqual((done, total), (2, 2))

    def test_skips_the_api_call_entirely_when_whole_batch_already_matches(self):
        df = pd.DataFrame({"Title": ["This is already a perfectly normal English sentence."]})

        with mock.patch.object(tools, "translate_texts",
                               side_effect=AssertionError("should not be called")):
            result, done, total, stats = tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus", request_delay=0, use_cache=False)

        self.assertEqual(result.loc[0, "Title"], "This is already a perfectly normal English sentence.")
        self.assertEqual((done, total), (1, 1))

    def test_empty_fields_are_skipped_not_counted_as_translated(self):
        df = pd.DataFrame({"Abstract": ["", "  ", "Dies ist ein deutscher Satz, der übersetzt werden muss."]})
        with mock.patch.object(tools, "translate_texts", side_effect=lambda texts, *a, **k: ["EN"] * len(texts)):
            result, _done, _total, stats = tools.translate_dataframe_fields(
                df, ["Abstract"], "en", "azure", "key", region="eastus", request_delay=0, use_cache=False)
        self.assertEqual(stats["Abstract"]["skipped_empty"], 2)
        self.assertEqual(stats["Abstract"]["translated"], 1)
        self.assertNotIn("unknown", stats["Abstract"]["by_source_language"])
        self.assertEqual(result.loc[0, "Abstract"], "")
        self.assertIn("Empty (nothing to translate): 2", tools.format_stats_summary(stats))


class TranslateDataframeFieldsTests(unittest.TestCase):
    def test_translates_title_and_abstract_in_place_with_brackets(self):
        df = pd.DataFrame({
            "Title": ["Hallo Welt"],
            "Abstract Note": ["Ein Test"],
        })

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            return [f"{target_lang.upper()}:{t}" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            result, done, total, stats = tools.translate_dataframe_fields(
                df, ["Title", "Abstract Note"], "en", "azure", "key", region="eastus",
                request_delay=0, use_cache=False)

        self.assertEqual(result.loc[0, "Title"], "Hallo Welt [EN:Hallo Welt]")
        self.assertEqual(result.loc[0, "Abstract Note"], "Ein Test [EN:Ein Test]")
        self.assertEqual((done, total), (2, 2))

    def test_reports_progress_across_multiple_rows_and_columns(self):
        df = pd.DataFrame({"Title": ["A", "B", "C"], "Abstract Note": ["D", "E", "F"]})
        calls = []

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            return [f"{t}-x" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            tools.translate_dataframe_fields(
                df, ["Title", "Abstract Note"], "en", "azure", "key", region="eastus",
                batch_size=2, request_delay=0, use_cache=False,
                progress_callback=lambda d, t: calls.append((d, t)))

        # 3 rows x 2 columns = 6 total; Title done in batches of 2 then 1, then Abstract likewise.
        self.assertEqual(calls, [(2, 6), (3, 6), (5, 6), (6, 6)])

    def test_failure_keeps_and_caches_rows_translated_so_far(self):
        df = pd.DataFrame({"Title": ["A", "B", "C", "D"]})
        calls = []

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            calls.append(list(texts))
            if len(calls) == 1:
                return [f"{t}-x" for t in texts]
            raise tools.TranslationError("quota", completed={0: "C-x"})

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(tools, "TRANSLATE_CACHE_FILE", str(Path(folder) / "cache.jsonl")), \
                mock.patch.object(tools, "already_in_target_language", return_value=False), \
                mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            with self.assertRaises(tools.TranslationError) as caught:
                tools.translate_dataframe_fields(
                    df, ["Title"], "en", "mymemory", None, batch_size=2, request_delay=0)
            frame, done, total, _stats = caught.exception.partial
            cached = tools.load_translate_cache()

        self.assertEqual(list(frame["Title"]), ["A [A-x]", "B [B-x]", "C [C-x]", "D"])
        self.assertEqual((done, total), (2, 4))
        self.assertEqual(sorted(cached.values()), ["A-x", "B-x", "C-x"])

    def test_stops_early_leaving_untouched_rows_as_original(self):
        df = pd.DataFrame({"Title": ["A", "B", "C", "D"]})
        cancel_event = threading.Event()
        cancel_event.set()

        with mock.patch.object(tools, "translate_texts", side_effect=AssertionError("should not be called")):
            result, done, total, stats = tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus", cancel_event=cancel_event,
                use_cache=False)

        self.assertEqual(done, 0)
        self.assertEqual(list(result["Title"]), ["A", "B", "C", "D"])

    def test_skips_columns_that_are_none(self):
        df = pd.DataFrame({"Title": ["A"]})

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            return [f"{t}-x" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            result, done, total, stats = tools.translate_dataframe_fields(
                df, ["Title", None], "en", "azure", "key", region="eastus", request_delay=0,
                use_cache=False)

        self.assertEqual((done, total), (1, 1))


class TranslateDataframeFieldsOverwriteExistingTests(unittest.TestCase):
    def test_overwrite_true_re_translates_from_the_bare_original(self):
        df = pd.DataFrame({"Title": ["Hallo Welt [Old English Title]"]})

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            self.assertEqual(texts, ["Hallo Welt"])  # old bracket must not leak into the request
            return ["New Hello World"]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            result, done, total, stats = tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus",
                overwrite_existing=True, request_delay=0, use_cache=False)

        self.assertEqual(result.loc[0, "Title"], "Hallo Welt [New Hello World]")
        self.assertEqual((done, total), (1, 1))

    def test_overwrite_false_leaves_already_translated_rows_untouched(self):
        df = pd.DataFrame({"Title": [
            "Hallo Welt [Old English Title]",
            "Dies ist neu und unübersetzt",
        ]})
        sent = []

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            sent.extend(texts)
            return [f"[{target_lang}] {t}" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            result, done, total, stats = tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus",
                overwrite_existing=False, request_delay=0, use_cache=False)

        # Only the not-yet-translated row was sent to the API.
        self.assertEqual(sent, ["Dies ist neu und unübersetzt"])
        self.assertEqual(result.loc[0, "Title"], "Hallo Welt [Old English Title]")
        self.assertIn("[en]", result.loc[1, "Title"])
        self.assertEqual((done, total), (2, 2))

    def test_overwrite_false_skips_the_api_call_entirely_when_whole_batch_already_translated(self):
        df = pd.DataFrame({"Title": ["Hallo Welt [Old English Title]"]})

        with mock.patch.object(tools, "translate_texts",
                               side_effect=AssertionError("should not be called")):
            result, done, total, stats = tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus",
                overwrite_existing=False, request_delay=0, use_cache=False)

        self.assertEqual(result.loc[0, "Title"], "Hallo Welt [Old English Title]")
        self.assertEqual((done, total), (1, 1))


class TranslateCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        cache_path = Path(self._tmpdir.name) / "translate_cache.jsonl"
        self.addCleanup(self._tmpdir.cleanup)
        self._patch = mock.patch.object(tools, "TRANSLATE_CACHE_FILE", str(cache_path))
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.cache_path = cache_path

    def test_second_run_is_served_from_cache_not_the_api(self):
        df = pd.DataFrame({"Title": ["Dies ist ein deutscher Satz, der übersetzt werden muss."]})
        call_count = 0

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            nonlocal call_count
            call_count += 1
            return [f"[{target_lang}] {t}" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            result1, _, _, stats1 = tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus", request_delay=0)
            # A second, independent run (e.g. re-importing the un-bracketed
            # source file) should hit the on-disk cache instead of the API.
            result2, _, _, stats2 = tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus", request_delay=0)

        self.assertEqual(call_count, 1)
        self.assertEqual(result1.loc[0, "Title"], result2.loc[0, "Title"])
        self.assertEqual(stats1["Title"]["freshly_translated"], 1)
        self.assertEqual(stats1["Title"]["served_from_cache"], 0)
        self.assertEqual(stats2["Title"]["freshly_translated"], 0)
        self.assertEqual(stats2["Title"]["served_from_cache"], 1)

    def test_use_cache_false_never_reads_or_writes_the_cache_file(self):
        df = pd.DataFrame({"Title": ["Dies ist ein deutscher Satz, der übersetzt werden muss."]})

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            return [f"[{target_lang}] {t}" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus", request_delay=0,
                use_cache=False)

        self.assertFalse(self.cache_path.exists())

    def test_cache_is_specific_to_target_language_and_provider(self):
        df_en = pd.DataFrame({"Title": ["Dies ist ein deutscher Satz, der übersetzt werden muss."]})
        df_fr = df_en.copy()
        calls = []

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            calls.append((target_lang, provider))
            return [f"[{target_lang}/{provider}] {t}" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            tools.translate_dataframe_fields(
                df_en, ["Title"], "en", "azure", "key", region="eastus", request_delay=0)
            tools.translate_dataframe_fields(
                df_fr, ["Title"], "fr", "azure", "key", region="eastus", request_delay=0)

        # Different target language -> cache miss both times, not served from a stale entry.
        self.assertEqual(len(calls), 2)

    def test_entry_count_is_zero_when_no_cache_file_exists(self):
        self.assertEqual(tools.translate_cache_entry_count(), 0)

    def test_entry_count_matches_number_of_cached_translations(self):
        df = pd.DataFrame({"Title": [
            "Dies ist ein deutscher Satz, der übersetzt werden muss.",
            "Ceci est une phrase française qui doit être traduite.",
        ]})

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            return [f"[{target_lang}] {t}" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus", request_delay=0)

        self.assertEqual(tools.translate_cache_entry_count(), 2)

    def test_clear_cache_removes_the_file_and_returns_the_prior_count(self):
        df = pd.DataFrame({"Title": ["Dies ist ein deutscher Satz, der übersetzt werden muss."]})

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            return [f"[{target_lang}] {t}" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus", request_delay=0)

        self.assertTrue(self.cache_path.exists())
        removed = tools.clear_translate_cache()
        self.assertEqual(removed, 1)
        self.assertFalse(self.cache_path.exists())
        self.assertEqual(tools.translate_cache_entry_count(), 0)

    def test_clear_cache_on_an_already_empty_cache_returns_zero(self):
        self.assertEqual(tools.clear_translate_cache(), 0)


class FormatStatsSummaryTests(unittest.TestCase):
    def test_reports_translated_and_skipped_counts_with_language_breakdown(self):
        df = pd.DataFrame({"Title": [
            "This is already a perfectly normal English sentence.",
            "Dies ist ein deutscher Satz, der übersetzt werden muss.",
            "Ceci est une phrase française qui doit être traduite.",
        ]})

        def fake_translate_texts(texts, target_lang, provider, api_key, region=None,
                                  source_lang=None, session=None):
            return [f"[{target_lang}] {t}" for t in texts]

        with mock.patch.object(tools, "translate_texts", side_effect=fake_translate_texts):
            _, _, _, stats = tools.translate_dataframe_fields(
                df, ["Title"], "en", "azure", "key", region="eastus", request_delay=0,
                use_cache=False)

        summary = tools.format_stats_summary(stats)
        self.assertIn("Title: 3 records", summary)
        self.assertIn("Already in the target language (skipped): 1", summary)
        self.assertIn("Translated (non-target-language records): 2", summary)
        self.assertIn("German: 1", summary)
        self.assertIn("French: 1", summary)


if __name__ == "__main__":
    unittest.main()
