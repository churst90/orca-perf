# User Extensions

## Status

This feature is early and experimental. The following are still pending:

- A page in Orca's Preferences dialog for managing approval and enabling/disabling
  user extensions (currently done manually via `dconf`).
- GSettings support for user extension settings (user extensions cannot yet
  define their own configurable options)
- Preferences UI for user extensions (built-in extensions have their own pages
  in the Preferences dialog; user extensions cannot yet provide one)
- Fixing bugs and improving the API based on user feedback.

## Overview

User extensions allow you to add custom commands to Orca. An extension is a
Python file containing a class that subclasses `Extension`. Each extension can
register keyboard commands that are available while Orca is running.

Extensions live in `~/.local/share/orca/extensions/`. Each `.py` file in that
directory that contains an `Extension` subclass is a potential extension.

## The Controller API

The supported API for user extensions comes from the remote controller, available as
`self.controller` on the `Extension` base class. The controller provides access to all
of Orca's settings and its commands, both bound and unbound.

The following in-process "internal" wrappers should be used in user extensions to avoid
making actual D-Bus calls:

### `present_message_internal(message)`

Presents a message via speech and/or braille:

```python
self.controller.present_message_internal("Text to speak and braille")
```

### `execute_command_internal(module_name, command_name)`

Executes a command registered by an Orca module. The module and command names
correspond to those listed in [remote-controller-commands.md](remote-controller-commands.md).

```python
self.controller.execute_command_internal("SpeechManager", "DecreaseRate")
self.controller.execute_command_internal("SpeechManager", "ToggleSpeech")
self.controller.execute_command_internal("WhereAmIPresenter", "WhereAmIBasic")
self.controller.execute_command_internal("StructuralNavigator", "NextHeading")
```

### `get_value_internal(module_name, property_name)`

Reads a runtime value from an Orca module. The property names correspond to the getter names in
[remote-controller-commands.md](remote-controller-commands.md).

```python
rate = self.controller.get_value_internal("SpeechManager", "Rate")
pitch = self.controller.get_value_internal("SpeechManager", "Pitch")
volume = self.controller.get_value_internal("SpeechManager", "Volume")
voice = self.controller.get_value_internal("SpeechManager", "CurrentVoice")
synth = self.controller.get_value_internal("SpeechManager", "CurrentSynthesizer")
muted = self.controller.get_value_internal("SpeechManager", "SpeechIsMuted")
layout = self.controller.get_value_internal("CommandManager", "KeyboardLayoutIsDesktop")
```

### `set_value_internal(module_name, property_name, value)`

Sets a runtime value on an Orca module. The property names follow the same convention as
`get_value_internal`:

```python
self.controller.set_value_internal("SpeechManager", "Rate", 50)
self.controller.set_value_internal("SpeechManager", "Pitch", 5.0)
self.controller.set_value_internal("SpeechManager", "Volume", 8.0)
```

## Event Subscription API (perf-branch addition)

Extensions can subscribe to Orca's internal events to react when speech,
braille, or keyboard input flows through the screen reader. Callbacks run on
Orca's main thread; subscribers should return promptly and dispatch heavier
work elsewhere.

### `subscribe_speech_emitted(callback)` / `unsubscribe_speech_emitted(callback)`

Fires whenever `SpeechManager.speak()` is called. The callback receives the
text Orca was about to send to the speech server.

```python
def on_speech(text, voice_name, rate, pitch, volume):
    # Mirror outbound speech to a remote master, a log, a transcript window...
    pass

self.controller.subscribe_speech_emitted(on_speech)
```

### `subscribe_braille_emitted(callback)` / `unsubscribe_braille_emitted(callback)`

Fires from `braille._paint_display` with the rendered braille string and
cursor cell position.

```python
def on_braille(text, cursor_cell):
    pass

self.controller.subscribe_braille_emitted(on_braille)
```

### `subscribe_keyboard_event(callback)` / `unsubscribe_keyboard_event(callback)`

