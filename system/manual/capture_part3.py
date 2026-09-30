from capture_lib import ll, start_app, pump, wait_until, open_file, shot, set_main_tab, DEMO, PROJECT
import os
from unittest import mock


def find(root, text, startswith=False):
    stack = [root]
    while stack:
        widget = stack.pop(0)
        try:
            value = widget.cget("text")
        except Exception:
            value = None
        if isinstance(value, str) and (value.startswith(text) if startswith else value == text):
            return widget
        stack.extend(widget.winfo_children())
    raise LookupError(text)


for name in ("showinfo", "showwarning", "showerror"):
    mock.patch.object(ll.messagebox, name).start()
app = start_app()

# ---- Verification: column-removal dialog shown on export ---------------------
columns = ["Verification Status", "Verified", "Link Valid", "Paper Match", "Verification Score",
           "Verification Message", "Verification Metadata Title", "Verification Metadata Authors",
           "Verification Metadata Year", "Verification Metadata Source", "Resolved URL",
           "Access Status", "Metadata Conflicts", "Metadata Warnings"]
dialog = ll.VerificationColumnRemovalDialog(app, columns, "RIS (.ris)")
dialog.geometry("680x650+60+40"); pump(app, 1.0); dialog.grab_release()
shot(dialog, "07_verification_remove_columns", [(1, find(dialog, "Select all")),
                                                (2, find(dialog, "Continue to save…"), "right")])
dialog.destroy()

# ---- Review & Convert --------------------------------------------------------------
set_main_tab(app, "Review & Convert")
r = app.review_page
open_file(r, os.path.join(DEMO, "lookup_results_verified.csv"))
pump(app, 0.5)
for column in ("Verification Status", "Verification Score"):
    if column in r.column_vars:
        r.column_vars[column].set(True)
r.render_page()
r.filter_column.set("Verification Status"); r._on_filter_column_change("Verification Status")
r.filter_operator.set("not equals"); r.filter_value.set("verified")
r.apply_filters()
r.output_format.set("RIS (.ris)"); r._update_export_summary()
pump(app, 0.5)
r.table.tree.selection_set("0"); r.select_row(0)
pump(app, 0.5)
shot(app, "12_review_convert", [(1, find(r, "Choose existing file…")), (2, r.output_format),
                                (3, r.export_rows), (4, r.export_columns), (5, r.save_btn, "top"),
                                (6, find(r, "Columns"), "right"), (7, r.filter_column), (8, r.review_btn, "right"),
                                (9, r.table, "right"), (10, r.decision)])

r.open_review_window(); pump(app, 1.0)
d = r.review_window
d.geometry("1260x940+20+10")
d._move(1); pump(app, 1.0)
rows = d._comparison_rows
shot(d, "13_review_dialog", [(1, d.compare_switch), (2, d.view_toggle), (3, d.edit_switch),
                             (4, d.next_btn, "right"), (5, rows["Title"]["holder"]),
                             (6, rows["Title"]["symbol"], "top"), (7, rows["Title"]["ret_holder"], "right"),
                             (8, d.message_box), (9, d.review_state), (10, find(d, "Save & Next"), "right")])
d._close()

# ---- Statistics ------------------------------------------------------------------
set_main_tab(app, "Statistics")
st = app.statistics_page
# Any large library works here; the chart just needs plenty of records.
open_file(st, os.path.join(PROJECT, "dataset", "ISSP Bibliography_final_restored.ris"))
st.value_col.set("Item Type"); st.on_value_column_changed()
st.chart_type.set("Bar chart")
st.on_generate(); pump(app, 1.5)
shot(app, "14_statistics", [(1, st.value_col), (2, st.group_col), (3, st.chart_type),
                            (4, st.generate_btn, "right"), (5, st.overview_table, "inside"),
                            (6, st.canvas.get_tk_widget(), "inside"), (7, st.counts_table, "inside"),
                            (8, st.export_chart_btn, "right")])

# ---- Compare Documents --------------------------------------------------------------
set_main_tab(app, "Compare Documents")
c = app.compare_page
import lookup_core as core
c.load_dataframe("a", core.read_records_file(os.path.join(DEMO, "lookup_results.csv"), as_text=True), "lookup_results.csv")
c.load_dataframe("b", core.read_records_file(os.path.join(DEMO, "lookup_results_verified.csv"), as_text=True),
                 "lookup_results_verified.csv")
c._add_match_pair(); c.match_pairs[0][1].set("Publication Year"); c.match_pairs[0][2].set("Publication Year")
for pair in c.compare_pairs:
    if pair[0] in ("url", "status"):
        pair[2] = True
c.extra_a.set("url"); c.extra_b.set("Resolved URL"); c._add_compare_pair()
pump(app, 0.5)
shot(app, "15_compare_setup", [(1, find(c, "Choose file A…")), (2, c.title_a), (3, find(c, "+ Add match column")),
                               (4, c.pair_tree, "inside"), (5, c.extra_a), (6, c.compare_btn, "right"),
                               (7, c.setup_toggle, "right")])
c.on_compare(); pump(app, 0.8)
if c.visible_records:
    c.table.tree.selection_set("0"); c._show_detail(0)
pump(app, 0.5)
shot(app, "16_compare_results", [(1, c.status_filter), (2, c.column_filter), (3, c.title_search, "right"),
                                 (4, c.table, "right"), (5, c.detail_table, "right"), (6, c.export_btn, "right")])

# ---- Translate ------------------------------------------------------------------------
set_main_tab(app, "Translate")
t = app.translate_page
open_file(t, os.path.join(DEMO, "ISSP_sample.ris"))
t.provider_menu.set("MyMemory (free, no signup)"); t._on_provider_change("MyMemory (free, no signup)")
t.abstract_col.set(ll.NO_COLUMN)
# Shown ready to run: the capture settings hold no API keys.
pump(app, 1.0)
shot(app, "17_translate", [(1, t.title_col), (2, t.abstract_col), (3, t.provider_menu), (4, t.language_menu),
                           (5, find(t, "Re-translate rows", startswith=True)), (6, t.run_btn),
                           (7, find(t, "⚙ Settings…"), "top")])
print("translate:", t.status_var.get()[:150])
settings = ll.TranslateSettingsDialog(app)
settings.geometry("620x560+60+40"); pump(app, 1.0)
shot(settings, "18_translate_settings", [(1, settings.azure_key_entry), (2, settings.deepl_key_entry),
                                         (3, settings.mymemory_email_entry)])
settings.destroy()
app.destroy()
