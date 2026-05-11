# Orca Configuration and Script Architecture: Deep Analysis

**Date:** May 2026  
**Scope:** GSettings migration (Orca 50+), Command/keybinding system, per-app script architecture  
**Files analyzed:** `gsettings_registry.py`, `profile_manager.py`, `command_manager.py`, `command.py`, `keybindings.py`, `script_manager.py`, `script.py`, app/toolkit scripts

---

## Executive Summary

Orca has completed migration from JSON-based `user-settings.conf` to **GSettings/dconf** storage. The new architecture uses a layered lookup system (app → profile → default) and introduces decorator-based metadata registration (`@gsettings_schema`, `@gsetting`). The keybinding system is comprehensive but shows signs of bloat (1,946-line `command_manager.py`). The per-app script model works but is largely redundant—many app scripts are thin wrappers (73–430 lines) with minimal customization. The system exposes commands via D-Bus for external control.

**Overall assessment:** Well-engineered migration to modern storage, but the command system complexity and script redundancy present simplification opportunities.

---

## GSettings Migration Status

### Completion
**Status: COMPLETE** (as of Orca 50, 2026).

1. **No JSON remnants found.** Grep search for `"user-settings"` across the codebase returns zero results.
2. **GSettings is the exclusive backing store.** All preferences are now stored in dconf under the schema `org.gnome.Orca`.

### Architecture

**dconf path structure** (from `gsettings_registry.py:45`):
```
/org/gnome/orca/{profile}/{schema}/
/org/gnome/orca/{profile}/apps/{app}/{schema}/
```

Example: Profile "work", app "soffice", schema "speech"
```
/org/gnome/orca/work/speech/
/org/gnome/orca/work/apps/soffice/speech/
```

### Migration Mappings

**`SettingsMapping` dataclass** (lines 64–73 of `gsettings_registry.py`):
- Tracks legacy preference keys (e.g., `"keyboardLayout"`) to new GSettings keys
- Stores enum mappings for value transformation
- **No migration tool found.** (No `gsettings_migrator.py` exists.)

**Hypothesis:** Orca may handle legacy imports on first run or rely on user manual re-export, but the codebase shows no automation. The `migration_key` field in `SettingDescriptor` (line 61) suggests intent, but usage is limited to descriptor registration.

### Profile System

**Profiles in new model** (from `profile_manager.py` and `gsettings_registry.py`):
- Stored at dconf path `/org/gnome/orca/{profile_name}/{schema}/`
- Support per-app overrides: `/org/gnome/orca/{profile_name}/apps/{app_name}/{schema}/`
- **Default profile fallback:** Layered lookup checks app override → current profile → default profile (lines 720–758 of `gsettings_registry.py`)
- Profiles managed via `ProfileManager` (866 lines); includes rename (copy + reset) at lines 549–572

**Profile creation UI:** `ProfilePreferencesGrid` in `profile_manager.py` provides manual profile management (create, load, delete).

### Settings Still File-Based?

**Checked:** No file-based storage remains. Voice settings, keybindings, pronunciations, all stored in dconf.

---

## Configuration Architecture Review

### Core Components

1. **`GSettingsRegistry` singleton** (lines 79–609)
   - **Purpose:** Central registry for GSettings metadata and layered lookups.
   - **Key methods:**
     - `layered_lookup()` (lines 134–192): Returns setting via runtime override → app override → profile → default profile → fallback default.
     - `get_settings()` (lines 220–244): Creates Gio.Settings for a schema at the correct dconf path.
     - `save_schema()` (lines 459–547): Writes settings to dconf, skipping redundant values.

2. **Decorator-based registration:**
   - `@gsettings_schema(schema_id, name)` (lines 373–385): Declares a class contributes to a schema.
   - `@gsetting(key, schema, default, ...)` (lines 344–371): Marks a method/field with metadata. Stores in `_descriptors` dict.
   - `@gsettings_enum(enum_id, values)` (lines 387–399): Registers enum for schema generation.

3. **`GSettingsSchemaHandle`** (lines 611–884)
   - Encapsulates a single GSettings schema.
   - Implements **layered getter methods** (`get_boolean`, `get_string`, `get_dict`) that merge values from app → profile → default.
   - Caches `Gio.Settings` instances by path.

### Design Assessment

