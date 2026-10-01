"""
literature_lookup.py
---------------------
Literature DOI / link lookup tool.

Main tabs include:
1. "Single Lookup": type in one title (author/year optional) and search.
2. "Batch Import": pick a CSV exported from Zotero, RIS, BibTeX/BibLaTeX, CSL JSON, or
   Excel file, auto-detect which fields hold title/author/year, run the
   lookup on all of them, then export a Zotero-compatible result file.
   Its "Sources settings" sub-tab picks which free databases to query, and
   optionally takes a contact email / API keys that improve some sources'
   rate limits (or, for CORE, are required for it to return anything at all).
   These sources are shared by every tab that searches.
3. "Verification": independently import an existing bibliographic file and
   batch-check its DOI/URL records without running lookup first. Its
   "Verification settings" sub-tab chooses which metadata fields are compared.
4. "Review & Convert": open an existing file (CSV, Excel, RIS, BibTeX, CSL
   JSON), display only selected columns, filter, click DOI/URL links, record
   human decisions without API calls, and save all or the filtered records
   with all or only the ticked columns as CSV, Excel, RIS or BibTeX.

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

import math
import os
import queue
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
from tkinter import Menu, filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

import customtkinter as ctk
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

try:  # Package import: import system.literature_lookup
    from . import abstract_note_tools as abstract_tools
    from . import compare_tools
    from . import convert_tools
    from . import issp_module_tags as issp_tags
    from . import lookup_core as core
    from . import stats_tools
    from . import translate_tools
except ImportError:  # Direct launch: python literature_lookup.py from system/
    import abstract_note_tools as abstract_tools
    import compare_tools
    import convert_tools
    import issp_module_tags as issp_tags
    import lookup_core as core
    import stats_tools
    import translate_tools


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
    "url_enriched": "URL added (Unpaywall)",
    "skipped_complete": "Already had a DOI/URL — skipped",
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
    _fit_readonly_text_height(textbox, min_height, max_height, line_height)


def _fit_readonly_text_height(textbox, min_height, max_height, line_height):
    if not textbox.winfo_exists():
        return
    if not textbox.winfo_ismapped() or textbox.winfo_width() <= 1:
        # Not on screen yet (e.g. a dialog's first record): its width is
        # unknown, so every word would count as a wrapped line and the box
        # would open at max_height. Measure once it has been laid out.
        textbox.after(80, lambda: _fit_readonly_text_height(textbox, min_height, max_height, line_height))
        return
    try:
        lines = textbox._textbox.count("1.0", "end", "update", "displaylines")
        if isinstance(lines, (tuple, list)):  # an int when "update" is passed
            lines = lines[0] if lines else 1
        lines = lines or 1
    except Exception:
        lines = 1
    height = min(max_height, max(min_height, lines * line_height + 24))
    if textbox.cget("height") != height:
        textbox.configure(height=height)


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
                      "opens the file in Review & Convert."),
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

    def set_rows(self, rows_values, copy_values=None, row_tags=None):
        """``row_tags`` optionally gives each row a Treeview tag (configured
        by the caller with tree.tag_configure) instead of zebra striping."""
        self.clear()
        source = copy_values if copy_values is not None else rows_values
        self.copy_values = [[self._text(value) for value in row] for row in source]
        for index, values in enumerate(rows_values):
            tag = row_tags[index] if row_tags is not None else (
                "evenrow" if index % 2 == 0 else "oddrow")
            self.tree.insert("", "end", iid=str(index),
                             values=[self._text(value) for value in values], tags=(tag,))

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

    def retitle(self, headers):
        """Relabel the existing columns without rebuilding them — only valid
        when the new headers are the same count as the table was built with."""
        for index, header in enumerate(headers):
            self.tree.heading(f"c{index}", text=header, anchor="w")
        self.headers = list(headers)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

BATCH_RUN_SUBTAB = "Batch Lookup"
SOURCES_SUBTAB = "Sources settings"
VERIFY_RUN_SUBTAB = "Verify file"
VERIFICATION_SETTINGS_SUBTAB = "Verification settings"
SOURCES_LOCATION = f"Batch Import → {SOURCES_SUBTAB}"


def _settings_subtabs(parent):
    """A compact tab strip inside a main tab: the task, then its settings."""
    subtabs = ctk.CTkTabview(parent, corner_radius=8, border_width=0, fg_color="transparent",
                             segmented_button_font=ctk.CTkFont(size=12))
    subtabs.pack(fill="both", expand=True)
    return subtabs

class App(ctk.CTk):
    TAB_FONT_SIZES = (13, 12, 11, 10)

    def _schedule_tab_fit(self, event):
        if event.widget is not self:
            return
        if self._tab_fit_pending is not None:
            self.after_cancel(self._tab_fit_pending)
        self._tab_fit_pending = self.after(120, self._fit_tab_labels)

    def _fit_tab_labels(self):
        self._tab_fit_pending = None
        """Use the largest tab-label font at which every main tab name fits.
        The tab strip can't wrap or scroll, so at a narrow window (or 150%
        display scaling) a fixed size clipped both ends of every label."""
        segmented = self.tabview._segmented_button
        available = self.tabview.winfo_width() - 50
        if available <= 50:
            return
        scale = ctk.ScalingTracker.get_widget_scaling(segmented) or 1
        family = ctk.CTkFont().cget("family")
        chosen = self.TAB_FONT_SIZES[-1]
        for size in self.TAB_FONT_SIZES:
            measure = tkfont.Font(family=family, size=-round(size * scale), weight="bold").measure
            needed = sum(measure(name) + 20 * scale for name in self.tabview._name_list)
            if needed <= available:
                chosen = size
                break
        if chosen != self._tab_font_size:
            self._tab_font_size = chosen
            segmented.configure(font=ctk.CTkFont(size=chosen, weight="bold"))

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
        abstract_tab = tabview.add("Abstract Finder")
        issp_module_tab = tabview.add("ISSP Module Tags")
        note_links_tab = tabview.add("Note Link Recovery")
        tag_cleanup_tab = tabview.add("Keyword Cleanup")
        review_tab = tabview.add("Review & Convert")
        statistics_tab = tabview.add("Statistics")
        compare_tab = tabview.add("Compare Documents")
        translate_tab = tabview.add("Translate")
        tabview.set("Single Lookup")
        self.tabview = tabview
        self._tab_font_size = 13
        # Toplevel bindings also see every child's events; react only to the window.
        # Measure after the resize has been laid out; the tab strip still has
        # its old width while the window's own <Configure> is delivered.
        self._tab_fit_pending = None
        self.bind("<Configure>", self._schedule_tab_fit, add="+")

        # Source selection lives under Batch Import and the verification
        # method under Verification, as sub-tabs. The settings objects are
        # still shared: Single Lookup, Abstract Finder and Verification read
        # the same sources, and Single Lookup the same verification fields.
        batch_subtabs = _settings_subtabs(batch_tab)
        batch_run_tab = batch_subtabs.add(BATCH_RUN_SUBTAB)
        sources_tab = batch_subtabs.add(SOURCES_SUBTAB)
        batch_subtabs.set(BATCH_RUN_SUBTAB)
        verification_subtabs = _settings_subtabs(verification_tab)
        verification_run_tab = verification_subtabs.add(VERIFY_RUN_SUBTAB)
        verification_settings_tab = verification_subtabs.add(VERIFICATION_SETTINGS_SUBTAB)
        verification_subtabs.set(VERIFY_RUN_SUBTAB)

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

        self.batch_page = BatchLookupPage(batch_run_tab, self.sources_page)
        self.batch_page.pack(fill="both", expand=True)

        self.verification_page = VerificationPage(
            verification_run_tab, self.sources_page, self.verification_settings_page)
        self.verification_page.pack(fill="both", expand=True)

        self.review_page = ManualReviewPage(review_tab)
        self.review_page.pack(fill="both", expand=True)

        self.statistics_page = StatisticsPage(statistics_tab)
        self.statistics_page.pack(fill="both", expand=True)

        self.compare_page = CompareDocumentsPage(compare_tab)
        self.compare_page.pack(fill="both", expand=True)

        self.translate_page = TranslatePage(translate_tab)
        self.translate_page.pack(fill="both", expand=True)



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

        export_row = ctk.CTkFrame(self, fg_color="transparent")
        export_row.pack(fill="x", padx=4, pady=(0, 8))
        self.output_format = _add_export_format_menu(export_row)
        self.export_btn = ctk.CTkButton(export_row, text="Export enriched copy…", width=170,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="left")

        settings = ctk.CTkFrame(self)
        settings.pack(fill="x", padx=4, pady=(0, 8))
        self.note_col = self._mapping(settings, "Notes column", 0)
        self.url_col = self._mapping(settings, "URL column", 1)
        self.doi_col = self._mapping(settings, "DOI column", 2)
        self.analyze_btn = ctk.CTkButton(settings, text="Analyze notes & add missing links", width=220,
                                         command=self.on_analyze, state="disabled")
        self.analyze_btn.grid(row=1, column=3, padx=10, pady=(0, 10))
        self.remove_links_var = ctk.BooleanVar(value=False)
        self.remove_issp_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            settings, text="Remove links from Notes once they are in the URL/DOI field",
            variable=self.remove_links_var).grid(
                row=2, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))
        ctk.CTkCheckBox(
            settings, text="Clear Notes that contain only \"ISSP\"",
            variable=self.remove_issp_var).grid(
                row=3, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 10))

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
            self.df, note_column=note, url_column=url, doi_column=doi,
            remove_imported_links=self.remove_links_var.get(),
            remove_issp_only_notes=self.remove_issp_var.get())
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
        if self.remove_links_var.get():
            lines.append(f"Links removed from Notes: {stats.get('Links removed from Notes', 0):,} "
                         f"(in {stats.get('Notes changed by link removal', 0):,} records)")
        if self.remove_issp_var.get():
            lines.append(f"ISSP-only Notes cleared: {stats.get('ISSP-only Notes cleared', 0):,}")
        _set_readonly_text(self.stats_box, "\n".join(lines))

    def on_export(self):
        if self.output_df is None:
            return
        _save_enriched_dataframe(self, self.output_df, self.file_path, "note_links",
                                 format_label=self.output_format.get(), patch_ris=True)


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

        export_row = ctk.CTkFrame(self, fg_color="transparent")
        export_row.pack(fill="x", padx=4, pady=(0, 8))
        self.output_format = _add_export_format_menu(export_row)
        self.export_btn = ctk.CTkButton(export_row, text="Export with abstracts…", width=170,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="left")

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
        lens_key = self.sources_page.get_lens_key()
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
                        s2_api_key=s2_key, core_api_key=core_key, lens_api_key=lens_key)
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
            _save_enriched_dataframe(self, self.output_df, self.file_path, "abstracts",
                                     format_label=self.output_format.get())


SAME_AS_SOURCE_FORMAT = "Same as source"


def _export_format_values():
    """Values for every page's "Export format" dropdown: "Same as source"
    (keeps writing whatever format the loaded file was in - the long-
    standing default) followed by every explicit format core.write_records_file
    supports, so a page's exported copy can be forced to a different
    format than what was loaded."""
    return [SAME_AS_SOURCE_FORMAT] + list(core.OUTPUT_FORMATS.keys())


def _add_export_format_menu(parent, width=170):
    """Label + CTkOptionMenu pair for "which format to export as", packed
    left-to-right into `parent`. Returns the CTkOptionMenu; read its
    .get() and pass it as `_save_enriched_dataframe`'s `format_label`."""
    ctk.CTkLabel(parent, text="Export format").pack(side="left", padx=(0, 8))
    menu = ctk.CTkOptionMenu(parent, values=_export_format_values(), width=width)
    menu.set(SAME_AS_SOURCE_FORMAT)
    menu.pack(side="left", padx=(0, 10))
    return menu


def _ask_note_columns(parent, columns, fmt):
    """Before saving to RIS/BibTeX, say which columns have no field there.
    Returns True to keep them in each record's Note, False to leave them
    out, or None to cancel the save."""
    if not columns or fmt not in ("ris", "bibtex"):
        return True
    names = "\n".join(f"  • {c}" for c in columns[:12])
    if len(columns) > 12:
        names += f"\n  … and {len(columns) - 12} more"
    return messagebox.askyesnocancel(
        "Columns without a field",
        f"{'RIS' if fmt == 'ris' else 'BibTeX'} has no field for these column(s):\n{names}\n\n"
        "Yes: keep them in each record's Note (one \"Literature Lookup field: …\" line "
        "each). This app turns them back into columns when it opens the file; Zotero "
        "shows them as note text.\n"
        "No: leave these columns out of the file.\n"
        "Cancel: don't save. (CSV or Excel keeps them as ordinary columns.)",
        parent=parent)


def _save_enriched_dataframe(parent, dataframe, source_path, suffix, format_label=None,
                             patch_ris=False, note_columns=()):
    """Export a page's result. With ``patch_ris`` (pages that only edit
    Notes / URL / DOI / keywords), a RIS source saved as RIS is written as a
    patch of the original file: only the changed fields' lines are replaced
    and every other line is kept exactly as it was."""
    base = os.path.splitext(os.path.basename(source_path or "records"))[0]
    source_label = core.preferred_output_format_label(source_path)
    chosen_label = format_label if format_label and format_label != SAME_AS_SOURCE_FORMAT else None
    default_label = chosen_label or source_label
    default_format, default_ext = core.OUTPUT_FORMATS[default_label]
    default_display = (f"{default_label} (chosen above)" if chosen_label
                        else f"Same format as source ({source_label})")
    filetypes = [(default_display, f"*{default_ext}")]
    filetypes.extend(
        (label, f"*{extension}")
        for label, (_format, extension) in core.OUTPUT_FORMATS.items()
        if label != default_label
    )
    path = filedialog.asksaveasfilename(
        title="Export enriched records", defaultextension=default_ext,
        filetypes=filetypes, initialfile=f"{base}_{suffix}{default_ext}")
    if not path:
        return
    ext = os.path.splitext(path)[1].casefold()
    formats = {".csv": "csv", ".xlsx": "excel", ".ris": "ris", ".bib": "bibtex",
               ".bibtex": "bibtex", ".json": "csl_json"}
    fmt = formats.get(ext, default_format)
    keep_notes = _ask_note_columns(parent, [c for c in note_columns if c in dataframe.columns], fmt)
    if keep_notes is None:
        return
    if not keep_notes:
        dataframe = dataframe.drop(columns=[c for c in note_columns if c in dataframe.columns])
        note_columns = ()
    patched = False
    try:
        if (patch_ris and fmt == "ris"
                and os.path.splitext(source_path or "")[1].casefold() == ".ris"):
            original = core.read_records_file(source_path).reset_index(drop=True)
            patched = core.write_ris_patch(source_path, original, dataframe, path,
                                           note_columns=note_columns)
        if not patched:
            keep = [c for c in note_columns if c in dataframe.columns]
            core.write_records_file(dataframe, path, fmt, portable_columns=keep or None)
    except Exception as exc:
        messagebox.showerror("Export failed", f"Couldn't save the enriched file:\n{exc}")
        return
    extra = ("\nOnly the changed Note / URL / DOI / keyword lines were rewritten; every "
             "other line of the source RIS was kept as it was." if patched else "")
    messagebox.showinfo("Export complete", f"Saved to:\n{path}{extra}")


def _export_table_rows(parent, rows, headers, default_name, format_label=None):
    """Export an arbitrary computed table (stats counts, a diff table — not
    a bibliography with a "same format as source" concept) to CSV or Excel."""
    want_excel = (format_label or "").startswith("Excel")
    default_ext = ".xlsx" if want_excel else ".csv"
    filetypes = ([("Excel (.xlsx)", "*.xlsx"), ("CSV table (.csv)", "*.csv")] if want_excel else
                 [("CSV table (.csv)", "*.csv"), ("Excel (.xlsx)", "*.xlsx")])
    default_name = os.path.splitext(default_name)[0] + default_ext
    path = filedialog.asksaveasfilename(
        title="Export table", defaultextension=default_ext,
        filetypes=filetypes,
        initialfile=default_name)
    if not path:
        return
    frame = pd.DataFrame(rows, columns=headers)
    try:
        if os.path.splitext(path)[1].casefold() == ".xlsx":
            frame.to_excel(path, index=False)
        else:
            frame.to_csv(path, index=False, encoding="utf-8-sig")
    except Exception as exc:
        messagebox.showerror("Export failed", f"Couldn't save the file:\n{exc}")
        return
    messagebox.showinfo("Export complete", f"Saved to:\n{path}")


def _chart_theme_colors():
    """Background/text/accent colors so an embedded matplotlib chart doesn't
    look like a bright white rectangle dropped into a dark-mode window."""
    dark = ctk.get_appearance_mode() == "Dark"
    return {
        "figure_bg": "#242424" if dark else "#F5F5F5",
        "axes_bg": "#242424" if dark else "#F5F5F5",
        "text": "#E7E7E7" if dark else "#1B1B1B",
        "grid": "#3A3A3A" if dark else "#D8D8D8",
        "accent": "#3E92E8" if dark else "#0F6CBD",
        "palette": ["#0F6CBD", "#3E92E8", "#66C2A5", "#FC8D62", "#8DA0CB",
                    "#E78AC3", "#A6D854", "#FFD92F"],
    }


