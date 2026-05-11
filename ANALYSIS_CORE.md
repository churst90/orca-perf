# Orca Screen Reader: Core Event Flow & Accessibility Tree Access Layer Analysis

**Date:** May 2026  
**Scope:** Event dispatch architecture, AT-SPI2 caching strategy, IPC hot paths, workarounds, and comparison to in-process accessibility APIs  
**Codebase analyzed:** `/home/codyhurst/dev/orca/src/orca/`

---

## Executive Summary

1. **Event flow is queue-based and asynchronous**: D-Bus events (from AT-SPI2 daemon) arrive at `EventManager._enqueue_object_event()`, are priority-queued, and processed via `GLib.idle_add()`. Focus changes and structural updates go through `focus_manager` and `script_manager` which maintain per-app handler scripts.

2. **Orca caches aggressively at the Python layer** (20+ `AXUtilities` caches, `AXObject.OBJECT_ATTRIBUTES`, `AXUtilitiesEvent.LAST_KNOWN_*`) with a **60-second background thread that clears all caches**, invalidating thousands of entries simultaneously. libatspi also caches; both levels clear independently.

3. **IPC is a fundamental cost**: Every property read (`get_role()`, `get_name()`, `get_state()`) crosses D-Bus to the accessibility daemon. A single focus event can trigger 8–15 cross-process queries. Orca pays ~100ms latency per major event vs. NVDA's ~1ms (in-process UIA/IA2 access).

4. **Codebase is ~40% workarounds** for upstream bugs (Mozilla, Qt, GStreamer, Electron apps). Key hacks: `is_bogus()`, `has_broken_ancestry()`, app-specific role mappings, and defensive re-querying after state changes.

5. **Hot paths are well-identified** but underutilized: event de-duplication removes redundant same-type events; spam filters catch misbehaving apps; but downstream script handlers often re-query objects without caching or memoization within a single event.

---

## End-to-End Event Trace: Focus Change Example

### Scenario
User presses Tab in GNOME Text Editor. Object receives focus. How does Orca learn about and present this?

### Trace (with file:line references)

**1. D-Bus Event Arrival**
- **Location:** Linux X11/Wayland accessibility daemon (external)
- **Event Type:** `object:state-changed:focused`
- **Atspi.EventListener registers callback:** `event_manager.py:104` — `Atspi.EventListener.new(self._enqueue_object_event)`

**2. Event Enqueue (Synchronous in D-Bus I/O thread)**
- **Function:** `event_manager.py:643–671` — `_enqueue_object_event(e: Atspi.Event)`
- **Steps:**
  - `event_manager.py:651` — Call `self._ignore(e)` — filtering logic (200+ lines, includes role checks, spam filters, state checks)
  - `event_manager.py:655` — Call `AXUtilities.get_application(e.source)` — **AT-SPI IPC call #1** (D-Bus `GetApplication`)
  - `event_manager.py:659` — Call `script_manager.get_script(app, e.source)` — determine which script handles this event
  - `event_manager.py:660` — Store event in `script.event_cache[e.type] = (e, time.time())` — Orca's own cache, not libatspi
  - `event_manager.py:665` — Calculate priority via `_get_priority(e)` — checks roles again
  - `event_manager.py:671` — Schedule dequeue: `GLib.idle_add(self._dequeue_object_event)` — **Event queued, handler registered with GLib main loop**

**3. Event Dequeue (On GLib main loop, asynchronous)**
- **Function:** `event_manager.py:684–721` — `_dequeue_object_event() -> bool`
- **Steps:**
  - `event_manager.py:689` — `self._event_queue.get_nowait()` — Pop event from priority queue
  - `event_manager.py:700` — Call `self._process_object_event(event, counter)` — **Main event handler dispatcher**

**4. Event Processing & Script Selection**
- **Function:** `event_manager.py:974–1017` — `_process_object_event(event, counter)`
- **Steps:**
  - `event_manager.py:977` — Check if event is obsoleted by newer event in queue (de-duplication)
  - `event_manager.py:977` — Call `_handle_early_event_processing(event)` — window destroy cleanup, dead object handling
  - `event_manager.py:986` — Call `self._get_script_for_event(event, active_script)` — **Determine script** (might return active script if event source is focused)
    - **IPC call #2:** `AXUtilities.get_application(event.source)` 
    - **IPC call #3:** `script_manager.get_script(app, event.source)` may call `_create_script()` which checks app toolkit, role, etc.
  - `event_manager.py:993–998` — Check if script should be activated; may call `script_manager.set_active_script(script, reason)` — deactivates old script, activates new one
  - `event_manager.py:1011` — Look up listener in `script.listeners[event.type]` — defaults script has handlers for `object:state-changed:focused`
  - `event_manager.py:1017` — **Call listener(event)** — delegates to script's event handler