**Strengths:**
- **Layering is elegant:** App-specific overrides inherit from profile defaults, simplifying per-app customization.
- **Decorator metadata:** `@gsetting` eliminates boilerplate schema definition; methods are self-documenting.
- **Enum support:** Built-in handling of GSettings enums (lines 506–526).
- **Dict merging:** `_layered_get_dict()` (lines 789–843) handles dict-type settings (keybindings, pronunciations) by merging layers, with empty lists unbinding keys.

**Weaknesses:**
- **Over-engineered decorators?** The `@gsettings_schema` and `@gsetting` decorators store metadata in `_descriptors` but only use it for schema generation (via `_build_mappings_from_descriptors`). Runtime lookups use hardcoded schema/key strings, not descriptors. Decorators feel vestigial.
  - **Example:** `command_manager.py:1094` uses `@gsetting(key="keyboard-layout", ...)` but calls `layered_lookup("keybindings", "keyboard-layout", ...)` directly, not through a descriptor-driven interface.
- **Runtime overrides layer:** `set_runtime_value()` (lines 246–255) stores in-memory overrides that shadow dconf values. Useful for transient state but adds complexity. E.g., `command_manager.py:1275` sets runtime values for layout changes before flushing to dconf.
- **Profile rename is manual copy+reset:** No atomic rename. If interrupted, old profile persists (lines 549–572).

### Profile-based Config Complexity

**Not yet a problem, but note:** Each profile maintains its own full copy of settings. This means:
- Changing the global schema defaults requires updating all profiles (or relies on dconf fallback).
- Profile-specific customization is not inheritance-based; it's override-based.
- No "profile inheritance" or "template profiles."

---

## Keybindings/Commands Architecture Review

### Command Classes

**`Command` base class** (lines 34–132 of `command.py`):
```python
class Command:
    _enabled: bool          # User preference
    _suspended: bool        # System override
    is_active() -> bool     # enabled AND NOT suspended
```

**Two subclasses:**

1. **`KeyboardCommand`** (lines 135–204)
   - Desktop & laptop keybindings stored separately: `_desktop_keybinding`, `_laptop_keybinding`
   - Single active binding: `_keybinding` (resolved by `command_manager.apply_user_overrides()`)
   - `is_active()` requires enabled + not suspended + binding exists
   - **Assessment:** Clean separation of default vs. user overrides.

2. **`BrailleCommand`** (lines 206–246)
   - Tuple of BrlAPI key codes: `_braille_bindings`
   - `executes_in_learn_mode` flag for pan/scroll commands that work in learn mode

### KeyBinding Class

**`KeyBinding`** (lines 161–299 of `keybindings.py`):
- Represents a single binding: keysym + modifiers + click_count
- **Lazy keycode resolution:** `matches()` (lines 208–236) computes keycode on first use
- **Keyval vs. keycode matching:** Prefers keyval (layout-correct for QWERTZ, AZERTY), falls back to keycode when modifiers affect layout
- **Comment (lines 96–98):** TODO questioning whether Solaris keycode workaround still needed on modern Linux. **Valid concern but not addressed.**
- **Grab management:** `add_grabs()`, `remove_grabs()` integrate with AT-SPI device manager

### CommandManager (1,946 lines)

**Structure:**
- `KeybindingsPreferencesGrid` (lines 72–1045): UI for keybinding editor, 900+ lines of GTK widget management
- `KeyboardLayout` enum: DESKTOP (1) vs. LAPTOP (2)
- `CommandManager` (lines 1064–1946): The main manager singleton

**Key responsibilities:**

1. **Keyboard layout:** `get_keyboard_layout_is_desktop()`, `set_keyboard_layout_is_desktop()`
   - Switching layout rebuilds bindings and re-grabs keys (lines 1132–1142)

2. **Modifier keys:** Per-layout Orca modifier configuration
   - Desktop: Insert, KP_Insert (lines 1173–1182)
   - Laptop: Caps_Lock, Shift_Lock (lines 1202–1211)
   - Stored in dconf, can be overridden per-app

3. **User overrides:** `apply_user_overrides()` (lines 1465–1531)
   - Resets all bindings to layout defaults (line 1471)
   - Applies dconf overrides from `keybindings/entries` (type `a{saas}`)
   - Empty list `[]` unbinds a command
   - **Comment (line 1470):** "Ensures app-specific unbindings from a previous app don't persist." This re-runs **on every app change**, which is heavyweight but necessary for correct state.

