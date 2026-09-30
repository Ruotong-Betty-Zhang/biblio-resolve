from capture_lib import *  # noqa: F401,F403
from capture_lib import ll, start_app, pump, wait_until, open_file, shot, set_main_tab, DEMO
import os
from unittest import mock


def find(root, text, startswith=False):
    """First descendant widget whose text equals (or starts with) ``text``."""
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


app = start_app()

# ---- Overview: main window + tab strip ---------------------------------
set_main_tab(app, "Single Lookup")
shot(app, "00_main_window", [(1, app.tabview._segmented_button, "top")])

# ---- Single Lookup --------------------------------------------------------
p = app.single_page
p.title_entry.insert(0, "Work orientations in Scandinavia: employment commitment and organizational commitment")
p.author_entry.insert(0, "Svallfors")
p.year_entry.insert(0, "2001")
p.on_search()
wait_until(app, lambda: p.current_results and str(p.search_btn.cget("state")) == "normal", timeout=120)
pump(app, 0.5)
if p.current_results:
    p.table.tree.selection_set("0"); p.on_select(0)
    p.on_verify()
    wait_until(app, lambda: p.verification_var.get() and not p.verification_var.get().startswith("Verifying"),
               timeout=90)
shot(app, "01_single_lookup", [(1, p.title_entry), (2, p.author_entry), (3, p.search_btn, "right"),
                              (4, p.table, "right"), (5, p.doi_entry), (6, p.verify_btn, "right")])

# ---- Batch Import: Sources settings sub-tab -------------------------------
set_main_tab(app, "Batch Import", ll.SOURCES_SUBTAB)
s = app.sources_page
shot(app, "02_sources_settings", [(1, s.email_entry), (2, s.s2_key_entry), (3, s.core_key_entry),
                                  (4, find(s, "Crossref"))])

# ---- Batch Import: lookup ----------------------------------------------------
set_main_tab(app, "Batch Import", ll.BATCH_RUN_SUBTAB)
b = app.batch_page
open_file(b, os.path.join(DEMO, "ISSP_sample.csv"))
pump(app, 0.5)
shot(app, "03_batch_loaded", [(1, find(b, "Choose file…")), (2, b.title_col), (3, b.doi_col),
                              (4, find(b, "Only records missing a DOI or a URL", startswith=True)),
                              (5, b.workers_slider), (6, b.start_btn)])
with mock.patch.object(ll.messagebox, "showinfo"), mock.patch.object(ll.messagebox, "showwarning"), \
        mock.patch.object(ll.messagebox, "askyesno", return_value=True):
    b.on_start()
wait_until(app, lambda: not b.batch_running, timeout=300)
pump(app, 1.0)
shot(app, "04_batch_results", [(1, b.progress, "top"), (2, b.table, "right"), (3, b.output_format),
                               (4, b.export_btn, "right")])
print("batch status:", b.status_var.get())
app.destroy()
