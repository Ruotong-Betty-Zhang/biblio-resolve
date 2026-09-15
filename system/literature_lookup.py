"""
literature_lookup.py
---------------------
Literature DOI / link lookup tool.

Five tabs:
1. "Single Lookup": type in one title (author/year optional) and search.
2. "Batch Import": pick a CSV exported from Zotero, RIS, BibTeX/BibLaTeX, CSL JSON, or
   Excel file, auto-detect which fields hold title/author/year, run the
   lookup on all of them, then export a Zotero-compatible result file.
3. "Sources": pick which of the 12 free databases to query, and optionally
   supply a contact email / API keys that improve some sources' rate
   limits (or, for CORE, are required for it to return anything at all).
4. "Verification": independently import an existing bibliographic file and
   batch-check its DOI/URL records without running lookup first.
5. "Manual Review": open an existing result file, display only selected
   columns, click DOI/URL links, and save human decisions without API calls.

The actual query/scoring logic (Crossref, OpenAlex, Semantic Scholar,
DataCite, GESIS, arXiv, PubMed, CORE, OpenAIRE, DNB, HAL, CiNii) lives in
doi_lookup_lib.py in this directory. The CSV research pipeline imports this
same system module so the lookup logic exists in exactly one place.

Run it:
    pip install -r requirements.txt
    python literature_lookup.py

Package it into a .exe a coworker can just double-click:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name "Literature Lookup" literature_lookup.py
"""

import os
import queue
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import Menu, filedialog, messagebox, simpledialog, ttk

import customtkinter as ctk
import pandas as pd

try:  # Package import: import system.literature_lookup
    from . import abstract_note_tools as abstract_tools
    from . import issp_module_tags as issp_tags
    from . import lookup_core as core
except ImportError:  # Direct launch: python literature_lookup.py from system/
    import abstract_note_tools as abstract_tools
    import issp_module_tags as issp_tags
    import lookup_core as core