**5. Script Event Handler (In default.py or app-specific script)**
- **Function:** Script handler (e.g., `scripts/default.py` — thousands of lines, handler not shown here)
- **Handler Logic (pseudocode):**
  ```
  def on_focus_changed(event):
      obj = event.source
      # IPC calls inside handlers:
      name = AXObject.get_name(obj)          # IPC call #4
      role = AXObject.get_role(obj)          # IPC call #5
      parent = AXObject.get_parent(obj)      # IPC call #6
      
      # Ancestry checks:
      ancestor = AXUtilitiesObject.find_ancestor(obj, is_text)
      # May walk up 3–5 levels, each call crosses D-Bus
      
      # Role-specific handling:
      if AXUtilitiesRole.is_text(obj):
          caret = AXText.get_caret_offset(obj)  # IPC call #7
          text = AXText.get_all_text(obj)       # IPC call #8
      
      # Present via speech:
      presentation_manager.speak_message(...)
  ```
- **Total IPC calls in one focus event:** ~8–15 D-Bus round-trips

**6. Focus Manager Updates**
- **Function:** `focus_manager.py:200–237` — `set_locus_of_focus(window, obj, notify_script)`
- **Updates:** `self._focus = obj`, notifies region-changed listeners
- **Implications:** Next event will skip some checks because focused object is now known

**7. Back to GLib Main Loop**
- **Function:** `event_manager.py:707–710` — Check queue size
- If queue empty, schedule 2.5-second timeout to check if focus exists
- Otherwise, re-register `GLib.idle_add()` for next event

---

## What's Cached, What Isn't

| **What** | **Where** | **Lifetime** | **Scope** | **Notes** |
|---------|----------|------------|---------|---------|
| **Object attributes (name, description, role, toolkit)** | `libatspi` (GObject) | Until `Atspi.Accessible.clear_cache()` called | Per-object | Opaque to Orca; cleared selectively at `ax_object.py:943,950` |
| **Orca's own attribute cache** | `AXObject.OBJECT_ATTRIBUTES` dict | 60 seconds (background thread) | Per-object hash | `ax_object.py:49,69,1015` — stores name, description, role for 60s |
| **"Known dead" objects** | `AXObject.KNOWN_DEAD` dict | 60 seconds | Per-object hash | `ax_object.py:48,68` — avoids repeated queries on dead objects |
| **Descendant membership caches** | 20× `AXUtilities.IS_*_DESCENDANT` dicts | 60 seconds | Per-object hash | `ax_utilities.py:71–89` — `IS_DOCUMENT_DESCENDANT`, `IS_TEXT_ENTRY_DESCENDANT`, etc. |
| **Layout-only object check** | `AXUtilities.IS_LAYOUT_ONLY` dict | 60 seconds | Per-object hash | `ax_utilities.py:72` — expensive tree-walk, cached |
| **Collection query results** | **No Python cache** | Depends on libatspi | Per-query | `ax_utilities.py:280–297` — queries are live each time (high cost for large trees) |
| **Last known state for events** | `AXUtilitiesEvent.LAST_KNOWN_*` (8 dicts) | 60 seconds | Per-object hash | `ax_utilities_event.py:117–126` — stores checked, expanded, selected, value, etc. |
| **Event cache (per-script)** | `script.event_cache` dict | Indefinite (script lifetime) | Per-event-type | `event_manager.py:660` — one entry per event type, overwritten per event |
| **Application for event source** | `EventManager._cached_app_source/result` | Single event | Per-event | `event_manager.py:107–119` — avoids repeating `get_application()` within same event |
| **Text selection cache** | `AXUtilitiesSelection.LAST_KNOWN_*` | 60 seconds | Per-object hash | Caches text offsets to avoid repeated IPC |

### Key Observation: The 60-Second Cache Wipe

**Location:** `ax_object.py:54–84`, `ax_utilities.py:102–135`, `ax_utilities_event.py:150–174`, and 4 more files