def _style_chart_axes(figure, axes):
    colors = _chart_theme_colors()
    figure.set_facecolor(colors["figure_bg"])
    axes.set_facecolor(colors["axes_bg"])
    axes.tick_params(colors=colors["text"], labelsize=8)
    for spine in axes.spines.values():
        spine.set_color(colors["grid"])
    axes.title.set_color(colors["text"])
    axes.xaxis.label.set_color(colors["text"])
    axes.yaxis.label.set_color(colors["text"])
    return colors


# ---------------------------------------------------------------------------
# ISSP module tags: classify which ISSP topical module a record used, from
# ZA study numbers / GESIS DOIs / exact module names / topic keywords /
# meaning-based matching, in that order of decreasing confidence (see
# issp_module_tags.py).
# ---------------------------------------------------------------------------

class IsspModulePage(ctk.CTkFrame):
    # Segmented-button label -> tag_issp_modules(keyword_min_confidence=...)
    NO_KEYWORD_TIERS = "None (review later)"
    KEYWORD_TIERS = {NO_KEYWORD_TIERS: None, "High": "high", "High + medium": "medium",
                     "All (incl. low)": "low"}

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

        export_row = ctk.CTkFrame(self, fg_color="transparent")
        export_row.pack(fill="x", padx=4, pady=(0, 8))
        self.output_format = _add_export_format_menu(export_row)
        self.export_btn = ctk.CTkButton(export_row, text="Export with module tags…", width=185,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="left")

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
        semantic_available = issp_tags.semantic_matching_available()
        self.semantic_var = ctk.BooleanVar(value=semantic_available)
        semantic_box = ctk.CTkCheckBox(
            settings, variable=self.semantic_var,
            text="Also match by meaning, even for records already tagged another way — free, "
                 "runs entirely on this computer (downloads a small open-source model the "
                 "first time; no account, no per-use cost, nothing sent anywhere after that)"
            if semantic_available else
            "Match by meaning — not included in this version of the app (it needs the "
            "sentence-transformers package). All other matching steps still run.")
        semantic_box.pack(anchor="w", pady=(4, 0))
        if not semantic_available:
            semantic_box.configure(state="disabled")
        self.full_text_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            settings, variable=self.full_text_var,
            text="Also download each record's linked page/PDF and search the full text — much "
                 "slower (one web request per record with a URL or DOI), but reaches Methods/"
                 "Data sections an abstract alone would miss, and also looks for which "
                 "country(ies)' data the record used, tagged \"DATA - <country>\". Uses OCR for "
                 "scanned PDFs when Tesseract is installed.").pack(anchor="w", pady=(4, 0))
        keyword_row = ctk.CTkFrame(settings, fg_color="transparent")
        keyword_row.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(keyword_row, text="Module tags into the keywords:").pack(side="left", padx=(0, 8))
        self.keyword_tiers = ctk.CTkSegmentedButton(
            keyword_row, values=list(self.KEYWORD_TIERS))
        self.keyword_tiers.set(self.NO_KEYWORD_TIERS)
        self.keyword_tiers.pack(side="left")
        ctk.CTkLabel(
            settings, justify="left", anchor="w", wraplength=760,
            text="By default module tags go only into the ISSP Tags (high) / (medium) / (low) "
                 "columns, which are saved with the file; add them to the keywords later on the "
                 "Review & Convert page (\"Add values\"). \"DATA - <country>\" tags are always "
                 "added to the keywords. Existing keyword tags are never removed.").pack(
                     fill="x", pady=(2, 0))

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
            self, headers=["Title", "Tag", "Confidence", "Status", "Data countries", "Where found",
                           "Quote / reason"],
            weights=[2, 0, 0, 0, 0, 1, 3])
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
        keyword_min_confidence = self.KEYWORD_TIERS[self.keyword_tiers.get()]

        def worker():
            try:
                result_df, stats, tag_column = issp_tags.tag_issp_modules(
                    self.df, text_columns=text_columns, url_column=url, doi_column=doi,
                    tag_column=tag, title_column=title,
                    use_network_doi_lookup=use_network_doi_lookup,
                    use_semantic_matching=use_semantic_matching,
                    fetch_full_text=fetch_full_text,
                    keyword_min_confidence=keyword_min_confidence,
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
                        f"Export to save, or open the export in Review & Convert to check "
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
            value = lambda column: abstract_tools.clean_value(row.get(column, ""))
            tagged = value("ISSP Module Status") == issp_tags.STATUS_TAGGED
            rows.append((
                value(title_col)[:100] if title_col else "",
                value("ISSP Module Tag"),
                value("ISSP Module Confidence"),
                value("ISSP Module Status"),
                value("ISSP Data Countries"),
                value("ISSP Module Evidence Location") if tagged else "",
                (value("ISSP Module Evidence Quote") or value("ISSP Module Evidence")) if tagged
                else value("ISSP Module Status Reason"),
            ))
        self.table.set_rows(rows)

    def on_export(self):
        if self.output_df is not None:
            # A RIS saved as RIS only has its keyword (KW) lines rewritten.
            # The per-confidence tag columns travel in the record Note, so they come
            # back as columns in Review & Convert (to add chosen ones to the keywords).
            _save_enriched_dataframe(self, self.output_df, self.file_path, "issp_module_tags",
                                     format_label=self.output_format.get(), patch_ris=True,
                                     note_columns=tuple(issp_tags.CONFIDENCE_TAG_COLUMNS.values()))


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
            text=("Keep keywords whose letters are all uppercase (for example: IST, SURVEY DESIGN, "
                  "COVID-19) and/or keywords that contain a country name (for example: Germany, DATA - Japan). "
                  "Every other keyword is removed. The source file is never overwritten."),
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

        export_row = ctk.CTkFrame(self, fg_color="transparent")
        export_row.pack(fill="x", padx=12, pady=(0, 8))
        self.output_format = _add_export_format_menu(export_row)
        self.export_btn = ctk.CTkButton(export_row, text="Export cleaned copy…", width=170,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="left")

        rules = ctk.CTkFrame(self)
        rules.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(rules, text="Keep keywords that are").pack(side="left", padx=(12, 10), pady=8)
        self.keep_upper_var = ctk.BooleanVar(value=True)
        self.keep_country_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(rules, text="all uppercase (IST, SURVEY DESIGN)",
                        variable=self.keep_upper_var).pack(side="left", padx=(0, 16), pady=8)
        ctk.CTkCheckBox(rules, text="or contain a country (Germany, DATA - Japan, USA)",
                        variable=self.keep_country_var).pack(side="left", pady=8)

        self.status_var = ctk.StringVar(value="No file loaded.")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w",
                     text_color=("gray30", "gray70")).pack(fill="x", padx=12, pady=(0, 6))
        self.table = ResultsTable(
            self, headers=["Record", "Original keywords", "Kept keywords", "Removed keywords"],
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
                "only keywords matching the ticked keep rules in the selected field will be kept.")
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
        keep_upper, keep_country = self.keep_upper_var.get(), self.keep_country_var.get()
        if not (keep_upper or keep_country):
            messagebox.showwarning("No keep rule", "Tick at least one rule for keywords to keep.")
            return
        output = self.df.copy()
        preview_rows, kept_total, removed_total, changed_records = [], 0, 0, 0
        headings = ["Record", "Original keywords", "Kept keywords", "Removed keywords"]
        for column_id, heading in zip(self.table.tree["columns"], headings):
            self.table.tree.heading(column_id, text=heading)
        for index, value in output[column].items():
            original = "" if pd.isna(value) else str(value).strip()
            cleaned, kept, removed = core.clean_tags(
                original, self.delimiter.get(), keep_uppercase=keep_upper, keep_countries=keep_country)
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
            f"Preview ready: {kept_total} keywords kept; {removed_total} keywords removed "
            f"from {changed_records} of {len(output)} records.")

    def on_export(self):
        if self.output_df is None:
            return
        _save_enriched_dataframe(self, self.output_df, self.file_path, "keywords_cleaned",
                                 format_label=self.output_format.get(), patch_ris=True)


# ---------------------------------------------------------------------------
# Manual review: inspect an existing result file without running APIs
# ---------------------------------------------------------------------------

_WHOLE_NUMBER_TEXT_RE = re.compile(r"^-?\d+\.0+$")