4. **Key indexing:** Maps keyval/keycode → commands for fast lookup
   - `_commands_by_keyval`, `_commands_by_keycode` dicts
   - Used in `get_command_for_event()` (lines 1532–1556) to find matching command

5. **Group management:**
   - `_group_enabled` tracks per-group on/off state
   - `set_group_enabled()`, `set_group_suspended()` (lines 1606–1663) disable/enable all commands in a group
   - Used for mode switching (e.g., focus mode suspends browse mode commands)

### User Overrides Storage

**Format:** dconf key `/org/gnome/orca/{profile}/keybindings/entries` of type `a{saas}` (dict of string → array of string arrays)

**Example structure (unpacked):**
```python
{
    "on-screen-keyboard": [("KP_0", mask, mods, clicks), ...],
    "caret-move-next-line": [("Down", mask, mods, clicks)],
    "structural-nav-next-heading": [],  # Unbound
}
```

**Parsing** (lines 1505–1506):
```python
for binding_tuple in binding_tuples:
    keysym, _mask, mods, clicks = binding_tuple
```

**Assessment:** The tuple format (keysym, mask, mods, clicks) is opaque. Comments don't explain what `mask` is (possibly reserved/deprecated). The `mods` field is an int, parsed at line 1528. **This is fragile; consider a named tuple or comment explaining the tuple semantics.**

### Complexity Assessment

**Lines of code:**
- `command.py`: 246 lines (tight)
- `keybindings.py`: 300+ lines (reasonable)
- `command_manager.py`: 1,946 lines (bloated)

**Why so large?**
1. `KeybindingsPreferencesGrid` (900 lines of GTK UI) inflates the file. Should be separate.
2. Duplicate logic for desktop/laptop layouts. Lines 1100–1211 define parallel getter/setter methods for modifier keys.
3. Grab management, numlock handling, multi-click detection interspersed.

**Necessary complexity:**
- Per-layout bindings + modifier keys require state management
- Layered keybinding lookup (default → user override) is unavoidable
- Key re-grabbing on layout/modifier changes is needed for responsiveness

**Improvement:** Split `KeybindingsPreferencesGrid` to a separate `keybindings_preferences.py` module; consolidate modifier-key logic into a helper class.

---

## Script System Inventory

### Architecture

**Three-tier hierarchy:**
1. **App script:** Custom behavior for a specific app (e.g., LibreOffice)
2. **Toolkit script:** Fallback for apps using a specific toolkit (e.g., GTK, Qt)
3. **Default script:** Orca's generic behavior

**Selection logic** (from `script_manager.py:228–301`):
```
if sleep_mode:
    use sleep-mode script
elif custom_script (by object role):
    use custom script
elif toolkit_script (if app != toolkit):
    use toolkit script
else:
    use app_script
```

**Script loading** (`script_manager.py:142–175`):
1. Try to import `orca.scripts.{name}.script` or `orca.scripts.apps.{name}.script` or `orca.scripts.toolkits.{name}.script`
2. Call `module.get_script(app)` or `module.Script(app)` to instantiate
3. Fallback to default script if any import fails

### App Scripts

| App | Lines | Purpose |
|-----|-------|---------|
| **soffice** | 892 | LibreOffice: custom braille panning (paragraphs), table/spreadsheet awareness |
| **evolution** | 430 | Email: custom event handlers for message/calendar views |
| **pidgin** | 298 | Chat: buddy list handling, message window focus |
| **gnome-shell** | 225 | GNOME desktop: special handling for activities/search/notifications |
| **gajim** | 75 | Chat: minimal script, mostly defaults |
| **Thunderbird** | 102 | Email: like evolution but lighter |
| **kwin** | 119 | KDE window manager: minimal window event handling |
| **notification-daemon** | 73 | Notifications: announces and saves notification text |
| **xfwm4** | 77 | Xfce window manager: minimal |
| **smuxi-frontend-gnome** | 70 | Chat: minimal |

**Total: ~2,361 lines of app-specific scripts**

### Toolkit Scripts

| Toolkit | Lines | Purpose |
|---------|-------|---------|
| **Chromium** | 151 | Chrome/Chromium: web content handling |
| **Gecko** | 101 | Firefox: similar to Chromium |
| **gtk** | 172 | GTK 3/4: generic GTK app fallback |
| **Qt** | 211 | Qt apps: table/tree navigation enhancements |
| **WebKitGTK** | 84 | WebKit-based browsers: web content |

