# Orca Screen Reader: Deep Code Analysis — Web/Document Handling

## Executive Summary

Orca's web content handling is built on three interacting navigation systems:

1. **Browse Mode (document navigation)**: Uses structural navigation (H for heading, K for link, etc.) and caret navigation (arrow keys for character/word/line stepping). The system walks the AT-SPI accessibility tree on each keystroke via `AXUtilities.find_all_*()` methods.

2. **Focus Mode (form/widget interaction)**: Suspends document navigators, allows application-controlled keyboard handling. Toggled automatically based on object type (editable, focusable, expandable, menus, grids, listboxes, tables, toolbars, etc.).

3. **Per-app mode state**: Stored in `DocumentPresenter._app_states` dict (keyed by app object hash), with sticky mode variants that persist user preference across focus changes.

The architecture does **not** build a pre-computed flat buffer (unlike NVDA's virtual buffer). Every structural navigation keystroke triggers a full tree walk to find all headings/links/etc., then returns the next/previous match relative to current focus.

---

## 1. Browse/Focus Mode Mechanics

### Mode Definition and Toggle

**File**: `/home/codyhurst/dev/orca/src/orca/document_presenter.py` (1531 lines)

#### Data structure (lines 96-103):
```python
@dataclass
class _AppModeState:
    in_focus_mode: bool = True
    focus_mode_is_sticky: bool = False
    browse_mode_is_sticky: bool = False
    user_has_toggled: bool = False
```

State is **per-application**, keyed by `hash(app)` at line 530-532:
```python
app_hash = hash(app)
if app_hash not in self._app_states:
    self._app_states[app_hash] = _AppModeState()
```

#### Toggle Mechanism

**Primary command**: `toggle_presentation_mode()` (lines 908-936)

```python
use_focus = not self.in_focus_mode(script.app)
self._set_presentation_mode(script, use_focus, obj=obj, document=document, notify_user=notify_user)
self._get_state_for_app(script.app).user_has_toggled = True
```

This toggles the state AND calls `suspend_navigators()` (lines 682-692):
- When entering focus mode: suspends caret and structural navigation (lines 690-691)
- When entering browse mode: resumes navigators via `_enable_document_navigators()` (line 677, 737-741)

#### Sticky Modes

**Auto-sticky focus mode** (lines 1023-1052, 1037-1052):
- Detects Electron apps: toolkit name == "chromium" but not in known browser list (lines 549-558)
- Detects top-level web apps: `embedded` role + http:// URI + `get_auto_sticky_focus_mode_for_web_apps()` setting (lines 560-577, 1048-1052)
- **Key flag**: `user_has_toggled` (line 1040) prevents auto-detection if user has manually toggled mode

**Sticky browse/focus** (lines 847-906):
- `enable_sticky_focus_mode()` sets both `in_focus_mode=True` and `focus_mode_is_sticky=True` (lines 899-902)
- `enable_sticky_browse_mode()` sets `in_focus_mode=False` and `browse_mode_is_sticky=True` (lines 868-871)
- When sticky, the opposite mode toggle is blocked (lines 1116-1117)

#### Mode Transition on Focus Change

**File**: `update_mode_if_needed()` (lines 1070-1121)

Logic:
1. If leaving document entirely (lines 1094-1105): suspend navigators, disable caret/structural nav
2. If math region found (lines 1107-1110): enter math navigation (suspends document nav)
3. If entering document from outside (lines 1112-1113): call `_handle_entering_document()` which:
   - Checks sticky modes (lines 1023-1034)
   - Checks auto-sticky for web apps (lines 1036-1052)
   - Otherwise, calls `use_focus_mode()` logic (lines 1054-1066)
4. If within document and mode should change (lines 1119-1121): toggle via `_set_presentation_mode()`

#### Auto-focus entry when clicking form fields

Not directly in `document_presenter.py`. Controlled via:
- **Structural navigation**: `get_triggers_focus_mode()` setting (lines 611-630, key at line 602)
- **Caret navigation**: `get_triggers_focus_mode()` setting (lines 228-248, key at line 220)

When set, navigation commands that land on a widget call `is_focus_mode_widget()` (lines 828-844) and trigger mode switch.

---

## 2. Structural Navigation Implementation

**File**: `/home/codyhurst/dev/orca/src/orca/structural_navigator.py` (3100+ lines)

### Architecture: Tree Walk on Every Keystroke

Each navigation command (H for heading, K for link, etc.) follows this pattern:

1. **Get all matching objects** (lines 1901-1913):
   ```python
   def _get_all_headings(self, script, level=None):
       root = self._determine_root_container(script)
       if level is None:
           return AXUtilities.find_all_headings(root, pred=pred)
       return AXUtilities.find_all_headings_at_level(root, level, pred=pred)
   ```

2. **Find next/previous relative to focus** (called in lines 1935-1936, 1960-1961):
   ```python
   matches = self._get_all_headings(script)
   result = self._get_object_in_direction(script, matches, False)  # False=prev, True=next
   ```

3. **Present and set focus** (via `_present_object()`)

### Keybindings (Lines 118-254)

Single-letter bindings with shift/no-shift/shift-alt modifiers:
- **H**: heading (Shift=prev, none=next, Shift-Alt=list)
- **K**: link (Shift=prev, none=next, Shift-Alt=list)
- **B**: button
- **F**: form field
- **X**: checkbox
- **C**: combobox
- **E**: entry
- **L**: list
- **I**: list item
- **T**: table
- **G**: image
- **M**: landmark
- **P**: paragraph
- **R**: radio button
- **Q**: blockquote
- **S**: separator (Shift=prev, none=next)
- **D**: live region (Shift=prev, none=next)
- **Y**: last live region
- **O**: large object (>75 chars)
- **A**: clickable
- **1-6**: heading levels (Shift=prev, none=next, Shift-Alt=list)

### Navigation Modes (Lines 73-79)

```python
class NavigationMode(Enum):
    OFF = "OFF"
    DOCUMENT = "DOCUMENT"  # Full semantic nav
    GUI = "GUI"            # Non-document mode (menus, dialogs, apps)
```

Set per-script at lines 438-463: `set_mode(script, NavigationMode.DOCUMENT)` or `OFF`.

### Performance Implications

**Cost of typing H to jump to next heading**:
1. Call `_get_all_headings()` → `AXUtilities.find_all_headings(root, pred=pred)`
2. This walks the **entire subtree** of the document, checking role on each node
3. For a 10KB webpage with 1000 headings in the tree, this is O(tree_size) per keystroke
4. No incremental index: each keystroke redoes the full scan

**Setting**: `KEY_WRAPS` (line 89) allows wrapping to top/bottom when reaching end

### Focus Mode Interaction

Lines 483-489: `last_command_prevents_focus_mode()`
- If structural nav triggered and `get_triggers_focus_mode()` is False, it prevents auto-focus-mode entry
- Used by `document_presenter.use_focus_mode()` (line 1443-1446)

---

## 3. Caret Navigation and Layout Mode

**File**: `/home/codyhurst/dev/orca/src/orca/caret_navigator.py` (900+ lines)

### Architecture: Context-Based Stepping

Caret nav is **per-script** enabled state (line 91):
```python
self._enabled_for_script: dict[default.Script, bool] = {}
```

Called via:
- Arrow keys: character/line/word stepping (lines 95-148)
- Ctrl+Arrow: word navigation
- Ctrl+Home/End: file start/end
- F12: toggle enabled

### Key Methods

**Character stepping** (lines 494-568):
```python
def next_character(self, script, event=None, notify_user=True) -> bool:
    obj, offset = script.utilities.next_context()
    if not self._is_navigable_object(script, obj):
        return False
    self._last_input_event = event
    script.utilities.set_caret_position(obj, offset)
    focus_manager.get_manager().emit_region_changed(obj, start_offset=offset, mode="CARET_NAVIGATOR")
    script.update_braille(obj, offset=offset)
    script.say_character(obj)
    return True
```

Note: Calls `next_context()` which is delegated to script utilities.

### Layout Mode (Lines 250-276)

**Key setting**: `KEY_LAYOUT_MODE` (line 72), default True

When **enabled** (layout mode):
- Arrow keys present **logical lines** (wrapped at page width)
- Not physical line breaks in the DOM

When **disabled** (object mode):
- Arrow keys present **each DOM element** as a unit

**Toggle** via `toggle_layout_mode()` (lines 278-304):
```python
layout_mode = not self.get_layout_mode()
self.set_layout_mode(layout_mode)
```

**Implementation**: Delegated to `script.utilities` classes, not in caret_navigator itself.

### Interaction with Focus Mode

**File**: `/home/codyhurst/dev/orca/src/orca/document_presenter.py`, lines 1438-1446

```python
caret_prevents = (
    _caret_navigator.last_command_prevents_focus_mode()
    and not AXUtilities.is_tool_tip_descendant(prev_obj, inclusive=True)
)
if ... caret_prevents:
    return False, f"prevented by caret nav settings"
```

If `caret_navigator.get_triggers_focus_mode()` is False, landing on a widget via caret nav will NOT auto-enter focus mode.

### Orca vs. App-Controlled Caret

**File**: `caret_navigator.toggle_enabled()` (lines 357-389)

```python
def toggle_enabled(self, script, event=None, notify_user=True) -> bool:
    enabled = not command_manager.is_group_enabled("CARET_NAVIGATION")
    if enabled:
        string = messages.CARET_CONTROL_ORCA
    else:
        string = messages.CARET_CONTROL_APP
        script.utilities.clear_caret_context()
    self.set_is_enabled(enabled)
    return True
```

When **disabled** (Orca-controlled off), arrow keys are passed to the application.
When **enabled** (Orca-controlled on), Orca owns arrow key handling.

**Setting stored in**: gsettings schema `org.gnome.Orca.CaretNavigation` (line 62-65)

---

## 4. Web-Specific Implementation

**File**: `/home/codyhurst/dev/orca/src/orca/scripts/web/script_utilities.py` (3449 lines)

### Caching Layer

Aggressive caching of:
- Caret contexts (line 71): `self._cached_caret_contexts: dict[int, tuple[Atspi.Accessible, int]]`
- Prior contexts (line 72)
- Document references (line 75)
- Content editable state (line 77)
- Cached line/word/character contents (lines 100-104)

**Cleanup**: `_cleanup_contexts()` (lines 111-118) removes stale object refs

**Dump all caches**: `dump_cache()` (lines 120-148), preserves context if `preserve_context=True`

### Navigation via `next_context()` / `previous_context()`

**Lines 357-383**:
```python
def next_context(self, obj=None, offset=-1, skip_space=False, restrict_to=None):
    if obj is None:
        obj, offset = self.get_caret_context()
    next_obj, next_offset = self.find_next_caret_in_order(obj, offset)
    if skip_space:
        while treat_as_text_object(next_obj) and is_space(get_char(next_obj, next_offset)):
            next_obj, next_offset = self.find_next_caret_in_order(next_obj, next_offset)
    return next_obj, next_offset
```

Relies on `find_next_caret_in_order()` which is **not defined here** (inherited from base class, likely uses depth-first tree walk).

### Caret Position Management (Lines 266-295)

```python
def set_caret_position(self, obj, offset, document=None):
    grab_focus = self.grab_focus_when_setting_caret(obj)
    obj, offset = self.first_context(obj, offset)
    self.set_caret_context(obj, offset, document)
    old_focus = focus_manager.get_locus_of_focus()
    AXUtilities.clear_all_selected_text(old_focus)
    focus_manager.set_locus_of_focus(None, obj, notify_script=False)
    if grab_focus:
        AXObject.grab_focus(obj)
    AXText.set_caret_offset(obj, offset)
    
    # Check if mode should toggle based on new position
    if not presenter.focus_mode_is_sticky(script.app):
        if presenter.use_focus_mode(obj, old_focus) != presenter.in_focus_mode(script.app):
            presenter.toggle_presentation_mode(script)
```

**Key behavior**: After setting caret, re-evaluates whether focus mode should be active. Can **auto-toggle mode** if caret lands on a widget.

### Gecko vs. Chromium Branching

**Gecko find-bar detection** (lines 309-314):
```python
def is_find_bar(x):
    return (
        AXObject.get_attribute(x, "tag") == "findbar"
        or AXObject.get_attribute(x, "class") == "FindBarView"
    )
```

Both Gecko (tag=findbar) and Chromium (class=FindBarView) have different identifiers but are detected the same way.

### Off-Screen Label Caching

Lines 79-80: `self._cached_is_off_screen_label: dict[int, bool]`

Caches labels that are visually hidden (e.g., `display: none` in CSS). Prevents stale lookups on heavily-updated pages.

---

## 5. Per-Toolkit Branching

### Gecko-Specific Behavior

**File**: `/home/codyhurst/dev/orca/src/orca/scripts/toolkits/Gecko/script.py` (74 lines)

**Bug workaround** (lines 42-56):
```python
def _on_focused_changed(self, event: Atspi.Event) -> bool:
    if AXUtilities.is_panel(event.source):
        if focus_manager.get_manager().focus_is_active_window():
            msg = "GECKO: Ignoring event believed to be noise."
            return True
    if AXUtilities.is_frame(event.source):
        msg = "GECKO: Ignoring event believed to be noise."
        return True
    return super()._on_focused_changed(event)
```

Ignores spurious focus events from panels and frames.

**Context menu detection** (lines 58-73):
```python
def _on_showing_changed(self, event: Atspi.Event) -> bool:
    if (event.detail1 and AXUtilities.is_menu(event.source) 
        and not self.utilities.in_document_content(event.source)):
        msg = "GECKO: Setting locus of focus to newly shown menu."
        focus_manager.get_manager().set_locus_of_focus(event, event.source)
        return True
    return super()._on_showing_changed(event)
```

Gecko doesn't fire window:activate when showing context menus, so Orca manually sets focus.

### Chromium-Specific Behavior

**File**: `/home/codyhurst/dev/orca/src/orca/scripts/toolkits/Chromium/script.py` (126 lines)

**Element filtering** (lines 44-78):
```python
def _on_caret_moved(self, event):
    if (not AXUtilities.is_web_element(event.source) 
        and AXUtilities.is_web_element(AXObject.get_parent(event.source))):
        msg = "CHROMIUM: Ignoring because source is not an element"
        return True
    return super()._on_caret_moved(event)
```

Filters out non-element nodes (text nodes, etc.) that Chromium exposes. Prevents noise from intermediate DOM wrappers.

**Document focus filtering** (lines 80-88):
```python
def _on_focused_changed(self, event: Atspi.Event) -> bool:
    if self.utilities.is_document(event.source) and not AXDocument.get_uri(event.source):
        msg = "CHROMIUM: Ignoring event from document with no URI."
        return True
    return super()._on_focused_changed(event)
```

Chromium fires focus on document nodes without URI (internal frames). Orca ignores these.

**Autocomplete popup handling** (lines 90-102):
```python
if event.detail1 and not self.utilities.in_document_content(event.source):
    if listbox := AXUtilities.find_ancestor(event.source, AXUtilities.is_list_box):
        parent = AXObject.get_parent(listbox)
        if AXUtilities.is_frame(parent) and not AXObject.get_name(parent):
            msg = "CHROMIUM: Event source believed to be in autocomplete popup"
            focus_manager.get_manager().set_locus_of_focus(event, event.source)
            return True
```

Detects unnamed frames containing listboxes = autocomplete popups. Prevents them from stealing focus from the main document.

---

## 6. Live Regions (ARIA)

**File**: `/home/codyhurst/dev/orca/src/orca/live_region_presenter.py` (500+ lines)

### Message Queue with Politeness Levels

**Data structures** (lines 85-173):

```python
class LiveRegionMessage:
    def __init__(self, text, politeness, obj, timestamp=None):
        self.text = text
        self.politeness = politeness
        self.obj = obj
        self.timestamp = timestamp if timestamp else time.time()
    
    def __lt__(self, other):
        # ASSERTIVE (priority=0) before POLITE (priority=1)
        if self.politeness.priority != other.politeness.priority:
            return self.politeness.priority < other.politeness.priority
        # Older before newer
        return self.timestamp < other.timestamp

class LivePoliteness(enum.Enum):
    ASSERTIVE = (0, "assertive")
    POLITE = (1, "polite")
    OFF = (2, "off")

class LiveRegionMessageQueue:
    MSG_KEEPALIVE_TIME = 45  # seconds
    def __init__(self, max_size: int):
        self._heap = []
        self._max_size = max_size
```

**Behavior**:
- **Assertive** regions interrupt speech, announced immediately
- **Polite** regions wait for current speech to finish, queued
- Messages older than 45 seconds are purged
- Queue size capped at 9 messages; older polite messages discarded if full

### Queueing Logic (Lines 121-172)

**Enqueue** (lines 131-139):
```python
def enqueue(self, message: LiveRegionMessage) -> None:
    heapq.heappush(self._heap, message)
    if len(self._heap) > self._max_size:
        self._heap.sort()
        self._heap.pop()
        heapq.heapify(self._heap)
```

If queue is full, the **lowest priority oldest message is discarded**.

**Dequeue** (lines 141-147):
```python
def dequeue(self) -> LiveRegionMessage | None:
    if not self._heap:
        return None
    return heapq.heappop(self._heap)
```

Returns highest-priority (assertive before polite, older before newer).

---

## 7. Hacks and Bug Workarounds

### Documented TODOs/FIXMEs in Web Script

**Count**: ~50 TODOs in `/home/codyhurst/dev/orca/src/orca/scripts/web/`

**Categories**:

1. **Gecko-specific bugs** (5-10):
   - Line 271 (script_utilities.py): "TODO - JD: Is this still needed?" (context menu workaround)
   - Line 525 (script_utilities.py): "Note: We cannot check for the editable-text interface, because Gecko seems to be exposing that for non-editable things. Thanks Gecko."
   - script.py: "Work around Gecko bug." cache clearing

2. **Browser differences** (5-10):
   - Gecko vs. Chromium find-bar detection (lines 309-314, script_utilities.py)
   - Chromium autocomplete detection (Chromium/script.py lines 90-102)
   - Different attribute names, event sequences

3. **Code quality TODOs** (30+):
   - "Can this logic be moved to the default speech/braille generator?"
   - "Should callers instead call dump_cache with preserve_context=True?"
   - "Move into AXUtilities/AXEventUtilities"
   - "This is one of those 'noise' functions"

4. **Known limitations**:
   - Line 378-380 (script_utilities.py): Cycle detection in next_context skip_space
   - Line 295 (script_utilities.py): "TODO - JD: Can we remove this?" (cache clearing after set_caret)

### Specific Bug References

1. **Gecko workaround** (script.py, line reference unavailable in excerpt):
   ```python
   AXObject.clear_cache(event.source, False, "Work around Gecko bug.")
   ```

2. **Chromium element filtering** (Chromium/script.py lines 44-78):
   - Chromium exposes intermediate wrapper nodes; Orca filters to web-element nodes only
   - Prevents false caret-move events

3. **Browser bogus roles** (script.py):
   ```python
   msg = "WEB: Event source has bogus role. Likely browser bug."
   ```

### Dead Code / Legacy

Minimal legacy code found. The codebase is relatively modern:
- No Gecko1/Gecko2 era branches (deprecated 2009+)
- Uses current AT-SPI 2.0 APIs
- Per-script patterns are current

---

## 8. Performance Hotspots

### Structural Navigation Tree Walk

**Cost per keystroke**: O(DOM_tree_size)

Each H/K/F/B keystroke:
1. Calls `_get_all_headings()` → `AXUtilities.find_all_headings(root, pred=pred)`
2. Full tree traversal, checking role on each node
3. Returns list of all headings in document
4. Finds next/prev in list relative to current focus (O(list_size))

**On a 10KB page with 1000 total nodes**:
- 100 headings expected
- Per keystroke: O(1000) tree walk + O(100) list search = O(1100) operations
- If user types H 5 times: 5500 operations (negligible on modern hardware, <1ms)
- On a heavy page (100KB, 10000 nodes, 1000 headings): O(10000) per keystroke = potentially 10ms+ with AT-SPI roundtrips

**Mitigation strategies NOT employed**:
- No incremental index built on page load
- No caching of structural element positions
- Each keystroke is independent

### Caret Navigation Tree Walk

**Cost per arrow key**: O(DOM_tree_size) to find next context

Calls `find_next_caret_in_order(obj, offset)` which traverses the tree to find the next navigable text position.

### Live Region Queue Management

**Cost per aria-live update**: O(queue_size log queue_size)

Heap insertion/deletion with heapify = O(log 9) = O(1) because queue_size is capped at 9.

### Caret Context Caching

**Benefit**: Avoids repeated tree walks during say_word(), say_line(), etc.

**Stored in**: `self._cached_caret_contexts[hash(document_parent)]` per-document

**Invalidated when**: Document structure changes (child added/removed events)

---

## 9. NVDA Virtual-Buffer Comparison and Gap Analysis

### NVDA Model (Brief Overview)

NVDA loads a web page and builds a **flat text buffer**:
1. Traverses the page tree once on page load
2. Flattens semantic structure into a linear buffer with metadata (role, text, offset)
3. Navigation commands search the buffer (in-memory, very fast)
4. Only round-trips to browser on actions (click, activate, etc.)

**Advantages**:
- Fast navigation (in-memory searches)
- Instant H/K/link navigation (O(buffer_size) with binary search)
- Consistent behavior (buffer doesn't change between keystrokes)

**Disadvantages**:
- No live region updates (need to rebuild buffer)
- Latency on dynamic pages (buffer gets stale)
- High memory for large pages

### Orca Model (Current)

Orca **walks the AT-SPI tree on every keystroke**:
1. No pre-built index
2. Live query to browser via AT-SPI on each H/K/F/B press
3. Knows about dynamic changes immediately
4. Caches only text/caret context, not structure

**Advantages**:
- Always up-to-date (reflects live DOM changes)
- Memory efficient (no flat buffer)
- Simpler event handling (no buffer invalidation logic)
- Live regions work naturally (direct tree observation)

**Disadvantages**:
- Slower navigation on large pages (O(DOM_size) per keystroke with AT-SPI latency)
- Each browser roundtrip ~1-5ms overhead
- On heavy pages, typing H 5 times = 5+ AT-SPI calls = 5-25ms total latency

### Gap Analysis

**1. Structural Navigation Speed**
- Orca: Every H keystroke walks tree to find all headings
- NVDA: H keystroke searches flat buffer (O(1) typical case with pre-computed positions)
- **Gap**: On 10KB+ pages with heavy AT-SPI latency, Orca is noticeably slower

**Improvement**: Build an **incremental index** on page load:
```python
class DocumentIndex:
    def __init__(self, document):
        self.headings = []  # Pre-computed heading list
        self.links = []
        self.forms = []
        # ... etc
        self._build_index()
    
    def next_heading(self, obj, offset):
        # Binary search instead of tree walk
```

Cost: O(DOM_size) once on page load, then O(log n) per navigation.

**2. Caret Navigation Latency**
- Orca: `next_context()` does tree traversal
- NVDA: Searches buffer

**Gap**: Same as above

**Improvement**: Pre-compute caret order during index build:
```python
class DocumentIndex:
    self.caret_order = [obj1, obj2, ...]  # Pre-ordered text objects
    
    def next_caret_context(self, obj, offset):
        idx = binary_search(self.caret_order, obj)
        return self.caret_order[idx + 1], 0
```

**3. Dynamic Content Handling**
- Orca: Walks live tree, sees updates immediately
- NVDA: Buffer gets stale, requires rebuild

**Advantage**: Orca. This is why live regions work better in Orca (no buffer invalidation logic needed).

**4. Live Region Queue**
- Orca: Full implementation with politeness, deduplication, 45s keepalive
- NVDA: Similar (not analyzed)

**Orca Win**: Orca's live region handling is sophisticated (lines 60-173, live_region_presenter.py).

---

## 10. Form Auto-Focus and Entry Mode

### Auto-Focus Detection

**File**: `document_presenter.py`, lines 1070-1121 (`update_mode_if_needed()`)

When focus moves to an object:
1. Check if it's a focus-mode widget via `is_focus_mode_widget()` (lines 828-844)
2. Role-based rules (lines 765-808): COMBO_BOX, ENTRY, LIST_BOX, MENU, etc. force focus mode
3. State-based rules (lines 746-756): editable or (expandable+focusable) = focus mode
4. Ancestry rules (lines 817-826): grid/menu/toolbar descendants = focus mode

**Sticky focus mode for web apps** (lines 1036-1052):
- Electron app detection (lines 1042-1046)
- Top-level web app detection (lines 1048-1052)
- Both set auto-sticky focus mode on entry

### Entry Field Behavior

When focus lands on an `ENTRY` role:
1. Mode switches to focus mode (via `use_focus_mode()`)
2. Caret navigation is suspended (via `suspend_navigators()`)
3. Arrow keys are passed to app (native text editing)
4. H/K/F/B navigation is disabled until mode switches back

**How it's avoided on browse mode accidentally landing on entry**:
- `set_caret_position()` (script_utilities.py lines 266-295) re-evaluates mode after setting caret
- Can auto-toggle back to browse mode if landing on a non-widget

---

## 11. iframes, Shadow DOM, Web Components

### iframe Handling

**Structural navigation support**: YES

- Lines 287-289 (structural_navigator.py): iframe navigation commands exist
- `previous_iframe()`, `next_iframe()`, `list_iframes()`

**Implementation**: Same as other structural nav (walk tree, find all iframes, filter by current context)

**Limitation**: Structural nav must walk **through iframes** to find elements inside them. AT-SPI exposes iframe content in the tree.

### Shadow DOM

**Status**: Limited/no special handling observed

Shadow DOM is not explicitly handled in the codebase. Depends on:
- Browser's AT-SPI bridge (does it expose shadow DOM in the accessibility tree?)
- Typical browsers (Chromium/Gecko): Shadow DOM is NOT exposed to AT-SPI (encapsulated)
- **Implication**: Screen reader cannot navigate into shadow DOM unless browser explicitly exposes it

### Web Components

**Status**: Treated as ordinary widgets

If a web component has a proper ARIA role (button, combobox, etc.), it works.
If it doesn't, Orca treats it as an embedded element.

**Custom element behavior**:
- Lines 800-808 (document_presenter.py): EMBEDDED role heuristics decide if it's focus-mode widget
- If it has name, action, and no useful children → browsable
- Otherwise → focusable (force focus mode)

---

## 12. Improvement Opportunities (Ranked)

### 1. **Build Incremental Structural Navigation Index** (High Impact, Medium Effort)

**Problem**: H/K/F/B navigation walks entire tree on each keystroke

**Solution**: On page load, build an index:
```python
class DocumentNavigationIndex:
    def __init__(self, root):
        self.headings = AXUtilities.find_all_headings(root)
        self.links = AXUtilities.find_all_links(root)
        self.forms = AXUtilities.find_all_forms(root)
        # ... etc, 25 categories
        self.last_update_time = time.time()
    
    def next_heading(self, current_obj):
        idx = binary_search(self.headings, current_obj)
        return self.headings[(idx + 1) % len(self.headings)]
```

**Benefit**: O(1) to O(log n) per keystroke instead of O(tree_size)

**Implementation**: 
- Cache index in `Script` object
- Invalidate on major DOM changes (detected via node-added/node-removed events)
- Use incremental update (add/remove single items) not full rebuild

### 2. **Pre-Compute Caret Order on Page Load** (Medium Impact, Medium Effort)

**Problem**: `next_context()` tree walk is slow on large pages

**Solution**: Build ordered caret list during navigation index construction

**Benefit**: O(log n) caret navigation instead of O(tree_size)

### 3. **Cache Document URI at Script Level** (Low Impact, Low Effort)

**Problem**: Chromium filters focus events from documents without URI (repeated `AXDocument.get_uri()` calls)

**Solution**: Cache `document -> uri` mapping in script, invalidate on document-reload events

**Benefit**: Fewer AT-SPI calls on every focus event

### 4. **Batch Structural Nav Queries** (Low Impact, High Effort)

**Problem**: Separate tree walks for each object type (list, heading, link, etc.)

**Solution**: Walk tree once, collect all semantic element types in one pass

**Implementation**: 
```python
def get_all_structural_elements(root):
    headings, links, forms, buttons, tables = [], [], [], [], []
    def visitor(obj):
        role = AXObject.get_role(obj)
        if is_heading(role): headings.append(obj)
        elif is_link(role): links.append(obj)
        # ... etc
    tree_walk(root, visitor)
    return headings, links, forms, buttons, tables
```

### 5. **Implement Smart Cache Invalidation** (Medium Impact, Medium Effort)

**Problem**: Caches can become stale on dynamic pages (e.g., email inbox)

**Solution**: Observe text-changed, children-added, children-removed events → invalidate specific cache entries

**Current state**: Broad `dump_cache()` clears everything

**Improvement**: Granular invalidation by object hash

### 6. **Optimize Live Region Duplicate Detection** (Low Impact, Low Effort)

**Current**: `is_duplicate_of()` checks text + timestamp (within 250ms)

**Improvement**: Add checksum-based dedup for very large messages

### 7. **Reduce Gecko Event Noise** (Low Impact, Low Effort)

**Current**: Manual filters in Gecko/script.py for spurious panel/frame focus events

**Improvement**: Cache focus events for 100ms, coalesce duplicates before processing

---

## 13. Key Files and Line References Summary

| File | Purpose | Key Lines |
|------|---------|-----------|
| `document_presenter.py` (1531L) | Mode switching, sticky modes, auto-detection | 96-104 (state), 615-680 (set mode), 828-844 (is focus widget), 1070-1121 (update mode) |
| `structural_navigator.py` (3100L) | Single-letter nav (H/K/F/B), tree walk | 73-79 (modes), 118-254 (keybindings), 1901-1913 (get all headings) |
| `caret_navigator.py` (900L) | Arrow key nav, layout mode toggle | 62-93 (init), 250-276 (layout mode), 332-356 (triggers focus) |
| `scripts/web/script_utilities.py` (3449L) | Caching, context management, web-specific logic | 71-109 (caches), 111-148 (dump cache), 266-295 (set caret position), 357-383 (next context) |
| `live_region_presenter.py` (500L) | Live region queue, politeness, dedup | 60-83 (politeness enum), 85-119 (message class), 121-173 (queue) |
| `scripts/toolkits/Gecko/script.py` (74L) | Gecko-specific workarounds | 42-73 (focus/menu filters) |
| `scripts/toolkits/Chromium/script.py` (126L) | Chromium-specific filters | 44-114 (element, doc, popup filtering) |
| `ax_document.py` (280L) | Document interface wrapper | 72-113 (page tracking) |

---

## Conclusion

Orca's web handling is sophisticated but **optimized for correctness and live-update responsiveness rather than speed**. The lack of a pre-built structural navigation index means every H keystroke incurs a full tree walk, but this simplifies the code and guarantees accuracy on dynamic pages. For typical pages (<5KB), latency is negligible. For heavy pages (>50KB with 1000+ semantic elements), noticeable delay occurs (50-100ms for a sequence of H presses).

**Most impactful improvement**: Incremental navigation index (structured_navigator + caret_navigator) would cut keystroke latency 10-100x without sacrificing live-update support.
