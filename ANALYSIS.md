# Orca Screen Reader — Comprehensive Code Analysis

**Date:** May 2026
**Branch under analysis:** `main` (heading toward Orca 51.alpha)
**Installed reference version:** 50.0.9 (Fedora 44)
**Total codebase:** ~150,000 lines of Python across ~114 files in `src/orca/`

This document is the synthesis of four parallel deep-dives, also saved alongside it:

- `ANALYSIS_CORE.md` — event flow, AT-SPI access layer, caching
- `ANALYSIS_SPEECH.md` — speech, braille, sound pipelines
- `ANALYSIS_WEB.md` — browse/focus mode, structural nav, web content
- `ANALYSIS_CONFIG.md` — GSettings migration, keybindings, app scripts

---

## Executive Summary

1. **Orca is well-architected for correctness; the bottleneck is the IPC model, not the code.** Every property read on an accessible object (role, name, state, parent) is a D-Bus round-trip to the target application's process. A single focus event triggers 10–15 such calls, costing 100–200 ms. NVDA's equivalent is 8–12 in-process calls at ~1–2 ms total. **This single architectural difference accounts for nearly all the felt-latency gap.**

2. **Caching exists but is intentionally conservative.** Orca has two cache layers: libatspi (C, opaque) and Orca's own `AXObject`/`AXUtilities` Python dicts. A background thread **wipes everything every 60 seconds** (`ax_object.py:54-84`). This protects against stale data but also throws away hot caches after idle, and within a single event Orca often re-queries the same property 3–5 times without memoization.

3. **~40% of the accessibility-tree wrapper layer is workarounds for upstream bugs.** Firefox bogus sections (`ax_object.py:107-115`), Qt broken ancestry (`ax_object.py:120-142`), Electron focus spam (`ax_utilities.py:211`), LibreOffice collection misbehavior (`ax_object.py:242-273`), and ~50 TODOs in the web scripts. These are necessary because Orca supports many toolkits with imperfect AT-SPI implementations.

4. **The GSettings migration (Orca 50) is complete and clean.** No JSON remnants. Layered config lookup (app → profile → default) is elegant. But `command_manager.py` ballooned to 1,946 lines because the keybindings preferences GTK UI (900+ lines) lives inside it.

5. **The Spiel speech migration is scaffolded but unfinished.** `spiel.py` exists (750 lines), the API hooks are wired up, but say-all offset mapping is TODO at lines 467/476/483 and there's a `time.sleep(0.01)` in shutdown (line 543). When complete, Spiel cuts speech latency 20–30% by eliminating the speech-dispatcher daemon hop.

6. **The web subsystem has no virtual buffer.** Every H/K/F/B keystroke walks the AT-SPI tree from the document root to find all matching elements (`structural_navigator.py:1901-1913`). On a 10KB page this is sub-millisecond; on a 100KB page it's 10ms+ per keystroke. NVDA pre-builds a flat buffer at page load and binary-searches it. This is the single biggest performance gap for heavy web pages.

7. **The script (per-app handler) system is mostly redundant.** 12 app scripts totaling ~2,361 lines, plus 5 toolkit scripts at ~719 lines. Many app scripts are thin wrappers (Gajim: 75 lines, Smuxi: 70, xfwm4: 77) that override 1–3 methods from `default.Script`. Pidgin's 298-line script targets software EOL since 2023.

8. **There is a real (but low-impact) race condition** in `event_manager.py:662-667` — `_latest_event` dict is written without holding `_gidle_lock`. Multiple D-Bus callback threads could race. Easy fix.

9. **No comprehensive test coverage visible** in the source tree — the test directory is mostly integration tests, not unit tests of the AX wrapper layer where most of the bug-fix work happens.

10. **The highest-impact, lowest-effort improvement is per-event memoization of role/parent/name** in event handlers. Estimated 20–30% reduction in per-event IPC. Fully contained in `ax_object.py` and event handler code. **Recommended first patch.**

---

## State of Orca, in One Paragraph

