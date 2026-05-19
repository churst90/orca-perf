# Orca caching architecture

This document is the single authoritative reference for how Orca caches
AT-SPI accessibility data. It explains the layers, their invalidation
rules, and how they interact, so contributors can answer "if I'm fixing
a stale-data bug, which cache do I need to invalidate?" without reading
1500 lines of `ax_object.py`.

## TL;DR — the five cache layers

| Layer | Where | Lifetime | Invalidated by |
|---|---|---|---|
| **Event-scope** | `AXObject._event_cache_tls` | One AT-SPI event handler | Scope exit (auto) |
| **Long-lived AT-SPI properties** | `AXObject.LONG_LIVED_*` | Process lifetime, ~8000 entries cap | property-change + defunct events |
| **State-change baselines** | `AXUtilitiesEvent.LAST_KNOWN_*` | Process lifetime | window:destroy + children-changed:remove + 600s safety wipe |
| **Descendant-of checks** | `AXUtilities.IS_*_DESCENDANT` | Process lifetime | 60s wipe (in `_clear_all_dictionaries`) |
| **Structural-nav matches** | `StructuralNavigator._nav_cache` | Process lifetime | children-changed (debounced) + defunct |

Each layer is independently invalidated and they intentionally
don't share state. The naming convention is consistent: anything
called `_event_*` is single-event-scoped, anything called
`LONG_LIVED_*` or `LAST_KNOWN_*` survives across events.

---

## Layer 1: Event-scope cache (`AXObject._event_cache_tls`)

**Purpose:** Memoize AT-SPI property reads within a single event handler.
A typical focus-change event handler may call `get_role(obj)` 8-10 times
through various utility checks. Without caching, each call is a D-Bus
round-trip; with the event scope, only the first is.

**Storage:** `threading.local()` — per-thread dicts. AT-SPI events
dispatch on the GLib main thread; the dicts are populated when
`event_scope()` enters and discarded when it exits.

**Entered:** `event_manager._process_object_event` wraps each event
handler invocation in `with AXObject.event_scope(label):`.

**Properties cached:** `roles`, `parents`, `names`, `state_sets`,
`cleared` (for clear_cache suppression).

**Why scoped this way:** Cross-event caching of `Atspi.StateSet`
crashes libatspi when the underlying object goes defunct between
events. Single-event scope means the cache is always fresh because
the underlying objects are still alive at handler entry. See the
comments at `ax_object.py:96-100` for the defunct-crash history.

**Invalidation:** Automatic on scope exit. There's also a `cleared`
set tracking which obj hashes had `clear_cache()` called this scope,
which lets `AXObject.clear_cache()` suppress duplicate clears within
the same handler (commit `ae5970ab9`).

---

## Layer 2: Long-lived property caches (`AXObject.LONG_LIVED_*`)

**Purpose:** Survive across events. AT-SPI properties for a given
accessible object almost never change during its lifetime; caching them
process-wide means even "first read after focus change" doesn't pay
D-Bus cost.

**Storage:** Module-level dicts on `AXObject`, keyed by `hash(obj)`,
guarded by `AXObject._lock`. Hard-capped at `_LL_CACHE_MAX` (8000)
entries each; on overflow the entire dict is cleared wholesale to
bound long-session memory.

**Caches:**
- `LONG_LIVED_ROLES: dict[hash, Atspi.Role]` — roles essentially never change after creation.
- `LONG_LIVED_PARENTS: dict[hash, Atspi.Accessible | None]` — parents rarely change.
- `LONG_LIVED_NAMES: dict[hash, str]` — names change occasionally (~3% of events).
- `LONG_LIVED_STATES: dict[hash, frozenset[int]]` — **stored as int values, not StateSet objects.** See "StateSet crash story" below.

**Populated:** `AXObject.get_role()`, `get_parent()`, `get_name()`,
`get_state_set()` write to the LL caches when fetching fresh data
from libatspi.

**Invalidation:** `AXObject.invalidate_for_event()`, called from
`event_manager._process_object_event` before each event is
dispatched. Specifically:

- `object:defunct` → drop the entry from every LL dict + mark
  `KNOWN_DEAD[key] = True`.
- `object:property-change:accessible-name` → drop `LONG_LIVED_NAMES[key]`.
- `object:property-change:accessible-role` → drop `LONG_LIVED_ROLES[key]`.
- `object:property-change:accessible-parent` → drop `LONG_LIVED_PARENTS[key]`.
- `object:state-changed:*` → drop `LONG_LIVED_STATES[key]` (any bit changed).