def _resource_path(filename):
    """Resolve a file next to this script, or inside the PyInstaller onefile
    bundle (sys._MEIPASS) when packaged."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)


ctk.set_appearance_mode("System")
ctk.set_default_color_theme(_resource_path("theme.json"))

DEFAULT_BATCH_WORKERS = 4
MIN_BATCH_WORKERS = 1
MAX_BATCH_WORKERS = 12
NO_COLUMN = "(none)"
TABLE_FONT_SIZE = 13
TABLE_HEADING_FONT_SIZE = 13
TABLE_ROW_HEIGHT = 34

STATUS_LABELS = {
    "auto_accepted": "Found",
    "needs_review": "Please verify",
    "accepted": "Manually accepted",
    "not_accepted": "Manually not accepted",
    "low_confidence": "Low confidence",
    "not_found": "Not found",
    "incomplete": "Not found — search incomplete",
    "no_title": "Missing title",
    "unverified": "Unverifiable - insufficient metadata",
    "verified": "Verified",
    "verified_with_warning": "Verified with warning",
    "verified_via_landing_page": "Verified via publisher page",
    "verified_via_container_page": "Verified via container page",
    "container_match": "Container match - not verified",
    "mismatch": "Mismatch",
    "invalid": "Invalid identifier",
    "unavailable": "Temporarily unavailable",
}


def _failed_sources_text(failed_sources):
    """"OpenAlex (timed out), CORE (HTTP 500)" - human-readable summary of
    which sources errored out instead of genuinely returning zero results,
    for display next to a result."""
    if not failed_sources:
        return ""
    parts = [f"{core.source_label(f['source'])} ({f['error']})" for f in failed_sources]
    return ", ".join(parts)


def _format_duration(seconds):
    """"1:05" for under an hour, "1:02:03" once it runs that long."""
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _set_readonly_text(textbox, value):
    textbox.configure(state="normal")
    textbox.delete("1.0", "end")
    textbox.insert("1.0", str(value or ""))
    textbox.configure(state="disabled")


def _set_readonly_text_autosize(textbox, value, min_height=90, max_height=280, line_height=20):
    """Like _set_readonly_text, but grows the box to fit the message (up to
    max_height) instead of leaving it at a fixed height the user has to drag
    open or scroll through by hand."""
    _set_readonly_text(textbox, value)
    textbox.update_idletasks()
    try:
        lines = textbox._textbox.count("1.0", "end", "displaylines")
        lines = lines[0] if lines else 1
    except Exception:
        lines = 1
    textbox.configure(height=min(max_height, max(min_height, lines * line_height + 24)))


def _copy_textbox(widget, textbox):
    value = textbox.get("1.0", "end-1c")
    if value:
        widget.clipboard_clear()
        widget.clipboard_append(value)
        widget.update_idletasks()


class SearchableCTkComboBox(ctk.CTkFrame):
    """CustomTkinter-styled editable picker with a bounded scrollable popup."""

    def __init__(self, parent, values, command=None, width=210, dropdown_rows=9, **kwargs):
        super().__init__(parent, width=width, height=32, fg_color="transparent", **kwargs)
        self.grid_propagate(False)
        self.pack_propagate(False)
        self.all_values = list(values)
        self.command = command
        self.dropdown_rows = max(3, int(dropdown_rows))
        self.popup = None
        self._open_after = None
        self.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(self, corner_radius=7, border_width=1)
        self.entry.grid(row=0, column=0, sticky="nsew")
        self.arrow = ctk.CTkButton(
            self, text="▾", width=28, height=28, corner_radius=7, command=self.toggle_popup,
            font=ctk.CTkFont(size=12),
            fg_color=("#FFFFFF", "#2B2B2B"), hover_color=("#F0F0F0", "#3A3A3A"),
            text_color=("#1B1B1B", "#F3F3F3"),
            border_width=1, border_color=("#D1D1D1", "#4A4A4A"))
        self.arrow.grid(row=0, column=1, sticky="ns", padx=(3, 0))
        self.entry.bind("<KeyRelease>", self._typed)
        self.entry.bind("<Return>", self._confirm)
        self.entry.bind("<Escape>", lambda _event: self.close_popup())
        self.entry.bind("<MouseWheel>", self._cycle)

    def get(self):
        return self.entry.get()

    def set(self, value):
        self.entry.delete(0, "end")
        self.entry.insert(0, str(value or ""))

    def configure(self, require_redraw=False, **kwargs):
        values = kwargs.pop("values", None)
        if values is not None:
            self.all_values = list(values)
            if self.popup is not None and self.popup.winfo_exists():
                self._render_popup(self._matches())
        return super().configure(require_redraw=require_redraw, **kwargs)

    config = configure

    def _matches(self):
        text = self.get().strip().casefold()
        matches = [value for value in self.all_values if text in str(value).casefold()] if text else self.all_values
        return matches or self.all_values

    def _typed(self, event=None):
        if event is not None and event.keysym in {"Return", "Escape", "Up", "Down", "Left", "Right", "Tab"}:
            return
        if self._open_after is not None:
            self.after_cancel(self._open_after)
        self._open_after = self.after(140, self.open_popup)

    def _confirm(self, _event=None):
        text = self.get().strip().casefold()
        exact = next((value for value in self.all_values if str(value).casefold() == text), None)
        selected = exact or (self._matches()[0] if self._matches() else None)
        if selected is not None:
            self._select(selected)
        return "break"

    def _cycle(self, event):
        values = self._matches()
        if not values:
            return "break"
        try:
            index = values.index(self.get())
        except ValueError:
            index = -1
        self._select(values[(index + (-1 if event.delta > 0 else 1)) % len(values)], close=False)
        return "break"

    def toggle_popup(self):
        if self.popup is not None and self.popup.winfo_exists():
            self.close_popup()
        else:
            self.open_popup()

    def open_popup(self):
        self._open_after = None
        if not self.winfo_exists():
            return
        if self.popup is None or not self.popup.winfo_exists():
            self.popup = ctk.CTkToplevel(self)
            self.popup.overrideredirect(True)
            self.popup.transient(self.winfo_toplevel())
            self.popup.bind("<Escape>", lambda _event: self.close_popup())
        width = max(220, self.winfo_width())
        visible = min(self.dropdown_rows, max(1, len(self._matches())))
        height = min(300, visible * 34 + 12)
        self.popup.geometry(f"{width}x{height}+{self.winfo_rootx()}+{self.winfo_rooty() + self.winfo_height() + 2}")
        self._render_popup(self._matches())
        self.popup.deiconify()
        self.popup.lift()

    def _render_popup(self, values):
        for child in self.popup.winfo_children():
            child.destroy()
        scroll = ctk.CTkScrollableFrame(self.popup, corner_radius=7)
        scroll.pack(fill="both", expand=True)
        for value in values:
            button = ctk.CTkButton(
                scroll, text=str(value), height=30, anchor="w", corner_radius=5,
                fg_color="transparent", text_color=("gray10", "gray90"),
                hover_color=("gray80", "gray25"),
                command=lambda selected=value: self._select(selected))
            button.pack(fill="x", padx=3, pady=1)

    def _select(self, value, close=True):
        self.set(value)
        if close:
            self.close_popup()
        if self.command:
            self.command(value)

    def close_popup(self):
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.withdraw()


class VerificationColumnRemovalDialog(ctk.CTkToplevel):
    """Choose verification-added columns to omit from an exported copy."""

    def __init__(self, master, columns, output_label):
        super().__init__(master)
        self.title("Choose verification columns to remove")
        self.geometry("680x650")
        self.minsize(560, 460)
        self.transient(master.winfo_toplevel())
        self.result = None
        self.variables = {column: ctk.BooleanVar(value=False) for column in columns}
        self.checkboxes = []
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        ctk.CTkLabel(
            self, text="Remove verification columns before export",
            font=ctk.CTkFont(size=20, weight="bold"), anchor="w").pack(
                fill="x", padx=18, pady=(18, 6))
        ctk.CTkLabel(
            self,
            text=("Checked columns will be removed only from the exported copy. All columns added by "
                  "Verification are checked by default. Uncheck anything you want to keep."),
            justify="left", anchor="w", wraplength=630,
            text_color=("gray25", "gray75")).pack(fill="x", padx=18, pady=(0, 5))
        if output_label not in {"CSV table (.csv)", "Excel (.xlsx)"}:
            ctk.CTkLabel(
                self,
                text=("This bibliographic format cannot store custom columns natively. Kept Verification "
                      "fields will be carried in each record's Note and restored as columns when this app "
                      "opens the file in Manual Review."),
                justify="left", anchor="w", wraplength=630,
                text_color=("#8a5500", "#f0b35a")).pack(fill="x", padx=18, pady=(0, 7))

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=18, pady=(2, 6))
        ctk.CTkButton(controls, text="Select all", width=105,
                      command=lambda: self._set_all(True)).pack(side="left")
        ctk.CTkButton(controls, text="Keep all", width=105,
                      command=lambda: self._set_all(False)).pack(side="left", padx=8)
        self.count_var = ctk.StringVar()
        ctk.CTkLabel(controls, textvariable=self.count_var, anchor="e").pack(side="right")

        scroll = ctk.CTkScrollableFrame(self)
        scroll.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        for column in columns:
            checkbox = ctk.CTkCheckBox(
                scroll, text=column, variable=self.variables[column],
                command=self._update_count)
            checkbox.pack(fill="x", padx=8, pady=4)
            checkbox.select()
            self.checkboxes.append(checkbox)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(0, 18))
        ctk.CTkButton(buttons, text="Cancel", width=110, fg_color=("gray65", "gray35"),
                      hover_color=("gray55", "gray45"), command=self._cancel).pack(side="right")
        ctk.CTkButton(buttons, text="Continue to save…", width=165,
                      command=self._confirm).pack(side="right", padx=(0, 8))
        self._update_count()
        self.after(50, self._activate_modal)

    def _activate_modal(self):
        if not self.winfo_exists():
            return
        self.grab_set()
        self.lift()
        self.focus_force()

    def _set_all(self, selected):
        for variable in self.variables.values():
            variable.set(selected)
        self._update_count()

    def _update_count(self):
        selected = sum(variable.get() for variable in self.variables.values())
        self.count_var.set(f"{selected} of {len(self.variables)} columns selected for removal")

    def _confirm(self):
        self.result = [column for column, variable in self.variables.items() if variable.get()]
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


def _make_searchable_combobox(parent, values, command=None, width=210, dropdown_rows=9, **kwargs):
    return SearchableCTkComboBox(
        parent, values=values, command=command, width=width,
        dropdown_rows=dropdown_rows, **kwargs)


def _update_combobox_values(combo, values):
    """Refresh both the visible dropdown list and the scroll/type-ahead
    master list on a combobox created by `_make_searchable_combobox`."""
    combo.all_values = list(values)
    combo.configure(values=combo.all_values)


# ---------------------------------------------------------------------------
# A lightweight table backed by one native Treeview. The earlier version made
# one CustomTkinter label per cell (often 1,000+ widgets per preview), which was
# expensive to create/layout and accumulated global mouse-wheel bindings.
# ---------------------------------------------------------------------------

_TREEVIEW_COLORS = {}
_TREEVIEW_STYLE_NAME = "Literature.Treeview"


def _style_literature_treeview(widget):
    """(Re)configure the shared Treeview ttk style to match theme.json,
    with flat borders, an accent selection color, and zebra striping."""
    dark = ctk.get_appearance_mode() == "Dark"
    colors = {
        "bg": "#2B2B2B" if dark else "#FFFFFF",
        "alt_bg": "#333333" if dark else "#F5F5F5",
        "fg": "#F3F3F3" if dark else "#1B1B1B",
        "heading_bg": "#333333" if dark else "#F3F3F3",
        "accent": "#1F6AA5" if dark else "#0F6CBD",
    }
    _TREEVIEW_COLORS.clear()
    _TREEVIEW_COLORS.update(colors)

    style = ttk.Style(widget)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(
        _TREEVIEW_STYLE_NAME,
        font=("Segoe UI", TABLE_FONT_SIZE),
        rowheight=TABLE_ROW_HEIGHT,
        background=colors["bg"], fieldbackground=colors["bg"], foreground=colors["fg"],
        borderwidth=0, relief="flat",
    )
    style.map(
        _TREEVIEW_STYLE_NAME,
        background=[("selected", colors["accent"])],
        foreground=[("selected", "#FFFFFF")],
    )
    style.configure(
        f"{_TREEVIEW_STYLE_NAME}.Heading",
        font=("Segoe UI", TABLE_HEADING_FONT_SIZE, "bold"),
        padding=(10, 8),
        background=colors["heading_bg"], foreground=colors["fg"],
        borderwidth=0, relief="flat",
    )
    style.map(f"{_TREEVIEW_STYLE_NAME}.Heading", background=[("active", colors["heading_bg"])])


class ResultsTable(ctk.CTkFrame):
    def __init__(self, master, headers, weights, on_select=None, on_activate=None,
                 link_columns=None, link_resolvers=None, **kwargs):
        super().__init__(master, **kwargs)
        self.headers = headers
        self.weights = weights
        self.on_select = on_select
        self.on_activate = on_activate
        self.link_columns = link_columns or set()
        self.link_resolvers = link_resolvers or {}
        self.selected_index = None
        self.selected_cell = None
        self.copy_values = []
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        columns = [f"c{i}" for i in range(len(headers))]
        _style_literature_treeview(self)
        self.tree = ttk.Treeview(
            self, columns=columns, show="headings", style="Literature.Treeview",
            selectmode="browse")
        self.tree.tag_configure("oddrow", background=_TREEVIEW_COLORS["alt_bg"])
        self.tree.tag_configure("evenrow", background=_TREEVIEW_COLORS["bg"])
        self.v_scroll = ctk.CTkScrollbar(self, orientation="vertical", command=self.tree.yview)
        self.h_scroll = ctk.CTkScrollbar(self, orientation="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=self.v_scroll.set, xscrollcommand=self.h_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.v_scroll.grid(row=0, column=1, sticky="ns", padx=(3, 0))
        self.h_scroll.grid(row=1, column=0, sticky="ew", pady=(3, 0))

        for col, (column_id, header) in enumerate(zip(columns, headers)):
            self.tree.heading(column_id, text=header, anchor="w")
            # Keep a useful minimum width. Wide tables naturally overflow and
            # use the horizontal scrollbar instead of squeezing text away.
            width = max(115, min(260, len(str(header)) * 10 + 35))
            if col < len(weights) and weights[col] > 1:
                width = max(width, 190)
            self.tree.column(column_id, width=width, minwidth=80, stretch=False, anchor="w")

        self.tree.bind("<ButtonRelease-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Control-c>", self._copy_shortcut)
        self.tree.bind("<Control-C>", self._copy_shortcut)
        self.tree.bind("<Motion>", self._on_motion)
        self.context_menu = Menu(self.tree, tearoff=False)
        self.context_menu.add_command(label="Copy cell", command=self.copy_selected_cell)
        self.context_menu.add_command(label="Copy row", command=self.copy_selected_row)

    def clear(self):
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self.selected_index = None
        self.selected_cell = None
        self.copy_values = []

    @staticmethod
    def _text(value):
        return "" if value is None else str(value)

    def set_rows(self, rows_values, copy_values=None):
        self.clear()
        source = copy_values if copy_values is not None else rows_values
        self.copy_values = [[self._text(value) for value in row] for row in source]
        for index, values in enumerate(rows_values):
            self.tree.insert("", "end", iid=str(index),
                             values=[self._text(value) for value in values],
                             tags=("evenrow" if index % 2 == 0 else "oddrow",))

    def _cell_at(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None, None
        row_id = self.tree.identify_row(event.y)
        column_id = self.tree.identify_column(event.x)
        if not row_id or not column_id:
            return None, None
        return row_id, int(column_id[1:]) - 1

    def _on_motion(self, event):
        row_id, col = self._cell_at(event)
        value = self.tree.set(row_id, f"c{col}") if row_id is not None else ""
        self.tree.configure(cursor="hand2" if col in self.link_columns and value.strip() else "")

    def _on_click(self, event):
        row_id, col = self._cell_at(event)
        if row_id is None:
            return
        idx = int(row_id)
        self.selected_index = idx
        self.selected_cell = (idx, col)
        self.tree.selection_set(row_id)
        self.tree.focus(row_id)
        self.tree.focus_set()
        value = self._selected_cell_value()
        if col in self.link_columns and value.strip():
            resolver = self.link_resolvers.get(col, lambda item: item)
            webbrowser.open(resolver(value))
            return
        if self.on_select:
            self.on_select(idx)

    def _on_double_click(self, event):
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        idx = int(row_id)
        self.selected_index = idx
        self.tree.selection_set(row_id)
        self.tree.focus(row_id)
        if self.on_select:
            self.on_select(idx)
        if self.on_activate:
            self.on_activate(idx)

    def _on_right_click(self, event):
        row_id, col = self._cell_at(event)
        if row_id is None:
            return
        self.selected_index = int(row_id)
        self.selected_cell = (self.selected_index, col)
        self.tree.selection_set(row_id)
        self.tree.focus(row_id)
        self.tree.focus_set()
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _selected_cell_value(self):
        if self.selected_cell is None:
            return ""
        row, col = self.selected_cell
        if row >= len(self.copy_values) or col >= len(self.copy_values[row]):
            return ""
        return self.copy_values[row][col]

    def _put_on_clipboard(self, value):
        self.clipboard_clear()
        self.clipboard_append(value)
        # Flush the clipboard ownership so the value remains available after
        # focus moves to another application.
        self.update_idletasks()

    def copy_selected_cell(self):
        if self.selected_cell is not None:
            self._put_on_clipboard(self._selected_cell_value())

    def copy_selected_row(self):
        if self.selected_index is not None and self.selected_index < len(self.copy_values):
            self._put_on_clipboard("\t".join(self.copy_values[self.selected_index]))

    def _copy_shortcut(self, _event=None):
        self.copy_selected_cell()
        return "break"


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Literature Lookup")
        self.geometry("1200x760")
        self.minsize(1080, 640)

        tabview = ctk.CTkTabview(
            self, corner_radius=10,
            segmented_button_font=ctk.CTkFont(size=13, weight="bold"))
        tabview.pack(fill="both", expand=True, padx=18, pady=16)
        single_tab = tabview.add("Single Lookup")
        batch_tab = tabview.add("Batch Import")
        verification_tab = tabview.add("Verification")
        verification_settings_tab = tabview.add("Verification Settings")
        abstract_tab = tabview.add("Abstract Finder")
        issp_module_tab = tabview.add("ISSP Module Tags")
        note_links_tab = tabview.add("Note Link Recovery")
        tag_cleanup_tab = tabview.add("Keyword Cleanup")
        review_tab = tabview.add("Manual Review")
        sources_tab = tabview.add("Sources")
        tabview.set("Single Lookup")

        self.sources_page = SourcesPage(sources_tab)
        self.sources_page.pack(fill="both", expand=True)

        self.verification_settings_page = VerificationSettingsPage(verification_settings_tab)
        self.verification_settings_page.pack(fill="both", expand=True)

        self.tag_cleanup_page = TagCleanupPage(tag_cleanup_tab)
        self.tag_cleanup_page.pack(fill="both", expand=True)

        self.abstract_page = AbstractFinderPage(abstract_tab, self.sources_page)
        self.abstract_page.pack(fill="both", expand=True)

        self.issp_module_page = IsspModulePage(issp_module_tab)
        self.issp_module_page.pack(fill="both", expand=True)

        self.note_link_page = NoteLinkRecoveryPage(note_links_tab)
        self.note_link_page.pack(fill="both", expand=True)

        self.single_page = SingleLookupPage(single_tab, self.sources_page, self.verification_settings_page)
        self.single_page.pack(fill="both", expand=True)

        self.batch_page = BatchLookupPage(batch_tab, self.sources_page)
        self.batch_page.pack(fill="both", expand=True)

        self.verification_page = VerificationPage(
            verification_tab, self.sources_page, self.verification_settings_page)
        self.verification_page.pack(fill="both", expand=True)

        self.review_page = ManualReviewPage(review_tab)
        self.review_page.pack(fill="both", expand=True)


# ---------------------------------------------------------------------------
# Note link recovery: local-only analysis of URLs embedded in Notes
# ---------------------------------------------------------------------------

class NoteLinkRecoveryPage(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.df = self.output_df = None
        self.file_path = None
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(6, 8))
        ctk.CTkButton(top, text="Choose file…", width=130, command=self.on_choose_file).pack(side="left")
        self.file_label = ctk.CTkLabel(top, text="Choose CSV, Excel, RIS, BibTeX, or CSL JSON", anchor="w")
        self.file_label.pack(side="left", padx=12, fill="x", expand=True)
        self.export_btn = ctk.CTkButton(top, text="Export enriched copy…", width=170,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="right")

        settings = ctk.CTkFrame(self)
        settings.pack(fill="x", padx=4, pady=(0, 8))
        self.note_col = self._mapping(settings, "Notes column", 0)
        self.url_col = self._mapping(settings, "URL column", 1)
        self.doi_col = self._mapping(settings, "DOI column", 2)
        self.analyze_btn = ctk.CTkButton(settings, text="Analyze notes & add missing links", width=220,
                                         command=self.on_analyze, state="disabled")
        self.analyze_btn.grid(row=1, column=3, padx=10, pady=(0, 10))

        self.stats_box = ctk.CTkTextbox(self, height=175, wrap="word", font=ctk.CTkFont(size=14))
        self.stats_box.pack(fill="x", padx=4, pady=(0, 8))
        _set_readonly_text(self.stats_box, "Load a file to analyze Note and link coverage. No webpages are accessed on this page.")
        self.table = ResultsTable(
            self, headers=["Title", "Existing / recovered link", "Note URLs found", "Added from Note"],
            weights=[2, 2, 2, 1], link_columns={1})
        self.table.pack(fill="both", expand=True, padx=4, pady=(0, 6))

    @staticmethod
    def _mapping(parent, label, column):
        ctk.CTkLabel(parent, text=label, font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=column, sticky="w", padx=10, pady=(8, 2))
        menu = ctk.CTkOptionMenu(parent, values=[NO_COLUMN], width=205)
        menu.grid(row=1, column=column, sticky="w", padx=10, pady=(0, 10))
        return menu

    def on_choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose a bibliographic file",
            filetypes=[("Supported files", "*.csv *.xlsx *.xls *.json *.ris *.bib *.bibtex"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            self.df = core.read_records_file(path).reset_index(drop=True)
        except Exception as exc:
            messagebox.showerror("Couldn't read file", f"Failed to read this file:\n{exc}")
            return
        self.file_path, self.output_df = path, None
        columns = list(self.df.columns)
        mappings = ((self.note_col, abstract_tools.NOTE_ALIASES),
                    (self.url_col, abstract_tools.URL_ALIASES),
                    (self.doi_col, abstract_tools.DOI_ALIASES))
        for menu, aliases in mappings:
            menu.configure(values=[NO_COLUMN] + columns)
            menu.set(abstract_tools.guess_column(columns, aliases) or NO_COLUMN)
        encoding = self.df.attrs.get("source_encoding")
        self.file_label.configure(
            text=f"{os.path.basename(path)} ({len(self.df):,} records)" + (f" · {encoding}" if encoding else ""))
        self.analyze_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.table.set_rows([])
        _set_readonly_text(self.stats_box, "Ready. This operation is local and does not access the internet.")

    def on_analyze(self):
        if self.df is None:
            return
        note = None if self.note_col.get() == NO_COLUMN else self.note_col.get()
        url = None if self.url_col.get() == NO_COLUMN else self.url_col.get()
        doi = None if self.doi_col.get() == NO_COLUMN else self.doi_col.get()
        self.output_df, stats, columns = abstract_tools.analyze_notes_and_add_links(
            self.df, note_column=note, url_column=url, doi_column=doi)
        self._show_stats(stats)
        title_col = core.guess_column(self.output_df.columns, core.TITLE_ALIASES)
        rows = []
        for _, row in self.output_df.head(250).iterrows():
            link = abstract_tools.record_link(row, columns["url_column"], columns["doi_column"])
            rows.append((abstract_tools.clean_value(row.get(title_col, ""))[:100], link,
                         abstract_tools.clean_value(row.get("Note URLs Found", "")),
                         "Yes" if bool(row.get("Link Added From Note", False)) else "No"))
        self.table.set_rows(rows)
        self.export_btn.configure(state="normal")

    def _show_stats(self, stats):
        total = max(1, stats.get("Total records", 0))
        lines = ["Note and link statistics"]
        displayed_stats = (
            ("Total records", "Total records"),
            ("Records with a Note", "Records with a Note"),
            ("Records with both a link and a Note", "Records with both a link and a Note"),
            ("Records without a link but whose Note contains a link",
             "Records without a link whose Note contains a URL"),
        )
        for label, stats_key in displayed_stats:
            count = stats.get(stats_key, 0)
            lines.append(
                f"{label}: {count:,} ({count / total:.1%})"
                if stats_key != "Total records" else f"{label}: {count:,}")
        _set_readonly_text(self.stats_box, "\n".join(lines))

    def on_export(self):
        if self.output_df is None:
            return
        _save_enriched_dataframe(self, self.output_df, self.file_path, "note_links")


# ---------------------------------------------------------------------------
# Abstract finder: network extraction from existing URL/DOI only
# ---------------------------------------------------------------------------

class AbstractFinderPage(ctk.CTkFrame):
    def __init__(self, master, sources_page):
        super().__init__(master, fg_color="transparent")
        self.sources_page = sources_page
        self.df = self.output_df = None
        self.file_path = None
        self.running = False
        self.cancel_event = threading.Event()
        self.events = queue.Queue()

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(6, 8))
        ctk.CTkButton(top, text="Choose file…", width=130, command=self.on_choose_file).pack(side="left")
        self.file_label = ctk.CTkLabel(top, text="Choose a Zotero-compatible bibliographic file", anchor="w")
        self.file_label.pack(side="left", padx=12, fill="x", expand=True)
        self.export_btn = ctk.CTkButton(top, text="Export with abstracts…", width=170,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="right")

        identity_settings = ctk.CTkFrame(self)
        identity_settings.pack(fill="x", padx=4, pady=(0, 6))
        self.title_col = NoteLinkRecoveryPage._mapping(identity_settings, "Title column (for matching)", 0)
        self.author_col = NoteLinkRecoveryPage._mapping(identity_settings, "Author column (for matching)", 1)
        self.year_col = NoteLinkRecoveryPage._mapping(identity_settings, "Year column (optional)", 2)

        settings = ctk.CTkFrame(self)
        settings.pack(fill="x", padx=4, pady=(0, 8))
        self.url_col = NoteLinkRecoveryPage._mapping(settings, "URL column", 0)
        self.doi_col = NoteLinkRecoveryPage._mapping(settings, "DOI column", 1)
        self.abstract_col = NoteLinkRecoveryPage._mapping(settings, "Abstract column (RIS AB)", 2)
        self.find_btn = ctk.CTkButton(settings, text="Find missing abstracts", width=165,
                                      command=self.on_find, state="disabled")
        self.find_btn.grid(row=1, column=3, padx=(10, 4), pady=(0, 10))
        self.stop_btn = ctk.CTkButton(settings, text="Stop", width=75, command=self.on_stop, state="disabled")
        self.stop_btn.grid(row=1, column=4, padx=(4, 10), pady=(0, 10))

        self.progress = ctk.CTkProgressBar(self)
        self.progress.pack(fill="x", padx=4, pady=(0, 5)); self.progress.set(0)
        self.status_var = ctk.StringVar(
            value="Abstracts are always saved; title/author identity checks add a dedicated review tag.")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=4, pady=(0, 5))
        self.stats_box = ctk.CTkTextbox(self, height=145, wrap="word", font=ctk.CTkFont(size=14))
        self.stats_box.pack(fill="x", padx=4, pady=(0, 8))
        _set_readonly_text(self.stats_box, "Load a file to see link and abstract coverage.")
        self.table = ResultsTable(
            self, headers=["Title", "Fetch status", "Review tag", "Page title", "Abstract source",
                           "Abstract", "Source URL"],
            weights=[2, 1, 2, 2, 1, 3, 2], link_columns={6})
        self.table.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        self.after(150, self._poll_events)

    def on_choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose a bibliographic file",
            filetypes=[("Supported files", "*.csv *.xlsx *.xls *.json *.ris *.bib *.bibtex"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            self.df = core.read_records_file(path).reset_index(drop=True)
        except Exception as exc:
            messagebox.showerror("Couldn't read file", f"Failed to read this file:\n{exc}")
            return
        self.file_path, self.output_df = path, None
        columns = list(self.df.columns)
        for menu, aliases in ((self.title_col, core.TITLE_ALIASES),
                              (self.author_col, core.AUTHOR_ALIASES),
                              (self.year_col, core.YEAR_ALIASES),
                              (self.url_col, abstract_tools.URL_ALIASES),
                              (self.doi_col, abstract_tools.DOI_ALIASES),
                              (self.abstract_col, abstract_tools.ABSTRACT_ALIASES)):
            menu.configure(values=[NO_COLUMN] + columns)
            menu.set(abstract_tools.guess_column(columns, aliases) or NO_COLUMN)
        encoding = self.df.attrs.get("source_encoding")
        self.file_label.configure(
            text=f"{os.path.basename(path)} ({len(self.df):,} records)" + (f" · {encoding}" if encoding else ""))
        self.find_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.progress.set(0); self.table.set_rows([])
        self._show_initial_stats()

    def _show_initial_stats(self):
        url = None if self.url_col.get() == NO_COLUMN else self.url_col.get()
        doi = None if self.doi_col.get() == NO_COLUMN else self.doi_col.get()
        abstract = None if self.abstract_col.get() == NO_COLUMN else self.abstract_col.get()
        title = None if self.title_col.get() == NO_COLUMN else self.title_col.get()
        rows = list(self.df.iterrows())
        existing_flags = {
            index: bool(abstract_tools.clean_value(row.get(abstract, ""))) if abstract else False
            for index, row in rows
        }
        link_flags = {
            index: bool(abstract_tools.record_link(row, url, doi)) for index, row in rows
        }
        title_flags = {
            index: bool(abstract_tools.clean_value(row.get(title, ""))) if title else False
            for index, row in rows
        }
        links = sum(link_flags.values())
        existing = sum(existing_flags.values())
        source_eligible = sum(
            not existing_flags[index] and title_flags[index] for index, _row in rows)
        webpage_fallback = sum(
            not existing_flags[index] and link_flags[index] for index, _row in rows)
        total = max(1, len(self.df))
        _set_readonly_text(self.stats_box, "\n".join((
            f"Total records: {len(self.df):,}",
            f"Records with an existing URL/DOI: {links:,} ({links / total:.1%})",
            f"Records without a URL/DOI: {len(self.df) - links:,} ({(len(self.df) - links) / total:.1%})",
            f"Records with an existing Abstract: {existing:,} ({existing / total:.1%})",
            f"Records eligible for Source lookup: {source_eligible:,}",
            f"Records potentially eligible for webpage fallback: {webpage_fallback:,}",
        )))

    def on_find(self):
        if self.df is None or self.running:
            return
        url = None if self.url_col.get() == NO_COLUMN else self.url_col.get()
        doi = None if self.doi_col.get() == NO_COLUMN else self.doi_col.get()
        abstract = None if self.abstract_col.get() == NO_COLUMN else self.abstract_col.get()
        title = None if self.title_col.get() == NO_COLUMN else self.title_col.get()
        author = None if self.author_col.get() == NO_COLUMN else self.author_col.get()
        year = None if self.year_col.get() == NO_COLUMN else self.year_col.get()
        enabled_sources = self.sources_page.get_enabled_sources()
        email = self.sources_page.get_email()
        s2_key = self.sources_page.get_s2_key()
        core_key = self.sources_page.get_core_key()
        if not enabled_sources and not url and not doi:
            messagebox.showwarning(
                "Nothing to search",
                "Enable at least one Source, or choose a URL/DOI column for webpage fallback.")
            return
        self.running = True; self.cancel_event.clear(); self.progress.set(0)
        self.find_btn.configure(state="disabled"); self.stop_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.status_var.set("Checking enabled Sources first, then using URL/DOI webpage fallback…")

        def worker():
            try:
                source_lookup = None
                if enabled_sources:
                    source_lookup = lambda **record: core.lookup_abstract_from_sources(
                        **record, enabled_sources=enabled_sources, email=email,
                        s2_api_key=s2_key, core_api_key=core_key)
                abstract_cache = core.load_abstract_cache()

                def cache_key(record):
                    return core.abstract_cache_key(
                        **record, enabled_sources=enabled_sources)

                def cache_lookup(**record):
                    return core.cached_abstract(abstract_cache, cache_key(record))

                def cache_store(payload, **record):
                    key = cache_key(record)
                    saved_at = time.time()
                    abstract_cache[key] = {"saved_at": saved_at, "payload": payload}
                    # Append just this record so large runs remain fast and resumable.
                    core.append_abstract_cache_entry(key, payload, saved_at=saved_at)

                result = abstract_tools.find_abstracts(
                    self.df, url_column=url, doi_column=doi, abstract_column=abstract,
                    title_column=title, author_column=author, year_column=year,
                    source_lookup=source_lookup,
                    cache_lookup=cache_lookup, cache_store=cache_store,
                    cancel_event=self.cancel_event,
                    progress_callback=lambda done, total, index, status:
                        self.events.put(("progress", (done, total, status))))
                self.events.put(("done", result))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def on_stop(self):
        if self.running:
            self.cancel_event.set()
            self.status_var.set("Stopping after the current request…")

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    done, total, status = payload
                    self.progress.set(done / max(1, total))
                    self.status_var.set(f"Processed {done:,}/{total:,} records · latest status: {status}")
                elif kind == "done":
                    self.output_df, stats, _columns = payload
                    self.running = False
                    self.find_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    self.export_btn.configure(state="normal")
                    self.status_var.set("Abstract processing complete. Export to save the enriched file.")
                    self._show_results(stats)
                elif kind == "error":
                    self.running = False
                    self.find_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    messagebox.showerror("Abstract lookup failed", payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_events)

    def _show_results(self, stats):
        total = max(1, stats.get("Total records", 0))
        _set_readonly_text(self.stats_box, "\n".join(
            f"{label}: {count:,}" + (f" ({count / total:.1%})" if label != "Total records" else "")
            for label, count in stats.items()))
        title_col = core.guess_column(self.output_df.columns, core.TITLE_ALIASES)
        abstract_col = abstract_tools.guess_column(self.output_df.columns, abstract_tools.ABSTRACT_ALIASES)
        rows = []
        for _, row in self.output_df.head(250).iterrows():
            abstract = abstract_tools.clean_value(row.get(abstract_col, ""))
            rows.append((abstract_tools.clean_value(row.get(title_col, ""))[:100],
                         abstract_tools.clean_value(row.get("Abstract Fetch Status", "")),
                         abstract_tools.clean_value(row.get("Abstract Review Tag", "")),
                         abstract_tools.clean_value(row.get("Abstract Page Title", ""))[:100],
                         abstract_tools.clean_value(row.get("Abstract Source", "")),
                         abstract[:220], abstract_tools.clean_value(row.get("Abstract Source URL", ""))))
        self.table.set_rows(rows)

    def on_export(self):
        if self.output_df is not None:
            _save_enriched_dataframe(self, self.output_df, self.file_path, "abstracts")


def _save_enriched_dataframe(parent, dataframe, source_path, suffix):
    base = os.path.splitext(os.path.basename(source_path or "records"))[0]
    source_label = core.preferred_output_format_label(source_path)
    default_format, default_ext = core.OUTPUT_FORMATS[source_label]
    filetypes = [(f"Same format as source ({source_label})", f"*{default_ext}")]
    filetypes.extend(
        (label, f"*{extension}")
        for label, (_format, extension) in core.OUTPUT_FORMATS.items()
        if label != source_label
    )
    path = filedialog.asksaveasfilename(
        title="Export enriched records", defaultextension=default_ext,
        filetypes=filetypes, initialfile=f"{base}_{suffix}{default_ext}")
    if not path:
        return
    ext = os.path.splitext(path)[1].casefold()
    formats = {".csv": "csv", ".xlsx": "excel", ".ris": "ris", ".bib": "bibtex",
               ".bibtex": "bibtex", ".json": "csl_json"}
    try:
        core.write_records_file(dataframe, path, formats.get(ext, default_format))
    except Exception as exc:
        messagebox.showerror("Export failed", f"Couldn't save the enriched file:\n{exc}")
        return
    messagebox.showinfo("Export complete", f"Saved to:\n{path}")


# ---------------------------------------------------------------------------
# ISSP module tags: classify which ISSP topical module a record used, from
# ZA study numbers / GESIS DOIs / "ISSP <year>" mentions / topic keywords,
# in that order of decreasing confidence (see issp_module_tags.py).
# ---------------------------------------------------------------------------

class IsspModulePage(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.df = self.output_df = None
        self.file_path = None
        self.running = False
        self.cancel_event = threading.Event()
        self.events = queue.Queue()

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(6, 8))
        ctk.CTkButton(top, text="Choose file…", width=130, command=self.on_choose_file).pack(side="left")
        self.file_label = ctk.CTkLabel(top, text="Choose a Zotero-compatible bibliographic file", anchor="w")
        self.file_label.pack(side="left", padx=12, fill="x", expand=True)
        self.export_btn = ctk.CTkButton(top, text="Export with module tags…", width=185,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="right")

        mapping_top = ctk.CTkFrame(self)
        mapping_top.pack(fill="x", padx=4, pady=(0, 4))
        self.title_col = NoteLinkRecoveryPage._mapping(mapping_top, "Title column", 0)
        self.abstract_col = NoteLinkRecoveryPage._mapping(mapping_top, "Abstract column", 1)
        self.notes_col = NoteLinkRecoveryPage._mapping(mapping_top, "Notes/Extra column (optional)", 2)

        mapping_bottom = ctk.CTkFrame(self)
        mapping_bottom.pack(fill="x", padx=4, pady=(0, 6))
        self.url_col = NoteLinkRecoveryPage._mapping(mapping_bottom, "URL column", 0)
        self.doi_col = NoteLinkRecoveryPage._mapping(mapping_bottom, "DOI column", 1)
        self.tag_col = NoteLinkRecoveryPage._mapping(mapping_bottom, "Tags/Keywords column", 2)

        settings = ctk.CTkFrame(self, fg_color="transparent")
        settings.pack(fill="x", padx=8, pady=(0, 6))
        self.network_doi_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            settings, variable=self.network_doi_var,
            text="Also resolve GESIS dataset DOIs online (10.4232/1.xxxxx) — needed when a record "
                 "cites only a DOI, not a ZA study number").pack(anchor="w")
        self.semantic_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            settings, variable=self.semantic_var,
            text="Also match by meaning, even for records already tagged another way — free, "
                 "runs entirely on this computer (downloads a small open-source model the "
                 "first time; no account, no per-use cost, nothing sent anywhere after that)").pack(
                     anchor="w", pady=(4, 0))
        self.full_text_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            settings, variable=self.full_text_var,
            text="Also download each record's linked page/PDF and search the full text — much "
                 "slower (one web request per record with a URL or DOI), but reaches Methods/"
                 "Data sections an abstract alone would miss, and also looks for which "
                 "country(ies)' data the record used, tagged \"DATA - <country>\". Uses OCR for "
                 "scanned PDFs when Tesseract is installed.").pack(anchor="w", pady=(4, 0))

        run_row = ctk.CTkFrame(self, fg_color="transparent")
        run_row.pack(fill="x", padx=4, pady=(0, 6))
        self.run_btn = ctk.CTkButton(run_row, text="Classify records", width=150,
                                     command=self.on_run, state="disabled")
        self.run_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(run_row, text="Stop", width=75, command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))

        self.progress = ctk.CTkProgressBar(self)
        self.progress.pack(fill="x", padx=4, pady=(0, 5)); self.progress.set(0)
        self.status_var = ctk.StringVar(
            value=("ZA study numbers, resolved GESIS DOIs, and exact module names are treated "
                   "as confirmed evidence; matching keywords or meaning-based matching are "
                   "flagged lower-confidence for review. A record can end up with more than "
                   "one module tag."))
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", justify="left",
                     wraplength=1100).pack(fill="x", padx=4, pady=(0, 5))
        self.stats_box = ctk.CTkTextbox(self, height=120, wrap="word", font=ctk.CTkFont(size=14))
        self.stats_box.pack(fill="x", padx=4, pady=(0, 8))
        _set_readonly_text(
            self.stats_box, "Load a file to classify. No webpages are accessed unless the DOI "
                            "resolution checkbox above is enabled.")
        self.table = ResultsTable(
            self, headers=["Title", "Tag", "Confidence", "Method", "Data countries", "Evidence"],
            weights=[2, 0, 0, 1, 0, 3])
        self.table.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        self.after(150, self._poll_events)

    def on_choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose a bibliographic file",
            filetypes=[("Supported files", "*.csv *.xlsx *.xls *.json *.ris *.bib *.bibtex"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            self.df = core.read_records_file(path).reset_index(drop=True)
        except Exception as exc:
            messagebox.showerror("Couldn't read file", f"Failed to read this file:\n{exc}")
            return
        self.file_path, self.output_df = path, None
        columns = list(self.df.columns)
        for menu, aliases in ((self.title_col, core.TITLE_ALIASES),
                              (self.abstract_col, abstract_tools.ABSTRACT_ALIASES),
                              (self.notes_col, abstract_tools.NOTE_ALIASES),
                              (self.url_col, abstract_tools.URL_ALIASES),
                              (self.doi_col, abstract_tools.DOI_ALIASES),
                              (self.tag_col, abstract_tools.TAG_ALIASES)):
            menu.configure(values=[NO_COLUMN] + columns)
            menu.set(abstract_tools.guess_column(columns, aliases) or NO_COLUMN)
        encoding = self.df.attrs.get("source_encoding")
        self.file_label.configure(
            text=f"{os.path.basename(path)} ({len(self.df):,} records)" + (f" · {encoding}" if encoding else ""))
        self.run_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.progress.set(0); self.table.set_rows([])
        _set_readonly_text(self.stats_box, "Ready. Confirm the column mapping above, then classify.")

    def on_run(self):
        if self.df is None or self.running:
            return
        title = None if self.title_col.get() == NO_COLUMN else self.title_col.get()
        abstract = None if self.abstract_col.get() == NO_COLUMN else self.abstract_col.get()
        notes = None if self.notes_col.get() == NO_COLUMN else self.notes_col.get()
        url = None if self.url_col.get() == NO_COLUMN else self.url_col.get()
        doi = None if self.doi_col.get() == NO_COLUMN else self.doi_col.get()
        tag = None if self.tag_col.get() == NO_COLUMN else self.tag_col.get()
        text_columns = [column for column in (title, abstract, notes) if column]
        if not text_columns and not doi:
            messagebox.showwarning(
                "Nothing to search", "Choose at least an Abstract, Notes/Extra, or DOI column.")
            return
        fetch_full_text = self.full_text_var.get()
        if fetch_full_text and not url and not doi:
            messagebox.showwarning(
                "Nothing to fetch", "Full-text fetching needs a URL or DOI column so it knows "
                                    "what to download.")
            return
        self.running = True; self.cancel_event.clear(); self.progress.set(0)
        self.run_btn.configure(state="disabled"); self.stop_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.status_var.set(
            "Downloading full text and classifying… this can take a while over a large batch."
            if fetch_full_text else "Classifying…")
        use_network_doi_lookup = self.network_doi_var.get()
        use_semantic_matching = self.semantic_var.get()

        def worker():
            try:
                result_df, stats, tag_column = issp_tags.tag_issp_modules(
                    self.df, text_columns=text_columns, url_column=url, doi_column=doi,
                    tag_column=tag,
                    use_network_doi_lookup=use_network_doi_lookup,
                    use_semantic_matching=use_semantic_matching,
                    fetch_full_text=fetch_full_text,
                    cancel_event=self.cancel_event,
                    progress_callback=lambda done, total, index, confidence:
                        self.events.put(("progress", (done, total, confidence))))
                self.events.put(("done", (result_df, stats, tag_column)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def on_stop(self):
        if self.running:
            self.cancel_event.set()
            self.status_var.set("Stopping after the current record…")

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    done, total, confidence = payload
                    self.progress.set(done / max(1, total))
                    self.status_var.set(f"Processed {done:,}/{total:,} records · latest: {confidence}")
                elif kind == "done":
                    self.output_df, stats, tag_column = payload
                    self.running = False
                    self.run_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    self.export_btn.configure(state="normal")
                    self.status_var.set(
                        f"Classification complete. Tags were written into '{tag_column}'. "
                        f"Export to save, or open the export in Manual Review to check "
                        f"low/medium-confidence rows.")
                    self._show_results(stats)
                elif kind == "error":
                    self.running = False
                    self.run_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    messagebox.showerror("Classification failed", payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_events)

    def _show_results(self, stats):
        total = max(1, stats.get("Total records", 0))
        _set_readonly_text(self.stats_box, "\n".join(
            f"{label}: {count:,}" + (f" ({count / total:.1%})" if label != "Total records" else "")
            for label, count in stats.items()))
        title_col = core.guess_column(self.output_df.columns, core.TITLE_ALIASES)
        rows = []
        for _, row in self.output_df.head(250).iterrows():
            rows.append((
                abstract_tools.clean_value(row.get(title_col, ""))[:100] if title_col else "",
                abstract_tools.clean_value(row.get("ISSP Module Tag", "")),
                abstract_tools.clean_value(row.get("ISSP Module Confidence", "")),
                abstract_tools.clean_value(row.get("ISSP Module Method", "")),
                abstract_tools.clean_value(row.get("ISSP Data Countries", "")),
                abstract_tools.clean_value(row.get("ISSP Module Evidence", "")),
            ))
        self.table.set_rows(rows)

    def on_export(self):
        if self.output_df is not None:
            _save_enriched_dataframe(self, self.output_df, self.file_path, "issp_module_tags")


# ---------------------------------------------------------------------------
# Keyword cleanup: keep only user-authored uppercase keywords
# ---------------------------------------------------------------------------

class TagCleanupPage(ctk.CTkFrame):
    """Remove non-uppercase keywords without touching the original file."""
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.df = None
        self.output_df = None
        self.file_path = None

        ctk.CTkLabel(self, text="Uppercase keyword cleanup",
                     font=ctk.CTkFont(size=20, weight="bold"), anchor="w").pack(
                         fill="x", padx=12, pady=(12, 4))
        ctk.CTkLabel(
            self,
            text=("Keep only keywords whose letters are all uppercase (for example: IST, SURVEY DESIGN, "
                  "COVID-19). Mixed-case and lowercase keywords are removed. The source file is never overwritten."),
            anchor="w", justify="left", wraplength=1050,
            text_color=("gray25", "gray75")).pack(fill="x", padx=12, pady=(0, 10))

        file_row = ctk.CTkFrame(self, fg_color="transparent")
        file_row.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkButton(file_row, text="Choose file…", width=130,
                      command=self.on_choose_file).pack(side="left")
        self.file_label = ctk.CTkLabel(
            file_row, text="Choose CSV, Excel, RIS, or BibTeX", anchor="w")
        self.file_label.pack(side="left", padx=12, fill="x", expand=True)

        settings = ctk.CTkFrame(self)
        settings.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(settings, text="Keywords field").pack(side="left", padx=(12, 6), pady=10)
        self.tag_column = ctk.CTkOptionMenu(settings, values=[NO_COLUMN], width=210)
        self.tag_column.set(NO_COLUMN)
        self.tag_column.pack(side="left", padx=(0, 18), pady=10)
        ctk.CTkLabel(settings, text="Keyword separator").pack(side="left", padx=(0, 6), pady=10)
        self.delimiter = ctk.CTkOptionMenu(
            settings, values=["Auto", "Semicolon (;)", "New line", "Comma (, )"], width=155)
        self.delimiter.set("Auto")
        self.delimiter.pack(side="left", padx=(0, 18), pady=10)
        self.clean_btn = ctk.CTkButton(settings, text="Preview cleanup", width=140,
                                       command=self.on_clean, state="disabled")
        self.clean_btn.pack(side="left", pady=10)
        self.export_btn = ctk.CTkButton(settings, text="Export cleaned copy…", width=170,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="right", padx=12, pady=10)

        self.status_var = ctk.StringVar(value="No file loaded.")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w",
                     text_color=("gray30", "gray70")).pack(fill="x", padx=12, pady=(0, 6))
        self.table = ResultsTable(
            self, headers=["Record", "Original keywords", "Kept uppercase keywords", "Removed keywords"],
            weights=[1, 2, 2, 2])
        self.table.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def on_choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose a file containing keywords",
            filetypes=[
                ("Supported bibliographic files", "*.csv *.xlsx *.xls *.ris *.bib *.bibtex"),
                ("CSV", "*.csv"), ("Excel", "*.xlsx *.xls"),
                ("RIS", "*.ris"), ("BibTeX / BibLaTeX", "*.bib *.bibtex"),
                ("All files", "*.*")])
        if not path:
            return
        if os.path.splitext(path)[1].lower() not in {
                ".csv", ".xlsx", ".xls", ".ris", ".bib", ".bibtex"}:
            messagebox.showwarning(
                "Unsupported keyword file",
                "Keyword Cleanup supports CSV, Excel, RIS, and BibTeX files. JSON and TSV are not supported.")
            return
        try:
            df = core.read_records_file(path)
        except Exception as exc:
            messagebox.showerror("Couldn't read file", f"Failed to read this file:\n{exc}")
            return
        if df.empty:
            messagebox.showwarning("Empty file", "No records were found in this file.")
            return
        self.df, self.file_path, self.output_df = df, path, None
        columns = list(df.columns)
        normalized = {re.sub(r"[^a-z]", "", str(column).casefold()): column for column in columns}
        manual_tags_column = normalized.get("manualtags") or normalized.get("manualtag")
        automatic_tags_column = normalized.get("automatictags") or normalized.get("automatictag")
        guess = manual_tags_column or core.guess_column(columns, core.TAG_ALIASES)
        self.tag_column.configure(values=[NO_COLUMN] + columns)
        self.tag_column.set(guess or NO_COLUMN)
        self.tag_column.configure(state="normal")
        encoding = df.attrs.get("source_encoding")
        encoding_note = f" · {encoding}" if encoding else ""
        self.file_label.configure(text=f"{os.path.basename(path)} ({len(df)} records){encoding_note}")
        self.clean_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.table.set_rows([])
        if manual_tags_column and automatic_tags_column:
            self.status_var.set(
                "Manual Tags and Automatic Tags detected. Manual Tags is selected by default; "
                "only uppercase keywords in the selected field will be kept.")
        else:
            self.status_var.set("Keywords field auto-detected. Confirm it, then preview the cleanup.")

    def on_clean(self):
        if self.df is None:
            return
        title_column = core.guess_column(self.df.columns, core.TITLE_ALIASES)
        column = self.tag_column.get()
        if column == NO_COLUMN:
            messagebox.showwarning("Missing Keywords field", "Choose the field containing keywords.")
            return
        output = self.df.copy()
        preview_rows, kept_total, removed_total, changed_records = [], 0, 0, 0
        headings = ["Record", "Original keywords", "Kept uppercase keywords", "Removed keywords"]
        for column_id, heading in zip(self.table.tree["columns"], headings):
            self.table.tree.heading(column_id, text=heading)
        for index, value in output[column].items():
            original = "" if pd.isna(value) else str(value).strip()
            cleaned, kept, removed = core.clean_uppercase_tags(original, self.delimiter.get())
            output.at[index, column] = cleaned
            kept_total += len(kept)
            removed_total += len(removed)
            changed_records += bool(removed)
            record = str(output.at[index, title_column]) if title_column else f"Row {index + 1}"
            preview_rows.append((record, original, "; ".join(kept), "; ".join(removed)))
        self.output_df = output
        self.table.set_rows(preview_rows)
        self.export_btn.configure(state="normal")
        self.status_var.set(
            f"Preview ready: {kept_total} uppercase keywords kept; {removed_total} keywords removed "
            f"from {changed_records} of {len(output)} records.")

    def on_export(self):
        if self.output_df is None:
            return
        source_ext = os.path.splitext(self.file_path)[1].lower()
        source_formats = {
            ".csv": ("csv", ".csv"),
            ".xlsx": ("excel", ".xlsx"), ".xls": ("excel", ".xlsx"),
            ".ris": ("ris", ".ris"), ".bib": ("bibtex", ".bib"),
            ".bibtex": ("bibtex", ".bibtex"),
        }
        fmt, ext = source_formats.get(source_ext, ("csv", ".csv"))
        base = os.path.splitext(os.path.basename(self.file_path))[0]
        path = filedialog.asksaveasfilename(
            title="Export cleaned keyword file", defaultextension=ext,
            filetypes=[
                ("Same format as source", f"*{ext}"), ("CSV", "*.csv"),
                ("Excel", "*.xlsx"), ("RIS", "*.ris"),
                ("BibTeX / BibLaTeX", "*.bib *.bibtex")],
            initialfile=f"{base}_keywords_cleaned{ext}")
        if not path:
            return
        selected_ext = os.path.splitext(path)[1].lower()
        fmt = source_formats.get(selected_ext, (fmt, selected_ext or ext))[0]
        try:
            core.write_records_file(self.output_df, path, fmt)
        except Exception as exc:
            messagebox.showerror("Save failed", f"Couldn't save the file:\n{exc}")
            return
        messagebox.showinfo("Saved", f"Cleaned copy saved to:\n{path}")


# ---------------------------------------------------------------------------
# Manual review: inspect an existing result file without running APIs
# ---------------------------------------------------------------------------

class ManualReviewPage(ctk.CTkFrame):
    """A compact, configurable review view over an already-generated file."""
    PAGE_SIZE = 100
    DEFAULT_ALIASES = [
        ("Title", core.TITLE_ALIASES), ("Year", core.YEAR_ALIASES),
        ("Author", core.AUTHOR_ALIASES), ("DOI", core.DOI_ALIASES),
        ("URL", core.URL_ALIASES),
    ]

    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.df = None
        self.file_path = None
        self.page = 0
        self.page_indices = []
        self.filtered_indices = []
        self.selected_df_index = None
        self.column_vars = {}
        self.custom_entries = {}
        self.filter_conditions = []
        self.review_window = None
        self.lookup_status_column = None

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(6, 8))
        ctk.CTkButton(top, text="Choose existing file…", width=155,
                      command=self.on_choose_file).pack(side="left")
        self.file_label = ctk.CTkLabel(top, text="No file loaded — this page does not call any API", anchor="w")
        self.file_label.pack(side="left", padx=12, fill="x", expand=True)
        self.save_btn = ctk.CTkButton(top, text="Save reviewed copy…", width=160,
                                      command=self.on_save, state="disabled")
        self.save_btn.pack(side="right")
        self.save_filtered_btn = ctk.CTkButton(
            top, text="Save filtered results…", width=165,
            command=self.on_save_filtered, state="disabled")
        self.save_filtered_btn.pack(side="right", padx=(0, 8))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=4)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(body, width=225)
        sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        sidebar.grid_propagate(False)
        ctk.CTkLabel(sidebar, text="Columns to display", font=ctk.CTkFont(weight="bold")) \
            .pack(anchor="w", padx=10, pady=(10, 4))
        self.column_box = ctk.CTkScrollableFrame(sidebar, height=270)
        self.column_box.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        ctk.CTkButton(sidebar, text="Add manual field…", command=self.add_manual_field) \
            .pack(fill="x", padx=10, pady=(0, 10))

        main = ctk.CTkFrame(body, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        filters = ctk.CTkFrame(main)
        filters.pack(fill="x", pady=(0, 7))
        ctk.CTkLabel(filters, text="Filter records", font=ctk.CTkFont(weight="bold")) \
            .grid(row=0, column=0, sticky="w", padx=10, pady=(7, 2))
        self.filter_column = _make_searchable_combobox(
            filters, values=[NO_COLUMN], command=self._on_filter_column_change, width=210)
        self.filter_column.grid(row=1, column=0, padx=(10, 5), pady=(0, 7))
        self.filter_operator = ctk.CTkOptionMenu(filters, values=["equals"], width=125)
        self.filter_operator.grid(row=1, column=1, padx=5, pady=(0, 7))
        self.filter_value = _make_searchable_combobox(
            filters, values=[""], width=260, dropdown_rows=9)
        self.filter_value.grid(row=1, column=2, padx=5, pady=(0, 7), sticky="w")
        self.filter_value_to = ctk.CTkEntry(filters, width=105, placeholder_text="Upper value")
        self.filter_value_to.grid(row=1, column=3, padx=5, pady=(0, 7))
        ctk.CTkButton(filters, text="Add another", width=110, command=self.add_filter_condition) \
            .grid(row=1, column=4, padx=5, pady=(0, 7))
        ctk.CTkButton(filters, text="Apply", width=70, command=self.apply_filters) \
            .grid(row=1, column=5, padx=5, pady=(0, 7))
        ctk.CTkButton(filters, text="Clear", width=65, command=self.clear_filters) \
            .grid(row=1, column=6, padx=(5, 10), pady=(0, 7))
        self.filter_summary_box = ctk.CTkTextbox(
            filters, height=52, wrap="word", activate_scrollbars=True,
            font=ctk.CTkFont(size=12), text_color=("gray30", "gray70"))
        self.filter_summary_box.grid(row=2, column=0, columnspan=8, sticky="ew", padx=10, pady=(0, 7))
        _set_readonly_text(
            self.filter_summary_box,
            "Choose a condition and click Apply. Multiple conditions use AND.")
        filters.grid_columnconfigure(7, weight=1)
        nav = ctk.CTkFrame(main, fg_color="transparent")
        nav.pack(fill="x", pady=(0, 5))
        self.prev_btn = ctk.CTkButton(nav, text="Previous", width=85, command=lambda: self.change_page(-1), state="disabled")
        self.prev_btn.pack(side="left")
        self.next_btn = ctk.CTkButton(nav, text="Next", width=70, command=lambda: self.change_page(1), state="disabled")
        self.next_btn.pack(side="left", padx=6)
        self.page_label = ctk.CTkLabel(nav, text="Choose a file to begin")
        self.page_label.pack(side="left", padx=8)
        self.review_btn = ctk.CTkButton(
            nav, text="Open review queue…", width=145,
            command=self.open_review_window, state="disabled")
        self.review_btn.pack(side="right")

        self.table_holder = ctk.CTkFrame(main, fg_color="transparent")
        self.table_holder.pack(fill="both", expand=True)
        self.table = None

        message_frame = ctk.CTkFrame(main)
        message_frame.pack(fill="x", pady=(7, 0))
        message_header = ctk.CTkFrame(message_frame, fg_color="transparent")
        message_header.pack(fill="x", padx=8, pady=(6, 2))
        ctk.CTkLabel(message_header, text="Selected verification message",
                     font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(
            message_header, text="Copy full message", width=135,
            command=lambda: _copy_textbox(self, self.message_view)).pack(side="right")
        self.message_view = ctk.CTkTextbox(
            message_frame, height=90, wrap="word", font=ctk.CTkFont(size=14))
        self.message_view.pack(fill="x", padx=8, pady=(0, 8))
        _set_readonly_text_autosize(self.message_view, "Select a record to see its full verification message.")

        editor = ctk.CTkFrame(main)
        editor.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(editor, text="Manual decision", font=ctk.CTkFont(weight="bold")) \
            .grid(row=0, column=0, sticky="w", padx=10, pady=(8, 2))
        self.decision = ctk.CTkOptionMenu(
            editor, values=["", "Approved", "Rejected", "Needs review", "Unverifiable"], width=155)
        self.decision.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 9))
        ctk.CTkLabel(editor, text="Notes").grid(row=0, column=1, sticky="w", padx=8, pady=(8, 2))
        self.notes = ctk.CTkEntry(editor, placeholder_text="Your notes for the selected paper")
        self.notes.grid(row=1, column=1, sticky="ew", padx=8, pady=(0, 9))
        self.apply_btn = ctk.CTkButton(editor, text="Apply to selected row", width=145,
                                       command=self.apply_review, state="disabled")
        self.apply_btn.grid(row=1, column=2, padx=10, pady=(0, 9))
        editor.grid_columnconfigure(1, weight=1)
        self.custom_editor = ctk.CTkFrame(editor, fg_color="transparent")
        self.custom_editor.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 5))

    def on_choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose an existing lookup or verification result",
            filetypes=[("Supported files", "*.csv *.xlsx *.xls *.json *.ris *.bib *.bibtex"),
                       ("CSV", "*.csv"), ("Excel", "*.xlsx *.xls"), ("All files", "*.*")])
        if not path:
            return
        try:
            df = core.read_records_file(path).reset_index(drop=True)
        except Exception as exc:
            messagebox.showerror("Couldn't read file", f"Failed to read this file:\n{exc}")
            return
        if df.empty:
            messagebox.showwarning("Empty file", "No records were found in this file.")
            return
        self.original_columns = set(df.columns)
        self.lookup_status_column = next(
            (column for column in df.columns
             if str(column).strip().casefold() in {"status", "state"}), None)
        for name in ("Manual Decision", "Manual Notes"):
            if name not in df.columns:
                df[name] = ""
        self.df, self.file_path, self.page, self.selected_df_index = df, path, 0, None
        self.filtered_indices = list(df.index)
        self.filter_conditions = []
        encoding = df.attrs.get("source_encoding")
        encoding_note = f" · {encoding}" if encoding else ""
        self.file_label.configure(text=f"{os.path.basename(path)} ({len(df)} records){encoding_note}")
        self.save_btn.configure(state="normal")
        self.save_filtered_btn.configure(state="normal")
        self.review_btn.configure(state="normal")
        self._build_column_choices()
        _update_combobox_values(self.filter_column, list(df.columns))
        self.filter_column.set(str(df.columns[0]))
        self._on_filter_column_change(str(df.columns[0]))
        _set_readonly_text(
            self.filter_summary_box,
            "Choose a condition and click Apply. Multiple conditions use AND.")
        _set_readonly_text_autosize(self.message_view, "Select a record to see its full verification message.")
        self.render_page()

    def _is_numeric_column(self, column):
        values = self.df[column].dropna().astype(str).str.strip()
        values = values[values.ne("")]
        return bool(not values.empty and pd.to_numeric(values, errors="coerce").notna().mean() >= 0.8)

    def _on_filter_column_change(self, column):
        if self.df is None or column not in self.df.columns:
            return
        if self._is_numeric_column(column):
            self.filter_operator.configure(values=[">", ">=", "<", "<=", "=", "!=", "between"])
            self.filter_operator.set(">=")
            _update_combobox_values(self.filter_value, [])
            self.filter_value.set("")
        else:
            self.filter_operator.configure(
                values=["equals", "not equals", "contains", "does not contain", "is blank", "is not blank"])
            self.filter_operator.set("equals")
            values = sorted({self._display_value(v).strip() for v in self.df[column]
                             if self._display_value(v).strip()}, key=str.casefold)
            _update_combobox_values(self.filter_value, values[:500] or [""])
            self.filter_value.set(values[0] if values and len(values) <= 100 else "")

    def _current_filter_condition(self, show_warnings=True):
        if self.df is None:
            if show_warnings:
                messagebox.showinfo("Choose a file", "Load a file before creating filters.")
            return None
        column, operator = self.filter_column.get(), self.filter_operator.get()
        value, upper = self.filter_value.get().strip(), self.filter_value_to.get().strip()
        if operator not in {"is blank", "is not blank"} and not value:
            if show_warnings:
                messagebox.showwarning("Missing filter value", "Enter or select a filter value.")
            return None
        if operator == "between" and not upper:
            if show_warnings:
                messagebox.showwarning("Missing upper value", "Enter both values for a between filter.")
            return None
        return column, operator, value, upper

    def add_filter_condition(self):
        condition = self._current_filter_condition()
        if condition is None:
            return
        if condition not in self.filter_conditions:
            self.filter_conditions.append(condition)
        self._update_filter_summary(False)

    def _update_filter_summary(self, applied=True):
        if not self.filter_conditions:
            _set_readonly_text(self.filter_summary_box, "No filters applied. Multiple conditions use AND.")
            return
        parts = []
        for column, operator, value, upper in self.filter_conditions:
            expression = f"{column} {operator} {value}".strip()
            parts.append(expression + (f" and {upper}" if operator == "between" else ""))
        prefix = f"Showing {len(self.filtered_indices):,}/{len(self.df):,} records | " if applied else "Pending | "
        _set_readonly_text(self.filter_summary_box, prefix + " AND ".join(parts))

    def apply_filters(self):
        if self.df is None:
            return
        # Apply the condition currently visible in the editor. "Add another"
        # is only needed when building an AND filter with multiple conditions.
        condition = self._current_filter_condition()
        if condition is None:
            return
        if condition not in self.filter_conditions:
            self.filter_conditions.append(condition)
        self._store_selected()
        mask = pd.Series(True, index=self.df.index)
        try:
            for column, operator, value, upper in self.filter_conditions:
                series = self.df[column]
                if self._is_numeric_column(column):
                    numeric, target = pd.to_numeric(series, errors="coerce"), float(value)
                    operations = {">": numeric > target, ">=": numeric >= target, "<": numeric < target,
                                  "<=": numeric <= target, "=": numeric == target, "!=": numeric != target}
                    condition = numeric.between(target, float(upper), inclusive="both") \
                        if operator == "between" else operations[operator]
                    condition &= numeric.notna()
                else:
                    text = series.fillna("").astype(str).str.strip()
                    folded, target = text.str.casefold(), value.casefold()
                    operations = {"equals": folded == target, "not equals": folded != target,
                                  "contains": folded.str.contains(re.escape(target), na=False),
                                  "does not contain": ~folded.str.contains(re.escape(target), na=False),
                                  "is blank": text.eq(""), "is not blank": text.ne("")}
                    condition = operations[operator]
                mask &= condition.fillna(False)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Invalid filter", f"The filter could not be applied:\n{exc}")
            return
        self.filtered_indices = self.df.index[mask].tolist()
        self.page, self.selected_df_index = 0, None
        self.apply_btn.configure(state="disabled")
        self._update_filter_summary(True)
        self.render_page()

    def clear_filters(self):
        if self.df is None:
            return
        self._store_selected()
        self.filter_conditions = []
        self.filtered_indices = list(self.df.index)
        self.page, self.selected_df_index = 0, None
        self.apply_btn.configure(state="disabled")
        _set_readonly_text(
            self.filter_summary_box,
            "Choose a condition and click Apply. Multiple conditions use AND.")
        self.render_page()

    def _build_column_choices(self):
        for child in self.column_box.winfo_children():
            child.destroy()
        self.column_vars = {}
        defaults = set()
        columns = list(self.df.columns)
        for _, aliases in self.DEFAULT_ALIASES:
            guessed = core.guess_column(columns, aliases)
            if guessed:
                defaults.add(guessed)
        for column in columns:
            var = ctk.BooleanVar(value=column in defaults)
            self.column_vars[column] = var
            ctk.CTkCheckBox(self.column_box, text=str(column), variable=var,
                            command=self.render_page).pack(anchor="w", padx=4, pady=3)

    @staticmethod
    def _display_value(value):
        if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
            return ""
        return str(value)

    @staticmethod
    def _doi_link(value):
        value = str(value).strip()
        if value.lower().startswith(("http://", "https://")):
            return value
        return "https://doi.org/" + value.removeprefix("doi:").strip()

    def selected_columns(self):
        return [column for column, var in self.column_vars.items() if var.get()]

    def render_page(self):
        if self.df is None:
            return
        columns = self.selected_columns()
        if not columns:
            self.page_label.configure(text="Select at least one display column")
            return
        if self.table is not None:
            self.table.destroy()
        active_indices = self.filtered_indices if self.filtered_indices or self.filter_conditions else list(self.df.index)
        start = self.page * self.PAGE_SIZE
        end = min(len(active_indices), start + self.PAGE_SIZE)
        self.page_indices = active_indices[start:end]
        link_columns, resolvers = set(), {}
        for i, column in enumerate(columns):
            normalized = str(column).strip().lower()
            if normalized in {a.lower() for a in core.DOI_ALIASES}:
                link_columns.add(i); resolvers[i] = self._doi_link
            elif normalized in {a.lower() for a in core.URL_ALIASES} or "url" in normalized:
                link_columns.add(i)
        self.table = ResultsTable(
            self.table_holder, headers=columns, weights=[1] * len(columns),
            on_select=self.select_row, on_activate=lambda _index: self.open_review_window(),
            link_columns=link_columns, link_resolvers=resolvers)
        self.table.pack(fill="both", expand=True)
        copy_rows = [[self._display_value(self.df.at[idx, col]) for col in columns]
                     for idx in self.page_indices]
        rows = [[value[:180] for value in row] for row in copy_rows]
        self.table.set_rows(rows, copy_values=copy_rows)
        total = len(active_indices)
        pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        first = start + 1 if total else 0
        self.page_label.configure(text=f"Rows {start + 1}–{end} of {len(self.df)} · Page {self.page + 1}/{pages}")
        self.page_label.configure(text=f"Rows {first}-{end} of {total} | Page {self.page + 1}/{pages}")
        self.prev_btn.configure(state="normal" if self.page > 0 else "disabled")
        self.next_btn.configure(state="normal" if end < total else "disabled")

    def select_row(self, local_index):
        if local_index >= len(self.page_indices):
            return
        self._store_selected()
        self.selected_df_index = self.page_indices[local_index]
        decision = self._display_value(self.df.at[self.selected_df_index, "Manual Decision"])
        self.decision.set(decision if decision in self.decision.cget("values") else "")
        self.notes.delete(0, "end")
        self.notes.insert(0, self._display_value(self.df.at[self.selected_df_index, "Manual Notes"]))
        row = self.df.loc[self.selected_df_index]
        detail_parts = []
        for label, column in (
                ("Status", "Verification Status"),
                ("Score", "Verification Score"),
                ("Decision rule", "Verification Decision Rule"),
                ("Conflicts", "Metadata Conflicts"),
                ("Warnings", "Metadata Warnings")):
            if column in self.df.columns:
                value = self._display_value(row.get(column, "")).strip()
                if value:
                    detail_parts.append(f"{label}: {value}")
        message = self._display_value(row.get("Verification Message", "")).strip()
        detail_parts.append(f"Message:\n{message or '(No verification message in this file.)'}")
        _set_readonly_text_autosize(self.message_view, "\n".join(detail_parts))
        self._render_custom_editor()
        self.apply_btn.configure(state="normal")

    def _store_selected(self):
        if self.selected_df_index is None:
            return
        self.df.at[self.selected_df_index, "Manual Decision"] = self.decision.get()
        self.df.at[self.selected_df_index, "Manual Notes"] = self.notes.get().strip()
        for column, entry in self.custom_entries.items():
            self.df.at[self.selected_df_index, column] = entry.get().strip()

    def apply_review(self):
        if self.selected_df_index is None:
            return
        saved_index = self.selected_df_index
        self._store_selected()
        # Refresh displayed values immediately while keeping the reviewer on
        # the same record and scroll position.
        self.render_page()
        if saved_index in self.page_indices:
            local_index = self.page_indices.index(saved_index)
            row_id = str(local_index)
            self.table.selected_index = local_index
            self.table.tree.selection_set(row_id)
            self.table.tree.focus(row_id)
            self.table.tree.see(row_id)
            self.selected_df_index = saved_index
        self.page_label.configure(
            text=self.page_label.cget("text") + " | Review saved in memory and table refreshed")

    def _render_custom_editor(self):
        for child in self.custom_editor.winfo_children():
            child.destroy()
        self.custom_entries = {}
        reserved = {"Manual Decision", "Manual Notes"}
        original_columns = getattr(self, "original_columns", set())
        manual_columns = [c for c in self.df.columns if c not in original_columns and c not in reserved]
        for col, column in enumerate(manual_columns):
            ctk.CTkLabel(self.custom_editor, text=column).grid(row=0, column=col, sticky="w", padx=4)
            entry = ctk.CTkEntry(self.custom_editor, width=170)
            entry.grid(row=1, column=col, sticky="ew", padx=4, pady=(0, 4))
            entry.insert(0, self._display_value(self.df.at[self.selected_df_index, column]))
            self.custom_editor.grid_columnconfigure(col, weight=1)
            self.custom_entries[column] = entry

    def add_manual_field(self):
        if self.df is None:
            messagebox.showinfo("Choose a file", "Load a file before adding a manual field.")
            return
        name = simpledialog.askstring("Add manual field", "New column name:", parent=self)
        name = str(name or "").strip()
        if not name:
            return
        if name not in self.df.columns:
            self.df[name] = ""
        self._build_column_choices()
        self.column_vars[name].set(True)
        self.render_page()
        if self.selected_df_index is not None:
            self._render_custom_editor()

    def change_page(self, delta):
        self._store_selected()
        self.page += delta
        self.selected_df_index = None
        self.apply_btn.configure(state="disabled")
        self.render_page()

    def open_review_window(self):
        if self.df is None:
            return
        self._store_selected()
        queue_indices = (self.filtered_indices if self.filter_conditions
                         else list(self.df.index))
        if not queue_indices:
            messagebox.showinfo("Empty review queue", "The current filters contain no records to review.")
            return
        start_index = self.selected_df_index
        if start_index not in queue_indices:
            unreviewed = [idx for idx in queue_indices
                          if not self._display_value(self.df.at[idx, "Manual Decision"]).strip()]
            start_index = unreviewed[0] if unreviewed else queue_indices[0]
        if self.review_window is not None and self.review_window.winfo_exists():
            self.review_window.set_queue(queue_indices, start_index)
            self.review_window.focus()
            self.review_window.lift()
            return
        self.review_window = ManualReviewDialog(self, queue_indices, start_index)

    def on_save(self):
        if self.df is None:
            return
        self._store_selected()
        self._save_dataframe(
            self.df, "Save manually reviewed records", "manual_review",
            "Reviewed copy")

    def on_save_filtered(self):
        """Save only the records remaining after the current filters are applied."""
        if self.df is None:
            return
        self._store_selected()
        active_indices = (self.filtered_indices if self.filter_conditions
                          else list(self.df.index))
        if not active_indices:
            messagebox.showwarning(
                "No filtered records",
                "The current filters return zero records, so there is nothing to save.")
            return
        filtered_df = self.df.loc[active_indices].copy().reset_index(drop=True)
        self._save_dataframe(
            filtered_df, "Save filtered review results", "filtered_review",
            f"Filtered copy ({len(filtered_df)} records)")

    def _save_dataframe(self, dataframe, dialog_title, filename_suffix, saved_label):
        base = os.path.splitext(os.path.basename(self.file_path or "records"))[0]
        path = filedialog.asksaveasfilename(
            title=dialog_title, defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Excel", "*.xlsx")],
            initialfile=f"{base}_{filename_suffix}.csv")
        if not path:
            return
        try:
            if path.lower().endswith(".xlsx"):
                dataframe.to_excel(path, index=False)
            else:
                dataframe.to_csv(path, index=False, encoding="utf-8-sig")
        except Exception as exc:
            messagebox.showerror("Save failed", f"Couldn't save the file:\n{exc}")
            return
        messagebox.showinfo("Saved", f"{saved_label} saved to:\n{path}")


class ManualReviewDialog(ctk.CTkToplevel):
    """Focused record-by-record comparison over the Manual Review queue."""

    REVIEW_STATE_PROMPT = "Select a review state…"
    REVIEW_STATES = {
        "Accepted": ("accepted", "Approved"),
        "Not accepted": ("not_accepted", "Rejected"),
    }

    def __init__(self, review_page, queue_indices, start_index):
        super().__init__(review_page)
        self.review_page = review_page
        self.queue_indices = list(queue_indices)
        self.position = 0
        self.title("Manual Verification Review")
        self.geometry("1260x820")
        self.minsize(940, 650)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.topmost_var = ctk.BooleanVar(value=False)

        header = ctk.CTkFrame(self)
        header.pack(fill="x", padx=10, pady=(10, 6))
        self.record_label = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=16, weight="bold"))
        self.record_label.pack(side="left", padx=10, pady=8)
        self.topmost_switch = ctk.CTkSwitch(
            header, text="Keep on top", variable=self.topmost_var,
            command=self._toggle_topmost)
        self.topmost_switch.pack(side="right", padx=10)
        self.next_btn = ctk.CTkButton(header, text="Next", width=75, command=lambda: self._move(1))
        self.next_btn.pack(side="right", padx=4)
        self.previous_btn = ctk.CTkButton(header, text="Previous", width=85, command=lambda: self._move(-1))
        self.previous_btn.pack(side="right", padx=4)

        self.status_label = ctk.CTkLabel(
            self, text="", anchor="w", justify="left", wraplength=1190,
            font=ctk.CTkFont(size=14, weight="bold"))
        self.status_label.pack(fill="x", padx=16, pady=(0, 5))

        headings = ctk.CTkFrame(self, fg_color="transparent")
        headings.pack(fill="x", padx=16)
        headings.grid_columnconfigure(1, weight=1)
        headings.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(headings, text="Field", width=145, anchor="w",
                     font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(headings, text="Input record", anchor="w",
                     font=ctk.CTkFont(weight="bold")).grid(row=0, column=1, sticky="w", padx=5)
        ctk.CTkLabel(headings, text="Retrieved verification metadata", anchor="w",
                     font=ctk.CTkFont(weight="bold")).grid(row=0, column=3, sticky="w", padx=5)

        self.comparison_frame = ctk.CTkScrollableFrame(self, height=300)
        self.comparison_frame.grid_columnconfigure(1, weight=1)
        self.comparison_frame.grid_columnconfigure(3, weight=1)

        message_frame = ctk.CTkFrame(self)
        ctk.CTkLabel(message_frame, text="Verification message",
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(7, 2))
        self.message_box = ctk.CTkTextbox(message_frame, height=70, wrap="word", font=ctk.CTkFont(size=13))
        self.message_box.pack(fill="x", padx=10, pady=(0, 8))

        editor = ctk.CTkFrame(self)
        ctk.CTkLabel(editor, text="Review state", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(7, 2))
        ctk.CTkLabel(editor, text="Notes", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=1, sticky="w", padx=8, pady=(7, 2))
        status_column = self.review_page.lookup_status_column
        self.review_state = ctk.CTkOptionMenu(
            editor, values=[self.REVIEW_STATE_PROMPT, *self.REVIEW_STATES], width=190)
        self.review_state.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 9))
        if status_column is None:
            self.review_state.configure(state="disabled")
        self.notes = ctk.CTkEntry(editor, placeholder_text="Manual review notes")
        self.notes.grid(row=1, column=1, sticky="ew", padx=8, pady=(0, 9))
        ctk.CTkButton(editor, text="Save", width=80, command=self._save).grid(
            row=1, column=2, padx=4, pady=(0, 9))
        ctk.CTkButton(editor, text="Save & Next", width=105, command=self._save_and_next).grid(
            row=1, column=3, padx=4, pady=(0, 9))
        ctk.CTkButton(editor, text="Close", width=75, command=self._close).grid(
            row=1, column=4, padx=(4, 10), pady=(0, 9))
        editor.grid_columnconfigure(1, weight=1)

        # Reserve the bottom controls before allocating the remaining height
        # to the scrollable comparison. Otherwise high DPI or long messages
        # can push the state selector and Save buttons off-screen.
        editor.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        message_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 6))
        self.comparison_frame.pack(side="top", fill="both", expand=True,
                                   padx=10, pady=(3, 6))

        self.set_queue(queue_indices, start_index)
        self.after(80, self.lift)

    def set_queue(self, queue_indices, start_index=None):
        self.queue_indices = list(queue_indices)
        if start_index in self.queue_indices:
            self.position = self.queue_indices.index(start_index)
        else:
            self.position = 0
        self._load_record()

    def _current_index(self):
        return self.queue_indices[self.position]

    def _column_value(self, row, aliases, exact_names=()):
        for name in exact_names:
            if name in row.index:
                value = self.review_page._display_value(row.get(name, "")).strip()
                if value:
                    return value
        column = core.guess_column(row.index, aliases)
        return self.review_page._display_value(row.get(column, "")).strip() if column else ""

    @staticmethod
    def _normal(value):
        return re.sub(r"[^\w]+", " ", core.lib.normalize_for_compare(str(value or ""))).strip()

    def _compare(self, field, input_value, retrieved_value):
        if not input_value or not retrieved_value:
            return "missing", "Not available on both sides"
        if field == "DOI":
            left, right = core.normalize_doi(input_value), core.normalize_doi(retrieved_value)
            return ("match", "Same DOI") if left and left.casefold() == right.casefold() else ("different", "Different DOI")
        if field == "Publication year":
            left = re.search(r"\b(?:18|19|20|21)\d{2}\b", input_value)
            right = re.search(r"\b(?:18|19|20|21)\d{2}\b", retrieved_value)
            if left and right:
                difference = abs(int(left.group()) - int(right.group()))
                return ("match", "Same year") if difference == 0 else (
                    ("warning", f"Year differs by {difference}") if difference <= 1 else
                    ("different", f"Year differs by {difference}"))
        if field == "Authors":
            left = {self._normal(v) for v in core.lib.clean_authors(input_value) if self._normal(v)}
            right = {self._normal(v) for v in core.lib.clean_authors(retrieved_value) if self._normal(v)}
            if left and right and left & right:
                return "match", "Author surname overlap"
            return "different", "Authors differ"
        left, right = self._normal(input_value), self._normal(retrieved_value)
        if left == right:
            return "match", "Same after normalization"
        score = float(core.lib.fuzz.token_sort_ratio(left, right)) if left and right else 0.0
        if score >= 90:
            return "match", f"Very close ({score:.0f})"
        if score >= 75:
            return "warning", f"Review recommended ({score:.0f})"
        return "different", f"Different ({score:.0f})"

    @staticmethod
    def _row_color(category):
        return {
            "match": ("#dff3e4", "#173d23"),
            "warning": ("#fff1c7", "#4a3912"),
            "different": ("#f8d7da", "#4b1f23"),
            "missing": ("#e8eaed", "#303236"),
        }[category]

    def _copy_value(self, value):
        if not value:
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()

    def _clickable_url(self, field, value):
        value = str(value or "").strip()
        if not value:
            return ""
        if field == "DOI":
            return self.review_page._doi_link(value)
        if value.casefold().startswith(("http://", "https://")):
            return value
        return "https://" + value

    def _add_comparison_row(self, row_number, field, input_value, retrieved_value):
        category, explanation = self._compare(field, input_value, retrieved_value)
        color = self._row_color(category)
        ctk.CTkLabel(self.comparison_frame, text=field, width=140, anchor="w",
                     font=ctk.CTkFont(weight="bold")).grid(row=row_number, column=0, sticky="nw", padx=6, pady=4)
        for column, value in ((1, input_value), (3, retrieved_value)):
            holder = ctk.CTkFrame(self.comparison_frame, fg_color=color)
            holder.grid(row=row_number, column=column, sticky="nsew", padx=4, pady=3)
            holder.grid_columnconfigure(0, weight=1)
            value_label = ctk.CTkLabel(holder, text=value or "(not available)", anchor="w", justify="left",
                                       wraplength=390)
            value_label.grid(row=0, column=0, sticky="ew", padx=7, pady=6)
            direct_url = self._clickable_url(field, value) if field in {"DOI", "URL"} else ""
            if direct_url:
                value_label.configure(text_color=("#1261a0", "#69b7ff"), cursor="hand2")
                value_label.bind("<Button-1>", lambda _event, url=direct_url: webbrowser.open(url))
            ctk.CTkButton(holder, text="Copy", width=48, height=24,
                          command=lambda item=value: self._copy_value(item)).grid(
                              row=0, column=1, padx=(2, 5), pady=4)
        symbol = {"match": "✓", "warning": "!", "different": "×", "missing": "—"}[category]
        ctk.CTkLabel(self.comparison_frame, text=f"{symbol}\n{explanation}", width=105,
                     justify="center", text_color=("gray20", "gray80")).grid(
                         row=row_number, column=2, sticky="nsew", padx=3, pady=4)

    def _load_record(self):
        if not self.queue_indices:
            return
        row = self.review_page.df.loc[self._current_index()]
        for child in self.comparison_frame.winfo_children():
            child.destroy()
        input_doi = self._column_value(row, core.DOI_ALIASES)
        input_url = self._column_value(row, core.URL_ALIASES)
        resolved_url = self._column_value(row, [], ("Resolved URL",))
        retrieved_doi = self._column_value(row, [], ("Verification Metadata DOI",))
        if not retrieved_doi and resolved_url and "doi.org/" in resolved_url.casefold():
            retrieved_doi = core.normalize_doi(resolved_url)
        fields = [
            ("Title", self._column_value(row, core.TITLE_ALIASES),
             self._column_value(row, [], ("Verification Metadata Combined Title", "Verification Metadata Title"))),
            ("Authors", self._column_value(row, core.AUTHOR_ALIASES),
             self._column_value(row, [], ("Verification Metadata Authors",))),
            ("Publication year", self._column_value(row, core.YEAR_ALIASES),
             self._column_value(row, [], ("Verification Metadata Year",))),
            ("Item type", self._column_value(row, core.ITEM_TYPE_ALIASES),
             self._column_value(row, [], ("Verification Metadata Item Type",))),
            ("Publisher", self._column_value(row, core.PUBLISHER_ALIASES),
             self._column_value(row, [], ("Verification Metadata Publisher",))),
            ("Publication / container", self._column_value(row, core.JOURNAL_ALIASES),
             self._column_value(row, [], ("Verification Metadata Container Title",))),
            ("DOI", input_doi, retrieved_doi),
            ("URL", input_url, resolved_url),
        ]
        for number, values in enumerate(fields):
            self._add_comparison_row(number, *values)

        status = self._column_value(row, [], ("Verification Status",)) or "(no verification status)"
        lookup_status = (self.review_page._display_value(
            row.get(self.review_page.lookup_status_column, "")).strip()
            if self.review_page.lookup_status_column else "")
        score = self._column_value(row, [], ("Verification Score",))
        warnings = self._column_value(row, [], ("Metadata Warnings",))
        conflicts = self._column_value(row, [], ("Metadata Conflicts",))
        summary = f"Lookup status: {lookup_status or '(not available)'}   |   Verification: {STATUS_LABELS.get(status, status)}"
        if score:
            summary += f"   |   Score: {score}"
        if warnings:
            summary += f"   |   Warnings: {warnings}"
        if conflicts:
            summary += f"   |   Conflicts: {conflicts}"
        self.status_label.configure(text=summary)
        _set_readonly_text_autosize(self.message_box, self._column_value(row, [], ("Verification Message",)) or
                           "No verification message is stored in this file.")
        if self.review_page.lookup_status_column:
            state = next(
                (label for label, (status_value, _) in self.REVIEW_STATES.items()
                 if lookup_status.casefold() == status_value), self.REVIEW_STATE_PROMPT)
            self.review_state.set(state)
        self.notes.delete(0, "end")
        self.notes.insert(0, self.review_page._display_value(row.get("Manual Notes", "")))
        self.record_label.configure(
            text=f"Record {self.position + 1} / {len(self.queue_indices)}  ·  source row {self._current_index() + 1}")
        self.previous_btn.configure(state="normal" if self.position > 0 else "disabled")
        self.next_btn.configure(state="normal" if self.position + 1 < len(self.queue_indices) else "disabled")

    def _save(self):
        index = self._current_index()
        state = self.review_state.get()
        if self.review_page.lookup_status_column and state in self.REVIEW_STATES:
            status_value, manual_decision = self.REVIEW_STATES[state]
            self.review_page.df.at[index, self.review_page.lookup_status_column] = status_value
            self.review_page.df.at[index, "Manual Decision"] = manual_decision
        self.review_page.df.at[index, "Manual Notes"] = self.notes.get().strip()
        self._load_record()
        # The main-page editor may still hold the previous values. Clear its
        # selection so a later row change cannot write those stale values back.
        self.review_page.selected_df_index = None
        self.review_page.apply_btn.configure(state="disabled")
        self.review_page.render_page()
        self.review_page.page_label.configure(
            text=self.review_page.page_label.cget("text") + " | Review saved in memory")

    def _save_and_next(self):
        self._save()
        if self.position + 1 < len(self.queue_indices):
            self.position += 1
            self._load_record()

    def _move(self, delta):
        new_position = self.position + delta
        if 0 <= new_position < len(self.queue_indices):
            self.position = new_position
            self._load_record()

    def _toggle_topmost(self):
        self.attributes("-topmost", bool(self.topmost_var.get()))
        self.topmost_switch.configure(
            text="Always on top: ON" if self.topmost_var.get() else "Keep on top")

    def _close(self):
        self.review_page.review_window = None
        self.destroy()


# ---------------------------------------------------------------------------
# Sources: which databases to query, plus optional email/API keys
# ---------------------------------------------------------------------------

class SourcesPage(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")

        settings = ctk.CTkFrame(self)
        settings.pack(fill="x", padx=4, pady=(6, 10))
        ctk.CTkLabel(settings, text="Optional settings", font=ctk.CTkFont(weight="bold")) \
            .pack(anchor="w", padx=10, pady=(10, 2))

        self.email_entry = self._labeled_entry(
            settings, "Contact email", "you@example.com",
            "Optional. Used by Crossref and PubMed to identify you as a courtesy, which gets you a "
            "more generous rate limit. Leave blank if you'd rather not share one.",
        )
        self.s2_key_entry = self._labeled_entry(
            settings, "Semantic Scholar API key", "optional",
            "Optional, free at semanticscholar.org/product/api#api-key-form. Without it, Semantic "
            "Scholar shares a public pool limited to roughly 100 requests/5min; with it, you get a "
            "faster dedicated rate.",
        )
        self.core_key_entry = self._labeled_entry(
            settings, "CORE API key", "required to use CORE below",
            "Only needed if you enable CORE below — CORE has no free/keyless search tier at all, so "
            "without a key it contributes nothing. Free registration at core.ac.uk/services/api.",
            pady_bottom=10,
        )

        ctk.CTkLabel(self, text="Sources to search", font=ctk.CTkFont(weight="bold")) \
            .pack(anchor="w", padx=8, pady=(4, 4))

        sources_scroll = ctk.CTkScrollableFrame(self)
        sources_scroll.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.source_vars = {}
        for src in core.SOURCES:
            row = ctk.CTkFrame(sources_scroll, fg_color="transparent")
            row.pack(fill="x", pady=5)
            var = ctk.BooleanVar(value=src["default_on"])
            self.source_vars[src["id"]] = var
            ctk.CTkCheckBox(
                row, text=src["label"], variable=var, width=230, command=self._save_current_settings,
            ).pack(side="left", anchor="n")
            ctk.CTkLabel(
                row, text=src["note"], text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11),
                anchor="w", justify="left", wraplength=680,
            ).pack(side="left", fill="x", expand=True, padx=(10, 0))

        ctk.CTkLabel(
            self, text="Your source selection and settings are saved automatically and restored next time you open the app.",
            text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11), anchor="w",
        ).pack(fill="x", padx=8, pady=(0, 6))

        self._load_saved_settings()

        # Save on every edit, not just on exit, so settings survive even if
        # the app is closed abruptly (crash, force-quit) rather than
        # relying on a clean shutdown hook.
        for entry in (self.email_entry, self.s2_key_entry, self.core_key_entry):
            entry.bind("<FocusOut>", lambda e: self._save_current_settings())
            entry.bind("<Return>", lambda e: self._save_current_settings())

    def _labeled_entry(self, parent, label, placeholder, note, pady_bottom=2):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(row, text=label, width=220, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row, placeholder_text=placeholder)
        entry.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            parent, text=note, text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11),
            anchor="w", justify="left", wraplength=900,
        ).pack(fill="x", padx=10, pady=(0, pady_bottom))
        return entry

    def _load_saved_settings(self):
        saved = core.load_settings()
        if not saved:
            return
        enabled = saved.get("enabled_sources")
        if isinstance(enabled, list):
            enabled_set = set(enabled)
            for sid, var in self.source_vars.items():
                var.set(sid in enabled_set)
        for entry, key in (
            (self.email_entry, "email"),
            (self.s2_key_entry, "s2_key"),
            (self.core_key_entry, "core_key"),
        ):
            value = saved.get(key)
            if value:
                entry.delete(0, "end")
                entry.insert(0, value)

    def _save_current_settings(self):
        core.save_settings({
            "enabled_sources": self.get_enabled_sources(),
            "email": self.get_email(),
            "s2_key": self.get_s2_key() or "",
            "core_key": self.get_core_key() or "",
        })

    def get_enabled_sources(self):
        return [sid for sid, var in self.source_vars.items() if var.get()]

    def get_email(self):
        return self.email_entry.get().strip()

    def get_s2_key(self):
        return self.s2_key_entry.get().strip() or None

    def get_core_key(self):
        return self.core_key_entry.get().strip() or None


# ---------------------------------------------------------------------------
# Verification method settings
# ---------------------------------------------------------------------------

class VerificationSettingsPage(ctk.CTkFrame):
    """Global controls for deciding which metadata fields affect verification."""
    FIELD_SPECS = [
        ("title", "Title (required)", True,
         "Primary identity field. Main title and title + subtitle are both tested."),
        ("authors", "Authors", True,
         "Compares original and accent-normalised author names; either form may match."),
        ("publisher", "Publisher", True,
         "Checks the publisher when both the input file and source provide it."),
        ("item_type", "Item Type", True,
         "Checks compatible types such as journal article, book, chapter, report, or thesis."),
        ("year", "Publication Year", False,
         "Optional. Accepts any online, print, or issued year returned by the source (±1 year)."),
        ("journal", "Publication Title / Journal", False,
         "Checks the journal, proceedings, or other container title."),
        ("volume", "Volume", False, "Checks volume when relevant to the item type."),
        ("issue", "Issue", False, "Checks issue when relevant to the item type."),
        ("pages", "Pages", False, "Checks the page or article-number field."),
        ("isbn", "ISBN", False, "Checks ISBN for books, chapters, and proceedings."),
        ("issn", "ISSN", False, "Checks ISSN for journal articles."),
    ]

    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.field_vars = {}
        ctk.CTkLabel(self, text="Verification method settings",
                     font=ctk.CTkFont(size=20, weight="bold"), anchor="w").pack(
                         fill="x", padx=18, pady=(18, 6))
        ctk.CTkLabel(
            self,
            text=("Choose which input metadata is compared with source metadata. DOI and URL are always "
                  "used to locate and validate the record; they are not optional scoring fields. Unchecked "
                  "fields are preserved in the exported file but do not affect the score, warnings, or decision."),
            justify="left", anchor="w", wraplength=1000,
            text_color=("gray25", "gray75")).pack(fill="x", padx=18, pady=(0, 14))
        fields = ctk.CTkScrollableFrame(self)
        fields.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        fields.grid_columnconfigure(1, weight=1)
        for row, (key, label, default, description) in enumerate(self.FIELD_SPECS):
            var = ctk.BooleanVar(value=default)
            self.field_vars[key] = var
            checkbox = ctk.CTkCheckBox(fields, text=label, variable=var, width=220)
            checkbox.grid(row=row, column=0, sticky="w", padx=12, pady=9)
            if key == "title":
                checkbox.configure(state="disabled")
            ctk.CTkLabel(fields, text=description, justify="left", anchor="w",
                         wraplength=760, text_color=("gray30", "gray70")).grid(
                             row=row, column=1, sticky="we", padx=12, pady=9)
        ctk.CTkLabel(
            self,
            text=("Default method: Title + Authors + Publisher + Item Type. Publication Year is off by "
                  "default so online-first and later volume-assignment dates cannot reduce a result."),
            justify="left", anchor="w", wraplength=1000,
            font=ctk.CTkFont(weight="bold")).pack(fill="x", padx=18, pady=(0, 18))

    def get_enabled_fields(self):
        return {key for key, var in self.field_vars.items() if var.get()}


# ---------------------------------------------------------------------------
# Single lookup
# ---------------------------------------------------------------------------

class SingleLookupPage(ctk.CTkFrame):
    def __init__(self, master, sources_page: SourcesPage, verification_settings_page):
        super().__init__(master, fg_color="transparent")
        self.sources_page = sources_page
        self.verification_settings_page = verification_settings_page

        self.result_queue = queue.Queue()
        self.current_results = []
        self.selected_result = None

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=4, pady=(6, 4))

        title_row = ctk.CTkFrame(form, fg_color="transparent")
        title_row.pack(fill="x", pady=4)
        ctk.CTkLabel(title_row, text="Title (required)", width=130, anchor="w").pack(side="left", padx=(0, 8))
        self.title_entry = ctk.CTkEntry(title_row, placeholder_text="e.g. Attitudes toward income inequality")
        self.title_entry.pack(side="left", fill="x", expand=True)
        self.title_entry.bind("<Return>", lambda e: self.on_search())

        second_row = ctk.CTkFrame(form, fg_color="transparent")
        second_row.pack(fill="x", pady=4)

        self.search_btn = ctk.CTkButton(second_row, text="Search", width=110, command=self.on_search)
        self.search_btn.pack(side="right")

        ctk.CTkLabel(second_row, text="Author surname", width=130, anchor="w").pack(side="left", padx=(0, 8))
        self.author_entry = ctk.CTkEntry(second_row, width=160, placeholder_text="e.g. Kelley")
        self.author_entry.pack(side="left", padx=(0, 20))
        self.author_entry.bind("<Return>", lambda e: self.on_search())

        ctk.CTkLabel(second_row, text="Year", width=50, anchor="w").pack(side="left", padx=(0, 8))
        self.year_entry = ctk.CTkEntry(second_row, width=90, placeholder_text="e.g. 2001")
        self.year_entry.pack(side="left")
        self.year_entry.bind("<Return>", lambda e: self.on_search())

        self.status_var = ctk.StringVar(value="Enter a title and click “Search”. Pick which sources to use on the Sources tab.")
        ctk.CTkLabel(self, textvariable=self.status_var, text_color=("gray30", "gray70"), anchor="w") \
            .pack(fill="x", padx=4, pady=(0, 8))

        self.table = ResultsTable(
            self, headers=["Score", "Source", "Candidate title", "Year"], weights=[0, 0, 1, 0],
            on_select=self.on_select,
        )
        self.table.pack(fill="both", expand=True, padx=4, pady=(0, 10))

        detail = ctk.CTkFrame(self)
        detail.pack(fill="x", padx=4, pady=(0, 4))
        detail.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(detail, text="DOI").grid(row=0, column=0, sticky="w", padx=10, pady=8)
        self.doi_entry = ctk.CTkEntry(detail)
        self.doi_entry.grid(row=0, column=1, sticky="we", padx=6, pady=8)
        ctk.CTkButton(detail, text="Copy DOI", width=100, command=lambda: self.copy_value(self.doi_entry)).grid(row=0, column=2, padx=6, pady=8)

        ctk.CTkLabel(detail, text="Link").grid(row=1, column=0, sticky="w", padx=10, pady=8)
        self.url_entry = ctk.CTkEntry(detail)
        self.url_entry.grid(row=1, column=1, sticky="we", padx=6, pady=8)
        ctk.CTkButton(detail, text="Copy link", width=100, command=lambda: self.copy_value(self.url_entry)).grid(row=1, column=2, padx=6, pady=8)
        ctk.CTkButton(detail, text="Open in browser", width=130, command=self.open_in_browser).grid(row=1, column=3, padx=(0, 10), pady=8)

        self.verify_btn = ctk.CTkButton(detail, text="Verify result", width=130, command=self.on_verify, state="disabled")
        self.verify_btn.grid(row=0, column=3, padx=(0, 10), pady=8)
        self.verification_var = ctk.StringVar(value="")
        ctk.CTkLabel(detail, textvariable=self.verification_var, anchor="w", justify="left", wraplength=930) \
            .grid(row=2, column=0, columnspan=4, sticky="we", padx=10, pady=(0, 8))

        self.after(150, self._poll_queue)

    def on_search(self):
        title = self.title_entry.get().strip()
        author = self.author_entry.get().strip()
        year = self.year_entry.get().strip()

        if not title:
            messagebox.showwarning("Missing title", "Please enter a title first.")
            return

        enabled_sources = self.sources_page.get_enabled_sources()
        if not enabled_sources:
            messagebox.showwarning("No sources selected", "Please enable at least one source on the Sources tab.")
            return

        self.table.set_rows([])
        self.doi_entry.delete(0, "end")
        self.url_entry.delete(0, "end")
        self.current_results = []
        self.selected_result = None
        self.verify_btn.configure(state="disabled")
        self.verification_var.set("")

        self.search_btn.configure(state="disabled")
        self.status_var.set("Searching…")

        threading.Thread(
            target=self._worker,
            args=(title, author, year, enabled_sources,
                  self.sources_page.get_email(), self.sources_page.get_s2_key(), self.sources_page.get_core_key()),
            daemon=True,
        ).start()

    def _worker(self, title, author, year, enabled_sources, email, s2_key, core_key):
        try:
            results, failed = core.run_search(title, author, year, enabled_sources, email=email, s2_api_key=s2_key, core_api_key=core_key)
            self.result_queue.put(("ok", (results, failed)))
        except Exception as e:
            self.result_queue.put(("error", str(e)))

    def _poll_queue(self):
        try:
            kind, payload = self.result_queue.get_nowait()
        except queue.Empty:
            pass
        else:
            if kind == "verification":
                self.verify_btn.configure(state="normal")
                verdict = "Verified" if payload["verified"] else "Not verified"
                metadata = ""
                if payload.get("metadata_title"):
                    metadata = f" Metadata: {payload['metadata_title']} ({payload.get('metadata_year') or 'year unknown'}), score {payload['score']:.0f}."
                self.verification_var.set(f"{verdict}: {payload['message']}{metadata}")
            elif kind == "verification_error":
                self.verify_btn.configure(state="normal")
                self.verification_var.set(f"Verification failed: {payload}")
            elif kind == "error":
                self.search_btn.configure(state="normal")
                self.status_var.set("Search failed.")
                messagebox.showerror("Something went wrong", f"An error occurred while searching:\n{payload}\n\nPlease check your internet connection and try again.")
            else:
                self.search_btn.configure(state="normal")
                results, failed = payload
                self.current_results = results
                failed_note = ""
                if failed:
                    failed_note = f" Note: {_failed_sources_text(failed)} didn't respond, so results may be incomplete."
                if not results:
                    self.status_var.set(
                        "No matches found. Try a more precise title, drop the author/year filters, "
                        "or enable more sources." + failed_note
                    )
                else:
                    self.status_var.set(
                        f"Found {len(results)} candidates, sorted by match score. "
                        "Click one to see details." + failed_note
                    )
                    rows = [(f"{r['score']:.0f}", r["source"], r["title"], r.get("year") or "") for r in results]
                    self.table.set_rows(rows)
        self.after(150, self._poll_queue)

    def on_select(self, idx):
        r = self.current_results[idx]
        self.selected_result = r
        self.doi_entry.delete(0, "end")
        self.doi_entry.insert(0, r.get("doi") or "(no DOI found)")
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, r.get("url") or "(no link found)")
        self.verify_btn.configure(state="normal")
        self.verification_var.set("")

    def on_verify(self):
        if not self.selected_result:
            return
        self.verify_btn.configure(state="disabled")
        self.verification_var.set("Verifying DOI/link and bibliographic metadata...")
        enabled_fields = self.verification_settings_page.get_enabled_fields()
        values = (self.title_entry.get().strip(),
                  self.author_entry.get().strip() if "authors" in enabled_fields else "",
                  self.year_entry.get().strip() if "year" in enabled_fields else "",
                  self.doi_entry.get(), self.url_entry.get(),
                  self.sources_page.get_enabled_sources(), self.sources_page.get_email(),
                  self.sources_page.get_s2_key(), self.sources_page.get_core_key())
        threading.Thread(target=self._verify_worker, args=values, daemon=True).start()

    def _verify_worker(self, title, author, year, doi, url, enabled_sources, email, s2_key, core_key):
        try:
            result = core.verify_reference(title, author, year, doi, url, enabled_sources=enabled_sources,
                                           email=email, s2_api_key=s2_key, core_api_key=core_key)
            self.result_queue.put(("verification", result))
        except Exception as e:
            self.result_queue.put(("verification_error", str(e)))

    def copy_value(self, entry):
        value = entry.get()
        if not value or value.startswith("("):
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self.status_var.set("Copied to clipboard.")

    def open_in_browser(self):
        url = self.url_entry.get()
        if not url or url.startswith("("):
            messagebox.showinfo("No link", "This result doesn't have a link to open.")
            return
        webbrowser.open(url)


# ---------------------------------------------------------------------------
# Independent batch verification
# ---------------------------------------------------------------------------

class VerificationPage(ctk.CTkFrame):
    """Verify DOI/URL columns from a file, with no lookup prerequisite."""
    def __init__(self, master, sources_page: SourcesPage, verification_settings_page):
        super().__init__(master, fg_color="transparent")
        self.sources_page = sources_page
        self.verification_settings_page = verification_settings_page
        self.df = None
        self.file_path = None
        self.output_df = None
        self.current_verification_results = []
        self.result_queue = queue.Queue()
        self.running = False
        self.total_rows = 0
        self.done_rows = 0
        self.cached_rows = 0
        self.start_time = None
        self.cancel_event = threading.Event()
        self.context_columns = {}
        self.verification_added_columns = []

        file_row = ctk.CTkFrame(self, fg_color="transparent")
        file_row.pack(fill="x", padx=4, pady=(6, 8))
        ctk.CTkButton(file_row, text="Choose file…", width=130, command=self.on_choose_file).pack(side="left")
        self.file_label = ctk.CTkLabel(
            file_row, text="Import CSV / RIS / BibTeX / CSL JSON / Excel", anchor="w")
        self.file_label.pack(side="left", padx=12)

        mapping = ctk.CTkFrame(self)
        mapping.pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(mapping, text="Column mapping", font=ctk.CTkFont(weight="bold")) \
            .grid(row=0, column=0, columnspan=5, sticky="w", padx=10, pady=(8, 4))
        labels = ["Title", "Author", "Year", "DOI", "URL"]
        self.mapping_menus = []
        for col, label in enumerate(labels):
            ctk.CTkLabel(mapping, text=label).grid(row=1, column=col, sticky="w", padx=8)
            menu = ctk.CTkOptionMenu(mapping, values=[NO_COLUMN], width=190)
            menu.set(NO_COLUMN)
            menu.grid(row=2, column=col, sticky="we", padx=8, pady=(0, 9))
            mapping.grid_columnconfigure(col, weight=1)
            self.mapping_menus.append(menu)
        self.title_col, self.author_col, self.year_col, self.doi_col, self.url_col = self.mapping_menus

        action_row = ctk.CTkFrame(self, fg_color="transparent")
        action_row.pack(fill="x", padx=4, pady=(0, 8))
        self.verify_btn = ctk.CTkButton(action_row, text="Verify file", width=140, command=self.on_verify)
        self.verify_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(action_row, text="Stop", width=75, command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(action_row, text="Mode").pack(side="left", padx=(16, 8))
        self.verification_mode = ctk.CTkOptionMenu(
            action_row, values=["Fast", "High confidence"], width=145,
            command=self._on_mode_change)
        self.verification_mode.set("Fast")
        self.verification_mode.pack(side="left")
        ctk.CTkLabel(action_row, text="Export format").pack(side="left", padx=(20, 8))
        self.output_format = ctk.CTkOptionMenu(action_row, values=list(core.OUTPUT_FORMATS.keys()), width=180)
        self.output_format.pack(side="left")
        self.export_btn = ctk.CTkButton(action_row, text="Export verified file…", width=165,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="left", padx=10)

        self.mode_help_var = ctk.StringVar()
        ctk.CTkLabel(
            self, textvariable=self.mode_help_var, anchor="w", justify="left",
            wraplength=1500, text_color=("gray25", "gray75"),
            font=ctk.CTkFont(size=12)).pack(fill="x", padx=8, pady=(0, 7))
        self._on_mode_change("Fast")

        self.progress = ctk.CTkProgressBar(self)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=4, pady=(0, 4))
        self.status_var = ctk.StringVar(value="Choose a file. Verification uses the databases enabled on the Sources tab.")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", text_color=("gray30", "gray70")) \
            .pack(fill="x", padx=4, pady=(0, 2))
        self.time_var = ctk.StringVar(value="Elapsed 0:00 · Estimated remaining: waiting to start")
        ctk.CTkLabel(
            self, textvariable=self.time_var, anchor="w",
            text_color=("gray35", "gray65"), font=ctk.CTkFont(size=12)) \
            .pack(fill="x", padx=4, pady=(0, 6))
        self.table = ResultsTable(
            self, headers=["Title", "Verification", "Score", "Metadata title", "Resolved URL"],
            weights=[1, 0, 0, 1, 1], link_columns={4}, on_select=self.on_result_select)
        self.table.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        detail = ctk.CTkFrame(self)
        detail.pack(fill="x", padx=4, pady=(3, 4))
        detail_header = ctk.CTkFrame(detail, fg_color="transparent")
        detail_header.pack(fill="x", padx=8, pady=(6, 2))
        ctk.CTkLabel(detail_header, text="Selected result details",
                     font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(
            detail_header, text="Copy full message", width=135,
            command=lambda: _copy_textbox(self, self.verification_message_view)).pack(side="right")
        self.verification_message_view = ctk.CTkTextbox(
            detail, height=90, wrap="word", font=ctk.CTkFont(size=14))
        self.verification_message_view.pack(fill="x", padx=8, pady=(0, 8))
        _set_readonly_text_autosize(
            self.verification_message_view,
            "Run verification, then select a record to see its full message and decision details.")
        self.after(150, self._poll_queue)

    def on_result_select(self, index):
        if index < 0 or index >= len(self.current_verification_results):
            return
        result = self.current_verification_results[index]
        parts = [
            f"Status: {result.get('status', '')}",
            f"Score: {result.get('score', '')}",
            f"Decision rule: {result.get('decision_rule', '') or '(not recorded)'}",
        ]
        conflicts = "; ".join(result.get("metadata_conflicts", []))
        warnings = "; ".join(result.get("metadata_warnings", []))
        if conflicts:
            parts.append(f"Conflicts: {conflicts}")
        if warnings:
            parts.append(f"Warnings: {warnings}")
        parts.append(f"Message:\n{result.get('message', '') or '(No message.)'}")
        _set_readonly_text_autosize(self.verification_message_view, "\n".join(parts))

    def _on_mode_change(self, selected_mode):
        if selected_mode == "High confidence":
            text = ("High confidence — verifies the identifier/URL first, then requires an additional "
                    "confirmation from a different enabled source family. It provides stronger evidence, "
                    "but is slower and more likely to encounter API rate limits. Without independent "
                    "confirmation, the result remains Unverifiable rather than Verified.")
        else:
            text = ("Fast — verifies DOI registry or page/PDF metadata first and stops immediately when it "
                    "matches. Enabled Sources are used only as fallback when primary evidence is missing or "
                    "insufficient. It is faster and uses fewer API requests, but does not require independent confirmation.")
        self.mode_help_var.set(text)

    def on_choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose records to verify",
            filetypes=[("Supported files", "*.csv *.xlsx *.xls *.json *.ris *.bib *.bibtex"),
                       ("CSV", "*.csv"), ("RIS", "*.ris"),
                       ("BibTeX / BibLaTeX", "*.bib *.bibtex"), ("CSL JSON", "*.json"),
                       ("Excel", "*.xlsx *.xls"), ("All files", "*.*")])
        if not path:
            return
        try:
            df = core.read_records_file(path)
        except Exception as exc:
            messagebox.showerror("Couldn't read file", f"Failed to read this file:\n{exc}")
            return
        if df.empty:
            messagebox.showwarning("Empty file", "No records were found in this file.")
            return
        self.df, self.file_path = df, path
        self.output_format.set(core.preferred_output_format_label(path))
        encoding = df.attrs.get("source_encoding")
        encoding_note = f" · {encoding}" if encoding else ""
        self.file_label.configure(text=f"{os.path.basename(path)} ({len(df)} records){encoding_note}")
        columns = list(df.columns)
        aliases = [core.TITLE_ALIASES, core.AUTHOR_ALIASES, core.YEAR_ALIASES,
                   core.DOI_ALIASES, core.URL_ALIASES]
        for menu, names in zip(self.mapping_menus, aliases):
            guess = core.guess_column(columns, names)
            menu.configure(values=[NO_COLUMN] + columns)
            menu.set(guess or NO_COLUMN)
        context_aliases = {
            "publisher": core.PUBLISHER_ALIASES, "item_type": core.ITEM_TYPE_ALIASES,
            "journal": core.JOURNAL_ALIASES, "volume": core.VOLUME_ALIASES,
            "issue": core.ISSUE_ALIASES, "pages": core.PAGES_ALIASES,
            "isbn": core.ISBN_ALIASES, "issn": core.ISSN_ALIASES,
        }
        self.context_columns = {
            key: core.guess_column(columns, names) or NO_COLUMN
            for key, names in context_aliases.items()
        }
        self.output_df = None
        self.verification_added_columns = []
        self.current_verification_results = []
        self.export_btn.configure(state="disabled")
        self.table.set_rows([])
        _set_readonly_text_autosize(
            self.verification_message_view,
            "Run verification, then select a record to see its full message and decision details.")
        self.progress.set(0)
        self.time_var.set("Elapsed 0:00 · Estimated remaining: waiting to start")
        self.status_var.set("Columns auto-detected. Confirm the mapping, then click Verify file.")

    @staticmethod
    def _cell(row, column):
        if column == NO_COLUMN:
            return ""
        value = row.get(column, "")
        if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
            return ""
        return str(value).strip()

    def on_verify(self):
        if self.df is None:
            messagebox.showwarning("No file", "Please choose a file first.")
            return
        if self.title_col.get() == NO_COLUMN:
            messagebox.showwarning("Missing title", "Choose the column containing the paper title.")
            return
        if self.doi_col.get() == NO_COLUMN and self.url_col.get() == NO_COLUMN:
            messagebox.showwarning("Missing DOI/URL", "Choose at least one DOI or URL column.")
            return
        columns = tuple(menu.get() for menu in self.mapping_menus)
        verification_fields = self.verification_settings_page.get_enabled_fields()
        source_settings = (self.sources_page.get_enabled_sources(), self.sources_page.get_email(),
                           self.sources_page.get_s2_key(), self.sources_page.get_core_key(),
                           "high_confidence" if self.verification_mode.get() == "High confidence" else "fast")
        self.running, self.total_rows, self.done_rows, self.cached_rows = True, len(self.df), 0, 0
        self.start_time = time.monotonic()
        self.verify_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.cancel_event.clear()
        self.export_btn.configure(state="disabled")
        self.table.set_rows([])
        self.current_verification_results = []
        _set_readonly_text_autosize(self.verification_message_view, "Verification is running…")
        self.progress.set(0)
        source_names = ", ".join(core.source_label(s) for s in source_settings[0]) or "no additional sources"
        if source_settings[4] == "high_confidence":
            self.status_var.set(
                f"Verifying 0/{self.total_rows} records · Independent confirmation sources: {source_names}")
        else:
            self.status_var.set(
                f"Verifying 0/{self.total_rows} records · Fallback sources if needed: {source_names}")
        self.time_var.set("Elapsed 0:00 · Estimating time remaining…")
        threading.Thread(target=self._worker, args=(columns, source_settings, verification_fields), daemon=True).start()

    def _worker(self, columns, source_settings, verification_fields):
        title_col, author_col, year_col, doi_col, url_col = columns
        enabled_sources, email, s2_key, core_key, verification_mode = source_settings
        results = [None] * len(self.df)
        cache = core.load_verification_cache()
        cache_lock = threading.Lock()
        core.lib.set_status_callback(lambda event: self.result_queue.put(("rate_limit", event)))

        def work(index, row):
            if self.cancel_event.is_set():
                return index, {"status": "cancelled", "verified": False, "link_valid": False,
                               "paper_match": False, "score": 0, "metadata_title": "",
                               "metadata_authors": [], "metadata_year": None, "metadata_source": "",
                               "resolved_url": "", "message": "Cancelled; rerun to resume from checkpoints.",
                               "doi": "", "from_cache": False}
            title = self._cell(row, title_col)
            author = self._cell(row, author_col) if "authors" in verification_fields else ""
            year = self._cell(row, year_col) if "year" in verification_fields else ""
            doi, url = self._cell(row, doi_col), self._cell(row, url_col)
            context = {
                field: self._cell(row, self.context_columns.get(field, NO_COLUMN))
                if field in verification_fields else ""
                for field in ("publisher", "item_type", "journal", "volume", "issue", "pages", "isbn", "issn")
            }
            context["container_title_hint"] = self._cell(
                row, self.context_columns.get("journal", NO_COLUMN))
            cache_context = {**context, "verification_fields": sorted(verification_fields)}
            key = core.verification_cache_key(
                title, author, year, doi, url, enabled_sources, verification_mode, cache_context)
            with cache_lock:
                cached = core.cached_verification(cache, key)
            if cached is not None:
                return index, {**cached, "from_cache": True,
                               "verification_fields": sorted(verification_fields)}
            try:
                result = core.verify_reference(
                    title, author, year, doi, url, enabled_sources=enabled_sources,
                    email=email, s2_api_key=s2_key, core_api_key=core_key, mode=verification_mode,
                    **context)
            except Exception as exc:
                result = {"status": "unavailable", "verified": False, "link_valid": False,
                          "paper_match": False, "score": 0, "metadata_title": "",
                          "metadata_authors": [], "metadata_year": None, "metadata_source": "",
                          "resolved_url": "", "message": str(exc), "doi": self._cell(row, doi_col)}
            result["from_cache"] = False
            result["verification_fields"] = sorted(verification_fields)
            with cache_lock:
                cache[key] = {"saved_at": time.time(), "result": result}
                core.save_verification_cache(cache)  # checkpoint every completed record
            return index, result

        try:
            with ThreadPoolExecutor(max_workers=DEFAULT_BATCH_WORKERS) as executor:
                futures = [executor.submit(work, i, row) for i, (_, row) in enumerate(self.df.iterrows())]
                for future in as_completed(futures):
                    index, result = future.result()
                    results[index] = result
                    self.result_queue.put(("progress", bool(result.get("from_cache"))))
            self.result_queue.put(("done", results))
        except Exception as exc:
            self.result_queue.put(("error", str(exc)))
        finally:
            core.lib.set_status_callback(None)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "progress":
                    self.done_rows += 1
                    if payload:
                        self.cached_rows += 1
                    self.progress.set(self.done_rows / max(1, self.total_rows))
                    self.status_var.set(
                        f"Verifying {self.done_rows}/{self.total_rows} records… {self.cached_rows} resumed from cache")
                    self.status_var.set(
                        f"Verifying {self.done_rows}/{self.total_rows} records · "
                        f"{self.cached_rows} resumed from cache")
                elif kind == "done":
                    self._finish(payload)
                elif kind == "rate_limit":
                    source = core.source_label(payload.get("source", "source"))
                    self.status_var.set(
                        f"{source} rate limited; retrying in {payload.get('delay', '?')}s "
                        f"(attempt {payload.get('attempt', '?')}/{payload.get('max_attempts', '?')})")
                elif kind == "error":
                    self.running = False
                    self.verify_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.status_var.set(f"Verification failed: {payload}")
        except queue.Empty:
            pass
        if self.running:
            self._update_verification_time_display()
        self.after(150, self._poll_queue)

    def _update_verification_time_display(self):
        if self.start_time is None:
            return
        elapsed = max(0, time.monotonic() - self.start_time)
        if self.done_rows > 0 and elapsed > 0:
            rate = self.done_rows / elapsed
            remaining = max(0, self.total_rows - self.done_rows) / rate if rate else 0
            average = elapsed / self.done_rows
            self.time_var.set(
                f"Elapsed {_format_duration(elapsed)} · Estimated remaining {_format_duration(remaining)} "
                f"· Average {average:.1f}s/record")
        else:
            self.time_var.set(f"Elapsed {_format_duration(elapsed)} · Estimating time remaining…")

    def _finish(self, results):
        self.running = False
        self.verify_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        output = self.df.copy().reset_index(drop=True)
        output["Verification Status"] = [r["status"] for r in results]
        output["Verified"] = [r["verified"] for r in results]
        output["Link Valid"] = [r["link_valid"] for r in results]
        output["Paper Match"] = [r["paper_match"] for r in results]
        output["Verification Score"] = [r["score"] for r in results]
        output["Verification Message"] = [r["message"] for r in results]
        output["Verification Metadata Title"] = [r["metadata_title"] for r in results]
        output["Verification Metadata Main Title"] = [r.get("metadata_main_title", "") for r in results]
        output["Verification Metadata Subtitle"] = [r.get("metadata_subtitle", "") for r in results]
        output["Verification Metadata Combined Title"] = [r.get("metadata_combined_title", "") for r in results]
        output["Title Match Variant"] = [r.get("title_match_variant", "") for r in results]
        output["Title Match Score"] = [r.get("title_score", "") for r in results]
        output["Author Match Type"] = [r.get("author_match_type", "") for r in results]
        output["Year Difference"] = [r.get("year_difference", "") for r in results]
        output["Available Publication Years"] = ["; ".join(map(str, r.get("publication_years", []))) for r in results]
        output["Identifier Match Type"] = [r.get("identifier_match_type", "") for r in results]
        output["Verification Decision Rule"] = [r.get("decision_rule", "") for r in results]
        output["Verification Metadata Authors"] = ["; ".join(r["metadata_authors"]) for r in results]
        output["Verification Metadata Year"] = [r["metadata_year"] for r in results]
        output["Verification Metadata Source"] = [r["metadata_source"] for r in results]
        output["Verification Metadata Item Type"] = [r.get("metadata_item_type", "") for r in results]
        output["Verification Metadata Publisher"] = [r.get("metadata_publisher", "") for r in results]
        output["Verification Metadata Container Title"] = [
            r.get("metadata_container_title", "") for r in results]
        output["Verification Metadata DOI"] = [r.get("metadata_doi", "") for r in results]
        output["Identifier Valid"] = [r.get("identifier_valid") for r in results]
        output["Landing Page Reachable"] = [r.get("landing_page_reachable") for r in results]
        output["Access Status"] = [r.get("access_status", "") for r in results]
        output["Content Type"] = [r.get("content_type", "") for r in results]
        output["Verification Level"] = [r.get("verification_level", "") for r in results]
        output["Core Identity Match"] = [r.get("core_identity_match", False) for r in results]
        output["Container Match"] = [r.get("container_match", False) for r in results]
        output["Container Page Checked"] = [r.get("container_page_checked", False) for r in results]
        output["Container Page Title Found"] = [r.get("container_page_title_found", False) for r in results]
        output["Container Page Matched Authors"] = [
            "; ".join(r.get("container_page_matched_authors", [])) for r in results]
        output["Container Evidence Source"] = [r.get("container_evidence_source", "") for r in results]
        output["Landing Page Evidence Checked"] = [
            r.get("landing_page_evidence_checked", False) for r in results]
        output["Alternate Language URL"] = [r.get("alternate_language_url", "") for r in results]
        output["Page Matched Title Variant"] = [r.get("page_matched_title_variant", "") for r in results]
        output["Page Title Evidence Score"] = [r.get("page_title_evidence_score", 0.0) for r in results]
        output["Page Matched Authors"] = [
            "; ".join(r.get("page_matched_authors", [])) for r in results]
        output["Secondary Metadata Match"] = [r.get("secondary_metadata_match") for r in results]
        output["PDF Detected"] = [r.get("pdf_detected", False) for r in results]
        output["PDF Text Extracted"] = [r.get("pdf_text_extracted", False) for r in results]
        output["OCR Used"] = [r.get("ocr_used", False) for r in results]
        output["OCR Status"] = [r.get("ocr_status", "") for r in results]
        output["Verified At"] = [r.get("verified_at", "") for r in results]
        output["Verification Mode"] = [r.get("verification_mode", "") for r in results]
        output["Verification Logic Version"] = [r.get("logic_version", "") for r in results]
        output["Verification Fields"] = ["; ".join(r.get("verification_fields", [])) for r in results]
        output["Metadata Conflicts"] = ["; ".join(r.get("metadata_conflicts", [])) for r in results]
        output["Metadata Warnings"] = ["; ".join(r.get("metadata_warnings", [])) for r in results]
        output["From Verification Cache"] = [r.get("from_cache", False) for r in results]
        output["Verification Sources"] = ["; ".join(core.source_label(s) for s in r.get("verification_sources", [])) for r in results]
        output["Verification Sources Checked"] = ["; ".join(core.source_label(s) for s in r.get("sources_checked", [])) for r in results]
        output["Verification Source Failures"] = [_failed_sources_text(r.get("source_failures")) for r in results]
        output["Resolved URL"] = [r["resolved_url"] for r in results]
        self.verification_added_columns = [
            column for column in output.columns if column not in self.df.columns]
        self.output_df = output
        self.current_verification_results = results[:200]
        verified = sum(bool(r["verified"]) for r in results)
        reachable = sum(bool(r["link_valid"]) for r in results)
        verified_status_counts = {}
        for result in results:
            if result.get("verified"):
                status = result.get("status", "verified")
                verified_status_counts[status] = verified_status_counts.get(status, 0) + 1
        breakdown_order = (
            ("verified", "direct"),
            ("verified_with_warning", "with warning"),
            ("verified_via_landing_page", "via publisher page"),
            ("verified_via_container_page", "via container page"),
        )
        breakdown = [f"{verified_status_counts[key]} {label}" for key, label in breakdown_order
                     if verified_status_counts.get(key)]
        known = {key for key, _ in breakdown_order}
        other_verified = sum(count for key, count in verified_status_counts.items() if key not in known)
        if other_verified:
            breakdown.append(f"{other_verified} other verified")
        detail = f" ({', '.join(breakdown)})" if breakdown else ""
        self.status_var.set(
            f"Done: {verified}/{len(results)} verified{detail}; "
            f"{len(results) - verified}/{len(results)} not verified; "
            f"{reachable}/{len(results)} links valid.")
        elapsed = time.monotonic() - self.start_time if self.start_time is not None else 0
        average = elapsed / len(results) if results else 0
        self.time_var.set(
            f"Completed in {_format_duration(elapsed)} · Average {average:.1f}s/record · Estimated remaining 0:00")
        title_col = self.title_col.get()
        rows = []
        copy_rows = []
        for i, result in enumerate(results[:200]):
            title = self._cell(self.df.iloc[i], title_col)
            metadata_title = result["metadata_title"]
            status_label = STATUS_LABELS.get(result["status"], result["status"])
            copy_rows.append((title, status_label, f"{result['score']:.0f}",
                              metadata_title, result["resolved_url"]))
            rows.append((title[:60], status_label, f"{result['score']:.0f}",
                          metadata_title[:60], result["resolved_url"]))
        self.table.set_rows(rows, copy_values=copy_rows)
        self.export_btn.configure(state="normal")

    def on_stop(self):
        if self.running:
            self.cancel_event.set()
            self.stop_btn.configure(state="disabled")
            self.status_var.set("Stopping after current requests finish. Completed records are checkpointed; rerun to resume.")

    def on_export(self):
        if self.output_df is None:
            return
        label = self.output_format.get()
        fmt, ext = core.OUTPUT_FORMATS[label]
        if fmt == "csv" and not messagebox.askyesno(
                "CSV is not a Zotero import format",
                "Zotero cannot import CSV files as references. Use RIS, BibTeX, or CSL JSON "
                "if this file is going back into Zotero.\n\nSave a CSV table anyway?"):
            return
        export_df = self.output_df
        removable_columns = [
            column for column in self.verification_added_columns
            if column in self.output_df.columns]
        if removable_columns:
            dialog = VerificationColumnRemovalDialog(self, removable_columns, label)
            self.wait_window(dialog)
            if dialog.result is None:
                return
            export_df = self.output_df.drop(columns=dialog.result, errors="ignore")
        base = os.path.splitext(os.path.basename(self.file_path or "records"))[0]
        path = filedialog.asksaveasfilename(title="Save verified records", defaultextension=ext,
                                            filetypes=[(label, f"*{ext}")], initialfile=f"{base}_verified{ext}")
        if not path:
            return
        try:
            portable_columns = None
            if fmt in {"ris", "bibtex"}:
                portable_columns = [
                    column for column in self.verification_added_columns
                    if column in export_df.columns
                ]
            core.write_records_file(
                export_df, path, fmt, portable_columns=portable_columns)
        except Exception as exc:
            messagebox.showerror("Save failed", f"Couldn't save the file:\n{exc}")
            return
        messagebox.showinfo("Saved", f"Verified records saved to:\n{path}")


# ---------------------------------------------------------------------------
# Batch import
# ---------------------------------------------------------------------------

class BatchLookupPage(ctk.CTkFrame):
    def __init__(self, master, sources_page: SourcesPage):
        super().__init__(master, fg_color="transparent")
        self.sources_page = sources_page

        self.df = None
        self.file_path = None
        self.result_queue = queue.Queue()
        self.total_rows = 0
        self.done_rows = 0
        self.output_df = None
        self.batch_running = False
        self.start_time = None

        # 1. Choose a file
        file_frame = ctk.CTkFrame(self, fg_color="transparent")
        file_frame.pack(fill="x", padx=4, pady=(6, 10))
        ctk.CTkButton(file_frame, text="Choose file…", width=130, command=self.on_choose_file).pack(side="left")
        self.file_label = ctk.CTkLabel(file_frame, text="No file selected (CSV / RIS / BibTeX / CSL JSON / Excel)", anchor="w")
        self.file_label.pack(side="left", padx=12)

        # 2. Column mapping
        mapping_frame = ctk.CTkFrame(self)
        mapping_frame.pack(fill="x", padx=4, pady=(0, 10))
        mapping_row = ctk.CTkFrame(mapping_frame, fg_color="transparent")
        mapping_row.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(mapping_row, text="Title column").pack(side="left", padx=(0, 6))
        self.title_col = ctk.CTkOptionMenu(mapping_row, values=["(choose a file first)"], width=170)
        self.title_col.pack(side="left", padx=(0, 20))

        ctk.CTkLabel(mapping_row, text="Author column").pack(side="left", padx=(0, 6))
        self.author_col = ctk.CTkOptionMenu(mapping_row, values=[NO_COLUMN], width=170)
        self.author_col.pack(side="left", padx=(0, 20))

        ctk.CTkLabel(mapping_row, text="Year column").pack(side="left", padx=(0, 6))
        self.year_col = ctk.CTkOptionMenu(mapping_row, values=[NO_COLUMN], width=150)
        self.year_col.pack(side="left")

        # 3. Concurrency
        workers_frame = ctk.CTkFrame(self)
        workers_frame.pack(fill="x", padx=4, pady=(0, 10))
        workers_row = ctk.CTkFrame(workers_frame, fg_color="transparent")
        workers_row.pack(fill="x", padx=10, pady=(10, 2))
        ctk.CTkLabel(workers_row, text="Concurrent workers").pack(side="left")
        self.workers_slider = ctk.CTkSlider(
            workers_row, from_=MIN_BATCH_WORKERS, to=MAX_BATCH_WORKERS,
            number_of_steps=MAX_BATCH_WORKERS - MIN_BATCH_WORKERS,
            width=200, command=self._on_workers_change,
        )
        self.workers_slider.set(DEFAULT_BATCH_WORKERS)
        self.workers_slider.pack(side="left", padx=(10, 8))
        self.workers_value_label = ctk.CTkLabel(workers_row, text=str(DEFAULT_BATCH_WORKERS), width=24)
        self.workers_value_label.pack(side="left")
        ctk.CTkLabel(
            workers_frame,
            text="How many records to look up at the same time. Higher = faster, but Semantic Scholar "
                 "and CORE are rate-limited globally no matter what this is set to, so extra workers "
                 "won't speed those two up. 4-6 is a good default; drop to 2-3 if you see errors or "
                 "timeouts; go up to 8-12 if you've disabled Semantic Scholar/CORE and want more speed.",
            text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11), anchor="w", justify="left",
            wraplength=950,
        ).pack(fill="x", padx=10, pady=(0, 10))

        # 4. Output format + run button
        run_frame = ctk.CTkFrame(self, fg_color="transparent")
        run_frame.pack(fill="x", padx=4, pady=(0, 10))
        ctk.CTkLabel(run_frame, text="Export format").pack(side="left")
        self.output_format = ctk.CTkOptionMenu(run_frame, values=list(core.OUTPUT_FORMATS.keys()), width=160)
        self.output_format.pack(side="left", padx=(8, 20))
        self.start_btn = ctk.CTkButton(run_frame, text="Run batch lookup", width=150, command=self.on_start)
        self.start_btn.pack(side="left")
        self.export_btn = ctk.CTkButton(run_frame, text="Export results…", width=140, command=self.on_export, state="disabled")
        self.export_btn.pack(side="left", padx=(10, 0))

        self.progress = ctk.CTkProgressBar(self)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=4, pady=(0, 2))

        self.time_var = ctk.StringVar(value="")
        ctk.CTkLabel(self, textvariable=self.time_var, text_color=("gray40", "gray60"),
                     font=ctk.CTkFont(size=11), anchor="w") \
            .pack(fill="x", padx=4, pady=(0, 6))

        self.status_var = ctk.StringVar(value="Choose a file with a list of literature records to get started. Pick which sources to use on the Sources tab.")
        ctk.CTkLabel(self, textvariable=self.status_var, text_color=("gray30", "gray70"), anchor="w") \
            .pack(fill="x", padx=4, pady=(0, 8))

        self.table = ResultsTable(
            self,
            headers=["Title", "Status", "Score", "Source", "DOI", "Link"],
            weights=[1, 0, 0, 0, 0, 1],
            link_columns={5},  # "Link" column: click to open straight in the browser
        )
        self.table.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.after(150, self._poll_queue)

    def _on_workers_change(self, value):
        self.workers_value_label.configure(text=str(int(round(value))))

    # -- File selection / column mapping ------------------------------------

    def on_choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose a file with literature records",
            filetypes=[
                ("Supported files", "*.csv *.xlsx *.xls *.json *.ris *.bib *.bibtex"),
                ("CSV", "*.csv"),
                ("RIS", "*.ris"),
                ("BibTeX / BibLaTeX", "*.bib *.bibtex"),
                ("CSL JSON", "*.json"),
                ("Excel", "*.xlsx *.xls"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            df = core.read_records_file(path)
        except Exception as e:
            messagebox.showerror("Couldn't read file", f"Failed to read this file:\n{e}")
            return
        if df.empty:
            messagebox.showwarning("Empty file", "No records were found in this file.")
            return

        self.df = df
        self.file_path = path
        encoding = df.attrs.get("source_encoding")
        encoding_note = f" · {encoding}" if encoding else ""
        self.file_label.configure(text=f"{os.path.basename(path)} ({len(df)} records){encoding_note}")

        columns = list(df.columns)
        title_guess = core.guess_column(columns, core.TITLE_ALIASES) or columns[0]
        author_guess = core.guess_column(columns, core.AUTHOR_ALIASES)
        year_guess = core.guess_column(columns, core.YEAR_ALIASES)

        self.title_col.configure(values=columns)
        self.title_col.set(title_guess)

        self.author_col.configure(values=[NO_COLUMN] + columns)
        self.author_col.set(author_guess or NO_COLUMN)

        self.year_col.configure(values=[NO_COLUMN] + columns)
        self.year_col.set(year_guess or NO_COLUMN)

        self.status_var.set("Columns auto-detected — fix them above if wrong, then click “Run batch lookup”.")
        self.export_btn.configure(state="disabled")
        self.table.set_rows([])

    # -- Batch lookup ---------------------------------------------------------

    def on_start(self):
        if self.df is None:
            messagebox.showwarning("No file", "Please choose a file first.")
            return

        title_col = self.title_col.get()
        author_col = self.author_col.get()
        year_col = self.year_col.get()
        if title_col not in self.df.columns:
            messagebox.showwarning("Missing column", "Please choose a valid title column.")
            return

        enabled_sources = self.sources_page.get_enabled_sources()
        if not enabled_sources:
            messagebox.showwarning("No sources selected", "Please enable at least one source on the Sources tab.")
            return

        workers = int(round(self.workers_slider.get()))

        self.start_btn.configure(state="disabled")
        self.export_btn.configure(state="disabled")
        self.table.set_rows([])
        self.progress.set(0)
        self.total_rows = len(self.df)
        self.done_rows = 0
        self.status_var.set(f"Running batch lookup on {self.total_rows} records with {workers} workers…")
        self.batch_running = True
        self.start_time = time.monotonic()
        self.time_var.set("Elapsed 0:00")

        threading.Thread(
            target=self._batch_worker,
            args=(title_col, author_col, year_col, enabled_sources,
                  self.sources_page.get_email(), self.sources_page.get_s2_key(), self.sources_page.get_core_key(),
                  workers),
            daemon=True,
        ).start()

    def _batch_worker(self, title_col, author_col, year_col, enabled_sources, email, s2_key, core_key, workers):
        df = self.df
        cache = core.load_cache()
        cache_lock = threading.Lock()
        results = [None] * len(df)

        def work(idx, row):
            # pandas represents an empty cell as the float NaN, not "" - and
            # since NaN is truthy, a naive `row.get(col, "") or ""` doesn't
            # catch it (str(NaN) silently becomes the literal text "nan",
            # which would then get sent to the APIs as a real author name).
            # Coerce to a string and explicitly strip that literal out too.
            title = str(row.get(title_col, "") or "")
            if title.strip().lower() == "nan":
                title = ""

            author = ""
            if author_col != NO_COLUMN:
                author = str(row.get(author_col, "") or "")
                if author.strip().lower() == "nan":
                    author = ""

            year = ""
            if year_col != NO_COLUMN:
                year_raw = str(row.get(year_col, "") or "")
                if year_raw.strip().lower() != "nan":
                    year = year_raw

            key = core.cache_key(title, author, year, enabled_sources)
            with cache_lock:
                cached = cache.get(key)
            if cached is not None:
                return idx, cached

            try:
                res = core.lookup_one(title, author, year, enabled_sources, email=email, s2_api_key=s2_key, core_api_key=core_key)
            except Exception:
                res = {"status": "not_found", "doi": "", "url": "", "matched_title": "", "source": "", "score": 0, "failed_sources": []}

            with cache_lock:
                cache[key] = res
            return idx, res

        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(work, idx, row) for idx, (_, row) in enumerate(df.iterrows())]
                for future in futures:
                    idx, res = future.result()
                    results[idx] = res
                    self.result_queue.put(("progress", (idx, res)))
        except Exception as e:
            self.result_queue.put(("error", str(e)))
            return
        finally:
            core.save_cache(cache)

        self.result_queue.put(("done", results))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "progress":
                    self.done_rows += 1
                    self.progress.set(self.done_rows / max(1, self.total_rows))
                    self.status_var.set(f"Running batch lookup… {self.done_rows}/{self.total_rows}")
                elif kind == "error":
                    self.batch_running = False
                    self.start_btn.configure(state="normal")
                    self.status_var.set("Batch lookup failed.")
                    messagebox.showerror("Something went wrong", f"An error occurred during batch lookup:\n{payload}")
                elif kind == "done":
                    self._on_batch_done(payload)
        except queue.Empty:
            pass

        if self.batch_running:
            self._update_time_display()

        self.after(150, self._poll_queue)

    def _update_time_display(self):
        elapsed = time.monotonic() - self.start_time
        if self.done_rows > 0:
            rate = self.done_rows / elapsed  # records/sec
            remaining = (self.total_rows - self.done_rows) / rate if rate > 0 else 0
            self.time_var.set(
                f"Elapsed {_format_duration(elapsed)} · "
                f"Estimated remaining {_format_duration(remaining)} "
                f"(~{rate:.1f} records/sec)"
            )
        else:
            self.time_var.set(f"Elapsed {_format_duration(elapsed)} · estimating time remaining…")

    def _on_batch_done(self, results):
        self.batch_running = False
        self.start_btn.configure(state="normal")

        elapsed = time.monotonic() - self.start_time if self.start_time else 0
        self.time_var.set(f"Finished in {_format_duration(elapsed)}.")

        result_df = self.df.copy().reset_index(drop=True)
        result_df["Lookup Status"] = [STATUS_LABELS.get(r["status"], r["status"]) for r in results]
        result_df["Match Score"] = [r["score"] for r in results]
        result_df["Match Source"] = [r["source"] for r in results]
        result_df["Matched Title"] = [r["matched_title"] for r in results]
        result_df["DOI"] = [r["doi"] for r in results]
        result_df["Link"] = [r["url"] for r in results]
        result_df["Failed Sources"] = [_failed_sources_text(r.get("failed_sources")) for r in results]
        self.output_df = result_df

        found = sum(1 for r in results if r["status"] in ("auto_accepted", "needs_review"))
        incomplete = sum(1 for r in results if r.get("failed_sources"))
        done_msg = f"Done: {found}/{len(results)} records got a DOI/link."
        if incomplete:
            done_msg += f" {incomplete} record(s) had at least one source fail to respond — see the Failed Sources column."
        done_msg += " Click “Export results” to save."
        self.status_var.set(done_msg)

        title_col = self.title_col.get()
        preview_rows = []
        copy_rows = []
        for i, r in enumerate(results[:200]):  # preview only the first 200 rows to keep the UI responsive
            title_text = str(self.df.iloc[i][title_col])
            status_text = STATUS_LABELS.get(r["status"], r["status"])
            if r.get("failed_sources"):
                status_text += " ⚠"
            preview_rows.append((
                title_text[:60],
                status_text,
                f"{r['score']:.0f}",
                r["source"],
                r["doi"],
                r["url"],
            ))
            copy_rows.append((
                title_text,
                status_text,
                f"{r['score']:.0f}",
                r["source"],
                r["doi"],
                r["url"],
            ))
        self.table.set_rows(preview_rows, copy_values=copy_rows)
        if len(results) > 200:
            self.status_var.set(self.status_var.get() + " (preview shows the first 200 rows only; the exported file has all of them)")

        self.export_btn.configure(state="normal")

    # -- Export ------------------------------------------------------------

    def on_export(self):
        if self.output_df is None:
            return
        fmt_label = self.output_format.get()
        fmt, ext = core.OUTPUT_FORMATS[fmt_label]
        if fmt == "csv" and not messagebox.askyesno(
                "CSV is not a Zotero import format",
                "Zotero cannot import CSV files as references. Use RIS, BibTeX, or CSL JSON "
                "if this file is going back into Zotero.\n\nSave a CSV table anyway?"):
            return
        path = filedialog.asksaveasfilename(
            title="Save lookup results",
            defaultextension=ext,
            filetypes=[(fmt_label, f"*{ext}")],
            initialfile=(os.path.splitext(os.path.basename(self.file_path))[0] + "_results" + ext)
            if self.file_path else f"results{ext}",
        )
        if not path:
            return
        try:
            core.write_records_file(self.output_df, path, fmt)
        except Exception as e:
            messagebox.showerror("Save failed", f"Couldn't save the file:\n{e}")
            return
        messagebox.showinfo("Saved", f"Results saved to:\n{path}")


if __name__ == "__main__":
    App().mainloop()