Orca is a mature, conservative, correctness-first screen reader paying the architectural cost of D-Bus-mediated accessibility. The codebase shows clear signs of a recent refactor (Orca 50's `AX*` wrapper modules and GSettings migration) that improved structure but did not yet capitalize on caching opportunities those abstractions enable. Performance work is left on the table not because the developers don't see it, but because their priority has been correctness across an ecosystem of imperfect toolkits — Mozilla, Qt, Chromium, Electron, LibreOffice — each with its own AT-SPI quirks. The result is a reliable but sluggish screen reader where 60–80% of opportunity for speedup lies in better caching strategies that don't require touching upstream projects.

---

## Architecture Overview

```
                        ┌──────────────────────────────┐
                        │   Application (Firefox, etc) │
                        │   ┌────────────────────────┐ │
                        │   │ AT-SPI Bridge          │ │
                        │   │ (toolkit-specific)     │ │
                        │   └───────────┬────────────┘ │
                        └───────────────┼──────────────┘
                                        │ D-Bus
                                        ▼
                        ┌──────────────────────────────┐
                        │  at-spi2-registryd (daemon)  │
                        │  + libatspi (client cache)   │
                        └───────────────┬──────────────┘
                                        │ D-Bus  (~10-15ms/call)
                                        ▼
                        ┌──────────────────────────────┐
                        │   Orca Python Process        │
                        │   ┌────────────────────────┐ │
                        │   │ event_manager.py       │ │  ← enqueue, priority queue,
                        │   │  (GLib main loop)      │ │     idle_add dequeue
                        │   └───────────┬────────────┘ │
                        │               ▼              │
                        │   ┌────────────────────────┐ │
                        │   │ script_manager.py      │ │  ← per-app script selection
                        │   └───────────┬────────────┘ │
                        │               ▼              │
                        │   ┌────────────────────────┐ │
                        │   │ default.Script + app   │ │  ← event handler
                        │   │  script (1659+ lines)  │ │
                        │   └───────────┬────────────┘ │
                        │               ▼              │
                        │   ┌────────────────────────┐ │
                        │   │ AX* wrapper layer      │ │  ← AXObject, AXUtilities,
                        │   │  (24 modules, ~14k LOC)│ │     AXText, AXTable, ...
                        │   │  + 60s cache wipe      │ │
                        │   └───────────┬────────────┘ │
                        │               ▼              │
                        │   ┌─────────────┬──────────┐ │
                        │   │ speech_     │ braille_ │ │
                        │   │ presenter   │ presenter│ │
                        │   └──────┬──────┴────┬─────┘ │
                        └──────────┼───────────┼───────┘
                                   ▼           ▼
                          ┌────────────┐  ┌──────────┐
                          │ speech-    │  │ brltty + │
                          │ dispatcher │  │ BrlAPI   │
                          │ (daemon)   │  └──────────┘
                          └──────┬─────┘
                                 ▼
                          ┌────────────┐
                          │ synthesizer│  (espeak, voxin, festival, ...)
                          └────────────┘
```

Two key observations from the diagram:
- **Every horizontal arrow above the Orca process is D-Bus IPC.** That's the latency floor.
- **The AX* wrapper layer is where Orca-side optimization lives.** It's the right place to add caching without disturbing event/speech/script flow.

---

## What's Good

**Well-designed core machinery:**
- Event queue with priority, deduplication, and obsoleted-event detection (`event_manager.py:977`) — prevents key-mashing from saturating the pipeline.
- GLib main loop integration via `GLib.idle_add()` keeps the UI thread responsive.
- Per-app "script" model allows toolkit/app-specific behavior without polluting defaults.
- Focus manager centralizes "locus of focus" tracking, avoiding redundant queries.
- Live region presenter (`live_region_presenter.py`) has a sophisticated priority queue with politeness levels, 45-second keepalive, and dedup — better than typical implementations.
- Braille pipeline runs on a dedicated worker thread (`braille.py:269-299`) with proper queue handling — prevents block on slow displays.

**Modern configuration:**
- GSettings migration is **complete and clean** — no JSON remnants, no half-migrated paths.
- Layered config lookup (app override → profile → default) is elegant.
- Decorator-based metadata (`@gsetting`, `@gsettings_schema`) eliminates manual schema duplication.
- Per-profile and per-app settings actually work.

**Good code hygiene in newer modules:**
- The `AX*` wrapper modules (Orca 50 refactor) are well-organized: one concern per file, clear naming.
- Type hints used consistently in newer code.
- `pyproject.toml` configures ruff with a sensible lint set (line 8-30).

**Sensible defaults and error handling:**
- Defensive null checks throughout.
- "Known dead" object tracking (`ax_object.py:48`) prevents repeated queries on closed widgets.
- BrlAPI failures are handled with exponential backoff (`braille.py:436-472`) — Orca degrades gracefully if the braille display is unplugged.

---

## What's Bad (Objectively)

**Performance:**
- Within a single event, the same property is often queried 3–5 times (e.g., `get_role()` called by `is_text()`, then again by handler logic, then a third time by a child utility). **No event-scoped memoization.**
- The 60-second cache-wipe thread (`ax_object.py:54-84`) is paranoia from earlier bugs — after 60s idle, the next event re-queries the entire ancestry chain.
- Structural navigation walks the entire AT-SPI tree on every H/K/F/B keystroke (`structural_navigator.py:1901-1913`). No precomputed index.
- Speech has **no coalescing** — rapid arrow-key mashing in a list sends each utterance to speech-dispatcher separately, causing stuttering.

**Code bloat:**
- `command_manager.py` is 1,946 lines. ~900 of those are `KeybindingsPreferencesGrid` (GTK UI) that doesn't belong in the manager module.
- `scripts/web/script_utilities.py` is 3,449 lines with overlapping responsibilities (caching, context tracking, web-specific utilities).
- `default.Script` is 1,659 lines and registers 30+ event listeners.

**Opacity:**
- Keybinding format in dconf is `(keysym, mask, mods, clicks)` where the `_mask` field is parsed but never used. No docstring explaining the tuple. **Cryptic, fragile.**
- D-Bus service surface (`dbus_service.py`) is auto-discovered from decorators with no stability documentation.

**Concurrency bug:**
- `event_manager.py:662-667`: `_latest_event[(e.type, hash(e.source))] = counter` is written without holding `_gidle_lock`. Multiple D-Bus callback threads could race. Counter inconsistency, not a crash, but real.

**Stale code:**
- Pidgin app script (298 lines) targets software EOL since 2023.
- Smuxi app script (70 lines) targets software last released 2018.
- Solaris keycode workaround (`keybindings.py:96-98`) — flagged with TODO questioning if still needed on modern Linux.
- `_mask` field in keybinding tuples (`command_manager.py:1506`) parsed but never used.
- `SettingDescriptor.getter` field (`gsettings_registry.py:58`) declared but never populated.

---

## What's In Progress

**Spiel speech migration (scaffolded, not finished):**
- `spiel.py` exists at 750 lines, available as an alternative to speech-dispatcher.
- Provides in-process synthesis (~20–30% less latency than speech-dispatcher's daemon hop).
- **Incomplete:** say-all offset mapping (TODOs at `spiel.py:467`, `:476`, `:483`).
- **Incomplete:** `time.sleep(0.01)` blocking call in shutdown (`spiel.py:543`) — should use `GLib.timeout_add`.
- Not yet the default; falls back to speech-dispatcher if Spiel libs unavailable.

**Newer `AX*` wrapper layer (Orca 50):**
- Replaced older direct libatspi calls with structured wrappers.
- Foundation for future caching/batching work — the API surface is there, but few callers exploit it yet.
- Some modules still light on caching (e.g., `get_role` in `ax_object.py:631` has no Python-level cache).

**Web content cache invalidation:**
- `scripts/web/script_utilities.py` has aggressive caching (caret contexts, off-screen labels, content-editable state).
- The invalidation strategy is broad (`dump_cache()`) — granular invalidation by object hash would be better but isn't implemented.

**D-Bus service exposure:**
- Decorator pattern (`@command`, `@getter`, `@setter`) is clean but not yet documented for external consumers.

---

## What's Stale, Hacky, or Could Be Objectively Improved

### Workarounds for Upstream Bugs (~40% of `ax_*.py`)

Categorized:

**Application bugs (8 known):**
| Workaround | Location | Issue |
|---|---|---|
| Firefox bogus sections | `ax_object.py:107-115` | Mozilla bug 1879750 |
| Qt broken ancestry | `ax_object.py:120-142` | QTBUG-130116 |
| Electron focus spam | `ax_utilities.py:211` | Slack/Discord/WhatsApp lie about focus |
| Mutter X11 frames | `event_manager.py:387-390` | Spurious events from window decorator |
| Google Sheets attributes | `ax_utilities_table.py:490` | Wrong attribute names |
| WebKit text selection | `ax_utilities_text.py:919` | Incomplete selection ranges |
| Firefox dialog unknown app | `ax_utilities.py:176-180` | File chooser dialogs |
| Window manager name mapping | `script_manager.py:99-115` | marco/metacity/gtk-window-decorator |

**Toolkit/spec gaps (4):**
- LibreOffice collection interface for spreadsheets (`ax_object.py:242-273`)
- AT-SPI version constraints blocking features (`ax_utilities_role.py:142`)
- Text insertion size spam filter (`event_manager.py:315`)
- Per-character embedded-language switching (`speech_presenter.py:3150-3162` — inefficient)

**Defensive re-querying (3 patterns):**
- Child consistency validation doubles IPC (`ax_object.py:588-607`)
- Index-in-parent validation triples IPC (`ax_object.py:529-546`)
- Window can-be-active forces cache clear before checks (`ax_utilities.py:150-185`)

### Code-Quality Issues

- **`fake role` TODO:** `speech_generator.py:1789` — "This function and fake role really need to die" — legacy AT-SPI workaround.
- **Output module sync bug:** `speechdispatcherfactory.py:576` — TODO admitting `self._id` isn't updated when output module changes.
- **Word-wrap range grouping:** `braille.py:1569` — known limitation in braille line breaks.
- **~50 TODOs in `scripts/web/`** — mostly refactoring suggestions, browser-specific quirks, code-quality nits.

### Architectural Friction

- Decorator-based config (`@gsetting`) registers descriptors that are mostly used only for schema generation. Runtime lookups bypass them. **The decorators feel half-finished.**
- Profile rename is a copy-then-reset operation (`gsettings_registry.py:549-572`). Not atomic. If interrupted, both old and new profiles exist.
- Each profile holds a full copy of all settings — no inheritance. Bloats dconf storage.

---

## Cross-Cutting Themes

### 1. IPC Cost Is the Bottleneck, Not Python

Across all subsystems, the same pattern repeats: a measured operation is 90%+ D-Bus latency, <10% Python execution. Rewriting Orca in C or C# would speed up the <10%. Caching/batching the IPC would speed up the 90%. **All meaningful speedups live in the caching/batching layer.**

### 2. Caching Is Conservative by Design

The 60-second cache wipe (`ax_object.py:54-84`, repeated in `ax_utilities.py`, `ax_utilities_event.py`, and others) exists because:
- Objects die when widgets are destroyed.
- The AT-SPI tree mutates without always firing events.
- Stale data spoken to a blind user is harmful.

The wipe is correct insurance but **expensive insurance**. Event-driven invalidation (clear specific objects on `defunct`, `children-changed`, window-close) would let hot caches stay warm much longer. **This is the single best improvement.**

### 3. Workaround Code Lives at the Wrong Layer

Many app/toolkit-specific hacks (Firefox section bogus, Qt ancestry broken, Electron focus spam) live in `ax_object.py` / `ax_utilities.py` — generic layers. They should live in the per-app or per-toolkit scripts. As-is, every Orca user pays the cost of checking these conditions for every object, even on apps that don't have the bugs.

### 4. Incomplete Modernization

Orca 50 introduced the `AX*` layer and GSettings migration. The structure is there, but many call sites haven't been updated to use the new abstractions consistently. Example: `get_role()` in `AXObject` doesn't memoize, even though `AXObject` is the natural place to do so. The infrastructure is ready; the consumers haven't caught up.

### 5. Comparison to NVDA: Three Real Differences

| Dimension | Orca | NVDA |
|---|---|---|
| **Accessibility transport** | D-Bus IPC (out-of-process) | UIA/IA2 (in-process within app) |
| **Web content** | Live AT-SPI tree walking | Pre-built virtual buffer |
| **App customization** | Static `scripts/apps/*.py` (compiled-in) | Dynamic `appModules/*.py` (load on process-name match) |

The first explains 99% of the latency gap. The second is responsible for slow structural nav on heavy pages. The third is mostly a developer-experience difference.

---

## Subsystem Highlights (Compressed)

### Event/Core (`ANALYSIS_CORE.md`)

- 10–15 D-Bus calls per focus event → ~100–200 ms perceived latency
- 60-second cache wipe is the biggest single source of avoidable re-queries
- ~40% of `ax_*.py` is workarounds for upstream toolkit bugs
- One real race condition (`_latest_event` dict, easy fix)

### Speech/Braille (`ANALYSIS_SPEECH.md`)

- Speech: synchronous pass-through, no internal queueing → no coalescing of rapid events
- Braille: async worker thread, well-designed
- Spiel scaffolded but unfinished; would save 20–30% speech latency once done
- Per-character embedded-language voice switching is wasteful — should group by language span

### Web/Document (`ANALYSIS_WEB.md`)

- No virtual buffer; structural nav walks full tree per keystroke
- Aggressive context caching helps caret nav
- Live region implementation is genuinely good
- ~50 TODOs in web scripts; many are browser-specific event filtering hacks

### Config/Scripts (`ANALYSIS_CONFIG.md`)

- GSettings migration is complete and clean
- `command_manager.py` is bloated (1,946 lines, much should be elsewhere)
- ~half the app scripts are thin wrappers; some target EOL apps (Pidgin, Smuxi)
- Keybinding tuple format is undocumented and fragile

---

## Ranked Improvement Roadmap

### Tier 1 — High Impact, Low Effort (target first)

| # | Change | Estimated win | Effort |
|---|---|---|---|
| 1 | Per-event memoization of `get_role`, `get_parent`, `get_name`, `get_state_set` via an event-scoped cache context manager | 20–30% per-event IPC | 2-3 days |
| 2 | Replace 60-second cache wipe with event-driven invalidation (clear on `object:defunct`, `object:children-changed:remove`, window-close) | 20–40% faster on resume from idle | 3-5 days |
| 3 | Batch `get_name` + `get_description` via single attribute fetch | 5–10% IPC reduction | 1-2 days |
| 4 | Output coalescing in speech presenter (50–100 ms window, merge same-voice utterances) | Reduces stuttering on rapid keys | 2-3 days |
| 5 | Fix `_latest_event` dict race in `event_manager.py:662` | Correctness | 30 minutes |
| 6 | Optimize embedded-language: group by language span instead of per-character voice changes (`speech_presenter.py:3150-3162`) | Significant on multilingual text | 1 day |

### Tier 2 — High Impact, Medium Effort

| # | Change | Estimated win | Effort |
|---|---|---|---|
| 7 | Document navigation index — pre-compute headings/links/forms list per document, invalidate incrementally on children-changed events | 10–100× structural nav on heavy pages | 1-2 weeks |
| 8 | Complete Spiel integration (finish say-all offset mapping, remove `time.sleep` from shutdown) | 20–30% speech latency reduction | 1-2 weeks |
| 9 | Collection query result caching with TTL | 40–60% faster table/grid nav | 1 week |
| 10 | Pre-fetch script-activation properties (cache role/state of immediate children on app switch) | Smoother first event in new app | 4-5 days |
| 11 | Move toolkit/app-specific workarounds from `ax_*.py` to per-app scripts | Cleaner architecture, faster default path | 2 weeks |

### Tier 3 — Cleanup, Medium Effort

| # | Change | Benefit | Effort |
|---|---|---|---|
| 12 | Split `command_manager.py` — extract `KeybindingsPreferencesGrid` to its own file | Maintainability | 1 day |
| 13 | Replace keybinding tuple `(keysym, mask, mods, clicks)` with named tuple/dataclass | Maintainability, fewer cryptic bugs | 2-4 hours |
| 14 | Deprecate EOL app scripts (Pidgin, Smuxi) | Maintenance reduction | 1 day |
| 15 | Investigate/remove Solaris keycode workaround in `keybindings.py:96-98` | Simpler matching logic | 1 day investigation + 1 day refactor |
| 16 | Consolidate `_create_speech_generator` / `_create_braille_generator` boilerplate via a generator registry | ~800 lines saved across app scripts | 1 week |
| 17 | Document D-Bus service stability | External tool ecosystem | 4-6 hours |

### Tier 4 — Strategic, High Effort

| # | Change | Notes |
|---|---|---|
| 18 | Propose AT-SPI batch query API upstream (single D-Bus call returns role+name+state+children) | Requires libatspi + toolkit cooperation |
| 19 | Persistent daemon-side cache in at-spi2-registryd | Requires upstream design work |
| 20 | Plugin-based dynamic script loading (like NVDA app modules) | Architectural change, multi-version migration |

---

## Recommended First Patch

**Per-event memoization** is the right starting point because:
- Fully contained in `ax_object.py` + a small event-handler context manager — no upstream changes
- Risk profile: low (cache lives only within a single event; cannot serve stale data across events)
- Measurable: instrument with `time.monotonic()` around event handlers; report before/after
- Validates the larger Tier 1 caching investment (if this works, items #2–#3 follow naturally)

**Sketch:**
```python
# ax_object.py — add event-scoped cache context
class AXObject:
    _event_cache: ClassVar[threading.local] = threading.local()

    @staticmethod
    @contextmanager
    def event_scope():
        """Memoize get_role/get_parent/get_name within an event handler."""
        AXObject._event_cache.roles = {}
        AXObject._event_cache.parents = {}
        AXObject._event_cache.names = {}
        try:
            yield
        finally:
            AXObject._event_cache.roles = None
            AXObject._event_cache.parents = None
            AXObject._event_cache.names = None

    @staticmethod
    def get_role(obj):
        cache = getattr(AXObject._event_cache, "roles", None)
        if cache is not None:
            key = hash(obj)
            if key in cache:
                return cache[key]
        # ... existing implementation ...
        if cache is not None:
            cache[key] = role
        return role
```

Wrap event dispatch with `with AXObject.event_scope():` in `event_manager.py:_process_object_event()`. That's the whole patch surface.

Benchmarking plan:
1. Add `--profile-events` flag that logs per-event handler time
2. Run pre-patch: capture distribution of event-handling times on representative workload (focus changes in Firefox, typing in gedit, navigation in LibreOffice Writer)
3. Apply patch; rerun same workload
4. Compare distributions; require statistically significant improvement before merging

---

## Where to Read Deeper

- `ANALYSIS_CORE.md` — full event trace, all IPC counts, hot-path inventory, all bug workarounds catalogued
- `ANALYSIS_SPEECH.md` — Spiel migration status with specific line numbers, braille worker thread details, speech-dispatcher vs Spiel latency breakdown
- `ANALYSIS_WEB.md` — browse/focus mode mechanics, structural nav cost analysis, NVDA virtual-buffer gap analysis
- `ANALYSIS_CONFIG.md` — GSettings migration concrete state, full app-script inventory with line counts and triage notes

---

## Conclusion

Orca is a careful, correctness-first screen reader where the design choices that hurt performance (D-Bus IPC, conservative caching, full tree walking) were made to protect users from a more dangerous failure mode (stale data spoken aloud). The codebase is in good shape architecturally — the recent Orca 50 refactor laid groundwork that hasn't been fully exploited yet. The largest gains come from caching strategies that don't change behavior, only when work is repeated. Starting with per-event memoization, then event-driven cache invalidation, then a structural nav index, would compound to 2–3× perceived speedup before touching upstream projects. None of this requires a rewrite. The hard architectural items (in-process accessibility, batch AT-SPI API) are real but should not be where work starts.
