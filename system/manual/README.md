# User guide

`Literature Lookup - User Guide.pdf` is the English user guide shipped with the
exe. It is generated from annotated screenshots of the running app.

## Regenerating it after the interface changes

Run from this folder, in order:

```bash
python make_demo.py        # sample files in demo/ (from ../../dataset and ../../AI URL)
python capture_part1.py    # main window, Single Lookup, Sources settings, Batch Import
python capture_part2.py    # Verification, Abstract Finder, ISSP tags, Note links, Keywords
python capture_part3.py    # dialogs, Review & Convert, Statistics, Compare, Translate
python build_manual.py     # shots/*.png -> the PDF (needs reportlab)
```

- The capture scripts start the app with a fresh, empty settings folder, so no
  personal email or API key appears in a screenshot, and hide the
  "match by meaning" option the same way the packaged exe does.
- Parts 1 and 2 query the real scholarly databases (a few minutes).
- Screenshots use Windows `PrintWindow`, so they work while the window is
  covered; if the screen is locked a shot is retried for up to a minute.
- Numbered red markers are drawn next to widgets; `build_manual.py` holds the
  matching explanation for every number, plus all the guide's text.
- `build_manual.py` needs `reportlab` (`pip install reportlab`); it uses the
  DejaVu Sans font that ships with matplotlib.
