import os
import tempfile
import unittest

import pandas as pd

try:
    from . import convert_tools as tools
    from . import lookup_core as core
except ImportError:
    import convert_tools as tools
    import lookup_core as core


def sample_frame():
    return pd.DataFrame({
        "Item Type": ["journalArticle", "book", "journalArticle"],
        "Title": ["Religion and trust", "Digital society", "Environmental attitudes"],
        "Author": ["Smith, Jane; Doe, John", "Lee, Kim", "Ng, Ana"],
        "Publication Year": ["2019", "2021", "2015"],
        "Publication Title": ["Social Forces", "", "Env. Politics"],
        "Volume": ["12", "", "4"],
        "Issue": ["3", "", "1"],
        "Pages": ["10-25", "", "100-120"],
        "DOI": ["10.1000/a1", "", "10.1000/c3"],
        "Url": ["", "https://example.org/b", ""],
        "Module": ["RELIG", "DIGSOC", "ENV"],
    })


class FilterTests(unittest.TestCase):
    def test_text_contains_is_case_insensitive(self):
        result = tools.filter_dataframe(sample_frame(), [("Title", "contains", "SOCIETY", "")])
        self.assertEqual(result["Title"].tolist(), ["Digital society"])

    def test_numeric_between(self):
        result = tools.filter_dataframe(
            sample_frame(), [("Publication Year", "between", "2016", "2021")])
        self.assertEqual(result["Publication Year"].tolist(), ["2019", "2021"])

    def test_and_versus_or(self):
        conditions = [("Module", "equals", "relig", ""), ("Publication Year", ">=", "2020", "")]
        self.assertEqual(len(tools.filter_dataframe(sample_frame(), conditions, "AND")), 0)
        self.assertEqual(len(tools.filter_dataframe(sample_frame(), conditions, "OR")), 2)

    def test_blank_operators(self):
        frame = sample_frame()
        self.assertEqual(len(tools.filter_dataframe(frame, [("DOI", "is blank", "", "")])), 1)
        self.assertEqual(len(tools.filter_dataframe(frame, [("DOI", "is not blank", "", "")])), 2)

    def test_no_conditions_keeps_everything(self):
        self.assertEqual(len(tools.filter_dataframe(sample_frame(), [])), 3)

    def test_unknown_column_raises(self):
        with self.assertRaises(KeyError):
            tools.filter_dataframe(sample_frame(), [("Nope", "equals", "x", "")])


class PortableColumnTests(unittest.TestCase):
    def test_custom_columns_are_portable_for_ris(self):
        portable = tools.portable_columns_for(["Title", "DOI", "Module"], "ris")
        self.assertEqual(portable, ["Module"])

    def test_shadowed_alias_is_portable(self):
        portable = tools.portable_columns_for(["Title", "Matched Title"], "bibtex")
        self.assertEqual(portable, ["Matched Title"])

    def test_csv_needs_no_portable_columns(self):
        self.assertEqual(tools.portable_columns_for(["Module"], "csv"), [])

    def test_exact_name_is_native_when_case_variants_collide(self):
        # A lookup result file holds the record's own "DOI" and the lookup's "doi".
        portable = tools.portable_columns_for(["doi", "DOI", "url", "Url"], "ris")
        self.assertEqual(portable, ["doi", "url"])


class BibtexEscapingRoundTripTests(unittest.TestCase):
    def test_newlines_backslashes_and_braces_survive(self):
        frame = pd.DataFrame({
            "Title": ["Paper {with} braces"],
            "Abstract Note": ["Line one&#13;\\n\\tliteral backslash-n"],
            "matched_title": ["Welfare States \nCompared"],  # portable: travels in the Note
        })
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.bib")
            tools.convert_file(frame, path, "bibtex", list(frame.columns))
            loaded = core.read_records_file(path, as_text=True)
        self.assertEqual(loaded.loc[0, "Title"], "Paper {with} braces")
        self.assertEqual(loaded.loc[0, "Abstract Note"], "Line one&#13;\\n\\tliteral backslash-n")
        self.assertEqual(loaded.loc[0, "matched_title"], "Welfare States \nCompared")

    def test_latex_accents_in_third_party_files_are_left_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "in.bib")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write('@article{k,\n  title = {M{\\"u}ller and Co \\& Partners}\n}\n')
            loaded = core.read_records_file(path, as_text=True)
        self.assertEqual(loaded.loc[0, "Title"], 'M{\\"u}ller and Co \\& Partners')


