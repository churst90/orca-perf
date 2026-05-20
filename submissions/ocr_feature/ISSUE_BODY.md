# Add OCR-based virtual buffer for inaccessible windows, terminal text selection, and "select to speak"

## Summary

Adds a new screen-reader mode that captures the focused window's pixels,
runs Tesseract over them, and presents the recognized text as a virtual
buffer the user navigates with NumPad keys. Selection works with
Shift+nav, copy goes through `clipboard.get_presenter().set_text`, and
synthesized mouse clicks at the cursor word's screen coordinates pass
through to the source window because no Orca surface exists on screen
during OCR mode.

This is the screen-reader equivalent of NVDA's content recognition
(NVDA+R) feature, adapted to Orca's existing input intercept and
extension machinery. It is opt-in (a single keybinding, dormant
otherwise) and uses entirely stable internal API surface.

## Issues this closes (or makes substantial progress on)

- **#706** Add a feature to allow selecting and copying of text in
  flat review. The OCR mode supports the exact workflow the issue
  author requested: move cursor to start, mark anchor, move cursor to
  end, copy. The terminal use case the issue mentions works because
  OCR reads pixels, not the accessibility tree.
- **#249** Select to speak a specific UI element. OCR fills this role
  for any window that can be screenshotted, including overlays and
  inaccessible widgets.
- **#670** Terminal output in GNOME Console and other GTK4/VTE4
  terminals is garbled and broken. Bypasses the VTE accessibility
  layer entirely.
- **#202** Orca cannot read in scrolled terminal. OCR reads what's
  on screen regardless of scrollback state.

## What the user experiences

Press `Orca+R` while focused on any window. Orca says "Recognizing.",
runs OCR (0.3 – 2 s depending on window size), and announces "OCR mode
on. N lines. <first word>". The user is now in a virtual buffer over
the recognized text.

| Press | Action |
|---|---|
| `NumPad 8` / `KP_Up` | previous line, spoken |
| `NumPad 2` / `KP_Down` | next line |
| `NumPad 4` / `KP_Left` | previous word |
| `NumPad 6` / `KP_Right` | next word |
| `NumPad 1` / `KP_End` | previous character |
| `NumPad 3` / `KP_Page_Down` | next character |
| `NumPad 7` / `KP_Home` | first word of buffer |
| `NumPad 9` / `KP_Page_Up` | last word of buffer |
| `NumPad 5` / `KP_Begin` | re-speak current word |
| Shift + any nav above | extend selection in that direction |
| `NumPad .` / `KP_Decimal` | set selection anchor at cursor |
| `NumPad +` / `KP_Add` | copy selection (or current line) to clipboard |
| `NumPad /` / `KP_Divide` | left-click in source window at cursor word |
| `NumPad *` / `KP_Multiply` | right-click at cursor word |
| `NumPad Enter` | left-click (alias of `NumPad /`) |
| `NumPad -` / `KP_Subtract` | exit OCR mode |
| `Orca+R` again | exit OCR mode |

Exiting restores the previously suspended flat-review bindings.

## Architecture

Four new modules, ~1400 lines of code total. All consumed API surface
is already present in `upstream/main` (confirmed by transplanting the
files into a fresh upstream worktree, applying the patch, and verifying
parse + smoke import).

- `ocr_buffer.py` — `OCRWord` / `OCRLine` / `OCRBuffer` frozen
  dataclasses. The buffer preserves per-word absolute screen
  coordinates so click pass-through can land on the original pixel
  without re-running OCR.
- `ocr_capture.py` — `Gdk.pixbuf_get_from_window` primary path,
  ImageMagick `import` subprocess fallback, and a `Gdk.Pixbuf`-based
  2x bilinear upscale helper. Upscaling before OCR raises Tesseract's
  hit rate on small UI text from ~70% to ~95% with ~50 ms overhead.
- `ocr_engine.py` — Subprocess wrapper around `tesseract <png> - tsv`.
  Parses TSV, filters words below confidence 30 (the standard NVDA-
  era threshold for UI text), translates image-local bounding boxes
  to absolute screen coordinates by capture origin and upscale
  factor.
- `ocr_presenter.py` — `Extension` singleton. Owns the `Orca+R`
  binding, the `(line, word, char)` virtual cursor, the selection
  anchor, the mode-gated NumPad keys, and the suspend/restore
  bookkeeping for the flat-review commands whose bindings it
  temporarily owns.

