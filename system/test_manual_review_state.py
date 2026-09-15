"""Headless checks for the double-click Manual Review state editor."""

import unittest
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from literature_lookup import ManualReviewDialog


class ManualReviewStateTests(unittest.TestCase):
    def _dialog(self, choice, note):
        frame = pd.DataFrame([{
            "status": "needs_review", "Manual Decision": "",
            "Manual Notes": "",
        }])
        page = SimpleNamespace(
            df=frame, lookup_status_column="status",
            selected_df_index=None, render_page=mock.Mock(),
            apply_btn=SimpleNamespace(configure=mock.Mock()),
            page_label=SimpleNamespace(
                cget=lambda _name: "Rows 1-1 of 1",
                configure=mock.Mock()),
        )
        dialog = SimpleNamespace(
            review_page=page, REVIEW_STATES=ManualReviewDialog.REVIEW_STATES,
            _current_index=lambda: 0, review_state=SimpleNamespace(get=lambda: choice),
            notes=SimpleNamespace(get=lambda: note), _load_record=mock.Mock(),
        )
        return dialog, page

    def test_accepted_updates_status_decision_and_note(self):
        dialog, page = self._dialog("Accepted", "Checked DOI manually")
        ManualReviewDialog._save(dialog)
        self.assertEqual(page.df.at[0, "status"], "accepted")
        self.assertEqual(page.df.at[0, "Manual Decision"], "Approved")
        self.assertEqual(page.df.at[0, "Manual Notes"], "Checked DOI manually")

    def test_not_accepted_updates_status_decision_and_note(self):
        dialog, page = self._dialog("Not accepted", "Different article")
        ManualReviewDialog._save(dialog)
        self.assertEqual(page.df.at[0, "status"], "not_accepted")
        self.assertEqual(page.df.at[0, "Manual Decision"], "Rejected")
        self.assertEqual(page.df.at[0, "Manual Notes"], "Different article")

    def test_no_selection_keeps_original_status(self):
        dialog, page = self._dialog("Select a review state…", "Need to check later")
        ManualReviewDialog._save(dialog)
        self.assertEqual(page.df.at[0, "status"], "needs_review")
        self.assertEqual(page.df.at[0, "Manual Decision"], "")
        self.assertEqual(page.df.at[0, "Manual Notes"], "Need to check later")


if __name__ == "__main__":
    unittest.main()
