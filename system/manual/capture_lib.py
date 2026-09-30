"""Shared helpers for capturing annotated screenshots of the Literature Lookup app."""
import json
import os
import sys
import tempfile
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM = os.path.dirname(HERE)
PROJECT = os.path.dirname(SYSTEM)
SHOTS = os.path.join(HERE, "shots")
DEMO = os.path.join(HERE, "demo")
os.makedirs(SHOTS, exist_ok=True)
sys.path.insert(0, SYSTEM)

# A fresh, empty settings/cache folder: no personal email or API keys in any shot.
import app_paths  # noqa: E402
_TMP = tempfile.mkdtemp(prefix="ll_manual_")
app_paths.data_dir = lambda: _TMP

import literature_lookup as ll  # noqa: E402
# Show what the packaged exe shows: it ships without sentence-transformers.
ll.issp_tags.semantic_matching_available = lambda: False
from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageStat  # noqa: E402

MARK_COLOR = (214, 40, 40)
_marks_log = {}


def start_app(width=1400, height=1040):
    app = ll.App()
    app.geometry(f"{width}x{height}+10+10")
    pump(app, 1.0)
    return app


def pump(widget, seconds):
    end = time.time() + seconds
    while time.time() < end:
        widget.update()
        time.sleep(0.02)


def wait_until(widget, condition, timeout=240, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        widget.update()
        if condition():
            return True
        time.sleep(step)
    return False


def open_file(page, path, method="on_choose_file"):
    with mock.patch.object(ll.filedialog, "askopenfilename", return_value=path), \
            mock.patch.object(ll.messagebox, "showinfo"), mock.patch.object(ll.messagebox, "showwarning"):
        getattr(page, method)()


def _font(size):
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _print_window(win):
    """Render the window's client area via PrintWindow, which works even when
    the screen is locked or the window is covered (screen grabs come out black)."""
    import ctypes
    from ctypes import wintypes
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = int(win.wm_frame(), 16)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(memory_dc, bitmap)
    user32.PrintWindow(hwnd, memory_dc, 2)  # PW_RENDERFULLCONTENT

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
    header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
    gdi32.DeleteObject(bitmap); gdi32.DeleteDC(memory_dc); user32.ReleaseDC(hwnd, window_dc)
    image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
    # Crop the title bar and borders: keep the Tk client area.
    left, top = win.winfo_rootx() - rect.left, win.winfo_rooty() - rect.top
    return image.crop((left, top, left + win.winfo_width(), top + win.winfo_height()))


def shot(win, name, marks=(), crop=None):
    """Capture ``win`` and draw numbered markers. ``marks`` is a list of
    (number, widget[, side]) where side is 'left' (default), 'right' or 'top'."""
    deadline = time.time() + 60
    while True:
        if win.state() != "normal":
            win.deiconify()
        win.lift()
        pump(win, 1.0)
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        image = _print_window(win).convert("RGB")
        if sum(ImageStat.Stat(image).mean) / 3 > 40:  # not a black frame
            break
        if time.time() > deadline:
            print("WARNING: black frame for", name, flush=True)
            break
        pump(win, 3.0)  # screen locked / display asleep: wait and retry
    draw = ImageDraw.Draw(image)
    radius = 19
    font = _font(24)
    for mark in marks:
        number, widget = mark[0], mark[1]
        side = mark[2] if len(mark) > 2 else "left"
        wx, wy = widget.winfo_rootx() - x, widget.winfo_rooty() - y
        ww, wh = widget.winfo_width(), widget.winfo_height()
        if side == "right":
            cx, cy = wx + ww + radius - 4, wy + wh // 2
        elif side == "top":
            cx, cy = wx + 18, wy - radius + 6
        elif side == "inside":
            cx, cy = wx + radius + 4, wy + radius + 4
        else:
            cx, cy = wx - radius + 6, wy + wh // 2
        cx = max(radius + 2, min(w - radius - 2, cx))
        cy = max(radius + 2, min(h - radius - 2, cy))
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     fill=MARK_COLOR, outline=(255, 255, 255), width=3)
        text = str(number)
        box = draw.textbbox((0, 0), text, font=font)
        draw.text((cx - (box[2] - box[0]) / 2 - box[0], cy - (box[3] - box[1]) / 2 - box[1]),
                  text, fill=(255, 255, 255), font=font)
    if crop:
        image = image.crop(crop(image.size))
    path = os.path.join(SHOTS, name + ".png")
    image.save(path)
    _marks_log[name] = [m[0] for m in marks]
    print("saved", name, image.size, flush=True)
    return path


def set_main_tab(app, name, sub=None):
    app.tabview.set(name)
    if sub:
        sub_tabs = [w for w in app.tabview.tab(name).winfo_children()
                    if isinstance(w, ll.ctk.CTkTabview)][0]
        sub_tabs.set(sub)
    pump(app, 0.6)