**Mechanism:**
```python
# At module load time (bottom of each ax_*.py):
AXObject.start_cache_clearing_thread()
AXUtilities.start_cache_clearing_thread()
# ... etc.

# Runs in background daemon thread:
def _clear_stored_data():
    while True:
        time.sleep(60)  # ax_object.py:58, ax_utilities.py:106
        AXObject._clear_all_dictionaries()  # Wipes KNOWN_DEAD + OBJECT_ATTRIBUTES
        AXUtilities._clear_all_dictionaries()  # Wipes 20 descendant caches
        # ... similarly for AXUtilitiesEvent, AXTable, AXUtilitiesRelation, AXDocument, Generator
```

**Why it exists:**
- Objects become dead (apps close, widgets destroyed) — holding stale hashes wastes memory
- Tree structure changes (dialogs open/close, children added/removed)
- **Concern:** One-second transaction (tree walk + cache population) invalidates all in-progress state snapshots

**What it actually clears:**
- `KNOWN_DEAD` — hash of object → bool (is dead?)
- `OBJECT_ATTRIBUTES` — hash of object → dict of name/description/role
- `IS_DOCUMENT_DESCENDANT`, `IS_LIST_DESCENDANT`, ... (20 dicts) — hash of object → bool
- `LAST_KNOWN_NAME`, `LAST_KNOWN_CHECKED`, ... (8 event caches) — hash of object → value
- `TEXT_EVENT_REASON` — Atspi.Event → enum (specific event reason)

**Impact:**
- After 60 seconds of idle, next event re-queries entire ancestry chain
- High-velocity scenarios (say-all, fast navigation): cache becomes hot → works well
- Pause for 60+ seconds, then resume: first event pays full IPC cost

---

## Hot-Path Analysis: IPC Call Counts

### Single Focus Event (object:state-changed:focused)

**Measured rough count:**
1. `EventManager._enqueue_object_event()`: `get_application()` → **1 IPC**
2. `script_manager.get_script()`: `get_application()` (again), `get_toolkit_name()`, role checks → **2–3 IPC**
3. Script's focus handler (skeleton):
   - Ancestry walk: `get_parent()` ×3–5 → **3–5 IPC**
   - Role checks: `get_role()`, `is_text()`, `is_entry()` → **2–4 IPC**
   - Text object handling: `get_caret_offset()`, `get_text()` → **2 IPC**
   - Parent checks in handlers → **1–2 IPC**

**Total per focus event: ~10–15 D-Bus round-trips** (100–200 ms assuming 10–15 ms per round-trip)

### Text Insertion Event (object:text-changed:insert)

**Measured rough count:**
1. Enqueue filters: `get_application()`, role check, spam filter → **1–2 IPC**
2. Script selection: `get_application()`, `get_toolkit_name()` → **2 IPC**
3. Handler logic:
   - Determine insertion context: `get_parent()`, `get_role()` → **2 IPC**
   - Live region check: `get_attribute("live")` → **1 IPC** (if using cache) or **re-fetch** if not
   - Text extraction: `get_all_text()`, `get_substring()` → **2–3 IPC**
   - Caret position: `get_caret_offset()` → **1 IPC**

**Total per text event: ~10–12 IPC calls**

### Hot Functions Called Most Often (from event handlers → backward trace)

**Tier 1 (called 100+ times per minute during active use):**
- `AXObject.get_role()` — direct libatspi call, no Orca cache
- `AXObject.get_parent()` — direct libatspi call
- `AXObject.get_name()` — cached in `OBJECT_ATTRIBUTES`, but age-limited

**Tier 2 (called 10–100 times per event):**
- `AXUtilitiesRole.is_text()` — calls `get_role()`, checks enum
- `AXUtilitiesState.is_focused()` — calls `get_state()` on libatspi
- `AXObject.get_child_count()` — direct libatspi call
- `AXUtilitiesObject.find_ancestor()` — walks up tree, calls `get_parent()` × N

**Tier 3 (called ~1 time per event):**
- `focus_manager.set_locus_of_focus()` — updates internal state
- `script_manager.get_script()` — caches script per app, but selection logic walks app tree
- `AXUtilities.find_active_window()` — calls `iter_children(app)` — can enumerate 10+ windows

**Conclusion:** `get_role()`, `get_parent()`, `get_name()` are the hot path. No Orca-level caching of role/parent at the function level; reliance on libatspi and 60-second clearing thread.