### The StateSet crash story

Earlier versions cached the `Atspi.StateSet` *object* directly. This
crashed: `Atspi.StateSet.contains(state)` dereferences an internal C
pointer to the underlying accessible. If the accessible went defunct
between cache write and read, libatspi would segfault inside `contains`.

The fix (commit `94e36c02d`): store `frozenset(int)` of state values
instead. `has_state()` checks the frozenset directly without going
through any `StateSet` object, so a later defunct cannot reach a
stale pointer. The frozenset is built once from a fresh StateSet via
`state_set.get_states()` at the moment of cache population, when the
object is provably alive.

---

## Layer 3: State-change baselines (`AXUtilitiesEvent.LAST_KNOWN_*`)

**Purpose:** Detect whether a state-change event represents a real
change worth announcing, vs. a redundant or self-induced one.

**Storage:** Module-level dicts on `AXUtilitiesEvent`, keyed by
`hash(obj)`. Stores the "value as of last announcement" for each
state, name, description.

**Caches:**
- `LAST_KNOWN_NAME / LAST_KNOWN_DESCRIPTION`
- `LAST_KNOWN_CHECKED / EXPANDED / INDETERMINATE / INVALID_ENTRY / PRESSED / SELECTED / VALUE`
- `IGNORE_NAME_CHANGES_FOR` — list of hashes to suppress name-change announcements for.
- `TEXT_EVENT_REASON: dict[Atspi.Event, TextEventReason]` — classification cache.

**Populated:** `AXUtilitiesEvent.save_object_info_for_events(obj)` is
called by event handlers when they're about to present state. Saves
the current state values as the new baseline.

**Invalidation:**
- `AXUtilitiesEvent.evict_object(obj)` — drops one obj from every dict.
  Called from `event_manager._handle_early_event_processing` on
  `window:destroy` (the destroyed window itself) and
  `object:children-changed:remove` (the removed child).
- `_clear_stored_data()` — background thread that calls
  `_clear_all_dictionaries()` every `_PERIODIC_WIPE_SECONDS` (600s).
  This is the long-tail safety net for entries we somehow didn't
  evict via events.

**Note:** The interval used to be 60s (commit `034f86369` extended it
to 600s once event-driven eviction was in place). Hot baselines now
survive normal idle periods.

---

## Layer 4: Descendant-of caches (`AXUtilities.IS_*_DESCENDANT`)

**Purpose:** Cache the answer to "is this object inside a list / a
combo box / a document / a tool tip / etc." Cheap-looking but
recursive: each call walks parents until it finds a match or runs out.

**Storage:** Module-level dicts on `AXUtilities`, keyed by
`hash(obj)`, value `bool`. One dict per descendant predicate (19
predicates total: `IS_DOCUMENT_DESCENDANT`, `IS_ENTRY_DESCENDANT`,
`IS_LIST_DESCENDANT`, etc.).

**Populated:** `AXUtilities._is_descendant()` walks the ancestor
chain ONCE and caches the resolved answer at *every visited node*.
A subsequent query at any intermediate node returns O(1). The walk
short-circuits when it hits an already-cached ancestor.

**Invalidation:** `AXUtilities._clear_all_dictionaries()` periodic
wipe (60s by default). Less aggressive event-driven invalidation
than Layers 2-3 because the descendant relationship rarely changes
once an object exists.

---

## Layer 5: Structural-nav match cache (`StructuralNavigator._nav_cache`)

**Purpose:** Cache the result of "find all headings / links / form
fields / etc. under root R" so that pressing H/K/B doesn't walk the
entire AT-SPI tree every time.

**Storage:** `_nav_cache: dict[(hash(root), cache_key), list[Atspi.Accessible]]`
with a parallel `_nav_cache_rebuilders: dict[same_key, Callable]`
holding the compute function for each entry. Lock: `_nav_cache_lock`.
Cap: `_NAV_CACHE_MAX = 200` entries.

**Populated:** `StructuralNavigator._cached_or_compute(root, cache_key, compute_fn)`.
Memoizes the result of `compute_fn()` (typically
`AXUtilities.find_all_X(root)`) keyed on the root.

**Invalidation (the most interesting layer):**

Two event types matter:
1. **`object:defunct`**: drop entries rooted at the source immediately.
   The root is gone, no point serving stale matches.
2. **`object:children-changed:*`**: the tree under some root may have
   changed. Mark all cache entries whose root is an ancestor of the
   event source as "stale" (in `_nav_cache_stale_keys`). Schedule a
   single deferred drop+rebuild via `DebouncedCallable.arm(150ms)`.

