# Example user extensions

This directory holds reference implementations of Orca user
extensions developed in the perf branch.

## ocr.py

NVDA-style OCR / content recognition as a user extension.
**Single-file** (~1400 lines) because the loader hashes per-file
and approves per-file; multi-file extensions would require
approving each helper module separately. See
`submissions/ext_api_gaps/06-multi-file-extensions.md` for the
upstream issue proposing package-style extensions.

To install:

```sh
cp examples/extensions/ocr.py ~/.local/share/orca/extensions/
orca --approve-extension ocr.py
# restart Orca
```

Then press `Orca+R` on any window to enter OCR mode.

**Requires** `tesseract` and at least one language pack
(e.g. `tesseract-langpack-eng`). Failure to find tesseract gives
a spoken error message; no other Orca behavior is affected.

### Controller API surface used

This extension is a demonstration of the public extension API:

| Used                                            | Where it comes from                 |
|-------------------------------------------------|-------------------------------------|
| `self.controller.present_message_internal`      | docs/user-extensions.md (existing)  |
| `self.controller.get_active_window`             | perf-branch commit `766a2e96a`      |
| `self.controller.get_active_window_screen_rect` | perf-branch commit `766a2e96a`      |
| `self.controller.set_clipboard_text`            | perf-branch commit `766a2e96a`      |
| `self.controller.synthesize_mouse_event`        | perf-branch commit `766a2e96a`      |

The single remaining direct-internal import is `command_manager`,
used for modal-key discipline (suspending other commands' bindings
while OCR mode is active). This is a known gap; see
`submissions/ext_api_gaps/05-modal-key-discipline.md` for the
proposed controller API to replace it.

### Companion built-in

The perf branch also carries OCR as a **built-in** extension at
`src/orca/ocr_presenter.py` (plus `ocr_buffer.py`, `ocr_capture.py`,
`ocr_engine.py`). The user-extension version here is intentionally
a single file -- the built-in version is the maintainable
multi-module reference. If you install the user extension, disable
the built-in to avoid both registering on the same keybindings:

```sh
dconf write /org/gnome/orca/profiles/default/extensions/disabled-extensions \
    "['OCRPresenter']"
```

(Reverse with `dconf reset`.)