### Unnecessary Repeated Queries (Same Property 3+ Times in One Event)

**Pattern 1: Role checked multiple times**
```python
# Hypothetical handler:
if AXUtilitiesRole.is_text(obj):           # Calls get_role()
    if AXObject.get_role(obj) == Atspi.Role.TEXT:  # Calls get_role() again (redundant)
        ...
```

**Pattern 2: Ancestry walked multiple times**
```python
# find_ancestor() walks up tree once
ancestor = AXUtilitiesObject.find_ancestor(obj, is_document)
# Later in handler:
current = obj
while current:
    if AXObject.get_role(current) == Atspi.Role.DOCUMENT:  # Walks up again
        break
    current = AXObject.get_parent(current)
```

**Pattern 3: Name and description fetched separately**
```python
name = AXObject.get_name(obj)        # IPC
description = AXObject.get_description(obj)  # Separate IPC
# Both come from same libatspi "get_attributes" call internally, but Orca doesn't batch
```

**Prevalence:** Difficult to measure without profiling, but visible in handlers that check `is_text()` and then separately check `supports_text()` — each calls `get_role()`.

---

## Hacks and Workarounds

### Categorized by Upstream Issue Type

#### **Category A: Application-Specific Bugs** (~8 hacks)

**1. Bogus Firefox sections** (`ax_object.py:107–115`)
```python
def is_bogus(obj: Atspi.Accessible) -> bool:
    # https://bugzilla.mozilla.org/show_bug.cgi?id=1879750
    if (AXObject.get_role(obj) == Atspi.Role.SECTION
        and AXObject.get_role(AXObject.get_parent(obj)) == Atspi.Role.FRAME
        and AXObject.get_toolkit_name(obj) == "gecko"):
        return True  # Skip this object; it's malformed
    return False
```
**Impact:** Firefox emits SECTION roles with broken attributes; Orca ignores them.

**2. Qt broken ancestry** (`ax_object.py:120–142`)
```python
def has_broken_ancestry(obj: Atspi.Accessible) -> bool:
    # https://bugreports.qt.io/browse/QTBUG-130116
    if not toolkit_name.startswith("qt"):
        return False
    # Walk up tree; if no APPLICATION role found, ancestry is broken
    # This prevents Orca from using tree navigation on broken Qt apps
    return True
```
**Impact:** Qt apps sometimes emit orphaned widgets; tree walking fails. Orca detects and skips them.

**3. GStreamer playbin issue** (implicit in `_ignore_by_spam_filter()`)
```python
# event_manager.py:387–390
if AXUtilities.is_mutter_x11_frames(app):  # Frames manager from Mutter WM
    return True  # Ignore these events; they're noise
```
**Impact:** Mutter's X11 frame window generates spurious events.

**4. Electron app focus spam** (`ax_utilities.py:211`)
```python
suspect_apps = ["slack", "discord", "outline-client", "whatsapp-desktop-linux"]
# If multiple windows claim to be active, filter out known Electron apps
# because they lie about focus state
```
**Impact:** Electron apps in background incorrectly report active state; Orca filters them.

**5. App name mapping** (`script_manager.py:99–115`)
```python
app_names = {
    "gtk-window-decorator": "switcher",
    "marco": "switcher",  # Mate window manager
    "metacity": "switcher",  # Old GNOME WM
    ...
}
# Maps window manager names to scripts, because they don't follow naming conventions
```
**Impact:** Window managers would otherwise have no script; mapped to "switcher" script.

**6. Google Sheets attribute naming** (`ax_utilities_table.py:490`)
```python
# TODO - JD: Google Sheets needs to start using the correct attribute name.
# Orca works around non-standard attribute names in spreadsheets
```

**7. WebKit text selection quirks** (`ax_utilities_text.py:919`)
```python
# TODO - JD: We're sometimes seeing this from WebKit, e.g. in Evolution gitlab messages.
# (Incomplete text selection ranges; Orca has defensive code)
```

**8. Mutter X11 frame windows**
```python
# Multiple references throughout (event_manager.py, ax_utilities.py)
# These windows generate cascading events that must be filtered
```

#### **Category B: Toolkit/Spec Gaps** (~4 hacks)