Fires before `event.process()` in the input event dispatcher. The callback
receives `(pressed, keycode, keysym, modifiers, text)`. Return `True` from
the callback to consume the event from *Orca's* dispatch (the focused
application may still receive it; for system-level grabs use
`orca_ext_utils.keyboard_grab.KeysetGrab`).

```python
def on_key(pressed, keycode, keysym, modifiers, text):
    if pressed and keysym == 0xff1b:  # XK_Escape
        return True  # Orca won't process this Escape press
    return False

self.controller.subscribe_keyboard_event(on_key)
```

## Output API (perf-branch addition)

### `display_braille_text(text, cursor_cell=-1, duration_ms=None)`

Pushes arbitrary text directly to the local BrlAPI display, bypassing the
region-stack composition path. Useful for notifications, calculator/status
widgets, and test harnesses that need to assert on display state.

```python
self.controller.display_braille_text("Connected", cursor_cell=0)
```

### `synthesize_key_event(keysym, pressed)`

Generates a key press or release at the AT-SPI input layer. The keysym is
an X11 keysym (use `orca_ext_utils.key_combo_helpers.name_to_keysym` to
look one up from a name).

### `synthesize_mouse_event(x, y, button="left")`

Synthesizes a mouse click at screen coords. Coords are interpreted relative
to the active window. Currently uses `Atspi.generate_mouse_event`; a
planned split will narrow this to the AT-SPI path only, with X11 XTest /
Wayland libei fallbacks living in `orca_ext_utils.mouse_input`.

## Clipboard API (perf-branch addition)

### `get_clipboard_text()` / `set_clipboard_text(text)`

Reads / writes the system clipboard as text. The implementation will land
upstream using a gpaste / klipper / GtkClipboard chain; perf-branch ships
a working interim implementation.

```python
text = self.controller.get_clipboard_text()
self.controller.set_clipboard_text("new content")
```

## Window API (perf-branch addition)

### `get_active_window()`

Returns the currently focused toplevel as an `Atspi.Accessible`, or
`None`. Same object the focus manager tracks.

### `get_active_window_screen_rect()` (transitional)

Returns `(x, y, width, height)` of the active toplevel in screen coords,
or `None` when AT-SPI / GTK4 / Wayland can't produce a real value.

**This surface is transitional.** Per upstream review the screen-rect
calculation will move to `orca_ext_utils.screen_rect` because it's only
reliable on X11 and the upstream maintainer does not want a "sometimes
works" API in core. Extensions should call
`orca_ext_utils.screen_rect.for_accessible(controller.get_active_window())`
or `screen_rect.for_active_window()` for new code.

## Modal Mode API (perf-branch addition)

Modal mode is a *surgical* key-grab mechanism: an extension can take over
a specific set of (keysym, modifier) pairs for the duration of a mode,
without affecting any other Orca chord. Motivating use case:
orca-remote's master-mode key forwarding, where a remote NVDA master
sends Orca+Ctrl+R via XTest and the local slave's own binding for that
chord must be suspended to prevent two voices saying "Recognizing..." in
unison.

### `enter_modal_mode(extension, keys)`

Take ownership of the given keys. `keys` is a `list[tuple[str, int]]`
of (keysym_name, modifier_mask) pairs. Returns `True` on success, `False`
if another extension is already modal.

```python
keys = [("r", 0x05)]  # Ctrl+Shift+r
ok = self.controller.enter_modal_mode(self, keys)
```

### `exit_modal_mode(extension)`

Release the modal grab. Must be called by the same extension instance
that entered it. The base `Extension.disable()` automatically exits any
modal mode the extension owns.

### `is_in_modal_mode()` / `get_modal_owner()`

Diagnostics. `get_modal_owner()` returns the `Extension` instance that
currently holds modal mode, or `None`.

## Companion Library: `orca-ext-utils`