When the 150ms debounce fires, `_drop_stale_keys()`:
1. Pops each stale key's rebuilder from `_nav_cache_rebuilders`.
2. Schedules each rebuild on `GLib.idle_add` (low priority).
3. Reads keep serving the *old* cached lists in the meantime.
4. When the idle rebuilder fires, it computes fresh matches and
   atomically swaps them into the cache.

The result: a dynamic page (Reddit comments updating, Gmail threads
refreshing, infinite scroll) doesn't cause every H/K press to pay a
tree-walk. The cache stays warm across the burst, drops once after
it settles, and rebuilds in idle time before the user's next press.

This pattern is what `DebouncedCallable` was extracted to support.

---

## How the layers interact

A typical AT-SPI focus event flow exercises all five layers:

1. Event arrives on `event_manager.event_listener`.
2. `_process_object_event` opens an `event_scope` (Layer 1).
3. Before dispatching, `AXObject.invalidate_for_event()` (Layer 2)
   and `AXUtilitiesEvent.evict_object()` (Layer 3) run as
   appropriate for the event type.
4. The script handler runs. It calls `AXUtilitiesRole.is_button(obj)`
   which calls `AXObject.get_role(obj)`:
   - Check Layer 1 (event scope) → miss.
   - Check Layer 2 (LL roles) → hit / miss.
   - If miss, fetch from libatspi via D-Bus → populate Layer 1 + 2.
5. The handler may call `AXUtilities.is_document_descendant(obj)`:
   - Check Layer 4 → hit / miss.
   - If miss, walk parents (using cached `get_parent` from Layer 2),
     populate Layer 4 at every visited node.
6. The handler may invoke structural nav (Layer 5) for next-heading
   queries — that layer has its own cache + rebuild logic separate
   from the others.
7. `event_scope` exits, Layer 1 cleared.

The takeaway: **don't try to consolidate the layers.** Each has
different lifetime and invalidation semantics that match its
workload. Mixing them caused real bugs in the project history.

---

## Measuring cache hit rate

Set `ORCA_PERF_LOG=1` in Orca's environment. The event-scope cache
will write a line to `~/orca-perf.log` after each event:

```
12.345 event_scope[object:state-changed:focused]: 4 ev-hits, 12 ll-hits, 3 misses (84% cached) in 2.1ms
```

- `ev-hits`: Layer 1 cache hits (within-event)
- `ll-hits`: Layer 2 hits (long-lived, across events)
- `misses`: actual D-Bus calls
- `% cached`: total hits / (hits + misses)

For the structural-nav cache (Layer 5), the same log shows:
```
nav_cache[headings] HIT n=23
nav_cache[headings] MISS n=23 fetch=18.3ms
```

Steady-state targets, measured on heavy web pages:
- Layer 1 + 2 combined: 95-98% cached
- Layer 5: depends on page churn rate; debounced-rebuild keeps the
  hit rate effective even during DOM updates.

---

## Common pitfalls when adding caches

If you're tempted to add a sixth cache layer or new entries to an
existing one, check:

1. **What's the invalidation event?** Be specific. If you can't name
   the AT-SPI event that means "this cached value is now wrong,"
   you're going to ship a stale-data bug.
2. **Can the cached value outlive its underlying object?** If yes,
   it must not hold any C pointer to that object (see the StateSet
   crash story). Cache primitive values instead.
3. **What's the cap?** A cache without a size bound is a memory
   leak. Even per-window caches grow during long sessions.
4. **What's the lock?** Reads from one thread, writes from another
   need synchronization. Look at `_latest_event` race fix
   (`551110555`) for the canonical example of what goes wrong without
   it.
5. **Is there already a cache for this?** Often yes -- and the
   right answer is to extend it rather than add a new one. This doc
   exists to help you find the existing ones.

---

## File reference

| File | Layer | Key types |
|---|---|---|
| `src/orca/ax_object.py` | 1, 2 | `_event_cache_tls`, `LONG_LIVED_*` |
| `src/orca/ax_utilities_event.py` | 3 | `LAST_KNOWN_*`, `evict_object()` |
| `src/orca/ax_utilities.py` | 4 | `IS_*_DESCENDANT`, `_is_descendant()` |
| `src/orca/structural_navigator.py` | 5 | `_nav_cache`, `_nav_cache_rebuilders` |
| `src/orca/event_manager.py` | — | Drives invalidation hooks for 1, 2, 3, 5 |
| `src/orca/util/debounce.py` | — | `DebouncedCallable` used by Layer 5 |