class RisPassThroughTests(unittest.TestCase):
    RIS = ("TY  - JOUR\nTI  - Work orientations\nAU  - Center for Social Research and Data Archives\n"
           "AU  - Kelley, Jonathan\nT2  - Acta Sociologica\nVL  - 44\nIS  - 2\nSP  - 97\nEP  - 110\n"
           "SN  - 0001-6993\nPB  - SAGE\nDA  - 2001/06/01\nLA  - en\nCY  - London\n"
           "A2  - Editor, One\nA2  - Editor, Two\nER  - \n")

    def test_every_tag_survives_a_ris_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = os.path.join(folder, "in.ris"), os.path.join(folder, "out.ris")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write(self.RIS)
            frame = core.read_records_file(source, as_text=True)
            self.assertEqual(frame.loc[0, "RIS A2"], "Editor, One\nEditor, Two")
            self.assertEqual(tools.portable_columns_for(list(frame.columns), "ris"), [])
            tools.convert_file(frame, target, "ris", list(frame.columns))
            with open(target, encoding="utf-8") as stream:
                written = stream.read()
        for line in ("VL  - 44", "IS  - 2", "SP  - 97", "EP  - 110", "SN  - 0001-6993", "PB  - SAGE",
                     "DA  - 2001/06/01", "LA  - en", "CY  - London", "A2  - Editor, One",
                     "A2  - Editor, Two", "JO  - Acta Sociologica",
                     "AU  - Center for Social Research and Data Archives", "AU  - Kelley, Jonathan"):
            self.assertIn(line, written)
        self.assertEqual(written.count("AU  - "), 2)


class RisPatchTests(unittest.TestCase):
    RIS = ("TY  - JOUR\nTI  - Work orientations\nAU  - Kelley, Jonathan\nT2  - Acta Sociologica\n"
           "VL  - 44\nSP  - 97\nEP  - 110\nDA  - 2001/06/01\nN1  - <p>see https://x.org/a</p>\n"
           "N1  - (ISSP)\nKW  - FAMGEN\nKW  - wrong\nER  - \n\n"
           "TY  - BOOK\nTI  - Second\nCY  - London\nER  - \n\n")

    def _run(self, change):
        with tempfile.TemporaryDirectory() as folder:
            source, target = os.path.join(folder, "in.ris"), os.path.join(folder, "out.ris")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write(self.RIS)
            original = core.read_records_file(source)
            changed = original.copy()
            change(changed)
            ok = core.write_ris_patch(source, original, changed, target)
            if not ok:
                self.assertFalse(os.path.exists(target))  # a refused patch writes nothing
                return ok, ""
            with open(target, encoding="utf-8") as stream:
                return ok, stream.read()

    def test_only_changed_fields_are_rewritten(self):
        def change(frame):
            frame.at[0, "Notes"] = "(ISSP)"
            frame.at[1, "Url"] = "https://x.org/b"
            frame["Link Added From Note"] = [False, True]  # audit column: ignored
        ok, text = self._run(change)
        self.assertTrue(ok)
        for line in ("T2  - Acta Sociologica", "VL  - 44", "SP  - 97", "EP  - 110",
                     "DA  - 2001/06/01", "KW  - FAMGEN", "KW  - wrong", "CY  - London"):
            self.assertIn(line, text)
        self.assertNotIn("https://x.org/a", text)
        self.assertEqual(text.count("N1  - "), 1)
        second = text.split("TY  - BOOK")[1]
        self.assertLess(second.index("UR  - https://x.org/b"), second.index("ER  -"))

    def test_keywords_patch(self):
        ok, text = self._run(lambda frame: frame.__setitem__("Keywords", ["FAMGEN", ""]))
        self.assertTrue(ok)
        self.assertIn("KW  - FAMGEN", text)
        self.assertNotIn("KW  - wrong", text)

    def test_other_column_change_refuses_patch(self):
        ok, _text = self._run(lambda frame: frame.__setitem__("Volume", ["45", ""]))
        self.assertFalse(ok)


class RisPatchNoteColumnsTests(unittest.TestCase):
    def test_new_columns_travel_in_the_note_and_come_back(self):
        ris = "TY  - JOUR\nTI  - Paper\nKW  - ENV\nN1  - A real note\nER  - \n"
        with tempfile.TemporaryDirectory() as folder:
            source, target = os.path.join(folder, "in.ris"), os.path.join(folder, "out.ris")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write(ris)
            original = core.read_records_file(source)
            new = original.copy()
            new["ISSP Tags (low)"] = ["HLTH; RELIG"]
            self.assertTrue(core.write_ris_patch(source, original, new, target,
                                                 note_columns=["ISSP Tags (low)"]))
            loaded = core.read_records_file(target)
            self.assertEqual(loaded.loc[0, "ISSP Tags (low)"], "HLTH; RELIG")
            self.assertEqual(loaded.loc[0, "Notes"], "A real note")
            # A second run replaces the carried value instead of adding another line.
            again = loaded.copy()
            again["ISSP Tags (low)"] = ["SOCNET"]
            second = os.path.join(folder, "out2.ris")
            self.assertTrue(core.write_ris_patch(target, loaded, again, second,
                                                 note_columns=["ISSP Tags (low)"]))
            with open(second, encoding="utf-8") as stream:
                text = stream.read()
        self.assertEqual(text.count("ISSP Tags (low)"), 1)
        self.assertIn('ISSP Tags (low) = "SOCNET"', text)

    def test_note_edit_keeps_carried_columns(self):
        # A Note cleanup (e.g. Note Link Recovery) knows nothing about the
        # carried columns; rewriting the Notes must not drop their lines.
        ris = ("TY  - JOUR\nTI  - Paper\nN1  - <p>https://example.org/x. (ISSP)</p>\n"
               "N1  - Literature Lookup field: ISSP Tags (high) = \"ENV\"\nER  - \n")
        with tempfile.TemporaryDirectory() as folder:
            source, target = os.path.join(folder, "in.ris"), os.path.join(folder, "out.ris")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write(ris)
            original = core.read_records_file(source)
            self.assertEqual(original.loc[0, "ISSP Tags (high)"], "ENV")
            new = original.copy()
            new.loc[0, "Notes"] = ""
            self.assertTrue(core.write_ris_patch(source, original, new, target))
            loaded = core.read_records_file(target)
        self.assertEqual(loaded.loc[0, "ISSP Tags (high)"], "ENV")
        self.assertNotIn("example.org", str(loaded.loc[0].get("Notes", "")))