class ManualReviewPage(ctk.CTkFrame):
    """A compact, configurable review view over an already-generated file."""
    PAGE_SIZE = 100
    EXPORT_FORMATS = {label: value for label, value in core.OUTPUT_FORMATS.items()
                      if value[0] in {"csv", "excel", "ris", "bibtex"}}
    ALL_ROWS, FILTERED_ROWS = "All", "Filtered"
    ALL_COLUMNS, TICKED_COLUMNS = "All", "Ticked only"
    SELECTED_SCOPE, FILTERED_SCOPE, ALL_SCOPE = "Selected record", "Filtered records", "All records"
    REVIEW_COLUMNS = ("Manual Decision", "Manual Notes")
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
        self.page_size = self.PAGE_SIZE

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(6, 8))
        ctk.CTkButton(top, text="Choose existing file…", width=155,
                      command=self.on_choose_file).pack(side="left")
        self.file_label = ctk.CTkLabel(top, text="No file loaded — this page does not call any API", anchor="w")
        self.file_label.pack(side="left", padx=12, fill="x", expand=True)

        # Save / convert (this page absorbed the former File Converter). RIS
        # and BibTeX have no tag for free-form columns (Manual Decision,
        # verification metadata, ...); those travel in the record Note and
        # come back as columns when this app reads the file again. CSL JSON
        # has no such carrier, so it isn't offered here.
        export_row = ctk.CTkFrame(self)
        export_row.pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(export_row, text="Save as", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=(10, 8), pady=8)
        self.output_format = ctk.CTkOptionMenu(
            export_row, values=list(self.EXPORT_FORMATS), width=160,
            command=lambda _value: self._update_export_summary())
        self.output_format.set("CSV table (.csv)")
        self.output_format.pack(side="left", padx=(0, 14))
        ctk.CTkLabel(export_row, text="Records").pack(side="left", padx=(0, 6))
        self.export_rows = ctk.CTkSegmentedButton(
            export_row, values=[self.ALL_ROWS, self.FILTERED_ROWS],
            command=lambda _value: self._update_export_summary())
        self.export_rows.set(self.ALL_ROWS)
        self.export_rows.pack(side="left", padx=(0, 14))
        ctk.CTkLabel(export_row, text="Columns").pack(side="left", padx=(0, 6))
        self.export_columns = ctk.CTkSegmentedButton(
            export_row, values=[self.ALL_COLUMNS, self.TICKED_COLUMNS],
            command=lambda _value: self._update_export_summary())
        self.export_columns.set(self.ALL_COLUMNS)
        self.export_columns.pack(side="left", padx=(0, 14))
        self.save_btn = ctk.CTkButton(export_row, text="Save / convert…", width=140,
                                      command=self.on_export, state="disabled")
        self.save_btn.pack(side="left")
        self.export_summary = ctk.CTkLabel(
            export_row, text="", anchor="w", justify="left", text_color=("gray30", "gray70"),
            font=ctk.CTkFont(size=12))
        self.export_summary.pack(side="left", padx=12, fill="x", expand=True)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=4)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(body, width=225)
        sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        sidebar.grid_propagate(False)
        ctk.CTkLabel(sidebar, text="Columns", font=ctk.CTkFont(weight="bold")) \
            .pack(anchor="w", padx=10, pady=(10, 0))
        ctk.CTkLabel(sidebar, text="Ticked columns are shown in the table, and are the ones kept "
                                   "when saving with Columns = Ticked only.",
                     text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11), anchor="w",
                     justify="left", wraplength=200).pack(fill="x", padx=10, pady=(0, 4))
        tick_row = ctk.CTkFrame(sidebar, fg_color="transparent")
        tick_row.pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkButton(tick_row, text="All", width=95, command=lambda: self._set_all_columns(True)) \
            .pack(side="left", padx=(0, 4))
        ctk.CTkButton(tick_row, text="None", width=95, command=lambda: self._set_all_columns(False)) \
            .pack(side="left")
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
            filters, values=[""], width=200, dropdown_rows=9)
        self.filter_value.grid(row=1, column=2, padx=5, pady=(0, 7), sticky="w")
        self.filter_value_to = ctk.CTkEntry(filters, width=95, placeholder_text="Upper value")
        self.filter_value_to.grid(row=1, column=3, padx=5, pady=(0, 7))
        ctk.CTkButton(filters, text="Add another", width=110, command=self.add_filter_condition) \
            .grid(row=1, column=4, padx=5, pady=(0, 7))
        ctk.CTkButton(filters, text="Apply", width=70, command=self.apply_filters) \
            .grid(row=1, column=5, padx=5, pady=(0, 7))
        ctk.CTkButton(filters, text="Clear", width=65, command=self.clear_filters) \
            .grid(row=1, column=6, padx=(5, 10), pady=(0, 7))
        ctk.CTkLabel(filters, text="Combine conditions with").grid(
            row=0, column=1, columnspan=2, sticky="e", padx=(0, 6), pady=(7, 2))
        self.filter_mode = ctk.CTkSegmentedButton(filters, values=["AND", "OR"], width=120)
        self.filter_mode.set("AND")
        self.filter_mode.grid(row=0, column=3, columnspan=2, sticky="w", padx=5, pady=(7, 2))
        self.filter_summary_box = ctk.CTkTextbox(
            filters, height=52, wrap="word", activate_scrollbars=True,
            font=ctk.CTkFont(size=12), text_color=("gray30", "gray70"))
        self.filter_summary_box.grid(row=2, column=0, columnspan=8, sticky="ew", padx=10, pady=(0, 7))
        _set_readonly_text(
            self.filter_summary_box,
            "Choose a condition and click Apply. Use 'Add another' to combine several conditions with AND or OR.")
        filters.grid_columnconfigure(7, weight=1)

        # Copy one column's values into another, e.g. accept the module tags
        # of "ISSP Tags (low)" into Keywords for the records you checked.
        add_row = ctk.CTkFrame(main)
        add_row.pack(fill="x", pady=(0, 7))
        ctk.CTkLabel(add_row, text="Add values of", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=(10, 6), pady=7)
        self.add_source = _make_searchable_combobox(add_row, values=[NO_COLUMN], width=190)
        self.add_source.pack(side="left", pady=7)
        ctk.CTkLabel(add_row, text="to").pack(side="left", padx=6)
        self.add_target = _make_searchable_combobox(add_row, values=[NO_COLUMN], width=190)
        self.add_target.pack(side="left", pady=7)
        ctk.CTkLabel(add_row, text="for").pack(side="left", padx=6)
        self.add_scope = ctk.CTkSegmentedButton(
            add_row, values=[self.SELECTED_SCOPE, self.FILTERED_SCOPE, self.ALL_SCOPE])
        self.add_scope.set(self.SELECTED_SCOPE)
        self.add_scope.pack(side="left")
        ctk.CTkButton(add_row, text="Add", width=70, command=self.on_add_column_values).pack(
            side="left", padx=8)
        ctk.CTkLabel(add_row, text="Items already in the target are skipped; nothing is removed.",
                     text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11)).pack(side="left", padx=4)
        nav = ctk.CTkFrame(main, fg_color="transparent")
        nav.pack(fill="x", pady=(0, 5))
        self.first_btn = ctk.CTkButton(nav, text="«", width=36, command=lambda: self.go_to_page(0), state="disabled")
        self.first_btn.pack(side="left")
        self.prev_btn = ctk.CTkButton(nav, text="Previous", width=85, command=lambda: self.change_page(-1), state="disabled")
        self.prev_btn.pack(side="left", padx=(6, 0))
        self.next_btn = ctk.CTkButton(nav, text="Next", width=70, command=lambda: self.change_page(1), state="disabled")
        self.next_btn.pack(side="left", padx=6)
        self.last_btn = ctk.CTkButton(nav, text="»", width=36, command=lambda: self.go_to_page(10 ** 9), state="disabled")
        self.last_btn.pack(side="left")
        self.page_label = ctk.CTkLabel(nav, text="Choose a file to begin")
        self.page_label.pack(side="left", padx=8)
        ctk.CTkLabel(nav, text="Go to page").pack(side="left", padx=(10, 4))
        self.page_entry = ctk.CTkEntry(nav, width=55)
        self.page_entry.pack(side="left")
        self.page_entry.bind("<Return>", lambda _e: self.jump_to_entered_page())
        ctk.CTkLabel(nav, text="Rows per page").pack(side="left", padx=(12, 4))
        self.page_size_menu = ctk.CTkOptionMenu(
            nav, values=["50", "100", "250", "500", "1000"], width=80,
            command=self.on_page_size_change)
        self.page_size_menu.set(str(self.page_size))
        self.page_size_menu.pack(side="left")
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
            title="Choose a file to review or convert",
            filetypes=[("Supported files", "*.csv *.xlsx *.xls *.json *.ris *.bib *.bibtex"),
                       ("CSV", "*.csv"), ("Excel", "*.xlsx *.xls"), ("RIS", "*.ris"),
                       ("BibTeX", "*.bib *.bibtex"), ("CSL JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            # Literal cell text, so a converted file keeps 12 rather than 12.0.
            df = core.read_records_file(path, as_text=True).reset_index(drop=True)
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
        self.filter_mode.set("AND")
        encoding = df.attrs.get("source_encoding")
        encoding_note = f" · {encoding}" if encoding else ""
        self.file_label.configure(text=f"{os.path.basename(path)} ({len(df)} records){encoding_note}")
        self.save_btn.configure(state="normal")
        self.review_btn.configure(state="normal")
        # Offer a different format than the source, as a converter would.
        source_label = core.preferred_output_format_label(path)
        if source_label in self.EXPORT_FORMATS:
            self.output_format.set(source_label)
        self._build_column_choices()
        _update_combobox_values(self.filter_column, list(df.columns))
        self._refresh_add_columns(initial=True)
        self.filter_column.set(str(df.columns[0]))
        self._on_filter_column_change(str(df.columns[0]))
        _set_readonly_text(
            self.filter_summary_box,
            "Choose a condition and click Apply. Use 'Add another' to combine several conditions with AND or OR.")
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
            _set_readonly_text(self.filter_summary_box, "No filters applied.")
            return
        parts = []
        for column, operator, value, upper in self.filter_conditions:
            expression = f"{column} {operator} {value}".strip()
            parts.append(expression + (f" and {upper}" if operator == "between" else ""))
        prefix = f"Showing {len(self.filtered_indices):,}/{len(self.df):,} records | " if applied else "Pending | "
        _set_readonly_text(self.filter_summary_box, prefix + f" {self.filter_mode.get()} ".join(parts))

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
        use_or = self.filter_mode.get() == "OR"
        mask = pd.Series(not use_or, index=self.df.index)
        try:
            for column, operator, value, upper in self.filter_conditions:
                series = self.df[column]
                if self._is_numeric_column(column):
                    numeric, target = pd.to_numeric(series, errors="coerce"), float(value)
                    operations = {">": numeric > target, ">=": numeric >= target, "<": numeric < target,
                                  "<=": numeric <= target, "=": numeric == target, "!=": numeric != target}
                    result = numeric.between(target, float(upper), inclusive="both") \
                        if operator == "between" else operations[operator]
                    result &= numeric.notna()
                else:
                    text = series.fillna("").astype(str).str.strip()
                    folded, target = text.str.casefold(), value.casefold()
                    operations = {"equals": folded == target, "not equals": folded != target,
                                  "contains": folded.str.contains(re.escape(target), na=False),
                                  "does not contain": ~folded.str.contains(re.escape(target), na=False),
                                  "is blank": text.eq(""), "is not blank": text.ne("")}
                    result = operations[operator]
                result = result.fillna(False)
                mask = (mask | result) if use_or else (mask & result)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Invalid filter", f"The filter could not be applied:\n{exc}")
            return
        self.filtered_indices = self.df.index[mask].tolist()
        if self.filter_conditions:
            self.export_rows.set(self.FILTERED_ROWS)  # saving usually means "what I'm looking at"
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
        self.export_rows.set(self.ALL_ROWS)
        self.page, self.selected_df_index = 0, None
        self.apply_btn.configure(state="disabled")
        _set_readonly_text(
            self.filter_summary_box,
            "Choose a condition and click Apply. Use 'Add another' to combine several conditions with AND or OR.")
        self.render_page()

    def _build_column_choices(self, selected=None):
        for child in self.column_box.winfo_children():
            child.destroy()
        self.column_vars = {}
        defaults = set(selected) if selected is not None else set()
        columns = list(self.df.columns)
        if selected is None:
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
        # Blank cells make pandas read whole-number columns (years, volumes)
        # as floats; show 1999.0 as 1999.
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        text = str(value)
        if _WHOLE_NUMBER_TEXT_RE.match(text):
            return text[:text.index(".")]
        return text

    @staticmethod
    def _doi_link(value):
        value = str(value).strip()
        if value.lower().startswith(("http://", "https://")):
            return value
        return "https://doi.org/" + value.removeprefix("doi:").strip()

    def selected_columns(self):
        return [column for column, var in self.column_vars.items() if var.get()]

    def _set_all_columns(self, selected):
        for var in self.column_vars.values():
            var.set(selected)
        self.render_page()

    def render_page(self):
        if self.df is None:
            return
        self._update_export_summary()
        columns = self.selected_columns()
        if not columns:
            self.page_label.configure(text="Select at least one display column")
            return
        if self.table is not None:
            self.table.destroy()
        active_indices = self.filtered_indices if self.filtered_indices or self.filter_conditions else list(self.df.index)
        total = len(active_indices)
        last_page = max(0, (total - 1) // self.page_size)
        self.page = min(max(self.page, 0), last_page)
        start = self.page * self.page_size
        end = min(total, start + self.page_size)
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
        pages = last_page + 1
        first = start + 1 if total else 0
        self.page_label.configure(text=f"Rows {first}-{end} of {total} | Page {self.page + 1}/{pages}")
        has_prev, has_next = self.page > 0, end < total
        self.first_btn.configure(state="normal" if has_prev else "disabled")
        self.prev_btn.configure(state="normal" if has_prev else "disabled")
        self.next_btn.configure(state="normal" if has_next else "disabled")
        self.last_btn.configure(state="normal" if has_next else "disabled")
        self.page_entry.delete(0, "end")
        self.page_entry.insert(0, str(self.page + 1))

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

    def _refresh_add_columns(self, initial=False):
        columns = list(self.df.columns)
        _update_combobox_values(self.add_source, columns)
        _update_combobox_values(self.add_target, columns)
        if initial or self.add_source.get() not in columns:
            low = issp_tags.CONFIDENCE_TAG_COLUMNS["low"]
            self.add_source.set(low if low in columns else NO_COLUMN)
        if initial or self.add_target.get() not in columns:
            self.add_target.set(core.guess_column(columns, core.TAG_ALIASES) or NO_COLUMN)

    def on_add_column_values(self):
        """Add one column's items to another for the selected, filtered or all records."""
        if self.df is None:
            return
        source, target = self.add_source.get().strip(), self.add_target.get().strip()
        if source not in self.df.columns:
            messagebox.showwarning("Choose a column", "Pick the column whose values should be added.")
            return
        if not target or target == NO_COLUMN or target == source:
            messagebox.showwarning("Choose a target column",
                                   "Pick (or type the name of) a different column to add the values to.")
            return
        self._store_selected()
        scope = self.add_scope.get()
        if scope == self.SELECTED_SCOPE:
            if self.selected_df_index is None:
                messagebox.showinfo("Select a record", "Click a record in the table first.")
                return
            indices = [self.selected_df_index]
        elif scope == self.FILTERED_SCOPE:
            indices = self.filtered_indices if self.filter_conditions else list(self.df.index)
        else:
            indices = list(self.df.index)
        with_values = [i for i in indices if convert_tools.split_values(self.df.at[i, source])]
        if not with_values:
            messagebox.showinfo("Nothing to add", f"“{source}” is empty for the chosen records.")
            return
        if len(indices) > 1 and not messagebox.askyesno(
                "Add values",
                f"Add the values of “{source}” to “{target}” for {len(with_values):,} record(s)?\n"
                "Values already in the target are skipped; nothing is removed."):
            return
        is_new = target not in self.df.columns
        changed = convert_tools.add_column_values(self.df, indices, source, target)
        if is_new:
            self.register_columns_changed(select=target)
        saved_index = self.selected_df_index
        self.render_page()
        if saved_index is not None and saved_index in self.page_indices:
            self.select_row(self.page_indices.index(saved_index))
            self.table.tree.selection_set(str(self.page_indices.index(saved_index)))
        self.page_label.configure(
            text=self.page_label.cget("text") + f" | Added “{source}” to “{target}” in {changed:,} record(s)")

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
        self.register_columns_changed(select=name)
        self.render_page()
        if self.selected_df_index is not None:
            self._render_custom_editor()

    def register_columns_changed(self, select=None):
        """Refresh the column chooser and filter list after columns were added,
        keeping the reviewer's current display selection."""
        chosen = set(self.selected_columns())
        if select:
            chosen.add(select)
        self._build_column_choices(selected=chosen)
        _update_combobox_values(self.filter_column, list(self.df.columns))
        self._refresh_add_columns()

    def change_page(self, delta):
        self.go_to_page(self.page + delta)

    def go_to_page(self, page):
        if self.df is None:
            return
        self._store_selected()
        self.page = max(0, page)  # render_page clamps to the last page
        self.selected_df_index = None
        self.apply_btn.configure(state="disabled")
        self.render_page()

    def jump_to_entered_page(self):
        try:
            page = int(self.page_entry.get().strip())
        except ValueError:
            return
        self.go_to_page(page - 1)

    def on_page_size_change(self, value):
        self.page_size = int(value)
        self.go_to_page(0)

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

    def _export_frame(self):
        """The records and columns the Save / convert settings select."""
        if self.export_rows.get() == self.FILTERED_ROWS and self.filter_conditions:
            indices = self.filtered_indices
        else:
            indices = list(self.df.index)
        if self.export_columns.get() == self.TICKED_COLUMNS:
            columns = self.selected_columns()
        else:
            columns = list(self.df.columns)
        frame = self.df.loc[indices, columns]
        # Review columns are added to every loaded file; leave them out of a
        # plain conversion where nobody reviewed anything.
        unused = [c for c in self.REVIEW_COLUMNS
                  if c in frame.columns and not frame[c].map(self._display_value).str.strip().any()]
        return frame.drop(columns=unused).reset_index(drop=True)

    def _update_export_summary(self):
        if self.df is None or not hasattr(self, "export_summary"):
            return
        frame = self._export_frame()
        fmt, _ext = self.EXPORT_FORMATS[self.output_format.get()]
        text = f"{len(frame):,} of {len(self.df):,} records × {len(frame.columns)} columns"
        if self.export_rows.get() == self.FILTERED_ROWS and not self.filter_conditions:
            text += " (no filter applied)"
        portable = convert_tools.portable_columns_for(list(frame.columns), fmt)
        if portable:
            text += f" · {len(portable)} kept in the record Note"
        self.export_summary.configure(text=text)

    def on_export(self):
        if self.df is None:
            return
        self._store_selected()
        frame = self._export_frame()
        if frame.empty or not len(frame.columns):
            messagebox.showwarning("Nothing to save",
                                   "The current record and column choices select nothing to save.")
            return
        label = self.output_format.get()
        fmt, ext = self.EXPORT_FORMATS[label]
        base = os.path.splitext(os.path.basename(self.file_path or "records"))[0]
        suffix = "filtered" if len(frame) < len(self.df) else "reviewed"
        path = filedialog.asksaveasfilename(
            title="Save / convert records", defaultextension=ext,
            filetypes=[(label, f"*{ext}")], initialfile=f"{base}_{suffix}{ext}")
        if not path:
            return
        # A typed extension wins over the menu so the content matches the name.
        typed = os.path.splitext(path)[1].lower().replace(".bibtex", ".bib").replace(".xls", ".xlsx")
        fmt = next((f for f, e in self.EXPORT_FORMATS.values() if e == typed), fmt)
        portable = convert_tools.portable_columns_for(list(frame.columns), fmt)
        keep_notes = _ask_note_columns(self, portable, fmt)
        if keep_notes is None:
            return
        if not keep_notes:
            frame, portable = frame.drop(columns=portable), []
        try:
            core.write_records_file(frame, path, fmt, portable_columns=portable)
        except Exception as exc:
            messagebox.showerror("Save failed", f"Couldn't save the file:\n{exc}")
            return
        extra = (f"\n{len(portable)} column(s) without a native tag were stored in each record's "
                 "Note and are restored when this app opens the file." if portable else "")
        messagebox.showinfo("Saved", f"Saved {len(frame):,} records × {len(frame.columns)} columns to:"
                                     f"\n{path}{extra}")


class _EditorText(tk.Text):
    """A wrapping text editor drawn like a CTkEntry.

    Plain Tk on purpose: a CTkTextbox redraws its (even hidden) scrollbars
    with update_idletasks() whenever its parent frame is re-coloured, so
    re-colouring the review rows (toggling the comparison) took seconds.
    Text past the visible lines scrolls with the mouse wheel."""

    def __init__(self, master):
        # Same colours as the theme's CTkEntry, so both kinds of field match.
        mode = 1 if ctk.get_appearance_mode() == "Dark" else 0
        entry_theme = ctk.ThemeManager.theme["CTkEntry"]
        pick = lambda value: value[mode] if isinstance(value, (tuple, list)) else value
        self._colors = {
            "bg": pick(entry_theme["fg_color"]),
            "fg": pick(entry_theme["text_color"]),
            "border": pick(entry_theme["border_color"]),
            "focus": "#1F6AA5" if mode else "#0F6CBD",
        }
        scale = ctk.ScalingTracker.get_widget_scaling(master) or 1
        family = ctk.CTkFont().cget("family")
        super().__init__(
            master, height=1, wrap="word", relief="flat", borderwidth=0,
            font=tkfont.Font(family=family, size=-round(13 * scale)),
            padx=round(7 * scale), pady=round(5 * scale),
            background=self._colors["bg"], foreground=self._colors["fg"],
            insertbackground=self._colors["fg"], highlightthickness=max(1, round(scale)),
            highlightbackground=self._colors["border"], highlightcolor=self._colors["focus"])

    def configure(self, cnf=None, **kwargs):
        # Accept CustomTkinter's text_color=(light, dark) like the entries do.
        text_color = kwargs.pop("text_color", None)
        if text_color is not None:
            if isinstance(text_color, (tuple, list)):
                text_color = text_color[1] if ctk.get_appearance_mode() == "Dark" else text_color[0]
            kwargs["foreground"] = text_color
        return super().configure(cnf, **kwargs)

    config = configure


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
        self.show_compare_var = ctk.BooleanVar(value=True)
        # Fields open read-only so browsing can't change data by accident.
        self.edit_mode_var = ctk.BooleanVar(value=False)
        self.field_entries = {}   # field label -> (widget getter, column name or None, canonical new-column name)
        self.other_editors = {}   # column name -> getter for the "All fields" view
        self.input_fallbacks = {}  # field label -> lookup value shown because the record's own cell is empty
        self._all_field_widgets = {}  # column -> (label, editor, is_textbox), reused between records
        self._comparison_rows = {}  # field label -> widgets of its key-field row, reused between records

        header = ctk.CTkFrame(self)
        header.pack(fill="x", padx=10, pady=(10, 6))
        self.record_label = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=16, weight="bold"))
        self.record_label.pack(side="left", padx=10, pady=8)
        self.compare_switch = ctk.CTkSwitch(
            header, text="Show comparison", variable=self.show_compare_var,
            command=self._on_view_change)
        self.compare_switch.pack(side="left", padx=(16, 8))
        self.view_toggle = ctk.CTkSegmentedButton(
            header, values=["Key fields", "All fields"], command=lambda _v: self._on_view_change())
        self.view_toggle.set("Key fields")
        self.view_toggle.pack(side="left", padx=8)
        self.edit_switch = ctk.CTkSwitch(
            header, text="Edit fields: OFF (view only)", variable=self.edit_mode_var,
            command=self._on_view_change)
        self.edit_switch.pack(side="left", padx=(16, 8))
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
        self.headings = headings
        ctk.CTkLabel(headings, text="Field", width=145, anchor="w",
                     font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w")
        self.input_heading = ctk.CTkLabel(
            headings, text="Input record", anchor="w", font=ctk.CTkFont(weight="bold"))
        self.input_heading.grid(row=0, column=1, sticky="w", padx=5)
        self.retrieved_heading = ctk.CTkLabel(
            headings, text="Retrieved metadata (from this file)", anchor="w",
            font=ctk.CTkFont(weight="bold"))
        self.retrieved_heading.grid(row=0, column=3, sticky="w", padx=5)

        self.comparison_frame = ctk.CTkScrollableFrame(self, height=300)
        # "uniform" keeps both sides equally wide; otherwise the retrieved
        # column's width-following wrap slowly squeezes the input column.
        self.comparison_frame.grid_columnconfigure(1, weight=1, uniform="sides")
        self.comparison_frame.grid_columnconfigure(3, weight=1, uniform="sides")
        self.all_fields_frame = ctk.CTkScrollableFrame(self, height=300)
        self.all_fields_frame.grid_columnconfigure(1, weight=1)

        # Compact: label beside the text, and the box only as tall as the
        # message (one line for "The URL returned HTTP 403", up to five).
        message_frame = ctk.CTkFrame(self)
        ctk.CTkLabel(message_frame, text="Verification\nmessage", justify="left", anchor="nw",
                     font=ctk.CTkFont(weight="bold")).pack(side="left", anchor="n", padx=(10, 6), pady=7)
        self.message_box = ctk.CTkTextbox(message_frame, height=34, wrap="word", font=ctk.CTkFont(size=13))
        self.message_box.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=6)

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

        self._apply_view_layout()
        self.set_queue(queue_indices, start_index)
        self.after(80, self.lift)

    def set_queue(self, queue_indices, start_index=None):
        self._commit_fields()
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

    def _on_view_change(self):
        # Keep typed edits when switching layouts, then redraw the record.
        self._commit_fields()
        self._apply_view_layout()
        self._load_record()

    def _apply_view_layout(self):
        show_compare = bool(self.show_compare_var.get())
        all_fields = self.view_toggle.get() == "All fields"
        if show_compare and not all_fields:
            self.retrieved_heading.grid()
        else:
            self.retrieved_heading.grid_remove()
        self.comparison_frame.grid_columnconfigure(
            3, weight=1 if show_compare else 0, uniform="sides" if show_compare else "")
        self.comparison_frame.grid_columnconfigure(2, minsize=105 if show_compare else 0)
        editable = bool(self.edit_mode_var.get())
        mode = "editable" if editable else "view only"
        self.edit_switch.configure(
            text="Edit fields: ON" if editable else "Edit fields: OFF (view only)")
        if all_fields:
            self.input_heading.configure(text=f"Value ({mode})")
            self.comparison_frame.pack_forget()
            self.all_fields_frame.pack(side="top", fill="both", expand=True, padx=10, pady=(3, 6))
        else:
            self.input_heading.configure(text=f"Input record ({mode})")
            self.all_fields_frame.pack_forget()
            self.comparison_frame.pack(side="top", fill="both", expand=True, padx=10, pady=(3, 6))

    @staticmethod
    def _needs_textbox(value):
        # Anything longer than roughly one line of the field width wraps in a
        # textbox; a single-line entry would hide the rest of the value.
        return len(value) > 45 or "\n" in value

    EDITOR_LINE_HEIGHT = 20
    EDITOR_MAX_LINES = 10  # longer values (abstracts) scroll inside the box with the mouse wheel

    def _fit_editor_height(self, widget, value=None):
        """Size a textbox editor to its wrapped content, estimated from the
        text length and the widget's current width (no layout pass needed)."""
        if not isinstance(widget, _EditorText):
            return
        if value is None:
            value = widget.get("1.0", "end-1c")
        lines = None
        if widget.winfo_width() > 1:
            # Laid out: let the text widget count its own wrapped lines.
            try:
                counted = widget.count("1.0", "end", "update", "displaylines")
                lines = counted[0] if isinstance(counted, (tuple, list)) else counted
            except Exception:
                lines = None
        if not lines:
            # Not shown yet: estimate; <Configure> re-measures once it is.
            parent_width = widget.master.winfo_width()
            width = max(300, parent_width - 130) if parent_width > 1 else 420
            chars_per_line = max(20, int((width - 20) / 9))
            lines = sum(max(1, math.ceil(len(part) / chars_per_line)) for part in value.split("\n"))
        height = min(self.EDITOR_MAX_LINES, max(1, lines))  # tk.Text height is in lines
        if int(widget.cget("height")) != height:
            widget.configure(height=height)

    def _refit_on_resize(self, widget):
        """Re-estimate a textbox's height when its width changes."""
        state = {"width": 0, "pending": None}

        def refit():
            state["pending"] = None
            if widget.winfo_exists():
                self._fit_editor_height(widget)

        def on_configure(event):
            # Deferred and coalesced: refitting inside the event resized the
            # box, which fired more <Configure>s - toggling the comparison
            # (half -> full width) cascaded into seconds of re-measuring.
            if abs(event.width - state["width"]) > 20:
                state["width"] = event.width
                if state["pending"] is not None:
                    widget.after_cancel(state["pending"])
                state["pending"] = widget.after(80, refit)
        widget.bind("<Configure>", on_configure, add="+")

    def _set_editor_value(self, widget, value):
        """Replace an editor's text and apply the current edit/view state."""
        editable = bool(self.edit_mode_var.get())
        widget.configure(state="normal")
        if isinstance(widget, _EditorText):
            widget.delete("1.0", "end")
            widget.insert("1.0", value)
            self._fit_editor_height(widget, value)
            if not editable:
                widget.configure(state="disabled")
        else:
            widget.delete(0, "end")
            widget.insert(0, value)
            if not editable:
                widget.configure(state="readonly")

    def _make_editor(self, parent, value, width=None, grid=None):
        """A single-line entry, or a small textbox for long/multi-line values.
        Returns (widget, getter). Outside edit mode the widget is read-only
        (text can still be selected and copied)."""
        if self._needs_textbox(value):
            widget = _EditorText(parent)
            self._refit_on_resize(widget)
            getter = lambda w=widget: w.get("1.0", "end-1c").strip()
        else:
            widget = ctk.CTkEntry(parent) if width is None else ctk.CTkEntry(parent, width=width)
            getter = lambda w=widget: w.get().strip()
        self._set_editor_value(widget, value)
        return widget, getter

    def _add_comparison_row(self, row_number, field, input_value, retrieved_value,
                            column=None, new_column_name=None, retrieved_column="",
                            input_fallback_column=""):
        """Show one key field. The row's widgets are built on first use and
        afterwards only updated in place: destroying and recreating them for
        every record made moving between records take seconds."""
        show_compare = bool(self.show_compare_var.get())
        if show_compare:
            category, explanation = self._compare(field, input_value, retrieved_value)
        else:
            category, explanation = "missing", ""
        color = self._row_color(category)
        widgets = self._comparison_rows.get(field)
        if widgets is None:
            widgets = self._build_comparison_row(row_number, field)

        # Input side. Swap entry/textbox only when the value's length needs it.
        holder = widgets["holder"]
        # Without the comparison every field keeps the neutral grey card, so
        # both views look alike (category is "missing" when not comparing).
        holder.configure(fg_color=color)
        if widgets["is_textbox"] != self._needs_textbox(input_value):
            widgets["editor"].destroy()
            widgets["editor"], _getter = self._make_editor(holder, input_value)
            widgets["editor"].grid(row=0, column=0, sticky="ew", padx=(7, 2), pady=6)
            widgets["is_textbox"] = self._needs_textbox(input_value)
        else:
            self._set_editor_value(widgets["editor"], input_value)
        widgets["editor"].configure(
            text_color=(("gray40", "gray65") if input_fallback_column
                        else ctk.ThemeManager.theme["CTkEntry"]["text_color"]))
        self.field_entries[field] = (widgets["getter"], column, new_column_name)
        if input_fallback_column:
            self.input_fallbacks[field] = input_value
            widgets["fallback_label"].configure(
                text=f"empty in record · showing lookup column: {input_fallback_column}")
            widgets["fallback_label"].grid()
        else:
            widgets["fallback_label"].grid_remove()

        # Retrieved side.
        if not show_compare:
            widgets["ret_holder"].grid_remove()
            widgets["symbol"].grid_remove()
            return
        widgets["ret_holder"].grid()
        widgets["symbol"].grid()
        widgets["ret_holder"].configure(fg_color=color)
        widgets["retrieved"] = retrieved_value
        direct_url = self._clickable_url(field, retrieved_value) if field in {"DOI", "URL"} else ""
        widgets["retrieved_url"] = direct_url
        widgets["value_label"].configure(
            text=retrieved_value or "(not available)",
            text_color=(("#1261a0", "#69b7ff") if direct_url
                        else ctk.ThemeManager.theme["CTkLabel"]["text_color"]),
            cursor="hand2" if direct_url else "")
        if retrieved_column:
            widgets["source_label"].configure(text=f"from column: {retrieved_column}")
            widgets["source_label"].grid()
        else:
            widgets["source_label"].grid_remove()
        symbol = {"match": "✓", "warning": "!", "different": "×", "missing": "—"}[category]
        widgets["symbol"].configure(text=f"{symbol}\n{explanation}")

    def _build_comparison_row(self, row_number, field):
        widgets = {"retrieved": "", "retrieved_url": ""}
        ctk.CTkLabel(self.comparison_frame, text=field, width=140, anchor="w", justify="left",
                     wraplength=135, font=ctk.CTkFont(weight="bold")).grid(
                         row=row_number, column=0, sticky="nw", padx=6, pady=4)
        holder = ctk.CTkFrame(self.comparison_frame)
        holder.grid(row=row_number, column=1, sticky="nsew", padx=4, pady=3)
        holder.grid_columnconfigure(0, weight=1)
        widgets["holder"] = holder
        widgets["editor"], _getter = self._make_editor(holder, "")
        widgets["editor"].grid(row=0, column=0, sticky="ew", padx=(7, 2), pady=6)
        widgets["is_textbox"] = False
        getter = lambda: (widgets["editor"].get("1.0", "end-1c").strip() if widgets["is_textbox"]
                          else widgets["editor"].get().strip())
        widgets["getter"] = getter
        widgets["fallback_label"] = ctk.CTkLabel(
            holder, text="", anchor="w", font=ctk.CTkFont(size=11), text_color=("#8a5a00", "#e0b060"))
        widgets["fallback_label"].grid(row=1, column=0, columnspan=3, sticky="w", padx=7, pady=(0, 4))
        if field in {"DOI", "URL"}:
            def open_input():
                url = self._clickable_url(field, getter())
                if url:
                    webbrowser.open(url)
            ctk.CTkButton(holder, text="Open", width=48, height=24, command=open_input).grid(
                row=0, column=1, padx=(2, 2), pady=4)
        ctk.CTkButton(holder, text="Copy", width=48, height=24,
                      command=lambda: self._copy_value(getter())).grid(
                          row=0, column=2, padx=(2, 5), pady=4)

        ret_holder = ctk.CTkFrame(self.comparison_frame)
        ret_holder.grid(row=row_number, column=3, sticky="nsew", padx=4, pady=3)
        ret_holder.grid_columnconfigure(0, weight=1)
        widgets["ret_holder"] = ret_holder
        value_label = ctk.CTkLabel(ret_holder, text="", anchor="w", justify="left", wraplength=390)
        value_label.grid(row=0, column=0, sticky="ew", padx=7, pady=6)
        wrap_state = {"width": 0}

        def rewrap(event):
            # Follow the column width instead of a fixed wrap, so long values
            # use the space available and nothing runs off the edge.
            if abs(event.width - wrap_state["width"]) > 20:
                wrap_state["width"] = event.width
                # wraplength is in unscaled units; event.width is in screen pixels.
                scale = ctk.ScalingTracker.get_widget_scaling(value_label) or 1
                value_label.configure(wraplength=max(150, int(event.width / scale) - 90))
        ret_holder.bind("<Configure>", rewrap, add="+")
        value_label.bind("<Button-1>", lambda _event: widgets["retrieved_url"]
                         and webbrowser.open(widgets["retrieved_url"]))
        widgets["value_label"] = value_label
        ctk.CTkButton(ret_holder, text="Copy", width=48, height=24,
                      command=lambda: self._copy_value(widgets["retrieved"])).grid(
                          row=0, column=1, padx=(2, 5), pady=4)
        widgets["source_label"] = ctk.CTkLabel(
            ret_holder, text="", anchor="w", font=ctk.CTkFont(size=11), text_color=("gray35", "gray65"))
        widgets["source_label"].grid(row=1, column=0, columnspan=2, sticky="w", padx=7, pady=(0, 4))
        widgets["symbol"] = ctk.CTkLabel(self.comparison_frame, text="", width=105,
                                         justify="center", text_color=("gray20", "gray80"))
        widgets["symbol"].grid(row=row_number, column=2, sticky="nsew", padx=3, pady=4)
        self._comparison_rows[field] = widgets
        return widgets

    # Key fields: label, input aliases, name used when the column must be created.
    EDITABLE_FIELDS = [
        ("Title", core.TITLE_ALIASES, "Title"),
        ("Authors", core.AUTHOR_ALIASES, "Author"),
        ("Publication year", core.YEAR_ALIASES, "Publication Year"),
        ("Item type", core.ITEM_TYPE_ALIASES, "Item Type"),
        ("Publisher", core.PUBLISHER_ALIASES, "Publisher"),
        ("Publication / container", core.JOURNAL_ALIASES, "Publication Title"),
        ("DOI", core.DOI_ALIASES, "DOI"),
        ("URL", core.URL_ALIASES, "Url"),
        ("Abstract", abstract_tools.ABSTRACT_ALIASES, "Abstract Note"),
    ]
    # Matched by exact column name only: a substring guess would pick up
    # Abstract Finder's evidence columns ("Abstract Source", ...).
    EXACT_NAME_FIELDS = {"Abstract"}

    # Columns already in the loaded file that hold looked-up / verified
    # values, in priority order. Verification output comes first, then the
    # lookup step's own columns (lowercase doi/url/matched_title from the
    # lookup scripts, Matched Title/Link from Batch Lookup), then Abstract
    # Finder's page metadata. Nothing is fetched from the network here.
    RETRIEVED_COLUMNS = {
        "Title": ("Verification Metadata Combined Title", "Verification Metadata Title",
                  "Matched Title", "matched_title", "Abstract Page Title"),
        "Authors": ("Verification Metadata Authors", "Abstract Page Authors"),
        "Publication year": ("Verification Metadata Year", "Abstract Page Year"),
        "Item type": ("Verification Metadata Item Type",),
        "Publisher": ("Verification Metadata Publisher",),
        "Publication / container": ("Verification Metadata Container Title",),
        "DOI": ("Verification Metadata DOI", "doi", "Abstract Page DOI"),
        "URL": ("Resolved URL", "url", "Link", "Resource URL"),
        "Abstract": (),  # no step retrieves a second abstract to compare with
    }
    _ALL_RETRIEVED_COLUMNS = frozenset(
        name for names in RETRIEVED_COLUMNS.values() for name in names)

    def _input_column(self, columns, field, aliases):
        """The record's own column for a field. Retrieved-value columns are
        excluded, so e.g. the original ``DOI`` is used rather than the
        lookup's lowercase ``doi`` (case-insensitive guessing picked that)."""
        original = [c for c in columns if c not in self._ALL_RETRIEVED_COLUMNS]
        if field in self.EXACT_NAME_FIELDS:
            by_name = {str(c).strip().casefold(): c for c in original}
            return next((by_name[a] for a in aliases if a in by_name), None)
        return core.guess_column(original, aliases)

    # Lookup-step columns shown on the input side when the record's own cell
    # is empty. Shown only; never written back unless the user edits them.
    LOOKUP_INPUT_FALLBACKS = {
        "Title": ("matched_title", "Matched Title"),
        "DOI": ("doi",),
        "URL": ("url", "Link"),
    }

    def _lookup_fallback(self, row, field, input_column):
        for name in self.LOOKUP_INPUT_FALLBACKS.get(field, ()):
            if name != input_column and name in row.index:
                value = self.review_page._display_value(row.get(name, "")).strip()
                if value:
                    return value, name
        return "", ""

    def _retrieved_value(self, row, field, input_column):
        """(value, column name) of the first non-empty retrieved column."""
        for name in self.RETRIEVED_COLUMNS[field]:
            if name != input_column and name in row.index:
                value = self.review_page._display_value(row.get(name, "")).strip()
                if value:
                    return value, name
        if field == "DOI":
            resolved = self._column_value(row, [], ("Resolved URL",))
            if "doi.org/" in resolved.casefold():
                return core.normalize_doi(resolved), "Resolved URL"
        return "", ""

    def _commit_fields(self):
        """Write edited field values back into the DataFrame. Returns True if anything changed."""
        if not self.queue_indices or not (self.field_entries or self.other_editors):
            return False
        page, index = self.review_page, self._current_index()
        changed, new_columns = False, False
        edits = [(column or new_name, getter, self.input_fallbacks.get(field))
                 for field, (getter, column, new_name) in self.field_entries.items()]
        edits += [(column, getter, None) for column, getter in self.other_editors.items()]
        for column, getter, fallback in edits:
            text = getter()
            if fallback and text == fallback:
                continue  # an untouched lookup value shown in an empty cell
            if not text and fallback is not None:
                # The user cleared a displayed lookup value; the record's
                # own cell was empty all along.
                continue
            if column not in page.df.columns:
                if not text:
                    continue
                page.df[column] = ""
                new_columns = True
            current = page._display_value(page.df.at[index, column]).strip()
            if text == current:
                continue
            if page.df[column].dtype != object:
                page.df[column] = page.df[column].astype(object)
            page.df.at[index, column] = text
            changed = True
        if new_columns:
            page.register_columns_changed()
        if changed:
            page.render_page()
        return changed

    def _build_all_fields(self, row):
        """Fill the "All fields" view. Widgets are created once and then only
        have their text replaced, since rebuilding 100+ CustomTkinter widgets
        on every record took seconds."""
        self.other_editors = {}
        mapped = {column for _getter, column, _n in self.field_entries.values() if column}
        skip = mapped | {"Manual Decision", "Manual Notes"}
        columns = [column for column in row.index if column not in skip]
        for column in set(self._all_field_widgets) - set(columns):
            label, editor, _is_textbox = self._all_field_widgets.pop(column)
            label.destroy()
            editor.destroy()
        for number, column in enumerate(columns):
            value = self.review_page._display_value(row.get(column, "")).strip()
            cached = self._all_field_widgets.get(column)
            if cached and cached[2] == self._needs_textbox(value):
                label, editor, is_textbox = cached
                self._set_editor_value(editor, value)
                getter = ((lambda w=editor: w.get("1.0", "end-1c").strip()) if is_textbox
                          else (lambda w=editor: w.get().strip()))
            else:
                if cached:
                    cached[1].destroy()
                    label = cached[0]
                else:
                    label = ctk.CTkLabel(self.all_fields_frame, text=str(column), width=200, anchor="nw",
                                         wraplength=190, justify="left")
                editor, getter = self._make_editor(self.all_fields_frame, value)
                self._all_field_widgets[column] = (label, editor, self._needs_textbox(value))
            label.grid(row=number, column=0, sticky="nw", padx=6, pady=4)
            editor.grid(row=number, column=1, sticky="ew", padx=4, pady=3)
            self.other_editors[column] = getter

    def _load_record(self):
        if not self.queue_indices:
            return
        row = self.review_page.df.loc[self._current_index()]
        self.field_entries = {}
        self.input_fallbacks = {}
        for number, (field, aliases, new_name) in enumerate(self.EDITABLE_FIELDS):
            column = self._input_column(row.index, field, aliases)
            value = self.review_page._display_value(row.get(column, "")).strip() if column else ""
            fallback_column = ""
            if not value:
                value, fallback_column = self._lookup_fallback(row, field, column)
            retrieved, retrieved_column = self._retrieved_value(row, field, column)
            self._add_comparison_row(number, field, value, retrieved, column, new_name,
                                     retrieved_column, fallback_column)
        if self.view_toggle.get() == "All fields":
            self._build_all_fields(row)
        else:
            self.other_editors = {}  # built only when that view is opened

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
        _set_readonly_text_autosize(
            self.message_box, self._column_value(row, [], ("Verification Message",)) or
            "No verification message is stored in this file.",
            min_height=34, max_height=120, line_height=20)
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
        self._commit_fields()
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
        self._commit_fields()
        new_position = self.position + delta
        if 0 <= new_position < len(self.queue_indices):
            self.position = new_position
            self._load_record()

    def _toggle_topmost(self):
        self.attributes("-topmost", bool(self.topmost_var.get()))
        self.topmost_switch.configure(
            text="Always on top: ON" if self.topmost_var.get() else "Keep on top")

    def _close(self):
        self._commit_fields()
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
        )
        self.lens_key_entry = self._labeled_entry(
            settings, "Lens.org API key", "required to use Lens.org below",
            "Only needed if you enable Lens.org below — like CORE, it has no keyless tier. Unlike CORE, "
            "even the free academic trial requires signing in at lens.org and requesting a token "
            "(an approval step, not instant registration).",
            pady_bottom=10,
        )

        ctk.CTkLabel(self, text="Sources to search", font=ctk.CTkFont(weight="bold")) \
            .pack(anchor="w", padx=8, pady=(4, 4))

        sources_scroll = ctk.CTkScrollableFrame(self)
        sources_scroll.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        # Grouped so a now-long source list stays scannable: broad,
        # discipline-agnostic sources first, then single-country/single-
        # discipline/key-gated ones after — see the "group" field on each
        # entry in core.SOURCES.
        GROUP_HEADINGS = {
            "mainstream": "Mainstream (broad, cross-discipline)",
            "specialized": "Specialized & regional",
        }
        self.source_vars = {}
        sources_by_group = {}
        for src in core.SOURCES:
            sources_by_group.setdefault(src.get("group", "mainstream"), []).append(src)
        for group_index, group in enumerate(("mainstream", "specialized")):
            group_sources = sources_by_group.get(group, [])
            if not group_sources:
                continue
            ctk.CTkLabel(
                sources_scroll, text=GROUP_HEADINGS.get(group, group),
                font=ctk.CTkFont(size=12, weight="bold"), text_color=("gray30", "gray70"), anchor="w",
            ).pack(fill="x", pady=(10 if group_index else 0, 2))
            for src in group_sources:
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
        for entry in (self.email_entry, self.s2_key_entry, self.core_key_entry, self.lens_key_entry):
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
            (self.lens_key_entry, "lens_key"),
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
            "lens_key": self.get_lens_key() or "",
        })

    def get_enabled_sources(self):
        return [sid for sid, var in self.source_vars.items() if var.get()]

    def get_email(self):
        return self.email_entry.get().strip()

    def get_s2_key(self):
        return self.s2_key_entry.get().strip() or None

    def get_core_key(self):
        return self.core_key_entry.get().strip() or None

    def get_lens_key(self):
        return self.lens_key_entry.get().strip() or None


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

        self.status_var = ctk.StringVar(value=f"Enter a title and click “Search”. Pick which sources to use under {SOURCES_LOCATION}.")
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
            messagebox.showwarning("No sources selected", f"Please enable at least one source under {SOURCES_LOCATION}.")
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
                  self.sources_page.get_email(), self.sources_page.get_s2_key(), self.sources_page.get_core_key(),
                  self.sources_page.get_lens_key()),
            daemon=True,
        ).start()

    def _worker(self, title, author, year, enabled_sources, email, s2_key, core_key, lens_key):
        try:
            results, failed = core.run_search(title, author, year, enabled_sources, email=email, s2_api_key=s2_key,
                                               core_api_key=core_key, lens_api_key=lens_key)
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
                  self.sources_page.get_s2_key(), self.sources_page.get_core_key(), self.sources_page.get_lens_key())
        threading.Thread(target=self._verify_worker, args=values, daemon=True).start()

    def _verify_worker(self, title, author, year, doi, url, enabled_sources, email, s2_key, core_key, lens_key):
        try:
            result = core.verify_reference(title, author, year, doi, url, enabled_sources=enabled_sources,
                                           email=email, s2_api_key=s2_key, core_api_key=core_key,
                                           lens_api_key=lens_key)
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
        self.status_var = ctk.StringVar(value=f"Choose a file. Verification uses the databases enabled under {SOURCES_LOCATION}; "
                  f"choose the compared fields on the {VERIFICATION_SETTINGS_SUBTAB} sub-tab.")
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
                           self.sources_page.get_lens_key(),
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
        if source_settings[5] == "high_confidence":
            self.status_var.set(
                f"Verifying 0/{self.total_rows} records · Independent confirmation sources: {source_names}")
        else:
            self.status_var.set(
                f"Verifying 0/{self.total_rows} records · Fallback sources if needed: {source_names}")
        self.time_var.set("Elapsed 0:00 · Estimating time remaining…")
        threading.Thread(target=self._worker, args=(columns, source_settings, verification_fields), daemon=True).start()

    def _worker(self, columns, source_settings, verification_fields):
        title_col, author_col, year_col, doi_col, url_col = columns
        enabled_sources, email, s2_key, core_key, lens_key, verification_mode = source_settings
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
                    email=email, s2_api_key=s2_key, core_api_key=core_key, lens_api_key=lens_key,
                    mode=verification_mode, **context)
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
        self._last_doi_col = NO_COLUMN
        self._last_url_col = NO_COLUMN

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

        mapping_row2 = ctk.CTkFrame(mapping_frame, fg_color="transparent")
        mapping_row2.pack(fill="x", padx=10, pady=(0, 10))

        ctk.CTkLabel(mapping_row2, text="DOI column").pack(side="left", padx=(0, 6))
        self.doi_col = ctk.CTkOptionMenu(mapping_row2, values=[NO_COLUMN], width=170)
        self.doi_col.pack(side="left", padx=(0, 20))

        ctk.CTkLabel(mapping_row2, text="URL column").pack(side="left", padx=(0, 6))
        self.url_col = ctk.CTkOptionMenu(mapping_row2, values=[NO_COLUMN], width=170)
        self.url_col.pack(side="left", padx=(0, 20))

        ctk.CTkLabel(mapping_row2, text="Item type column").pack(side="left", padx=(0, 6))
        self.item_type_col = ctk.CTkOptionMenu(mapping_row2, values=[NO_COLUMN], width=150)
        self.item_type_col.pack(side="left")
        ctk.CTkLabel(
            mapping_frame,
            text="DOI/URL columns decide which records already have a link and control the "
                 "\"only missing\" scope below. Item type lets type-restricted sources (e.g. Google "
                 "Books, only for Book/Book Section) fire — without it, they're skipped entirely "
                 "rather than guessed at, to protect their limited quota.",
            text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11), anchor="w", justify="left",
            wraplength=950,
        ).pack(fill="x", padx=10, pady=(0, 10))

        # 2b. Scope: everything, or only records still missing a DOI or URL
        scope_frame = ctk.CTkFrame(self)
        scope_frame.pack(fill="x", padx=4, pady=(0, 10))
        ctk.CTkLabel(scope_frame, text="Which records to look up", font=ctk.CTkFont(weight="bold")) \
            .pack(anchor="w", padx=10, pady=(10, 4))
        self.scope_var = ctk.StringVar(value="missing_only")
        ctk.CTkRadioButton(
            scope_frame, text="Only records missing a DOI or a URL (recommended — fills gaps, saves quota)",
            variable=self.scope_var, value="missing_only",
        ).pack(anchor="w", padx=10, pady=(0, 4))
        ctk.CTkRadioButton(
            scope_frame,
            text="Only records missing BOTH a DOI and a URL (most conservative — a record that already has "
                 "either one is skipped entirely, so a fresh search can never end up replacing an existing, "
                 "correct value with a different match)",
            variable=self.scope_var, value="missing_both",
        ).pack(anchor="w", padx=10, pady=(0, 4))
        ctk.CTkRadioButton(
            scope_frame, text="All records (re-searches everything, including rows that already have both)",
            variable=self.scope_var, value="all",
        ).pack(anchor="w", padx=10, pady=(0, 4))
        ctk.CTkLabel(
            scope_frame,
            text="Whichever you pick, a record's existing DOI/URL is only ever replaced by a newly found "
                 "value — a row this run doesn't find anything for keeps what it already had, it is never "
                 "blanked out. \"Missing both\" is the only option that guarantees an already-filled field is "
                 "never touched at all, even by a different match; \"Missing a DOI or a URL\" may still "
                 "re-search (and, if it finds a different paper, replace) the field you already had, while "
                 "trying to fill in the one you didn't.",
            text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11), anchor="w", justify="left",
            wraplength=950,
        ).pack(fill="x", padx=10, pady=(0, 10))

        self.clear_cache_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            scope_frame, text="Clear cached results before running",
            variable=self.clear_cache_var,
        ).pack(anchor="w", padx=10, pady=(0, 4))
        ctk.CTkLabel(
            scope_frame,
            text="This app caches every lookup result (by title/author/year/sources/item type/existing DOI) "
                 "so re-running the same file doesn't re-query the APIs for rows it already resolved. Leave "
                 "this unchecked to reuse that cache (faster, fewer requests). Check it to force every "
                 "selected record to be looked up fresh — e.g. after changing which sources are enabled, "
                 "after this app added new sources, or if you suspect a cached result is stale/wrong. This "
                 "clears the shared lookup cache file, so it also affects Single Lookup.",
            text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11), anchor="w", justify="left",
            wraplength=950,
        ).pack(fill="x", padx=10, pady=(0, 10))

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

        self.status_var = ctk.StringVar(value=f"Choose a file with a list of literature records to get started. Pick which sources to use on the {SOURCES_SUBTAB} sub-tab.")
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
        doi_guess = abstract_tools.guess_column(columns, abstract_tools.DOI_ALIASES)
        url_guess = abstract_tools.guess_column(columns, abstract_tools.URL_ALIASES)
        item_type_guess = core.guess_column(columns, core.ITEM_TYPE_ALIASES)

        self.title_col.configure(values=columns)
        self.title_col.set(title_guess)

        self.author_col.configure(values=[NO_COLUMN] + columns)
        self.author_col.set(author_guess or NO_COLUMN)

        self.year_col.configure(values=[NO_COLUMN] + columns)
        self.year_col.set(year_guess or NO_COLUMN)

        self.doi_col.configure(values=[NO_COLUMN] + columns)
        self.doi_col.set(doi_guess or NO_COLUMN)

        self.url_col.configure(values=[NO_COLUMN] + columns)
        self.url_col.set(url_guess or NO_COLUMN)

        self.item_type_col.configure(values=[NO_COLUMN] + columns)
        self.item_type_col.set(item_type_guess or NO_COLUMN)

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
        doi_col = self.doi_col.get()
        url_col = self.url_col.get()
        item_type_col = self.item_type_col.get()
        if title_col not in self.df.columns:
            messagebox.showwarning("Missing column", "Please choose a valid title column.")
            return

        enabled_sources = self.sources_page.get_enabled_sources()
        if not enabled_sources:
            messagebox.showwarning("No sources selected", f"Please enable at least one source under {SOURCES_LOCATION}.")
            return

        scope = self.scope_var.get()
        if scope in ("missing_only", "missing_both") and doi_col == NO_COLUMN and url_col == NO_COLUMN:
            messagebox.showwarning(
                "No DOI/URL column chosen",
                "This scope needs at least a DOI or a URL column so it can tell which rows already have "
                "one. Choose one above, or switch to \"All records\".")
            return
        if scope == "all" and (doi_col != NO_COLUMN or url_col != NO_COLUMN) and not messagebox.askyesno(
                "Re-search every record?",
                "\"All records\" re-searches and re-scores every row, including ones that already have "
                "a DOI/URL. A row a source no longer finds a match for keeps its existing value "
                "(nothing is ever blanked out), but any row that finds a different match will have its "
                "DOI/URL replaced with the new one.\n\nContinue?"):
            return

        clear_cache = self.clear_cache_var.get()
        if clear_cache and not messagebox.askyesno(
                "Clear the lookup cache?",
                "This deletes every previously cached lookup result (for every file, not just this one) "
                "so this run looks everything up fresh. This also affects Single Lookup until it's rebuilt. "
                "Continue?"):
            return

        workers = int(round(self.workers_slider.get()))
        self._last_doi_col, self._last_url_col = doi_col, url_col
        if clear_cache:
            core.clear_cache()

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
            args=(title_col, author_col, year_col, doi_col, url_col, item_type_col, scope, enabled_sources,
                  self.sources_page.get_email(), self.sources_page.get_s2_key(), self.sources_page.get_core_key(),
                  self.sources_page.get_lens_key(), workers),
            daemon=True,
        ).start()

    def _batch_worker(self, title_col, author_col, year_col, doi_col, url_col, item_type_col, scope,
                       enabled_sources, email, s2_key, core_key, lens_key, workers):
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

            existing_doi = abstract_tools.clean_value(row.get(doi_col, "")) if doi_col != NO_COLUMN else ""
            existing_url = abstract_tools.clean_value(row.get(url_col, "")) if url_col != NO_COLUMN else ""
            item_type = abstract_tools.clean_value(row.get(item_type_col, "")) if item_type_col != NO_COLUMN else ""

            # "missing_only" skips a row once it has BOTH; "missing_both" is
            # more conservative and skips it the moment it has EITHER one -
            # so a fresh search can never overwrite an already-correct
            # value with a different match found while chasing the other,
            # missing field. Either way, a skipped row's existing value(s)
            # are carried forward untouched, without spending any source's
            # quota on it.
            already_has_enough = (
                (existing_doi and existing_url) if scope == "missing_only" else
                (existing_doi or existing_url) if scope == "missing_both" else False)
            if already_has_enough:
                return idx, {"status": "skipped_complete", "doi": existing_doi, "url": existing_url,
                             "matched_title": "", "source": "", "score": 0, "failed_sources": []}

            key = core.cache_key(title, author, year, enabled_sources, item_type=item_type, existing_doi=existing_doi)
            with cache_lock:
                cached = cache.get(key)
            if cached is not None:
                return idx, cached

            try:
                res = core.lookup_one(title, author, year, enabled_sources, email=email, s2_api_key=s2_key,
                                       core_api_key=core_key, lens_api_key=lens_key,
                                       item_type=item_type, existing_doi=existing_doi)
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
        # A row this run didn't find anything new for keeps whatever DOI/URL
        # it already had (read from the mapped DOI/URL columns) rather than
        # being blanked out - only a genuinely found value replaces it.
        doi_col, url_col = self._last_doi_col, self._last_url_col
        original_dois = ([abstract_tools.clean_value(v) for v in self.df.get(doi_col, [""] * len(self.df))]
                          if doi_col != NO_COLUMN else [""] * len(self.df))
        original_urls = ([abstract_tools.clean_value(v) for v in self.df.get(url_col, [""] * len(self.df))]
                          if url_col != NO_COLUMN else [""] * len(self.df))
        result_df["Lookup Status"] = [STATUS_LABELS.get(r["status"], r["status"]) for r in results]
        result_df["Match Score"] = [r["score"] for r in results]
        result_df["Match Source"] = [r["source"] for r in results]
        result_df["Matched Title"] = [r["matched_title"] for r in results]
        result_df["DOI"] = [r["doi"] or original for r, original in zip(results, original_dois)]
        result_df["Link"] = [r["url"] or original for r, original in zip(results, original_urls)]
        result_df["Failed Sources"] = [_failed_sources_text(r.get("failed_sources")) for r in results]
        self.output_df = result_df

        found = sum(1 for r in results
                    if r["status"] in ("auto_accepted", "needs_review", "url_enriched", "skipped_complete"))
        incomplete = sum(1 for r in results if r.get("failed_sources"))
        done_msg = f"Done: {found}/{len(results)} records have a DOI/link."
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
            # Show the merged (possibly-preserved-original) DOI/Link from
            # result_df, not the raw per-row result - a "not found" row that
            # already had a DOI/URL still shows it here, matching the export.
            merged_doi, merged_url = result_df["DOI"].iat[i], result_df["Link"].iat[i]
            preview_rows.append((
                title_text[:60],
                status_text,
                f"{r['score']:.0f}",
                r["source"],
                merged_doi,
                merged_url,
            ))
            copy_rows.append((
                title_text,
                status_text,
                f"{r['score']:.0f}",
                r["source"],
                merged_doi,
                merged_url,
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


# ---------------------------------------------------------------------------
# Statistics: local, flexible column-level charts/tables over any imported
# bibliography file. See stats_tools.py for the (unit-tested) counting logic;
# this class is just the chart/table wiring around it.
# ---------------------------------------------------------------------------

class StatisticsPage(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.df = None
        self.file_path = None
        self.current_rows = []
        self.current_headers = []

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(6, 8))
        ctk.CTkButton(top, text="Choose file…", width=130, command=self.on_choose_file).pack(side="left")
        self.file_label = ctk.CTkLabel(top, text="Choose a CSV, Excel, or RIS file to summarize", anchor="w")
        self.file_label.pack(side="left", padx=12, fill="x", expand=True)
        self.export_chart_btn = ctk.CTkButton(top, text="Save chart…", width=110,
                                              command=self.on_export_chart, state="disabled")
        self.export_chart_btn.pack(side="right")
        self.export_table_btn = ctk.CTkButton(top, text="Export table…", width=125,
                                              command=self.on_export_table, state="disabled")
        self.export_table_btn.pack(side="right", padx=(0, 6))

        controls = ctk.CTkFrame(self)
        controls.pack(fill="x", padx=4, pady=(0, 6))
        ctk.CTkLabel(controls, text="Column", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(8, 2))
        self.value_col = _make_searchable_combobox(controls, values=[NO_COLUMN], width=220,
                                                    command=lambda _v: self.on_value_column_changed())
        self.value_col.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 10))

        ctk.CTkLabel(controls, text="Group by (optional)", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=1, sticky="w", padx=10, pady=(8, 2))
        self.group_col = _make_searchable_combobox(controls, values=[NO_COLUMN], width=220)
        self.group_col.set(NO_COLUMN)
        self.group_col.grid(row=1, column=1, sticky="w", padx=10, pady=(0, 10))

        ctk.CTkLabel(controls, text="Chart type", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=2, sticky="w", padx=10, pady=(8, 2))
        self.chart_type = ctk.CTkOptionMenu(controls, values=["Table only"], width=150)
        self.chart_type.grid(row=1, column=2, sticky="w", padx=10, pady=(0, 10))

        self.generate_btn = ctk.CTkButton(controls, text="Generate", width=110,
                                          command=self.on_generate, state="disabled")
        self.generate_btn.grid(row=1, column=3, sticky="w", padx=10, pady=(0, 10))

        # Resizable areas: drag the grey bars to trade space between the column
        # overview / chart (top) and the counts table (bottom), and between
        # the overview and the chart.
        ctk.CTkLabel(self, text="Drag the grey bars to resize the tables and the chart.",
                     text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11), anchor="w").pack(
                         fill="x", padx=6, pady=(0, 2))
        sash = "#4A4A4A" if ctk.get_appearance_mode() == "Dark" else "#D2D7DE"
        panes = dict(sashwidth=8, sashrelief="flat", bd=0, bg=sash, opaqueresize=True)
        self.vertical_panes = tk.PanedWindow(self, orient="vertical", sashcursor="sb_v_double_arrow",
                                             **panes)
        self.vertical_panes.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        self.horizontal_panes = tk.PanedWindow(self.vertical_panes, orient="horizontal",
                                               sashcursor="sb_h_double_arrow", **panes)

        self.overview_table = ResultsTable(
            self.horizontal_panes, headers=["Column", "Kind", "Non-empty", "Unique values"],
            weights=[2, 0, 0, 0])
        chart_frame = ctk.CTkFrame(self.horizontal_panes)
        self.figure = Figure(figsize=(5, 4), dpi=100)
        self.axes = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=chart_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)
        self._clear_chart("Pick a column above and click Generate.")
        self.horizontal_panes.add(self.overview_table, minsize=200, stretch="always")
        self.horizontal_panes.add(chart_frame, minsize=200, stretch="always")

        self.counts_table = ResultsTable(self.vertical_panes, headers=["Value", "Count", "Percentage"],
                                         weights=[2, 0, 0])
        self.vertical_panes.add(self.horizontal_panes, minsize=120, stretch="always")
        self.vertical_panes.add(self.counts_table, minsize=90, stretch="always")
        self._panes_placed = False
        self.vertical_panes.bind("<Configure>", self._place_sashes, add="+")

    def _place_sashes(self, _event=None):
        """Start with an even split once the panes have a real size."""
        if self._panes_placed:
            return
        height, width = self.vertical_panes.winfo_height(), self.horizontal_panes.winfo_width()
        if height > 50 and width > 50:
            self._panes_placed = True
            self.vertical_panes.sash_place(0, 0, int(height * 0.55))
            self.horizontal_panes.sash_place(0, int(width * 0.5), 0)

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
        self.file_path = path
        encoding = self.df.attrs.get("source_encoding")
        self.file_label.configure(
            text=f"{os.path.basename(path)} ({len(self.df):,} records)" + (f" · {encoding}" if encoding else ""))

        columns = list(self.df.columns)
        _update_combobox_values(self.value_col, columns)
        _update_combobox_values(self.group_col, [NO_COLUMN] + columns)
        self.group_col.set(NO_COLUMN)
        if columns:
            self.value_col.set(columns[0])
            self.on_value_column_changed()
        self.generate_btn.configure(state="normal" if columns else "disabled")
        self.export_table_btn.configure(state="disabled")
        self.export_chart_btn.configure(state="disabled")
        self.counts_table.set_rows([])
        self._show_overview()
        self._clear_chart("Pick a column above and click Generate.")

    def _show_overview(self):
        rows = [(row["column"], row["kind"], f"{row['non_empty']:,} ({row['non_empty_pct']:.0%})",
                 f"{row['unique_values']:,}") for row in stats_tools.summarize_columns(self.df)]
        self.overview_table.set_rows(rows)

    def on_value_column_changed(self):
        column = self.value_col.get()
        if self.df is None or column not in self.df.columns:
            return
        kind = stats_tools.classify_column(self.df[column], column)
        chart_types = stats_tools.suggested_chart_types(kind)
        self.chart_type.configure(values=chart_types)
        self.chart_type.set(chart_types[0])

    def _clear_chart(self, message):
        self.axes.clear()
        colors = _style_chart_axes(self.figure, self.axes)
        self.axes.text(0.5, 0.5, message, ha="center", va="center", color=colors["text"], wrap=True)
        self.axes.set_xticks([]); self.axes.set_yticks([])
        self.canvas.draw_idle()

    def on_generate(self):
        if self.df is None:
            return
        value_column = self.value_col.get()
        if value_column not in self.df.columns:
            messagebox.showwarning("Choose a column", "Pick a column to summarize first.")
            return
        group_column = self.group_col.get()
        group_column = None if group_column in (NO_COLUMN, "", value_column) else group_column
        if group_column and group_column not in self.df.columns:
            group_column = None
        chart_type = self.chart_type.get()

        if group_column:
            self._generate_grouped(group_column, value_column)
        else:
            self._generate_single(value_column, chart_type)
        self.export_table_btn.configure(state="normal" if self.current_rows else "disabled")
        self.export_chart_btn.configure(state="normal")

    def _generate_single(self, column, chart_type):
        kind = stats_tools.classify_column(self.df[column], column)
        if chart_type == "Histogram" or kind == "numeric":
            edges, counts = stats_tools.numeric_histogram(self.df, column)
            headers = ["Bin start", "Bin end", "Count"]
            rows = [(f"{edges[i]:.3g}", f"{edges[i + 1]:.3g}", counts[i]) for i in range(len(counts))]
            if chart_type == "Table only":
                self._clear_chart(f"{len(counts):,} bins for '{column}' — see the table below.")
            else:
                self._draw_histogram(edges, counts, column)
        else:
            order = "key" if kind == "year" else "count"
            counts = stats_tools.value_counts(self.df, column, order=order)
            total = sum(count for _value, count in counts) or 1
            headers = ["Value", "Count", "Percentage"]
            rows = [(value, count, f"{count / total:.1%}") for value, count in counts]
            if chart_type == "Pie chart":
                self._draw_pie(counts, column)
            elif chart_type == "Table only":
                self._clear_chart(f"{len(counts):,} distinct values in '{column}' — see the table below.")
            else:
                self._draw_bar(counts, column)
        self.current_headers, self.current_rows = headers, rows
        self.counts_table.retitle(headers)
        self.counts_table.set_rows(rows)

    def _generate_grouped(self, group_column, value_column):
        rows_values, col_values, matrix = stats_tools.cross_tab_counts(self.df, group_column, value_column)
        headers = [group_column, value_column, "Count"]
        rows = [(row_value, col_value, matrix[i][j])
                for i, row_value in enumerate(rows_values)
                for j, col_value in enumerate(col_values) if matrix[i][j]]
        rows.sort(key=lambda item: (-item[2], item[0], item[1]))
        self.current_headers, self.current_rows = headers, rows
        self.counts_table.retitle(headers)
        self.counts_table.set_rows(rows)
        self._draw_grouped_bar(rows_values, col_values, matrix, group_column, value_column)

    def _draw_bar(self, counts, column, max_bars=15):
        self.axes.clear()
        colors = _style_chart_axes(self.figure, self.axes)
        shown = counts[:max_bars]
        labels = [str(value)[:20] for value, _count in shown]
        self.axes.bar(labels, [count for _value, count in shown], color=colors["accent"])
        self.axes.set_title(f"{column}" + (f" (top {max_bars})" if len(counts) > max_bars else ""))
        self.axes.tick_params(axis="x", rotation=45)
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def _draw_pie(self, counts, column, max_slices=8):
        self.axes.clear()
        colors = _style_chart_axes(self.figure, self.axes)
        shown = counts[:max_slices]
        other_total = sum(count for _value, count in counts[max_slices:])
        labels = [str(value)[:20] for value, _count in shown]
        values = [count for _value, count in shown]
        if other_total:
            labels.append("Other"); values.append(other_total)
        self.axes.pie(values, labels=labels, autopct="%1.0f%%", colors=colors["palette"],
                      textprops={"color": colors["text"], "fontsize": 8})
        self.axes.set_title(column)
        self.canvas.draw_idle()

    def _draw_histogram(self, edges, counts, column):
        self.axes.clear()
        colors = _style_chart_axes(self.figure, self.axes)
        if counts:
            widths = [edges[i + 1] - edges[i] for i in range(len(counts))]
            self.axes.bar(edges[:-1], counts, width=widths, align="edge", color=colors["accent"])
        self.axes.set_title(column)
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def _draw_grouped_bar(self, row_values, col_values, matrix, group_column, value_column,
                          max_rows=15, max_series=6):
        self.axes.clear()
        colors = _style_chart_axes(self.figure, self.axes)
        shown_rows = row_values[:max_rows]
        shown_cols = col_values[:max_series]
        bottoms = [0.0] * len(shown_rows)
        for series_index, col_value in enumerate(shown_cols):
            heights = [matrix[row_values.index(row_value)][col_values.index(col_value)]
                      for row_value in shown_rows]
            self.axes.bar([str(v)[:16] for v in shown_rows], heights, bottom=bottoms,
                         label=str(col_value)[:16], color=colors["palette"][series_index % len(colors["palette"])])
            bottoms = [b + h for b, h in zip(bottoms, heights)]
        self.axes.set_title(f"{value_column} by {group_column}"
                            + (" (top values)" if len(row_values) > max_rows or len(col_values) > max_series else ""))
        self.axes.tick_params(axis="x", rotation=45)
        self.axes.legend(fontsize=7, facecolor=colors["axes_bg"], labelcolor=colors["text"])
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def on_export_table(self):
        if self.current_rows:
            base = os.path.splitext(os.path.basename(self.file_path or "records"))[0]
            _export_table_rows(self, self.current_rows, self.current_headers, f"{base}_stats.csv")

    def on_export_chart(self):
        path = filedialog.asksaveasfilename(
            title="Save chart", defaultextension=".png",
            filetypes=[("PNG image (.png)", "*.png"), ("PDF document (.pdf)", "*.pdf")])
        if not path:
            return
        try:
            self.figure.savefig(path, facecolor=self.figure.get_facecolor())
        except Exception as exc:
            messagebox.showerror("Export failed", f"Couldn't save the chart:\n{exc}")
            return
        messagebox.showinfo("Export complete", f"Saved to:\n{path}")