Some capabilities that extensions need are *deliberately* not in the
controller, on the principle that Orca shouldn't carry utilities it
doesn't need itself. The companion library
[`orca-ext-utils`](https://github.com/churst90/orca-ext-utils) covers
those gaps. It is plain Python; extensions can either depend on it or
vendor the relevant modules into their `.orca-ext` archive.

| Module | What it does | Why not in controller |
|---|---|---|
| `screen_rect` | Screen-coord rect for an AT-SPI accessible | X11-only path; AT-SPI doesn't reliably support it on Wayland |
| `screen_capture` | Region capture (Gdk / ImageMagick / xdg-desktop-portal) | Three-backend chain; portal path requires per-session token management |
| `mouse_input` | Click / press / move synthesis at coords | XTest path is X11-only; libei / portal path is the Wayland fallback |
| `keyboard_grab` | `Atspi.Device.add_key_grab` batch wrapper | System-level (not Orca-dispatch) consume; only some extensions need it |
| `window_info` | Active toplevel rect + X11 window ID | XID is X11-only; Wayland would need portal session |
| `compositor_query` | Multi-monitor geometry, DPI scaling, refresh rate | Gdk-direct; extension-specific need (overlay positioning) |
| `text_to_braille` | Text -> cell bytes (liblouis optional) | liblouis is a heavy optional C dep |
| `notification` | libnotify desktop-notification facade | Orca doesn't use desktop notifications itself |
| `process_supervisor` | Sync + GLib-async subprocess with timeout | Extension-specific; Orca doesn't spawn helper processes |
| `key_combo_helpers` | keysym / modifier parsing and formatting | Pure-Python convenience; no Orca-side need |
| `extension_settings` | Per-extension key/value store (GSettings or JSON) | Bridges the gap until upstream extension-settings UI lands |

## Example

```python
"""Example extension demonstrating custom Orca commands."""

from orca import keybindings
from orca.command import Command, KeyboardCommand
from orca.extension import Extension


class HelloWorld(Extension):
    """Example extension with greeting and voice-info commands."""

    GROUP_LABEL = "Hello World"

    def _get_commands(self) -> list[Command]:
        return [
            KeyboardCommand(
                "say_hello_slowly",
                self.say_hello_slowly,
                self.GROUP_LABEL,
                "Says hello slowly",
                desktop_keybinding=keybindings.KeyBinding(
                    "F9", keybindings.ORCA_MODIFIER_MASK,
                ),
                laptop_keybinding=keybindings.KeyBinding(
                    "F9", keybindings.ORCA_MODIFIER_MASK,
                ),
            ),
            KeyboardCommand(
                "say_goodbye_fast",
                self.say_goodbye_fast,
                self.GROUP_LABEL,
                "Says goodbye fast",
                desktop_keybinding=keybindings.KeyBinding(
                    "F10", keybindings.ORCA_MODIFIER_MASK,
                ),
                laptop_keybinding=keybindings.KeyBinding(
                    "F10", keybindings.ORCA_MODIFIER_MASK,
                ),
            ),
            KeyboardCommand(
                "get_voice_settings",
                self.get_voice_settings,
                self.GROUP_LABEL,
                "Reports current voice settings",
                desktop_keybinding=keybindings.KeyBinding(
                    "F8", keybindings.ORCA_MODIFIER_MASK,
                ),
                laptop_keybinding=keybindings.KeyBinding(
                    "F8", keybindings.ORCA_MODIFIER_MASK,
                ),
            ),
        ]

    def say_hello_slowly(self):
        """Decreases the speech rate, says hello, then restores it."""

        original_rate = self.controller.get_value_internal("SpeechManager", "Rate")
        self.controller.set_value_internal("SpeechManager", "Rate", 20)
        self.controller.present_message_internal("Hello, world!")
        self.controller.set_value_internal("SpeechManager", "Rate", original_rate)
        return True

    def say_goodbye_fast(self):
        """Increases the speech rate 5 times, says goodbye, then restores it."""

        original_rate = self.controller.get_value_internal("SpeechManager", "Rate")
        for _i in range(5):
            self.controller.execute_command_internal(
                "SpeechManager", "IncreaseRate",
            )
        self.controller.present_message_internal("Goodbye, world!")
        self.controller.set_value_internal("SpeechManager", "Rate", original_rate)
        return True

    def get_voice_settings(self):
        """Reports the current voice settings."""

        rate = self.controller.get_value_internal("SpeechManager", "Rate")
        volume = self.controller.get_value_internal("SpeechManager", "Volume")
        pitch = self.controller.get_value_internal("SpeechManager", "Pitch")
        pitch_range = self.controller.get_value_internal(
            "SpeechManager", "PitchRange",
        )
        voice = self.controller.get_value_internal(
            "SpeechManager", "CurrentVoice",
        )
        synthesizer = self.controller.get_value_internal(
            "SpeechManager", "CurrentSynthesizer",
        )

        parts = [
            f"Voice: {voice}",
            f"Synthesizer: {synthesizer}",
            f"Rate: {rate}",
            f"Pitch: {pitch}",
            f"Pitch range: {pitch_range}",
            f"Volume: {volume}",
        ]
        self.controller.present_message_internal(". ".join(parts))
        return True
```

### Required Class Attributes

- `GROUP_LABEL`: The label used to group this extension's commands in Orca's
  keybindings list.

### Command Functions

Command functions take no arguments (other than `self`) and return `True` when
handled. The base class automatically wraps them to be compatible with Orca's
internal command dispatch.

### Keybindings

Each command needs a `KeyboardCommand` with a name, function, group label,
description, and optional keybindings for desktop and laptop layouts. If no
keybinding is provided, the user can assign one in preferences.

Available modifier masks:

- `keybindings.NO_MODIFIER_MASK`
- `keybindings.ORCA_MODIFIER_MASK` (Insert or Caps Lock, depending on layout)
- `keybindings.SHIFT_MODIFIER_MASK`
- `keybindings.CTRL_MODIFIER_MASK`
- `keybindings.ALT_MODIFIER_MASK`
- `keybindings.ORCA_SHIFT_MODIFIER_MASK`
- `keybindings.ORCA_CTRL_MODIFIER_MASK`

## Approving Extensions

For security, extensions must be approved before Orca will load them. When Orca
discovers an unapproved extension, it logs the file's SHA256 hash.

Approval and revocation can be done via the command line:

```sh
# Approve an extension:
orca --approve-extension my_extension.py

# Revoke approval:
orca --revoke-extension my_extension.py
```

These commands compute the file's SHA256 hash and persist the approval in dconf.
The extension will be loaded on the next Orca startup.

If you edit an approved extension, its hash changes and Orca will not load it
until you re-approve it. If you delete an extension file, its approval entry
remains but has no effect. There is currently no automatic cleanup of stale
approvals.

## Disabling Extensions

Any extension (built-in or user) can be disabled by adding its class name to
the `disabled-extensions` list. A disabled extension's commands are not
registered, do not appear in the keybindings list, and cannot be triggered.
For built-in extensions like `StructuralNavigator` or `CaretNavigator`, this
effectively removes that functionality from Orca entirely until re-enabled.

Until the extension management UI is implemented, disabling is done via `dconf`.
Note that `dconf write` replaces the entire list:

```sh
# Disable one extension:
dconf write /org/gnome/orca/default/extensions/disabled-extensions \
    "['HelloWorld']"

# Disable multiple extensions:
dconf write /org/gnome/orca/default/extensions/disabled-extensions \
    "['HelloWorld', 'StructuralNavigator']"

# Re-enable all:
dconf reset /org/gnome/orca/default/extensions/disabled-extensions
```

Disabling a user extension does not revoke its approval. Re-enabling it is just
a matter of removing it from the disabled list.