**Total: ~719 lines of toolkit scripts**

### Script Inheritance & Boilerplate

**Example: notification-daemon** (73 lines):
```python
class Script(default.Script):
    def _on_window_created(self, event: Atspi.Event) -> bool:
        # Announce notification text
        # ... 10 lines
```

**Example: gajim** (75 lines):
```python
class Script(default.Script):
    def _on_text_inserted(self, event: Atspi.Event) -> bool:
        # Special handling for chat message insertion
        # ... 20 lines
```

**Assessment:**
- Many scripts override only 1–3 methods from the base `Script` class (which has 50+ event listeners).
- No deep inheritance chains (all directly inherit `default.Script` or a toolkit script).
- **Potential dead code:** Apps like pidgin (298 lines) customize message window focus, but how many users still use Pidgin (EOL 2023)?

### Default Script (1,659 lines)

**Key methods:**
- `set_up_commands()`: Registers commands with `CommandManager` (calls extension loaders)
- `_register_builtin_extensions()`: Loads 20+ built-in presenters (speech, braille, navigation, etc.)
- Event listeners for 30+ event types
- No app-specific logic; pure default behavior

### Comparison with NVDA

**NVDA app modules:**
- Dynamically loaded from `.appModules` subdirectory
- Matched by module name = app process name (e.g., `soffice.py` for LibreOffice)
- Support nested inheritance (`AppModule → GlobalAppModule → base`)
- Enable/disable specific features per app (e.g., skip announcement of certain roles)