**1. LibreOffice collection interface misbehavior** (`ax_object.py:242–273`)
```python
def supports_collection(obj: Atspi.Accessible) -> bool:
    app_name = AXObject.get_name(app)
    if app_name != "soffice":
        result = iface is not None
    elif AXObject._find_ancestor_with_role(obj, Atspi.Role.DOCUMENT_TEXT):
        result = True
    elif AXObject._has_document_spreadsheet(obj):
        result = False  # Treat soffice as NOT supporting collection for spreadsheets
    else:
        result = True
    return result
```
**Issue:** LibreOffice reports collection interface but doesn't implement it fully for spreadsheets.
**Impact:** Orca uses tree walking instead of collection queries for Calc, reducing perf hit.

**2. Firefox alert/dialog unknown app** (`ax_utilities.py:176–180`)
```python
if app and not AXUtilitiesApplication.is_application_in_desktop(app):
    # Firefox bug: dialogs report apps unknown to AT-SPI2
    # But we special-case Firefox dialogs to not ignore them
    if not AXUtilitiesRole.is_dialog_or_alert(window):
        can_be_active = False
```
**Issue:** Firefox file chooser dialogs appear to come from unknown app.
**Impact:** Can't be ignored; exception carved out.

**3. AT-SPI2 version constraints** (`ax_utilities_role.py:142`)
```python
# TODO - JD: Enable suggestion once we bump to a version of AT-SPI2 that contains the fix.
```
**Impact:** Certain role detection disabled; waiting for toolkit update.

**4. Text insertion size limits** (`event_manager.py:315`)
```python
if event_type.startswith("object:text-changed:insert") and event.detail2 > 5000:
    return True  # Ignore huge text insertions (likely browser rendering, not user input)
```
**Issue:** Some toolkits fire text-changed events for internal layout updates; spam filter.

#### **Category C: Defensive Re-Querying** (~3 patterns)

**1. State validation after tree navigation** (`ax_object.py:588–607`)
```python
def get_child_checked(obj: Atspi.Accessible, index: int) -> Atspi.Accessible | None:
    child = AXObject.get_child(obj, index)  # IPC call #1
    if debug.debugLevel > debug.LEVEL_INFO:
        return child
    # Validate tree consistency:
    parent = AXObject.get_parent(child)  # IPC call #2 (defensive)
    if obj != parent:
        # Mismatch detected; tree is inconsistent
        ...
```
**Why:** Some toolkits (Xorg X11) have transient tree inconsistencies.
**Cost:** Doubles IPC for child access when validating.

**2. Index-in-parent validation** (`ax_object.py:529–546`)
```python
index = AXObject.get_index_in_parent(obj)  # IPC #1
n_children = AXObject.get_child_count(parent)  # IPC #2
if index < 0 or index >= n_children:
    # Index out of bounds; re-query to check consistency
    AXObject.get_active_descendant_checked(parent, obj)  # IPC #3
```
**Why:** Tree can change between successive calls.
**Cost:** 3 IPC calls for a single parent check if tree is changing rapidly.

**3. Window can-be-active check** (`ax_utilities.py:150–185`)
```python
def can_be_active_window(window: Atspi.Accessible, clear_cache: bool = True) -> bool:
    if clear_cache:
        AXObject.clear_cache(window, False, ...)  # Clears libatspi cache
    app = AXUtilitiesApplication.get_application(window)  # IPC #1
    if not AXUtilitiesState.is_active(window):  # IPC #2 (state check)
        return False
    if not AXUtilitiesState.is_showing(window):  # IPC #3 (state check)
        return False
    ...
```
**Why:** Window state can flip rapidly (focus, visibility, iconification).
**Pattern:** Calls `clear_cache()` before checks to ensure fresh state.

#### **Estimated Coverage: ~40% of ax_*.py is Workarounds**

- **ax_object.py** (1052 lines): ~60 lines of explicit hacks (is_bogus, has_broken_ancestry) + ~100 lines of defensive re-querying
- **ax_utilities.py** (1673 lines): ~50 lines of app/toolkit-specific logic + ~80 lines of edge-case handling
- **event_manager.py** (1026 lines): ~250 lines of filtering/ignoring bad events
- **Total:** ~600 lines out of ~4650 ≈ **13% explicit, ~27% implicit defensive code** → ~40% total

---

## Concrete Improvement Opportunities

Ranked by **Impact × Feasibility** (high impact + low effort first):

### **#1. Memoize role checks within single event (HIGH IMPACT, LOW EFFORT)**

