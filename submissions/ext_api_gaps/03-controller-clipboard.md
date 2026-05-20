# Title

User extensions need controller API for reading and writing the system clipboard

# Description

## Summary

`ClipboardPresenter` is one of the controller-exposed modules, but
its only command is `PresentClipboardContents` (speak what's on the
clipboard). There is no way for a user extension to **set** the
clipboard contents through the controller, or to read them without
forcing a speech presentation.

Today extensions must `from orca import clipboard` directly and call
`clipboard.get_presenter().set_text(text)` and `.get_text()`. The
user-extensions doc does not sanction `clipboard` as a public
extension surface.

## Proposed API

Two new methods on the controller:

```python
# Place text on the system clipboard. Returns True on success.
self.controller.set_clipboard_text(text: str) -> bool

# Returns the current clipboard text contents, or "" if empty /
# unavailable.
self.controller.get_clipboard_text() -> str
```

Implementation is a thin wrapper:

```python
def set_clipboard_text(self, text):
    return clipboard.get_presenter().set_text(text)

def get_clipboard_text(self):
    return clipboard.get_presenter().get_text() or ""
```

(Whether `get_text` exists today or needs adding to the
`ClipboardPresenter` is a small adjacent question -- the public
`PresentClipboardContents` clearly has access to the contents, so
exposing them programmatically is just plumbing.)

## Use case

The OCR extension copies the user's text selection (Shift+nav +
NumPad +) to the clipboard so they can paste it elsewhere -- a
core workflow for "extract text from an inaccessible window" (the
explicit motivation behind upstream issue #706). Today:

```python
from orca import clipboard  # not sanctioned
# ...
clipboard.get_presenter().set_text(selected_text)
```

Other extension use cases:
- An "OCR + translate" extension: read the clipboard, translate
  text, write it back
- A "compose-and-speak" extension: lets the user dictate text via
  speech recognition, write to clipboard
- A code-snippet manager extension
- A "paste with phonetic spelling" extension that reads the
  clipboard and announces each letter

## Why through the controller rather than allowing direct import

The clipboard implementation in Orca has multiple backends
(GPaste, Klipper, GTK fallback -- see
`src/orca/clipboard.py:106, 161, 256`). Extensions that import
`clipboard` directly couple themselves to the public-but-private
internal split of `_ClipboardManagerFallback` vs. the
`ClipboardPresenter` wrapper. A controller method abstracts that
away.

## Suggested labels

- `1. Feature`
- `8. Accessibility`

## Related

Companion issue to "User extensions need controller API for the
currently focused window" -- both are simple wrappers around
existing internals that user extensions can't reach today without
unsanctioned imports.