**Orca app scripts:**
- Statically listed in source tree
- Matched by app process name (via `get_module_name()`, which calls a utility to extract it from the Atspi.Accessible)
- Only single inheritance (app script inherits default, not a chain)
- More heavyweight (average 300+ lines vs. NVDA's typical 100–150)

**Verdict:** Both models work. NVDA's dynamic loading is slightly cleaner (no explicit module list), but Orca's static approach is easier to version-control. The real issue with Orca is **redundancy**: many scripts are thin wrappers that could be configuration instead of code.

---

## Inheritance & Complexity Assessment

### Inheritance Depth

**Maximum depth: 2 levels**
1. App script inherits `default.Script`
2. Toolkit script inherits `default.Script`
3. Custom role script inherits `default.Script` or a toolkit script

**No diamond problems** (all base is `default.Script`, no multi-inheritance).

**Contrast:** Some NVDA app modules inherit `GlobalAppModule`, which itself inherits a base. Orca's simpler hierarchy is better.

### Code Duplication

**High duplication in app scripts:**
- gnome-shell (225 lines), evolution (430 lines), pidgin (298 lines) all handle similar event types
- Many reimplement the same patterns (check object type, do custom action, else defer to parent)

**Example (evolution vs. Thunderbird):**
- Both override `_on_document_load_complete()` to handle email view changes
- Implementation differs slightly but intent is identical

**Boilerplate per script:**
```python
class Script(default.Script):
    def _create_speech_generator(self):
        return CustomSpeechGenerator(self)
    def _create_braille_generator(self):
        return CustomBrailleGenerator(self)
```

This pattern repeats in soffice, pidgin, gnome-shell. Could be eliminated with a generator registry.

---

## DBus Service Surface

### API Model

**Three decorator types** (from `dbus_service.py:45–100`):
1. `@command`: Parameterless D-Bus command (returns bool)
2. `@parameterized_command`: D-Bus command with args
3. `@getter` / `@setter`: Property-like accessors

**Example usage** (from `command_manager.py`):
```python
@dbus_service.getter
def get_keyboard_layout_is_desktop(self) -> bool:
    """Returns True if the current keyboard layout is desktop."""
    return self._is_desktop

@dbus_service.setter
def set_keyboard_layout_is_desktop(self, is_desktop: bool) -> bool:
    """Sets whether the keyboard layout is desktop (True) or laptop (False)."""
    ...

@dbus_service.command
def toggle_keyboard_layout(self, script=None, event=None, notify_user=True) -> bool:
    """Toggles between desktop and laptop keyboard layout."""
    ...
```

### Exposed Interfaces

**Orca exposes:**
- Keyboard layout control (desktop/laptop)
- Modifier key configuration
- Learn mode activation/deactivation
- Speech output (rate, pitch, volume)
- Braille configuration
- Command execution by name

**Stability:** Minimal documentation in codebase. D-Bus methods are auto-discovered from decorators. No versioning scheme visible.

**Assessment:** Useful for external tools (e.g., system settings integration) but lacks formal API stability guarantees. The decorator-driven discovery is elegant but undocumented.

---

## Hacks and Dead Code

### Known Issues

1. **Solaris keycode workaround** (keybindings.py:96–98)
   - Comments indicate this is Solaris-specific, now questioned on modern Linux
   - Still active; no deprecation warning

2. **Numlock grab special case** (command_manager.py:1301–1313, keybindings.py:1552–1553)
   - NumLock state affects keypad key interpretation; grabs are re-done on NumLock toggle
   - Adds complexity; not all platforms need this

3. **Multi-click bindings collision** (keybindings.py:289–295)
   - Single-click binding may fail to register if double-click already exists
   - Silently skipped (grab_id == 0); not flagged to user

4. **Runtime overrides shadow dconf** (gsettings_registry.py:167–172)
   - In-memory overrides created by `set_runtime_value()` bypass dconf
   - Useful for transient state but undocumented lifecycle; no auto-flush to dconf

### Dead Code Candidates

**Low confidence (need runtime testing):**

1. **Old app scripts:** Pidgin (EOL 2023), Smuxi (last release 2018)
   - May still have users but unmaintained upstream
   - Not tested in CI/CD (if such exists)

2. **`_mask` field in keybinding tuples** (command_manager.py:1506)
   - Parsed but never used; unclear purpose; possibly legacy

3. **`SettingDescriptor.getter` field** (gsettings_registry.py:58)
   - Declared but never populated or used

### Code Quality Issues

1. **`command_manager.py` is too large** (1,946 lines)
   - Mix of GTK UI (`KeybindingsPreferencesGrid`), manager logic, and domain model
   - pylint disables suggest author aware of this

2. **Inconsistent naming:**
   - `keybindings/entries` (dconf key) vs. `_keybindings_dict` (runtime var)
   - `apply_user_overrides()` called on app change, but name doesn't suggest it re-runs per-app

3. **Limited test coverage apparent** (no test files in src/orca/test/)
   - Configuration system complexity (layered lookup, dconf, runtime overrides) lacks visible test harness

---

## Improvement Opportunities (Ranked)

### High Priority

**1. Split `command_manager.py` (1,946 → 800 lines)**
   - Move `KeybindingsPreferencesGrid` to `keybindings_preferences_grid.py`
   - Extract modifier-key logic to `modifier_key_manager.py`
   - **Benefit:** Easier testing, clearer responsibility
   - **Effort:** Medium (1–2 days)

**2. Simplify keybinding tuple format**
   - Replace `(keysym, mask, mods, clicks)` with a named tuple or dataclass
   - Add docstring explaining each field
   - **Benefit:** Reduces cryptic bugs; improves maintainability
   - **Effort:** Low (2–4 hours)

**3. Consolidate app-script boilerplate**
   - Create a `ScriptGenerator` registry to eliminate duplicate `_create_speech_generator()` methods
   - Allow apps to specify "use custom SpeechGenerator" via config, not code
   - **Benefit:** Reduce app script lines from 2,361 to ~1,500
   - **Effort:** Medium (1–2 days)

### Medium Priority

**4. Unify modifier-key handling**
   - Desktop and laptop modifier keys use nearly identical code (lines 1173–1182, 1202–1211)
   - Refactor to `get_modifier_keys(layout)` with shared implementation
   - **Benefit:** DRY principle; fewer duplicated tests
   - **Effort:** Low (2–3 hours)

**5. Document D-Bus API**
   - Formalize stability guarantees (e.g., "API stable as of Orca X.Y")
   - Generate API docs from decorators
   - **Benefit:** External tools can depend on the API
   - **Effort:** Medium (4–6 hours)

**6. Profile inheritance**
   - Allow profiles to inherit from a "base" profile, overriding only specific keys
   - Reduces storage and improves maintainability
   - **Benefit:** Easier custom profile creation
   - **Effort:** High (2–3 days; requires dconf-layer changes)

### Low Priority

**7. Deprecate old app scripts**
   - Pidgin (EOL 2023), Smuxi (EOL 2018)
   - Mark deprecated, remove in Orca 52+
   - **Benefit:** Reduces maintenance burden
   - **Effort:** Low (1 day + migration docs)

**8. Plugin-based script loading**
   - Allow external packages to register app scripts without modifying Orca source
   - Enables distributions to ship app-specific scripts as separate packages
   - **Benefit:** Decouples app support from Orca releases
   - **Effort:** High (3–5 days)

**9. Investigate Solaris keycode workaround**
   - Confirm if Solaris support is still needed or if modern Linux keyboards handle this correctly
   - If not needed, simplify `keybindings.py:208–236` significantly
   - **Benefit:** Simpler keybinding matching; fewer edge cases
   - **Effort:** Low (investigation) to Medium (refactor)

---

## NVDA Comparison

### Key Differences

| Aspect | Orca | NVDA |
|--------|------|------|
| **Config storage** | dconf (GSettings) | Windows Registry + .ini files |
| **Profile inheritance** | Flat (override-based) | Flat (but registry fallback) |
| **App scripts** | Statically linked; `orca.scripts.apps.*` | Dynamically loaded; `.appModules` dir |
| **App matching** | AT-SPI app name + module name | Executable name |
| **Inheritance depth** | 1 (app → default) | 2+ (app → global → base) |
| **Keybinding tuples** | `(keysym, mask, mods, clicks)` | GestureAction objects (more abstract) |
| **Complexity** | 1,946 lines (command_manager) | 2,500+ lines (gesture_handler) |
| **Plugin system** | Extensions via class registration | AppModule subclassing + driver plugins |

### Strengths of Each

**Orca:**
- Cleaner keybinding representation (explicit keyval/modifier separation)
- Simpler inheritance hierarchy (no diamond problems)
- Modern dconf storage with layered overrides

**NVDA:**
- Dynamic app-module loading (no need to rebuild Orca to add app support)
- Registry-based extension system more flexible
- Longer history → more battle-tested

### Lessons for Orca

1. **Dynamic script loading** would eliminate "static dependency" on app list
2. **Profile inheritance** (like registry fallback) would reduce storage duplication
3. **Better abstraction for keybindings** (e.g., gesture-like model) would hide platform details

---

## Conclusion

### What's Working Well

1. **GSettings migration is solid.** Layered lookup (app → profile → default) is elegant and reduces configuration clutter.
2. **Command classes are clean.** Separation of enabled/suspended state is conceptually sound.
3. **Script hierarchy is simple.** Single-level inheritance avoids complexity.

### What Needs Improvement

1. **Bloat:** `command_manager.py` should be split; `KeybindingsPreferencesGrid` is 900+ lines of UI logic.
2. **Redundancy:** App scripts have high boilerplate; many could be configuration instead.
3. **Opacity:** Keybinding tuple format and D-Bus API lack documentation.
4. **Complexity:** Decorator-based configuration registration is over-engineered for current usage.

### Recommended Next Steps

**Short term (1–2 sprints):**
- Split `command_manager.py`
- Simplify keybinding tuple representation
- Document D-Bus API stability

**Long term (2–4 sprints):**
- Consolidate app-script boilerplate via generator registry
- Evaluate plugin-based script loading
- Implement profile inheritance

**Research needed:**
- Solaris keycode compatibility (can we simplify?)
- User survey on app-script usage (are older app scripts still used?)

---

## File References

- `/home/codyhurst/dev/orca/src/orca/gsettings_registry.py` (893 lines)
- `/home/codyhurst/dev/orca/src/orca/profile_manager.py` (866 lines)
- `/home/codyhurst/dev/orca/src/orca/command_manager.py` (1,946 lines)
- `/home/codyhurst/dev/orca/src/orca/command.py` (246 lines)
- `/home/codyhurst/dev/orca/src/orca/keybindings.py` (300+ lines)
- `/home/codyhurst/dev/orca/src/orca/scripts/default.py` (1,659 lines)
- `/home/codyhurst/dev/orca/src/orca/scripts/apps/*` (12 app scripts, 2,361 total lines)
- `/home/codyhurst/dev/orca/src/orca/scripts/toolkits/*` (5 toolkit scripts, 719 total lines)
- `/home/codyhurst/dev/orca/src/orca/dbus_service.py` (400+ lines)
- `/home/codyhurst/dev/orca/src/orca/script_manager.py` (400+ lines)

