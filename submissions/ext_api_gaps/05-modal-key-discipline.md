# Title

User extensions need a way to take temporary ownership of keys (modal mode) without manually suspending other commands

# Description

## Summary

Some extensions are inherently modal: they enter a state where a
known set of keys should be interpreted by the extension, and the
other commands those keys are normally bound to should be
suspended until the extension exits the mode. Browse mode, OCR
mode, future "review cursor on a virtual buffer" modes, and
NVDA-style content-recognition modes all share this shape.

Today the only way to achieve this from a user extension is to
import `command_manager` directly, iterate
`get_all_keyboard_commands()`, call `cmd.set_suspended(True)` on
each external command that conflicts, and reverse the operation
on mode exit. This is exactly what the perf-branch built-in OCR
does (and what the user-extension version has to do too -- see
`OcrExtension._activate_mode_keys` in the attached extension).

The user-extensions doc does not sanction `command_manager` as a
public extension surface, and the manual suspend/restore dance is
fragile (forgetting to restore on an unexpected exit path leaves
the user's bindings permanently suspended until Orca restart).

## Proposed API

```python
# Take temporary ownership of the listed key combinations. While
# in modal mode, any other command bound to one of these keys is
# suspended; this extension's commands on these keys remain active.
# Returns a context manager (or False on failure).
self.controller.enter_modal_mode(
    keys: list[tuple[str, int]],   # [(keysym, modifier_mask), ...]
) -> bool

# Reverse enter_modal_mode: restore previously-suspended commands
# and suspend the extension's mode-only commands.
self.controller.exit_modal_mode() -> bool

# Convenience: True iff the calling extension is currently in
# modal mode.
self.controller.is_in_modal_mode() -> bool
```

If we adopt the context-manager style:

```python
class OCRExtension(Extension):
    def enter_ocr_mode(self):
        self.controller.enter_modal_mode([
            ("KP_Up",    keybindings.NO_MODIFIER_MASK),
            ("KP_Down",  keybindings.NO_MODIFIER_MASK),
            # ... etc
        ])
        # extension's own commands on these keys still fire;
        # flat-review's commands on the same keys are suspended
        # automatically.

    def exit_ocr_mode(self):
        self.controller.exit_modal_mode()
```

The framework tracks which commands it suspended (per-extension
state) so cleanup on Orca shutdown, extension disable, or process
crash is automatic. The user is guaranteed never to be stuck with
their bindings half-suspended.

## Implementation sketch

Extension framework adds a per-extension
`_modal_suspended_commands: list[Command]` slot. `enter_modal_mode`:

1. For each `(keysym, mod)` in the requested set:
   - Walk `command_manager.get_all_keyboard_commands()`
   - For each command whose binding matches AND whose name is not
     in the calling extension's own commands:
     - If already suspended, skip (don't double-suspend)
     - Else `set_suspended(True)`, append to
       `_modal_suspended_commands`
2. Activate the calling extension's commands for those keys
   (`set_suspended(False)`).
3. Record on the extension instance that it is in modal mode.

`exit_modal_mode` reverses: unsuspend the saved commands,
re-suspend the extension's mode-only commands, clear the modal
state.

On extension disable, framework calls `exit_modal_mode` if the
extension is still in modal mode -- safety cleanup.

## Why a framework API is better than per-extension dancing

1. **Ownership tracking**: extension framework knows which
   commands which extension suspended, so multiple modal
   extensions don't accidentally restore each other's commands.
2. **Cleanup guarantees**: framework auto-restores on extension
   disable, Orca shutdown, crashed handler, etc.
3. **Conflict detection**: framework can refuse `enter_modal_mode`
   if another extension is already modal on overlapping keys (or
   queue / stack the modes -- design discussion).
4. **Public API**: extension code is portable and doesn't break
   when `command_manager` internals change.

## Use case

The OCR extension takes ownership of NumPad keys while OCR mode
is active so user can navigate the virtual OCR buffer with flat-
review-like keys without flat-review's own commands firing. Today
this requires:

```python
from orca import command_manager  # not sanctioned
manager = command_manager.get_manager()
for cmd in manager.get_all_keyboard_commands():
    binding = cmd.get_keybinding()
    if binding and (binding.keysymstring, binding.modifiers) in target_pairs \
       and cmd.get_name() not in our_command_names \
       and not cmd.is_suspended():
        cmd.set_suspended(True)
        self._externally_suspended.append(cmd)
# ... activate our commands ...
```

Other extension use cases:
- A "vim-like cursor mode" extension (h/j/k/l navigation)
- A "screen-region selector" extension (Shift+arrow to define a
  rectangle on screen)
- Audio-themes' theme-editor mode
- Any extension that exposes a "press these keys to navigate this
  thing I just built"

## Design discussion welcome

This is the largest of the controller-API additions and
intentionally proposed as a sketch rather than a finished spec.
Open questions worth thinking through:

- **Stacking vs. flat**: can two extensions be in modal mode at
  once on disjoint keys? On overlapping keys?
- **Scope**: are some keys "off limits" (e.g. always-active Orca
  modifier combos)?
- **Discovery**: should `enter_modal_mode` accept a
  human-readable mode name that shows up in Orca's diagnostics
  ("OCR mode is currently active")?
- **Auto-exit**: should modal mode auto-exit on focus change to a
  different window? On Orca command that wasn't part of the modal
  set? (Probably yes for some, no for others.)

I'd love to defer to your design preferences on these. Happy to
draft an MR once the shape is settled.

## Suggested labels

- `1. Feature`
- `8. Accessibility`
- Possibly: `Discussion` if you have a label for design-first
  issues

## Related

The most invasive of the user-extension API additions. Builds on
the focus-window, clipboard, and mouse-event controller methods
(small, mechanical) but is itself a design call that touches the
command-manager core.
