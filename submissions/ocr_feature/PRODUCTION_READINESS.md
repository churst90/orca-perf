# OCR feature — production-readiness assessment

Honest disclosure of what's solid, what's untested, and what would
need to change before this can be considered ready for an upstream
merge.

## Verdict

**For personal-fork use:** ready. Confirmed working end-to-end on
Fedora 44 / MATE / X11 with Voxin, MATE prefs dialog, and other
real apps; cursor tracks correctly across char/word/line; selection
and copy work; click pass-through reaches the source window with
no Orca-surface interference.

**For upstream merge:** functionally ready, polish-pending. The
architecture is sound and the API surface used is stable on
upstream/main. The remaining gaps are all additive (i18n, tests,
docs, settings schema) and don't require any redesign.

## What works

| Capability | Status |
|---|---|
| OCR pipeline (capture → upscale → tesseract → buffer) | ✓ working |
| (line, word, char) virtual cursor with NumPad nav | ✓ working |
| Selection with Shift+nav, anchor via NumPad . | ✓ working |
| Copy selection (or current line) to clipboard | ✓ working |
| Click pass-through via XTest at cursor screen coords | ✓ working |
| Mode enter/exit with audio cue + flat-review suspend/restore | ✓ working |
| Tesseract availability check at runtime (graceful) | ✓ working |
| Upstream API compatibility | ✓ verified |
| Patch applies cleanly against upstream/main HEAD | ✓ verified |

## What's verified about upstream compatibility

Every module imported by `ocr_presenter.py` exists on
`upstream/main` and was checked individually:

```
ax_device_manager  ✓   command_manager     ✓   keybindings         ✓
ax_object          ✓   dbus_service        ✓   presentation_manager ✓
clipboard          ✓   debug               ✓   command.KeyboardCommand ✓
focus_manager      ✓   extension.Extension ✓   input_event         ✓
```

The `Extension` class itself is Joanie's own 2026 addition, so
this feature is following her own extension framework, not an
alternative path.

The patch was applied to a fresh worktree at upstream/main HEAD
`b20d990c6` and `git apply --check` returned 0. The 4 new modules
parse cleanly in the upstream tree, leaf modules (`ocr_buffer`,
`ocr_engine`) import and execute correctly in isolation.

## What's not yet upstream-quality

### 1. Internationalization (required for merge)

All spoken strings are bare English literals:

```python
presentation_manager.get_manager().present_message("Recognizing.")
presentation_manager.get_manager().present_message(f"OCR mode on. ...")
```

Upstream convention is to route through:

- `messages.py` for spoken messages
- `cmdnames.py` for command descriptions
- `guilabels.py` for `GROUP_LABEL` etc.

Approximately 25 strings to migrate. Mechanical work, single
follow-up patch.

### 2. Settings schema (recommended for merge)

The OCR language is hardcoded:

```python
def recognize(png_bytes, ..., lang: str = "eng", ...)
```

Should be a gsettings entry under `org.gnome.orca.ocr`:

- `lang`: string, default "eng"
- `upscale-factor`: double, default 2.0
- `confidence-threshold`: int, default 30

Surface in the Orca prefs dialog under a new "Content Recognition"
panel. Follow-up patch.

### 3. Unit tests (required for merge)

Currently zero. The pieces are pure-python-friendly:

- `ocr_buffer.py`: trivial to test with synthetic OCRWord lists
- `ocr_engine.py`: test the TSV parser with prerecorded fixtures
- `ocr_presenter.py`: needs a fake command_manager + focus_manager
  for mode-toggle tests; selection-text extraction is pure logic
  and easy to unit-test in isolation

Estimate: 40-60 unit tests, single follow-up patch.

### 4. Wayland portal capture (defer)

`ocr_capture.py` is X11-only:

```python
xlib = Gdk.get_default_root_window()
pixbuf = Gdk.pixbuf_get_from_window(root, x, y, width, height)
```

On real Wayland this returns `None`. xdg-desktop-portal's
`org.freedesktop.portal.Screenshot.Screenshot` is the standard
path. Adds a permission prompt the first time but works on every
modern Wayland compositor.

This is additive to `ocr_capture.py`; no changes to anything
downstream. Could ship as a separate patch once a Wayland-using
tester is available.

### 5. Async pipeline (nice to have)

Tesseract runs synchronously on the GLib main loop:

```python
result = subprocess.run(
    ["tesseract", str(tmp_path), "-", "-l", lang, "tsv"],
    ..., timeout=timeout, check=False,
)
```

For typical UI windows this is 0.3 - 2 seconds. A spoken
"Recognizing." cue masks the freeze. GLib.spawn_async_with_pipes
+ a callback on stdout completion would let Orca remain responsive
during recognition. Behavior-preserving refactor, single follow-up
patch.

### 6. User documentation (recommended for merge)

No entry in `help/C/orca/`. Should add a `commands_ocr.page`
following the pattern of `commands_flat_review.page`. Single
follow-up patch.

### 7. Private API access (one-line cleanup)

`ocr_presenter._activate_mode_keys` iterates
`command_manager._keyboard_commands.values()` -- a private
attribute -- to find external commands on its target keys for
suspension. A public iter helper on command_manager would let
this become cleaner:

```python
# add to command_manager.py:
def iter_keyboard_commands(self) -> Iterator[KeyboardCommand]:
    return iter(self._keyboard_commands.values())
```

Trivial. Could go in a prep-patch before the OCR patch lands.

## Risk assessment

| Risk | Likelihood | Severity | Mitigation |
|---|---|---|---|
| Click lands on wrong widget | Low | Medium | Coords verified via debug log; user confirmed clicking icons works |
| Suspension fails for some flat-review command | Very Low | Medium | Tested: 19 external commands suspended, all OCR commands fire correctly |
| Buffer state corrupts on long sessions | Low | Low | Cursor is immutable tuple, anchor is None or tuple, no mutable state |
| Tesseract missing breaks Orca startup | Negligible | High if it happened | `ocr_engine.is_available()` checked at command time, not import time; verified Orca starts cleanly without tesseract installed |
| OCR captures Orca's own dialog | N/A | N/A | No Orca surface exists on screen in OCR mode by design |
| Mode-key suspension leaks on crash | Low | Low | If Orca crashes mid-mode, suspended commands stay suspended until restart; no persistent state |

## Where the design has been pressure-tested

Iterations leading to the final design specifically failed at one
or more of these and were discarded. The current design avoids
each:

1. **Modal dialog with synthesized click** — modal grab + async X11
   unmap-notify race made clicks land in our own widget
2. **Non-modal dialog with action invocation** — only works for
   accessible targets; useless for inaccessible apps
3. **"Invisible" Gtk.Window** — MATE without compositor ignored
   opacity hint; WM clamped off-screen positions back on-screen;
   OCR re-captured its own window text on subsequent runs
4. **Orca+arrow keybindings** — clashed with existing Orca+Down
   "say next line" binding; cursor felt detached from any "buffer"
5. **Plain arrows without mode-discipline** — would have eaten
   arrow keys in every application even when OCR was not in use

The pure-virtual + mode-gated NumPad design that landed is
specifically what survived against all of these.

## Bottom line for merge consideration

If you're willing to take a feature with deferred i18n / tests /
docs, this is ready. If those are gates, we have a known path to
deliver all three as small follow-up patches without architectural
change.

If the answer is "interesting, but rebuild N component first," the
isolated module structure makes that easy too -- any of the four
files can be swapped without touching the others, and the
presenter alone owns all of the Orca-integration surface.