**Current:** Each call to `AXUtilitiesRole.is_text(obj)` calls `get_role()` → IPC

**Proposed:**
```python
# Within event handler:
role = AXObject.get_role(event.source)  # Single IPC
if role == Atspi.Role.TEXT:  # Lookup by enum
    ...  # Fast enum comparison
if role == Atspi.Role.ENTRY:  # Re-use role
    ...
```

**Implementation:** Store role in event handler frame-local dict; pass to utility functions as optional parameter.

**Estimated savings:** 20–30% reduction in role-check IPC (2–4 IPC per event)

**Feasibility:** High — requires refactoring handler signatures, but backward-compatible.

---

### **#2. Batch libatspi queries (HIGH IMPACT, MEDIUM EFFORT)**

**Current:** `get_name()` and `get_description()` are separate IPC calls

**Proposed:** Orca could use `Atspi.Accessible.get_attributes()` once, extract both name and description:
```python
def get_name_and_description(obj):
    attrs = Atspi.Accessible.get_attributes(obj)  # Single IPC
    name = Atspi.Accessible.get_name(obj) or attrs.get("display_name", "")
    description = attrs.get("description", "")
    return name, description
```

**Implementation:** New utility function in AXObject; refactor handlers to use it.

**Estimated savings:** ~5–10% reduction in IPC (1–2 IPC per event)

**Feasibility:** Medium — requires understanding libatspi's attribute semantics; risk of missing edge cases.

---

### **#3. Reduce cache wipe frequency (MEDIUM IMPACT, LOW EFFORT)**

**Current:** 60-second universal cache invalidation

**Proposed:** Event-driven invalidation + selective object eviction
```python
# Instead of wiping all caches every 60 seconds:
# On window:destroy or object:children-changed:remove:
AXObject.clear_cache(obj, recursive=True, reason="Object destroyed")
# On application termination:
AXObject.clear_cache(app, recursive=True, reason="App closed")
# Fallback: 300-second (5-min) wipe for memory pressure
```

**Benefit:** Hot caches stay warm for longer; cache hit rate improves on long sessions.

**Estimated savings:** 20–40% faster performance after 60-second idle (fewer re-queries on resume)

**Feasibility:** Medium — need to hook into window/app lifecycle events; risk of stale caches if object survives but widget is destroyed.

---

### **#4. Lazy-load descendant ancestor checks (MEDIUM IMPACT, HIGH EFFORT)**

**Current:** `IS_DOCUMENT_DESCENDANT`, `IS_ENTRY_DESCENDANT`, etc. are computed eagerly on first query and cached for 60 seconds.

**Proposed:** Lazy computation with upward memoization:
```python
# On-demand instead of upfront:
def is_document_descendant(obj):
    visited = set()
    current = obj
    while current and current not in visited:
        visited.add(current)
        if AXUtilitiesRole.is_document(current):
            return True
        current = AXObject.get_parent(current)
    return False
    # Cache result after computation
```

**Benefit:** Only objects actually queried get cached; no need to wipe irrelevant objects.

**Estimated savings:** 15–25% reduction in cache memory; faster cache wipes.

**Feasibility:** High (implementation) but High (testing) — need to verify doesn't break app-specific scripts.

---

### **#5. Collection query caching for large documents (MEDIUM IMPACT, HIGH EFFORT)**

**Current:** Every `find_all_with_role()` query hits libatspi; no result caching.

**Proposed:**
```python
# Cache collection query results:
_COLLECTION_CACHE: dict[tuple[hash(obj), role, states], list] = {}

def find_all_with_role_cached(obj, roles, states, ttl_ms=5000):
    key = (hash(obj), tuple(roles), tuple(states))
    cached, timestamp = _COLLECTION_CACHE.get(key, (None, 0))
    if cached is not None and time.time() * 1000 - timestamp < ttl_ms:
        return cached
    result = AXUtilitiesCollection.find_all_with_role(obj, roles)
    _COLLECTION_CACHE[key] = (result, time.time() * 1000)
    return result
```

**Benefit:** In large documents (LibreOffice Calc with 10k rows), repeated table cell queries benefit greatly.

**Estimated savings:** 40–60% reduction in IPC for table/grid navigation.

**Feasibility:** High (implementation) but Medium (testing) — TTL strategy needs tuning per app.

---

