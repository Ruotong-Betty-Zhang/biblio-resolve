from capture_lib import ll, start_app, pump, wait_until, open_file, shot, set_main_tab, DEMO
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


RIS = os.path.join(DEMO, "ISSP_sample.ris")
quiet = [mock.patch.object(ll.messagebox, name) for name in ("showinfo", "showwarning")]
for patcher in quiet:
    patcher.start()
app = start_app()

# ---- Verification: settings sub-tab ----------------------------------------
set_main_tab(app, "Verification", ll.VERIFICATION_SETTINGS_SUBTAB)
vs = app.verification_settings_page
shot(app, "05_verification_settings", [(1, find(vs, "Title (required)")), (2, find(vs, "Authors")),
                                       (3, find(vs, "Publication Year"))])

# ---- Verification: run -------------------------------------------------------
# Only the records that have a DOI/URL, and without the rate-limit-prone
# OpenAlex / Semantic Scholar, so the run finishes in seconds.
set_main_tab(app, "Verification", ll.VERIFY_RUN_SUBTAB)
for source_id in ("openalex", "semantic_scholar"):
    app.sources_page.source_vars[source_id].set(False)
v = app.verification_page
open_file(v, os.path.join(DEMO, "ISSP_sample_linked.ris"))
v.on_verify()
wait_until(app, lambda: not v.running, timeout=560)
pump(app, 1.0)
row = next((i for i, r in enumerate(v.current_verification_results)
            if r.get("status") == "verified_with_warning"), 1)
v.table.tree.selection_set(str(row)); v.on_result_select(row); pump(app, 0.5)
shot(app, "06_verification_results", [(1, v.title_col), (2, v.doi_col), (3, v.verification_mode),
                                      (4, v.verify_btn), (5, v.table, "right"),
                                      (6, v.verification_message_view, "right"), (7, v.export_btn, "right")])
print("verification:", v.status_var.get())
for source_id in ("openalex", "semantic_scholar"):
    app.sources_page.source_vars[source_id].set(True)

# ---- Abstract Finder -----------------------------------------------------------
set_main_tab(app, "Abstract Finder")
a = app.abstract_page
open_file(a, RIS)
pump(app, 0.5)
a.on_find()
finished = wait_until(app, lambda: not a.running, timeout=300)
if not finished:
    a.on_stop(); wait_until(app, lambda: not a.running, timeout=120)
pump(app, 1.0)
shot(app, "08_abstract_finder", [(1, a.title_col), (2, a.url_col), (3, a.abstract_col),
                                 (4, a.find_btn, "right"), (5, a.stats_box, "right"), (6, a.table, "right")])
print("abstracts:", a.status_var.get())

# ---- ISSP Module Tags ------------------------------------------------------------
set_main_tab(app, "ISSP Module Tags")
m = app.issp_module_page
open_file(m, RIS)
m.network_doi_var.set(False)
m.on_run()
wait_until(app, lambda: not m.running, timeout=300)
pump(app, 1.0)
shot(app, "09_issp_module_tags", [(1, m.title_col), (2, m.tag_col), (3, find(m, "Also resolve GESIS", startswith=True)),
                                  (4, find(m, "Also download each record", startswith=True)),
                                  (5, m.run_btn, "right"), (6, m.table, "right")])
print("issp:", m.status_var.get()[:120])

# ---- Note Link Recovery ------------------------------------------------------------
set_main_tab(app, "Note Link Recovery")
n = app.note_link_page
open_file(n, RIS)
n.remove_links_var.set(True)
n.on_analyze()
pump(app, 1.0)
shot(app, "10_note_link_recovery", [(1, n.note_col), (2, n.analyze_btn, "right"),
                                    (3, find(n, "Remove links from Notes", startswith=True)),
                                    (4, find(n, "Clear Notes that contain", startswith=True)),
                                    (5, n.stats_box, "right"), (6, n.table, "right"), (7, n.export_btn, "right")])

# ---- Keyword Cleanup -----------------------------------------------------------------
set_main_tab(app, "Keyword Cleanup")
k = app.tag_cleanup_page
open_file(k, RIS)
k.on_clean()
pump(app, 1.0)
shot(app, "11_keyword_cleanup", [(1, k.tag_column), (2, k.delimiter), (3, k.clean_btn, "right"),
                                 (4, find(k, "all uppercase (IST, SURVEY DESIGN)")),
                                 (5, k.table, "right"), (6, k.export_btn, "right")])
print("keywords:", k.status_var.get())
app.destroy()