# ---------------------------------------------------------------------------
# Compare documents: match two files on user-chosen column pairs (Title
# required) and diff chosen column pairs. See compare_tools.py
# (compare_by_column_pairs) for the unit-tested matching/diff logic.
# ---------------------------------------------------------------------------

class CompareDocumentsPage(ctk.CTkFrame):
    """Match the records of two files on chosen column pairs (Title required)
    and show, one row per record, how the chosen compared columns differ."""

    STATUS_COLORS = {  # (light, dark) row backgrounds per comparison status
        compare_tools.DIFFERENT: ("#FBE3E4", "#4B2326"),
        compare_tools.SAME: ("#E6F4E9", "#1F3A26"),
        compare_tools.ONLY_A: ("#FFF1D6", "#4A3B14"),
        compare_tools.ONLY_B: ("#E3EEFB", "#1D3350"),
        compare_tools.NO_TITLE: ("#ECECEC", "#3A3A3A"),
    }
    ALL_FILTER = "All"
    ANY_COLUMN = "(any column)"

    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.df_a = self.df_b = None
        self.records, self.summary, self.visible_records = [], None, []
        self.match_pairs = []       # (row frame, combo A, combo B) for extra match keys
        self.compare_pairs = []     # [column A, column B, selected]
        self.used_title_pair = None
        self.used_match_pairs = []

        file_row = ctk.CTkFrame(self, fg_color="transparent")
        file_row.pack(fill="x", padx=4, pady=(6, 4))
        ctk.CTkButton(file_row, text="Choose file A…", width=130,
                      command=lambda: self.on_choose_file("a")).pack(side="left")
        self.label_a = ctk.CTkLabel(file_row, text="File A not chosen", anchor="w")
        self.label_a.pack(side="left", padx=(8, 24))
        ctk.CTkButton(file_row, text="Choose file B…", width=130,
                      command=lambda: self.on_choose_file("b")).pack(side="left")
        self.label_b = ctk.CTkLabel(file_row, text="File B not chosen", anchor="w")
        self.label_b.pack(side="left", padx=(8, 0))
        self.setup_toggle = ctk.CTkButton(file_row, text="Hide setup ▲", width=120,
                                          command=self._toggle_setup)
        self.setup_toggle.pack(side="right")

        # --- Setup: match keys (left) and compared columns (right) ---------
        self.setup = ctk.CTkFrame(self, fg_color="transparent")
        self.setup.pack(fill="x", padx=4, pady=(0, 4))
        self.setup.grid_columnconfigure(0, weight=2, uniform="setup")
        self.setup.grid_columnconfigure(1, weight=3, uniform="setup")

        match_card = ctk.CTkFrame(self.setup)
        match_card.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        ctk.CTkLabel(match_card, text="1. Match records by", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=10, pady=(8, 2))
        header = ctk.CTkFrame(match_card, fg_color="transparent")
        header.pack(fill="x", padx=10)
        ctk.CTkLabel(header, text="File A column", width=175, anchor="w",
                     text_color=("gray35", "gray65")).pack(side="left")
        ctk.CTkLabel(header, text="File B column", anchor="w",
                     text_color=("gray35", "gray65")).pack(side="left", padx=(30, 0))
        self.match_rows = ctk.CTkFrame(match_card, fg_color="transparent")
        self.match_rows.pack(fill="x", padx=10)
        title_row = ctk.CTkFrame(self.match_rows, fg_color="transparent")
        title_row.pack(fill="x", pady=2)
        self.title_a, self.title_b = self._pair_combos(title_row, width=175)
        ctk.CTkLabel(title_row, text="Title · required", text_color=("#8a5a00", "#e0b060")).pack(
            side="left", padx=(6, 0))
        ctk.CTkButton(match_card, text="+ Add match column", width=150,
                      command=self._add_match_pair).pack(anchor="w", padx=10, pady=(4, 2))
        ctk.CTkLabel(
            match_card,
            text="Records are paired when their titles match (case and punctuation ignored). "
                 "Extra match columns (e.g. DOI, Year, Author) only have to agree where both "
                 "records have a value; among several same-title candidates the one agreeing "
                 "on the most extra columns is used.",
            text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11), anchor="w", justify="left",
            wraplength=430).pack(fill="x", padx=10, pady=(0, 8))

        compare_card = ctk.CTkFrame(self.setup)
        compare_card.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        compare_header = ctk.CTkFrame(compare_card, fg_color="transparent")
        compare_header.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(compare_header, text="2. Columns to compare",
                     font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.compare_count = ctk.CTkLabel(compare_header, text="", text_color=("gray35", "gray65"))
        self.compare_count.pack(side="left", padx=10)
        ctk.CTkButton(compare_header, text="Clear", width=60,
                      command=lambda: self._select_shown_pairs(False)).pack(side="right")
        ctk.CTkButton(compare_header, text="Select shown", width=100,
                      command=lambda: self._select_shown_pairs(True)).pack(side="right", padx=6)
        self.pair_filter = ctk.CTkEntry(compare_header, width=160, placeholder_text="Filter columns…")
        self.pair_filter.pack(side="right")
        self.pair_filter.bind("<KeyRelease>", lambda _event: self._render_compare_pairs())

        pair_holder = ctk.CTkFrame(compare_card, fg_color="transparent")
        pair_holder.pack(fill="both", expand=True, padx=10)
        _style_literature_treeview(self)
        self.pair_tree = ttk.Treeview(pair_holder, columns=("use", "a", "b"), show="headings",
                                      style="Literature.Treeview", selectmode="none", height=5)
        for column, heading, width in (("use", "Use", 60), ("a", "File A column", 260),
                                       ("b", "File B column", 260)):
            self.pair_tree.heading(column, text=heading, anchor="w")
            self.pair_tree.column(column, width=width, minwidth=60, stretch=column != "use", anchor="w")
        pair_scroll = ctk.CTkScrollbar(pair_holder, orientation="vertical", command=self.pair_tree.yview)
        self.pair_tree.configure(yscrollcommand=pair_scroll.set)
        self.pair_tree.pack(side="left", fill="both", expand=True)
        pair_scroll.pack(side="left", fill="y", padx=(3, 0))
        self.pair_tree.bind("<ButtonRelease-1>", self._toggle_pair)
        self.pair_tree.tag_configure("on", foreground=_TREEVIEW_COLORS["accent"])

        add_row = ctk.CTkFrame(compare_card, fg_color="transparent")
        add_row.pack(fill="x", padx=10, pady=(4, 8))
        ctk.CTkLabel(add_row, text="Pair differently named columns:",
                     text_color=("gray35", "gray65")).pack(side="left", padx=(0, 6))
        self.extra_a, self.extra_b = self._pair_combos(add_row, width=170)
        ctk.CTkButton(add_row, text="Add pair", width=80, command=self._add_compare_pair).pack(
            side="left", padx=(6, 0))

        # --- Run --------------------------------------------------------------
        run_row = ctk.CTkFrame(self, fg_color="transparent")
        run_row.pack(fill="x", padx=4, pady=(2, 4))
        self.compare_btn = ctk.CTkButton(run_row, text="Compare", width=110,
                                         command=self.on_compare, state="disabled")
        self.compare_btn.pack(side="left")
        self.ignore_case_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(run_row, text="Ignore case & punctuation in compared values",
                        variable=self.ignore_case_var).pack(side="left", padx=14)

        export_row = ctk.CTkFrame(self, fg_color="transparent")
        export_row.pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(export_row, text="Export format").pack(side="left", padx=(0, 8))
        self.output_format = ctk.CTkOptionMenu(
            export_row, values=["CSV table (.csv)", "Excel (.xlsx)"], width=170)
        self.output_format.set("CSV table (.csv)")
        self.output_format.pack(side="left", padx=(0, 10))
        self.export_btn = ctk.CTkButton(export_row, text="Export shown records…", width=170,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="left")
        self.status_var = ctk.StringVar(
            value="Choose both files, pick the match columns and the columns to compare, then click Compare.")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", justify="left",
                     text_color=("gray30", "gray70"), wraplength=1400).pack(fill="x", padx=6, pady=(0, 4))

        # --- Result filters -----------------------------------------------------
        filter_row = ctk.CTkFrame(self, fg_color="transparent")
        filter_row.pack(fill="x", padx=4, pady=(0, 4))
        self.status_filter = ctk.CTkSegmentedButton(
            filter_row, values=[self.ALL_FILTER], command=lambda _value: self._render_results())
        self.status_filter.set(self.ALL_FILTER)
        self.status_filter.pack(side="left")
        ctk.CTkLabel(filter_row, text="Column").pack(side="left", padx=(16, 6))
        self.column_filter = ctk.CTkOptionMenu(
            filter_row, values=[self.ANY_COLUMN], width=230, command=lambda _value: self._render_results())
        self.column_filter.pack(side="left")
        self.title_search = ctk.CTkEntry(filter_row, width=220, placeholder_text="Search title…")
        self.title_search.pack(side="right")
        self.title_search.bind("<KeyRelease>", lambda _event: self._render_results())

        # Packed at the bottom first so the expanding results table below
        # can never squeeze the side-by-side panel to nothing.
        detail = ctk.CTkFrame(self)
        detail.pack(side="bottom", fill="x", padx=4, pady=(0, 4))
        self.detail_label = ctk.CTkLabel(detail, text="Select a record to see both files side by side.",
                                         anchor="w", font=ctk.CTkFont(weight="bold"))
        self.detail_label.pack(fill="x", padx=10, pady=(6, 2))
        self.detail_table = ResultsTable(detail, headers=["Column", "File A", "File B"], weights=[1, 2, 2])
        self.detail_table.tree.configure(height=6)
        self.detail_table.tree.column("c0", width=280, stretch=False)
        self.detail_table.tree.column("c1", width=520, stretch=True)
        self.detail_table.tree.column("c2", width=520, stretch=True)
        self._configure_status_tags(self.detail_table.tree)
        self.detail_table.pack(fill="x", padx=8, pady=(0, 8))

        self.results_holder = ctk.CTkFrame(self, fg_color="transparent")
        self.results_holder.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.table = None
        self._build_results_table([])

    # -- setup helpers -------------------------------------------------------

    def _pair_combos(self, parent, width=200):
        combo_a = _make_searchable_combobox(parent, values=[NO_COLUMN], width=width)
        combo_a.pack(side="left")
        combo_a.set(NO_COLUMN)
        ctk.CTkLabel(parent, text="↔", width=24).pack(side="left")
        combo_b = _make_searchable_combobox(parent, values=[NO_COLUMN], width=width)
        combo_b.pack(side="left")
        combo_b.set(NO_COLUMN)
        for combo, df_getter in ((combo_a, lambda: self.df_a), (combo_b, lambda: self.df_b)):
            df = df_getter()
            if df is not None:
                _update_combobox_values(combo, [NO_COLUMN] + list(df.columns))
        return combo_a, combo_b

    def _add_match_pair(self):
        row = ctk.CTkFrame(self.match_rows, fg_color="transparent")
        row.pack(fill="x", pady=2)
        combo_a, combo_b = self._pair_combos(row, width=175)
        entry = (row, combo_a, combo_b)
        ctk.CTkButton(row, text="✕", width=28, fg_color=("gray65", "gray35"),
                      hover_color=("gray55", "gray45"),
                      command=lambda: self._remove_match_pair(entry)).pack(side="left", padx=(6, 0))
        self.match_pairs.append(entry)

    def _remove_match_pair(self, entry):
        self.match_pairs.remove(entry)
        entry[0].destroy()

    def _toggle_setup(self):
        if self.setup.winfo_manager():
            self.setup.pack_forget()
            self.setup_toggle.configure(text="Show setup ▼")
        else:
            self.setup.pack(fill="x", padx=4, pady=(0, 4), after=self.setup_toggle.master)
            self.setup_toggle.configure(text="Hide setup ▲")

    def _selected_column(self, combo, df):
        value = combo.get().strip()
        return value if df is not None and value in df.columns else None

    def _reset_compare_pairs(self):
        if self.df_a is None or self.df_b is None:
            self.compare_pairs = []
        else:
            common = [column for column in self.df_a.columns if column in self.df_b.columns]
            self.compare_pairs = [[column, column, False] for column in common]
        self._render_compare_pairs()

    def _shown_pairs(self):
        text = self.pair_filter.get().strip().casefold()
        return [pair for pair in self.compare_pairs
                if not text or text in pair[0].casefold() or text in pair[1].casefold()]

    def _render_compare_pairs(self):
        self.pair_tree.delete(*self.pair_tree.get_children())
        for pair in self._shown_pairs():
            index = self.compare_pairs.index(pair)
            self.pair_tree.insert("", "end", iid=str(index), tags=("on",) if pair[2] else (),
                                  values=("☑" if pair[2] else "☐", pair[0], pair[1]))
        selected = sum(1 for pair in self.compare_pairs if pair[2])
        self.compare_count.configure(
            text=f"{selected} selected of {len(self.compare_pairs)}" if self.compare_pairs else
            "choose both files first")

    def _toggle_pair(self, event):
        row_id = self.pair_tree.identify_row(event.y)
        if row_id:
            pair = self.compare_pairs[int(row_id)]
            pair[2] = not pair[2]
            self._render_compare_pairs()

    def _select_shown_pairs(self, selected):
        for pair in self._shown_pairs():
            pair[2] = selected
        self._render_compare_pairs()

    def _add_compare_pair(self):
        column_a = self._selected_column(self.extra_a, self.df_a)
        column_b = self._selected_column(self.extra_b, self.df_b)
        if not column_a or not column_b:
            messagebox.showwarning("Choose two columns", "Pick one column from file A and one from file B.")
            return
        for pair in self.compare_pairs:
            if pair[0] == column_a and pair[1] == column_b:
                pair[2] = True
                break
        else:
            self.compare_pairs.insert(0, [column_a, column_b, True])
        self.pair_filter.delete(0, "end")
        self._render_compare_pairs()

    # -- files -------------------------------------------------------------

    def on_choose_file(self, which):
        path = filedialog.askopenfilename(
            title="Choose a bibliographic file",
            filetypes=[("Supported files", "*.csv *.xlsx *.xls *.json *.ris *.bib *.bibtex"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            # Keep cells as literal text so 12 isn't read back as 12.0.
            df = core.read_records_file(path, as_text=True).reset_index(drop=True)
        except Exception as exc:
            messagebox.showerror("Couldn't read file", f"Failed to read this file:\n{exc}")
            return
        self.load_dataframe(which, df, os.path.basename(path))

    def load_dataframe(self, which, df, name):
        columns = [NO_COLUMN] + list(df.columns)
        guess = abstract_tools.guess_column(list(df.columns), core.TITLE_ALIASES) or NO_COLUMN
        if which == "a":
            self.df_a = df
            self.label_a.configure(text=f"A: {name} ({len(df):,} records)")
            combos = [self.title_a, self.extra_a] + [entry[1] for entry in self.match_pairs]
        else:
            self.df_b = df
            self.label_b.configure(text=f"B: {name} ({len(df):,} records)")
            combos = [self.title_b, self.extra_b] + [entry[2] for entry in self.match_pairs]
        for combo in combos:
            _update_combobox_values(combo, columns)
            if combo.get() not in columns:
                combo.set(NO_COLUMN)
        combos[0].set(guess)
        self._reset_compare_pairs()
        self.summary = None
        self.status_filter.configure(values=[self.ALL_FILTER])
        self.status_filter.set(self.ALL_FILTER)
        self.column_filter.configure(values=[self.ANY_COLUMN])
        self.column_filter.set(self.ANY_COLUMN)
        ready = self.df_a is not None and self.df_b is not None
        self.compare_btn.configure(state="normal" if ready else "disabled")
        self.records, self.visible_records = [], []
        self._render_results()

    # -- comparison ----------------------------------------------------------

    def on_compare(self):
        if self.df_a is None or self.df_b is None:
            return
        title_pair = (self._selected_column(self.title_a, self.df_a),
                      self._selected_column(self.title_b, self.df_b))
        if not all(title_pair):
            messagebox.showwarning("Choose the title columns",
                                   "Title is required for matching: pick the title column in both files.")
            return
        match_pairs = []
        for _row, combo_a, combo_b in self.match_pairs:
            pair = (self._selected_column(combo_a, self.df_a), self._selected_column(combo_b, self.df_b))
            if all(pair):
                match_pairs.append(pair)
            elif any(pair):
                messagebox.showwarning("Incomplete match column",
                                       "Each extra match column needs a column from both files (or remove it).")
                return
        compare_pairs = [(a, b) for a, b, selected in self.compare_pairs if selected]
        if not compare_pairs:
            messagebox.showwarning("Choose columns to compare",
                                   "Tick at least one column pair under “2. Columns to compare”.")
            return
        self.used_title_pair, self.used_match_pairs = title_pair, match_pairs
        self.records, self.summary = compare_tools.compare_by_column_pairs(
            self.df_a, self.df_b, title_pair, match_pairs, compare_pairs,
            ignore_case=bool(self.ignore_case_var.get()))
        counts = self.summary["status_counts"]
        self.status_filter.configure(values=[f"{self.ALL_FILTER} ({len(self.records):,})"] + [
            f"{status} ({counts[status]:,})" for status in compare_tools.STATUS_ORDER if counts[status]])
        self.status_filter.set(
            f"{compare_tools.DIFFERENT} ({counts[compare_tools.DIFFERENT]:,})"
            if counts[compare_tools.DIFFERENT] else f"{self.ALL_FILTER} ({len(self.records):,})")
        field_counts = self.summary["field_difference_counts"]
        self.column_filter.configure(values=[self.ANY_COLUMN] + [
            f"{label} ({count:,})" for label, count in
            sorted(field_counts.items(), key=lambda item: -item[1]) if count])
        self.column_filter.set(self.ANY_COLUMN)
        matched = counts[compare_tools.SAME] + counts[compare_tools.DIFFERENT]
        self.status_var.set(
            f"Matched {matched:,} records ({counts[compare_tools.DIFFERENT]:,} with differences, "
            f"{counts[compare_tools.SAME]:,} identical in the compared columns) · "
            f"{counts[compare_tools.ONLY_A]:,} only in A · {counts[compare_tools.ONLY_B]:,} only in B"
            + (f" · {counts[compare_tools.NO_TITLE]:,} without a title" if counts[compare_tools.NO_TITLE] else "")
            + f" · compared {len(compare_pairs)} column(s).")
        self._build_results_table(self.summary["compared_labels"])
        self._render_results()
        if self.setup.winfo_manager():
            self._toggle_setup()  # give the results the room; "Show setup" brings it back

    def _configure_status_tags(self, tree):
        dark = ctk.get_appearance_mode() == "Dark"
        for status, (light, dark_color) in self.STATUS_COLORS.items():
            tree.tag_configure(status, background=dark_color if dark else light)
        tree.tag_configure("key", foreground=_TREEVIEW_COLORS["accent"])

    def _build_results_table(self, labels):
        if self.table is not None:
            self.table.destroy()
        headers = ["Status", "Title", "Differing columns"] + [f"{label}  (A → B)" for label in labels]
        self.table = ResultsTable(self.results_holder, headers=headers,
                                  weights=[0, 3, 1] + [2] * len(labels), on_select=self._show_detail)
        self.table.tree.column("c0", width=95, stretch=False)
        self.table.tree.column("c1", width=360, stretch=False)
        for position in range(3, len(headers)):
            self.table.tree.column(f"c{position}", width=380, stretch=False)
        self._configure_status_tags(self.table.tree)
        self.table.pack(fill="both", expand=True)

    def _filtered_records(self):
        status = self.status_filter.get().rsplit(" (", 1)[0]
        column = self.column_filter.get()
        column = None if column == self.ANY_COLUMN else column.rsplit(" (", 1)[0]
        text = self.title_search.get().strip().casefold()
        return [record for record in self.records
                if (status == self.ALL_FILTER or record["status"] == status)
                and (column is None or column in record["differences"])
                and (not text or text in record["title"].casefold())]

    def _render_results(self):
        self.visible_records = self._filtered_records()
        labels = self.summary["compared_labels"] if self.summary and self.records else []
        rows, full = [], []
        for record in self.visible_records:
            cells_full, cells = [], []
            for label in labels:
                value_a, value_b, differs = record["values"][label]
                text = f"{value_a or '(empty)'}  →  {value_b or '(empty)'}" if differs else ""
                cells_full.append(text)
                cells.append(text if len(text) <= 160 else text[:157] + "…")
            base = [record["status"], record["title"], ", ".join(record["differences"])]
            rows.append(base[:1] + [base[1][:150]] + base[2:] + cells)
            full.append(base + cells_full)
        self.table.set_rows(rows, copy_values=full,
                            row_tags=[record["status"] for record in self.visible_records])
        self.export_btn.configure(state="normal" if self.visible_records else "disabled")
        self.detail_table.set_rows([])
        self.detail_label.configure(
            text=f"Showing {len(self.visible_records):,} of {len(self.records):,} records — select one to see "
                 "both files side by side." if self.records else
                 "Select a record to see both files side by side.")

    def _show_detail(self, index):
        if index >= len(self.visible_records):
            return
        record = self.visible_records[index]
        row_a = self.df_a.loc[record["index_a"]] if record["index_a"] is not None else None
        row_b = self.df_b.loc[record["index_b"]] if record["index_b"] is not None else None
        where = []
        if row_a is not None:
            where.append(f"row {record['index_a'] + 2} in A")
        if row_b is not None:
            where.append(f"row {record['index_b'] + 2} in B")
        note = f" · matched by {record['match_note']}" if record["match_note"] and row_a is not None \
            and row_b is not None else (f" · {record['match_note']}" if record["match_note"] else "")
        self.detail_label.configure(text=f"{record['status']} · {' ↔ '.join(where)}{note}")
        rows, tags = [], []
        for column_a, column_b in [self.used_title_pair] + list(self.used_match_pairs):
            value_a = compare_tools._clean(row_a.get(column_a, "")) if row_a is not None else ""
            value_b = compare_tools._clean(row_b.get(column_b, "")) if row_b is not None else ""
            rows.append((f"{compare_tools.pair_label(column_a, column_b)}  (match key)", value_a, value_b))
            tags.append("key")
        for label in self.summary["compared_labels"]:
            value_a, value_b, differs = record["values"][label]
            rows.append((label, value_a, value_b))
            tags.append(compare_tools.DIFFERENT if differs else "plain")
        self.detail_table.set_rows(rows, row_tags=tags)

    def on_export(self):
        if self.visible_records:
            headers, rows = compare_tools.comparison_export_rows(
                self.visible_records, self.summary["compared_labels"])
            _export_table_rows(self, rows, headers, "comparison.csv", format_label=self.output_format.get())


# ---------------------------------------------------------------------------
# Translate: machine-translate a text column with Azure Translator or DeepL
# ---------------------------------------------------------------------------

class TranslateSettingsDialog(ctk.CTkToplevel):
    """API keys for the translation providers, saved into the same shared
    settings file as Sources' email/API keys (see core.save_settings)."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Translation API keys")
        self.geometry("620x520")
        self.minsize(560, 460)
        self.transient(master.winfo_toplevel())

        ctk.CTkLabel(
            self, text="Translation API keys", font=ctk.CTkFont(size=20, weight="bold"),
            anchor="w").pack(fill="x", padx=18, pady=(18, 6))
        ctk.CTkLabel(
            self, text="Saved automatically as you type, and restored next time you open the app. "
                       "You only need a key for whichever provider you pick on the Translate page.",
            justify="left", anchor="w", wraplength=560,
            text_color=("gray25", "gray75")).pack(fill="x", padx=18, pady=(0, 12))

        self.azure_key_entry = self._labeled_entry(
            "Azure Translator API key", "optional",
            "Free F0 tier: 2,000,000 characters/month. Create a \"Translator\" resource at "
            "portal.azure.com to get a key and region.")
        self.azure_region_entry = self._labeled_entry(
            "Azure Translator region", "e.g. eastus",
            "The \"Location/Region\" shown next to the key on the Azure resource's Keys and "
            "Endpoint page - required, translation requests fail without it.")
        self.deepl_key_entry = self._labeled_entry(
            "DeepL API key", "optional",
            "Free tier: 500,000 characters/month, generally the higher translation quality. "
            "Sign up at deepl.com/pro-api - a free-tier key ends in \":fx\".")
        self.mymemory_email_entry = self._labeled_entry(
            "MyMemory contact email", "optional, no account needed",
            "No signup and no key at all - MyMemory works with nothing filled in here, at "
            "5,000 characters/day. Adding an email here raises that to 50,000/day (that pool "
            "is shared by everyone using the same email, so your own address only helps if "
            "you're the only one using it). Translation quality is behind Azure/DeepL, but "
            "it's the only option here with no account and no card needed.")

        self._load_saved_settings()
        for entry, key in ((self.azure_key_entry, "translate_azure_key"),
                           (self.azure_region_entry, "translate_azure_region"),
                           (self.deepl_key_entry, "translate_deepl_key"),
                           (self.mymemory_email_entry, "translate_mymemory_email")):
            entry.bind("<FocusOut>", lambda e: self._save())
            entry.bind("<Return>", lambda e: self._save())

        ctk.CTkButton(self, text="Close", width=100, command=self.destroy).pack(
            anchor="e", padx=18, pady=(6, 16))

    def _labeled_entry(self, label, placeholder, note):
        ctk.CTkLabel(self, text=label, anchor="w").pack(fill="x", padx=18, pady=(6, 0))
        entry = ctk.CTkEntry(self, placeholder_text=placeholder)
        entry.pack(fill="x", padx=18, pady=(2, 0))
        ctk.CTkLabel(self, text=note, text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11),
                    anchor="w", justify="left", wraplength=560).pack(fill="x", padx=18, pady=(0, 4))
        return entry

    def _load_saved_settings(self):
        saved = core.load_settings()
        for entry, key in ((self.azure_key_entry, "translate_azure_key"),
                           (self.azure_region_entry, "translate_azure_region"),
                           (self.deepl_key_entry, "translate_deepl_key"),
                           (self.mymemory_email_entry, "translate_mymemory_email")):
            value = saved.get(key)
            if value:
                entry.delete(0, "end")
                entry.insert(0, value)

    def _save(self):
        core.save_settings({
            "translate_azure_key": self.azure_key_entry.get().strip(),
            "translate_azure_region": self.azure_region_entry.get().strip(),
            "translate_deepl_key": self.deepl_key_entry.get().strip(),
            "translate_mymemory_email": self.mymemory_email_entry.get().strip(),
        })


class TranslatePage(ctk.CTkFrame):
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
        ctk.CTkButton(top, text="⚙ Settings…", width=110, command=self.on_open_settings).pack(side="right")
        ctk.CTkButton(top, text="🗑 Clear cache…", width=130, command=self.on_clear_cache).pack(
            side="right", padx=(0, 6))

        export_row = ctk.CTkFrame(self, fg_color="transparent")
        export_row.pack(fill="x", padx=4, pady=(0, 8))
        self.output_format = _add_export_format_menu(export_row)
        self.export_btn = ctk.CTkButton(export_row, text="Export translated copy…", width=190,
                                        command=self.on_export, state="disabled")
        self.export_btn.pack(side="left")

        mapping = ctk.CTkFrame(self)
        mapping.pack(fill="x", padx=4, pady=(0, 4))
        self.title_col = NoteLinkRecoveryPage._mapping(mapping, "Title column", 0)
        self.abstract_col = NoteLinkRecoveryPage._mapping(mapping, "Abstract column", 1)

        settings_row = ctk.CTkFrame(self)
        settings_row.pack(fill="x", padx=4, pady=(0, 6))
        ctk.CTkLabel(settings_row, text="Provider", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(8, 2))
        self.provider_menu = ctk.CTkOptionMenu(
            settings_row, values=[translate_tools.PROVIDER_LABELS[p] for p in translate_tools.PROVIDERS],
            width=180, command=self._on_provider_change)
        self.provider_menu.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 10))

        ctk.CTkLabel(settings_row, text="Translate to", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=1, sticky="w", padx=10, pady=(8, 2))
        self.language_menu = ctk.CTkOptionMenu(settings_row, values=["English"], width=180)
        self.language_menu.grid(row=1, column=1, sticky="w", padx=10, pady=(0, 10))
        self._on_provider_change(self.provider_menu.get())

        self.overwrite_var = ctk.BooleanVar(
            value=bool(core.load_settings().get("translate_overwrite_existing", False)))
        ctk.CTkCheckBox(
            settings_row, variable=self.overwrite_var, command=self._save_overwrite_setting,
            text="Re-translate rows that already have a bracketed translation\n"
                 "(unchecked = skip them and save quota)").grid(
                     row=1, column=2, sticky="w", padx=10, pady=(0, 10))

        run_row = ctk.CTkFrame(self, fg_color="transparent")
        run_row.pack(fill="x", padx=4, pady=(0, 6))
        self.run_btn = ctk.CTkButton(run_row, text="Translate", width=110,
                                     command=self.on_run, state="disabled")
        self.run_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(run_row, text="Stop", width=75, command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))

        self.progress = ctk.CTkProgressBar(self)
        self.progress.pack(fill="x", padx=4, pady=(0, 5)); self.progress.set(0)
        self.status_var = ctk.StringVar(
            value="Pick a Title and/or Abstract column (at least one). Each is translated in "
                  "place as \"original [translated]\" — the original text is always kept, nothing "
                  "is overwritten with translation-only text. The source language is auto-detected, "
                  "and a field already in the target language is skipped entirely — not even sent "
                  "to the API — so it costs no quota and is left as the bare original with no "
                  "brackets added. Add your API key(s) under Settings before translating.")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", justify="left",
                     wraplength=1100).pack(fill="x", padx=4, pady=(0, 5))
        self.stats_box = ctk.CTkTextbox(self, height=110, wrap="word", font=ctk.CTkFont(size=14))
        self.stats_box.pack(fill="x", padx=4, pady=(0, 8))
        _set_readonly_text(
            self.stats_box, "Run a translation to see a summary here: how many records were "
                            "already in the target language, how many were translated (and from "
                            "which source languages), and how many came from the local cache.")
        self.table = ResultsTable(
            self, headers=["Title", "Abstract"], weights=[1, 2])
        self.table.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        self.after(150, self._poll_events)

    def _on_provider_change(self, _label=None):
        provider = self._current_provider()
        labels = translate_tools.provider_language_labels(provider)
        current = self.language_menu.get()
        self.language_menu.configure(values=labels)
        self.language_menu.set(current if current in labels else ("English" if "English" in labels else labels[0]))

    def _current_provider(self):
        label = self.provider_menu.get()
        for provider, provider_label in translate_tools.PROVIDER_LABELS.items():
            if provider_label == label:
                return provider
        return translate_tools.PROVIDERS[0]

    def _save_overwrite_setting(self):
        core.save_settings({"translate_overwrite_existing": bool(self.overwrite_var.get())})

    def on_open_settings(self):
        TranslateSettingsDialog(self)

    def on_clear_cache(self):
        count = translate_tools.translate_cache_entry_count()
        if count == 0:
            messagebox.showinfo("Translation cache", "The cache is already empty.")
            return
        if not messagebox.askyesno(
                "Clear translation cache",
                f"Delete {count:,} cached translation(s)? This can't be undone — anything that "
                f"was cached will need a fresh API call (and quota) the next time it comes up."):
            return
        translate_tools.clear_translate_cache()
        messagebox.showinfo("Translation cache", f"Cleared {count:,} cached translation(s).")

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
                              (self.abstract_col, abstract_tools.ABSTRACT_ALIASES)):
            menu.configure(values=[NO_COLUMN] + columns)
            menu.set(abstract_tools.guess_column(columns, aliases) or NO_COLUMN)
        encoding = self.df.attrs.get("source_encoding")
        self.file_label.configure(
            text=f"{os.path.basename(path)} ({len(self.df):,} records)" + (f" · {encoding}" if encoding else ""))
        self.run_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.progress.set(0); self.table.set_rows([])

    def on_run(self):
        if self.df is None or self.running:
            return
        title_column = None if self.title_col.get() == NO_COLUMN else self.title_col.get()
        abstract_column = None if self.abstract_col.get() == NO_COLUMN else self.abstract_col.get()
        columns = [column for column in (title_column, abstract_column) if column]
        if not columns:
            messagebox.showwarning(
                "Nothing to translate", "Choose a Title and/or Abstract column to translate.")
            return
        provider = self._current_provider()
        target_label = self.language_menu.get()
        target_lang = translate_tools.language_code(target_label, provider)
        if not target_lang:
            messagebox.showwarning(
                "Language not supported",
                f"{translate_tools.PROVIDER_LABELS[provider]} has no code for {target_label!r}.")
            return
        saved = core.load_settings()
        if provider == "azure":
            api_key, region = saved.get("translate_azure_key"), saved.get("translate_azure_region")
            if not api_key or not region:
                messagebox.showwarning(
                    "Missing API key", "Add an Azure Translator API key and region under Settings first.")
                return
        elif provider == "deepl":
            api_key, region = saved.get("translate_deepl_key"), None
            if not api_key:
                messagebox.showwarning("Missing API key", "Add a DeepL API key under Settings first.")
                return
        else:  # mymemory - no key required at all, an email is only an optional quota boost
            api_key, region = saved.get("translate_mymemory_email") or None, None

        self.running = True; self.cancel_event.clear(); self.progress.set(0)
        self.run_btn.configure(state="disabled"); self.stop_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.status_var.set(f"Translating with {translate_tools.PROVIDER_LABELS[provider]}…")

        def worker():
            try:
                result_df, done, total, stats = translate_tools.translate_dataframe_fields(
                    self.df, columns, target_lang, provider, api_key, region=region,
                    overwrite_existing=self.overwrite_var.get(),
                    cancel_event=self.cancel_event,
                    progress_callback=lambda done, total: self.events.put(("progress", (done, total))))
                self.events.put(("done", (result_df, title_column, abstract_column, done, total, stats)))
            except translate_tools.TranslationError as exc:
                partial = (exc.partial, title_column, abstract_column) if exc.partial else None
                self.events.put(("error", (str(exc), partial)))
            except Exception as exc:
                self.events.put(("error", (str(exc), None)))
        threading.Thread(target=worker, daemon=True).start()

    def on_stop(self):
        if self.running:
            self.cancel_event.set()
            self.status_var.set("Stopping after the current batch…")

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    done, total = payload
                    self.progress.set(done / max(1, total))
                    self.status_var.set(f"Translated {done:,}/{total:,} records…")
                elif kind == "done":
                    self.output_df, title_column, abstract_column, done, total, stats = payload
                    self.running = False
                    self.run_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    self.export_btn.configure(state="normal")
                    fields = ", ".join(f"'{c}'" for c in (title_column, abstract_column) if c)
                    if done < total:
                        self.status_var.set(f"Stopped after {done:,}/{total:,} — {fields} updated so far.")
                    else:
                        self.status_var.set(f"Done. {fields} translated in place.")
                    _set_readonly_text(self.stats_box, translate_tools.format_stats_summary(stats))
                    self._show_results(title_column, abstract_column)
                elif kind == "error":
                    message, partial = payload
                    self.running = False
                    self.run_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    if partial is not None:
                        # Keep what was translated before the failure visible
                        # and exportable instead of discarding it.
                        (self.output_df, done, total, stats), title_column, abstract_column = partial
                        self.export_btn.configure(state="normal")
                        self.status_var.set(
                            f"Stopped after {done:,}/{total:,}: translation failed. Rows translated so far "
                            "are shown and can be exported; they're cached, so a later run skips them.")
                        _set_readonly_text(self.stats_box, translate_tools.format_stats_summary(stats))
                        self._show_results(title_column, abstract_column)
                    else:
                        self.status_var.set("Translation failed.")
                    messagebox.showerror("Translation stopped", message)
        except queue.Empty:
            pass
        self.after(150, self._poll_events)

    def _show_results(self, title_column, abstract_column):
        rows = []
        for _, row in self.output_df.head(250).iterrows():
            rows.append((
                abstract_tools.clean_value(row.get(title_column, ""))[:300] if title_column else "",
                abstract_tools.clean_value(row.get(abstract_column, ""))[:300] if abstract_column else "",
            ))
        self.table.set_rows(rows)

    def on_export(self):
        if self.output_df is not None:
            _save_enriched_dataframe(self, self.output_df, self.file_path, "translated",
                                     format_label=self.output_format.get())


# ---------------------------------------------------------------------------
# File converter: CSV / RIS / BibTeX with column selection and row filters
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    App().mainloop()
