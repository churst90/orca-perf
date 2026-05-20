# Title

User extensions need controller API for the currently focused window and its screen geometry

# Description

## Summary

The user-extensions framework's `controller` object exposes
`present_message_internal`, `execute_command_internal`,
`get_value_internal`, and `set_value_internal`. None of those let a
user extension answer the questions "what window has the keyboard
focus right now?" and "where is it on the screen?".

Both questions are needed by virtually any extension that wants to
act on the focused content. Today extensions answer them by reaching
into `orca.focus_manager` directly and calling
`Atspi.Component.get_extents()` with `Atspi.CoordType.SCREEN`. The
user-extensions doc does not sanction `focus_manager` as a public
extension surface.

## Proposed API

Two new methods on the controller:

```python
# Returns the Atspi.Accessible currently designated as the active
# window by focus_manager, or None if no window has focus.
self.controller.get_active_window() -> "Atspi.Accessible | None"

# Returns (x, y, width, height) of the active window in absolute
# screen coordinates, or None if no window is focused or its
# component extents cannot be read.
self.controller.get_active_window_screen_rect() -> "tuple[int, int, int, int] | None"
```

Implementation is a one-line wrapper for each:

```python
def get_active_window(self):
    return focus_manager.get_manager().get_active_window()

def get_active_window_screen_rect(self):
    win = focus_manager.get_manager().get_active_window()
    if win is None:
        return None
    try:
        r = Atspi.Component.get_extents(win, Atspi.CoordType.SCREEN)
    except GLib.Error:
        return None
    return (int(r.x), int(r.y), int(r.width), int(r.height))
```

## Why a controller method rather than "just use Atspi"

`Atspi` is already a public dep — that part is fine. The piece that
requires Orca internals is **"which window has the focus right
now,"** which is Orca's own focus tracking (not raw AT-SPI focus,
which has subtle differences for some toolkits). Extensions that
roll their own focus tracking will get inconsistent answers
compared to what Orca is announcing to the user. A controller
method ensures the extension and Orca always agree on "the focused
window."

The `_screen_rect` variant exists because the `CoordType.SCREEN` vs
`CoordType.WINDOW` distinction is a well-known footgun (a
top-level window queried with `CoordType.WINDOW` returns `(0, 0)`
on most AT-SPI implementations, which silently breaks any
screen-coordinate math downstream). A controller method removes the
ambiguity.

## Use case

The OCR extension (`~/.local/share/orca/extensions/ocr.py`) needs
both calls in `_enter_ocr_mode`:

```python
window = focus_manager.get_manager().get_active_window()  # <-- GAP
if window is None:
    self._say("OCR: no focused window.")
    return
rect = Atspi.Component.get_extents(window, Atspi.CoordType.SCREEN)
x, y, w, h = int(rect.x), int(rect.y), int(rect.width), int(rect.height)
# ...capture pixels at (x, y, w, h)
```

Other plausible extension use cases:
- A "save focused window as PDF" extension
- A "describe focused window" extension (size, position on screen, app name)
- Any future content-recognition / vision-AI extension
- An "always announce screen-corner position" accessibility helper

## Workaround today

```python
from orca import focus_manager  # not sanctioned by extension docs
window = focus_manager.get_manager().get_active_window()
```

Works but couples the extension to an internal module name that may
be refactored.

## Suggested labels

- `1. Feature`
- `8. Accessibility`

## Related

Encountered while adapting the OCR extension to the user-extensions
framework. See companion issues for clipboard, mouse-event
synthesis, modal-key discipline, and multi-file extensions.