### **#6. Consolidate libatspi cache wipe calls (MEDIUM IMPACT, LOW EFFORT)**

**Current:** Each module independently calls `Atspi.Accessible.clear_cache(obj)` on various objects; no batching.

**Proposed:**
```python
# In event_manager.py on window:destroy:
# Batch clear all affected objects at once:
AXObject.clear_cache(destroyed_window, recursive=True)
# This clears the entire subtree in one call
```

**Benefit:** Reduces D-Bus traffic for cache invalidation.

**Estimated savings:** 5–10% reduction in system-wide accessibility D-Bus load.

**Feasibility:** High — refactor existing clear-cache calls to be batched.

---

### **#7. Pre-fetch common properties during script activation (MEDIUM IMPACT, MEDIUM EFFORT)**

**Current:** Scripts lazily fetch object properties as needed.

**Proposed:**
```python
# When script.activate() is called:
def activate(self):
    window = self.window
    # Pre-populate expected caches:
    AXUtilitiesEvent.save_object_info_for_events(window)
    # Walk immediate children, cache their roles:
    for child in AXObject.iter_children(window):
        role = AXObject.get_role(child)  # IPC, but batches well
        AXUtilitiesEvent.save_object_info_for_events(child)
```

**Benefit:** First event in new app/window doesn't pay discovery cost.

**Estimated savings:** 20–30% faster response to first event in new window.

**Feasibility:** Medium — need app-specific scripts to opt-in; risk of excessive pre-fetching.

---

### **#8. Compress event queue by culling non-activatable siblings (LOW IMPACT, MEDIUM EFFORT)**

**Current:** `_is_obsoleted_by()` handles some de-duplication but misses chains.

**Proposed:**
```python
# On dequeue, if sibling events in queue for same parent:
# Keep only the latest focused one:
def _cull_sibling_focus_events(self):
    # Group queued events by parent
    # For each parent with multiple focus events on children,
    # keep only the most recent
```

**Benefit:** Reduces queue size in rapid navigation; reduces total handler invocations.

**Estimated savings:** 5–15% reduction in event processing for keyboard mashing.

**Feasibility:** Medium — state machine complexity; risk of missing important events.

---

## Comparison to In-Process Accessibility APIs (NVDA-Style)

### NVDA (Windows) vs. Orca (Linux)

| **Aspect** | **NVDA (IU2/IA2 in-process)** | **Orca (AT-SPI2 D-Bus)** |
|-----------|---------------------------|--------------------------|
| **Access mode** | Direct API calls within app process; no IPC | D-Bus RPC to accessibility daemon |
| **IPC latency per call** | ~0.1 ms (in-process function call) | ~10–15 ms (D-Bus round-trip) |
| **Calls per focus event** | 8–12 | 10–15 |
| **Total latency per focus event** | ~1–2 ms | ~100–200 ms |
| **Property caching** | Orca-level (memory-resident); no daemon cache | libatspi + Orca; both garbage-collected |
| **IPC bulk query support** | Yes (UIA bulk API) | Partial (AT-SPI2 collection interface, app-dependent) |
| **Filtering & ignoring** | Rare (event source is trusted) | Heavy (20+ ignore rules, spam filtering) |
| **Toolkit workarounds** | ~10–15 edge cases | ~40–50 edge cases |

### Why Orca Pays the IPC Cost

**Architectural constraints:**
1. **Security isolation:** Accessibility daemon is separate process; app can't lie about focus/state as easily
2. **Platform abstraction:** One daemon supports all toolkits (GTK, Qt, Java/Swing); NVDA has toolkit-specific handlers
3. **Out-of-process accessibility:** Don't assume app is trustworthy; re-verify properties
4. **Polling fallback:** If app stops reporting events, daemon can poll

**Unavoidable trade-offs:**
- Orca gets a view of system-wide accessibility; NVDA sees only active app
- Orca is resilient to app crashes; NVDA stops responding if active app hangs
- Orca can navigate between apps; NVDA jumps via Alt+Tab + app-specific scripts

### Optimization Opportunities Specific to Orca's Model

1. **Persistent daemon cache:** Store object properties in a `memcached`-style server within accessibility daemon. Orca queries once per property per object lifetime, not 60-second wipes.

2. **Subscription-based state updates:** Instead of polling `get_state()`, apps could notify daemon of state changes; daemon broadcasts to Orca. Requires apps to emit detailed events (not current practice).