class AddColumnValuesTests(unittest.TestCase):
    def test_adds_missing_items_only_and_keeps_existing(self):
        frame = pd.DataFrame({"Low": ["HLTH; RELIG", "", "ENV"], "Keywords": ["RELIG; MINE", "X", ""]})
        changed = tools.add_column_values(frame, [0, 1, 2], "Low", "Keywords")
        self.assertEqual(changed, 2)
        self.assertEqual(list(frame["Keywords"]), ["RELIG; MINE; HLTH", "X", "ENV"])

    def test_only_given_rows_and_new_target_column(self):
        frame = pd.DataFrame({"Low": ["HLTH", "ENV"]})
        tools.add_column_values(frame, [1], "Low", "Accepted")
        self.assertEqual(list(frame["Accepted"]), ["", "ENV"])


class SplitAuthorsTests(unittest.TestCase):
    def test_organisation_with_and_is_one_author(self):
        self.assertEqual(core._split_authors("Department of Economic and Social Affairs"),
                         ["Department of Economic and Social Affairs"])

    def test_bibtex_style_people_are_split(self):
        self.assertEqual(core._split_authors("Bonsang, E. and A. Van Soest"), ["Bonsang, E.", "A. Van Soest"])

    def test_semicolons_win(self):
        self.assertEqual(core._split_authors("Smith, J.; Doe and Partners Ltd"),
                         ["Smith, J.", "Doe and Partners Ltd"])


class CaseCollisionRoundTripTests(unittest.TestCase):
    def test_record_doi_written_to_tag_and_lookup_doi_kept_in_note(self):
        frame = pd.DataFrame({"Title": ["Paper"], "DOI": ["10.1/own"], "doi": ["10.1/lookup"]})
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.ris")
            tools.convert_file(frame, path, "ris", list(frame.columns))
            with open(path, encoding="utf-8") as stream:
                text = stream.read()
            loaded = core.read_records_file(path, as_text=True)
        self.assertIn("DO  - 10.1/own", text)
        self.assertEqual(loaded.loc[0, "DOI"], "10.1/own")
        self.assertEqual(loaded.loc[0, "doi"], "10.1/lookup")


class ConvertRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_column_selection_and_filter_applied(self):
        path = self._path("out.csv")
        count, _portable = tools.convert_file(
            sample_frame(), path, "csv", ["Title", "DOI"],
            [("DOI", "is not blank", "", "")])
        loaded = core.read_records_file(path)
        self.assertEqual(count, 2)
        self.assertEqual(list(loaded.columns), ["Title", "DOI"])

    def test_every_pair_of_formats_keeps_fields(self):
        columns = ["Title", "Author", "Publication Year", "Publication Title",
                   "Volume", "Issue", "Pages", "DOI", "Module"]
        for source_fmt, source_ext in [("csv", ".csv"), ("ris", ".ris"), ("bibtex", ".bib")]:
            source = self._path("source" + source_ext)
            tools.convert_file(sample_frame(), source, source_fmt, columns)
            loaded = core.read_records_file(source, as_text=True)
            for target_fmt, target_ext in [("csv", ".csv"), ("ris", ".ris"), ("bibtex", ".bib")]:
                with self.subTest(source=source_fmt, target=target_fmt):
                    target = self._path("target" + target_ext)
                    tools.convert_file(loaded, target, target_fmt, columns)
                    result = core.read_records_file(target, as_text=True).fillna("")
                    self.assertEqual(result["Title"].tolist(), sample_frame()["Title"].tolist())
                    self.assertEqual(result["Module"].tolist(), ["RELIG", "DIGSOC", "ENV"])
                    self.assertEqual(result["Pages"].tolist(), ["10-25", "", "100-120"])
                    self.assertEqual(result["Volume"].tolist(), ["12", "", "4"])
                    self.assertEqual(result["DOI"].tolist(), ["10.1000/a1", "", "10.1000/c3"])

    def test_rejects_empty_column_selection(self):
        with self.assertRaises(ValueError):
            tools.convert_file(sample_frame(), self._path("x.csv"), "csv", [])


if __name__ == "__main__":
    unittest.main()
