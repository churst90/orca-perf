# Title

User extensions need controller API for synthesizing mouse events

# Description

## Summary

The user-extensions framework has no way for an extension to
synthesize a mouse click. Extensions that need to programmatically
interact with the focused window (click a UI element identified by
some non-AT-SPI means, route the pointer for sighted handoff,
trigger a hover, etc.) must today reach into
`ax_device_manager.get_manager().generate_mouse_event(obj, x, y,
button)`. The user-extensions doc does not sanction
`ax_device_manager` as a public extension surface.

## Proposed API

```python
# Synthesize a mouse event at the given absolute screen
# coordinates. `button` is one of:
#   "left"           - left button single click
#   "right"          - right button single click
#   "middle"         - middle button single click
#   "double-left"    - left button double click
#   "left-press"     - left button press (no release)
#   "left-release"   - left button release (paired with press)
#   "move"           - pointer motion only, no buttons
# Returns True on success.
self.controller.synthesize_mouse_event(
    screen_x: int, screen_y: int, button: str = "left",
) -> bool
```

Implementation wraps the existing `ax_device_manager` call, with
the obj parameter determined from `get_active_window()` plus
relative-coord translation:

```python
def synthesize_mouse_event(self, screen_x, screen_y, button="left"):
    win = focus_manager.get_manager().get_active_window()
    if win is None:
        return False
    try:
        rect = Atspi.Component.get_extents(win, Atspi.CoordType.SCREEN)
    except GLib.Error:
        return False
    code = {
        "left": "b1c", "right": "b3c", "middle": "b2c",
        "double-left": "b1d",
        "left-press": "b1p", "left-release": "b1r",
        "move": "abs",
    }.get(button)
    if code is None:
        return False
    rel_x = screen_x - int(rect.x)
    rel_y = screen_y - int(rect.y)
    return ax_device_manager.get_manager().generate_mouse_event(
        win, rel_x, rel_y, code,
    )
```

The screen-vs-window coord conversion is something every extension
that synthesizes clicks would otherwise reimplement (and would
easily get wrong; the AT-SPI device API takes coords relative to
the obj's screen position, not absolute screen coords). Putting it
behind the controller method eliminates a common footgun.

## Security note

Synthesized mouse events go through the same XTest pathway as
real input on X11 and through the compositor on Wayland (via
AT-SPI device); they are indistinguishable from real input. An
extension with this API can click anywhere on the user's screen.
This is no different from the situation today (extensions can
import `ax_device_manager` anyway), but the user-extensions
framework's approval gate becomes load-bearing for this API: the
SHA256 approval is what stops a malicious extension from being
loaded silently.

Worth considering: an extension manifest declaring
`requires-mouse-synth = true` and an explicit user prompt on
first use, similar to the `xdg-desktop-portal` screenshot
permission model.

## Use case

The OCR extension's whole point is to make inaccessible windows
clickable by the user navigating recognized text and pressing
NumPad / or NumPad *. Without mouse synthesis, OCR is read-only
and "click on what you found" is impossible. Today:

```python
from orca import ax_device_manager  # not sanctioned
# ...
ax_device_manager.get_manager().generate_mouse_event(
    self._source_window, rel_x, rel_y, "b1c",
)
```

Other extension use cases:
- A future "click on the named UI element" voice-command extension
- "Right-click the focused widget" as a single keystroke
- Hover triggers for tooltip-bearing widgets
- Demo / training extensions that walk a sighted user through clicks

## Suggested labels

- `1. Feature`
- `8. Accessibility`

## Related

Companion issue to the focus-window and clipboard controller-API
additions. All three are the "act on the focused window"
primitives that any non-trivial extension needs.