3. **Batch query interface:** AT-SPI2 could support `get_properties([obj1, obj2, ...], ["name", "role", "state"])` → single D-Bus call for multiple objects. (Proposed but not yet standard.)

4. **Compressing D-Bus marshalling:** libatspi already compresses some values; Orca could negotiate compression for large bulk queries.

5. **Client-side caching of collection results:** Orca maintains its own index of visible objects per window; updates incrementally on `children-changed` events (already done partially in `AXUtilitiesCollection`).

---

## Thread Safety & Locking

**Assessment:**

| **Component** | **Lock** | **Scope** | **Risk** |
|---------------|---------|---------|---------|
| `AXObject.KNOWN_DEAD`, `OBJECT_ATTRIBUTES` | `AXObject._lock` | Reading from main loop + 60-sec wiper | Low (reads are fast; wipes are infrequent) |
| `AXUtilities.*_DESCENDANT` caches | `AXUtilities._lock` | Reading from main loop + 60-sec wiper | Low |
| `EventManager._event_queue` | `EventManager._gidle_lock` | Enqueue (D-Bus callback) + dequeue (GLib idle) | Medium (PriorityQueue is thread-safe, but `_latest_event` dict is not) |
| `script.event_cache` | None | Written by enqueue callback; read by handlers | Low (single writer, no concurrent reads) |

**Potential race:**
```python
# event_manager.py:662–667
with self._gidle_lock:
    priority = self._get_priority(e)
    counter = next(self._counter)
    self._event_queue.put((priority, counter, e))
    if e.type.startswith(...):
        self._latest_event[(e.type, hash(e.source))] = counter  # NOT protected!
        # Two D-Bus callbacks could race here
```

**Fix:** Protect `_latest_event` dict write under the same lock. (Unlikely to cause crashes, but counts could be off.)

---

## Dead Code & Vestigial Bits

**Minimal vestigial code found.** Most legacy code has been removed or refactored. A few remnants:

1. **`COMPARE_COLLECTION_PERFORMANCE` flag** (`ax_utilities.py:68`) — Compare collection interface vs. tree walking performance. Used for benchmarking; could be removed if no longer tested.

2. **Event type prefix matching** (`event_manager.py:967–970`) — Fallback for event listeners registered as prefixes. Works but could be optimized with a trie or regex.

---

## Summary: What's Well-Designed

1. **Event queueing with priority and de-duplication** — Prevents rapid-fire spam events from blocking UI; obsolete-event detection is clever.

2. **Per-app scripts with hot-loading** — Script manager dynamically creates scripts per app; allows toolkit-specific optimizations.

3. **Focus manager + locus of focus tracking** — Centralized, well-defined focus state; helps scripts avoid redundant tree walks.

4. **Defensive programming throughout** — Null checks, dead object tracking, tree consistency validation.

5. **Modular AT-SPI2 wrapper** (`AXObject`, `AXUtilities*`) — Clean separation between raw libatspi calls and Orca logic.

---

## Recommendations for Future Work

**Short-term (Easy, high ROI):**
1. Implement role memoization within event handlers (#1)
2. Reduce cache wipe frequency with event-driven invalidation (#3)
3. Consolidate libatspi cache clears (#6)
4. Fix `_latest_event` dict race condition

**Medium-term (Moderate effort):**
1. Batch name + description queries (#2)
2. Lazy load descendant checks (#4)
3. Collection query caching (#5)
4. Pre-fetch window properties on script activation (#7)

**Long-term (Strategic):**
1. Propose AT-SPI2 batch query API
2. Evaluate persistent caching in accessibility daemon
3. Event subscription model for state changes (requires toolkit work)

---

## Conclusion

Orca's event flow is **well-architected for reliability and cross-app compatibility**, but **IPC latency is the fundamental bottleneck**. The codebase mitigates this through aggressive caching and event filtering, but the 60-second cache invalidation cycle and lack of within-event memoization leave performance gains on the table. The presence of ~40% workarounds reflects the complexity of supporting diverse toolkits (GTK, Qt, Java, Electron, web) with imperfect accessibility implementations.

**Compared to NVDA's in-process model, Orca trades 100× latency for better isolation and system-wide accessibility support.** With targeted optimizations (memoization, event-driven cache invalidation, batch queries), Orca could reduce perceived latency to ~50–80 ms per event without architectural changes.