Integration with the rest of Orca:

- One line added to `src/orca/meson.build` for each new file.
- One entry added to `default.Script._register_builtin_extensions` so
  the loader sets up the OCR commands the same way it does for every
  other extension.
- Zero changes to any other file.

No private API is touched by `ocr_presenter` other than a single
iteration over `command_manager._keyboard_commands.values()` during
mode activation, used to find external commands whose bindings need
to be suspended (matching the mode-discipline pattern). A small
public `command_manager.iter_keyboard_commands()` would let this
become public API; happy to add it in a follow-up patch if you'd
prefer.

## Production-readiness disclosure

The feature works end-to-end on the author's Fedora 44 / MATE / X11
setup. It has not yet been polished to upstream standards in several
specific places, all of which are additive (no architectural rework):

- **i18n**: all spoken strings are bare English literals. They need
  to be routed through `messages.py`, `cmdnames.py`, and
  `guilabels.py`. The `GROUP_LABEL = "OCR"` constant should become
  a `guilabels.KB_GROUP_OCR` entry.
- **Settings**: the OCR language is hardcoded `"eng"`. A new
  gsettings schema entry under `org.gnome.orca.ocr` with a string
  value would let users switch.
- **Unit tests**: none yet. `ocr_buffer` has the cleanest pure-Python
  surface to start with; `ocr_engine` can be tested with prerecorded
  TSV fixtures; `ocr_presenter` would benefit from a fake
  `command_manager` for mode-toggle behavior.
- **Wayland portal capture**: the capture backend is X11-only via Gdk
  + ImageMagick. xdg-desktop-portal support is a clean addition to
  `ocr_capture.py` without touching the presenter.
- **Async pipeline**: Tesseract runs synchronously on the GLib main
  loop. A spoken "Recognizing." cue masks the freeze, but a
  GLib spawn_async-based version would be better UX for large
  windows.
- **User documentation**: no entry in `help/C/orca/`. A
  `commands_OCR.page` is a small clean addition.

The author is happy to do any of these as follow-up patches once
the architecture is approved.

## Why no GTK widget

Earlier iterations of this feature tried a visible result dialog and
several invisible-window approaches. All of them fought one or more of:

- **Synthesized click delivery**: XTest events go to the topmost
  window at the target coords. Any Orca window mapped at those coords
  receives the click instead of the source app. On compositor-less
  X11 (MATE without picom etc.), even `opacity=0` windows are real
  input surfaces. Off-screen positioning is clamped back on-screen by
  many window managers.
- **Focus traffic**: Any Orca window that grabs and returns focus
  corrupts the source window's selection state in GTK list/tree
  widgets, because focus-on-row and selected-row are independent in
  those widgets and focus return doesn't restore selection. This
  surfaces as the prefs-categories list reporting "not selected"
  for every row after OCR.
- **Re-OCR feedback loop**: A visible Orca window gets captured by
  the next OCR pass, leading to the user navigating to text inside
  Orca's own window and "clicking" on its position.

The pure-virtual cursor design avoids all three by being precisely
nothing on screen. The buffer state lives only in the presenter; the
keyboard intercept that drives it is Orca's existing
`Atspi.Device`-based listener via `command_manager`.

## Tested on

- Fedora 44 + MATE + X11 (real Xorg), Python 3.14, AT-SPI 2.60.3,
  Tesseract 4.1.0 + `tesseract-langpack-eng`.
- Targets exercised: Orca's own preferences dialog (clicking
  categories), GNOME Calculator, the desktop / panel, terminal
  windows including text inside scrollback.

## Patch

Single squashed patch against `upstream/main` HEAD `b20d990c6`:
`0001-ocr_presenter-Add-NVDA-style-OCR-buffer-with-click-p.patch`
(58 KB, 6 files changed, 1457 insertions).

Apply with:
```sh
git checkout -b try-ocr upstream/main
git apply 0001-ocr_presenter-Add-NVDA-style-OCR-buffer-with-click-p.patch
meson install -C builddir
sudo dnf install tesseract tesseract-langpack-eng  # or your distro's
                                                    # equivalent
```

Then `orca --replace` and press `Orca+R` on any window with text.
