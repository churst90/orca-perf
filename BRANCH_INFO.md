# perf/atspi-event-cache — branch metadata

This branch is a personal fork of GNOME Orca with performance and stability
patches. It is intended for **personal use**, not for upstream submission as-is.
The patches are documented in detail in `ANALYSIS.md` and the
`ANALYSIS_*.md` sibling files.

## Upstream base

This branch was forked from upstream `main` at:

| Field | Value |
|---|---|
| Upstream repo | https://gitlab.gnome.org/GNOME/orca.git |
| Base commit | `2e5830e714511807ccbf9e42cef0d15fc0faed69` |
| Base commit date | 2026-05-08 |
| Base commit subject | Update Slovenian translation |
| Upstream version at base | `51.alpha` (per `meson.build`) |
| Stable release closest to base | `50.1.2` |
| Branch created | 2026-05-11 |
| Branch name | `perf/atspi-event-cache` |

## What the patches change

This branch carries three categories of patches:

**Performance / stability (commits `d1b418a19` through `713b9cc29`)**

Aggressive caching of AT-SPI property reads (role, parent, name) within
and across events; a per-document cache of structural-navigation match
lists (headings, links, etc.); and held-key coalescing so that key
auto-repeat does not flood `script.present_object()` with overlapping
scroll-and-speech calls.

**Round-2 perf (commits `2176c4952` through `7267c32d3`)**

Five follow-on patches taken from the per-subsystem ANALYSIS docs:
- `2176c4952` — re-enable the long-lived name cache (the held-key
  coalesce fix in `ccda9d591` was the actual cure for the
  wrong-window-title-on-Alt-Tab issue, not the name-cache disable).
- `41760ec68` — batch the language-attribute lookup in
  `spell_item` / `spell_phonetically`. Was one AT-SPI IPC per
  character; now one per language run (typically one for the whole
  word on monolingual input).
- `4bceeca8a` — pre-warm AT-SPI caches at `Script.activate()`. Touches
  the active window and up to 50 immediate children, populating the
  LL role and name caches so the first focus event after Alt-Tab
  doesn't pay full discovery cost.
- `034f86369` — replace the 60-second blanket `AXUtilitiesEvent`
  wipe with event-driven eviction via `evict_object()`. Hooked from
  `event_manager` on `window:destroy` and
  `object:children-changed:remove`. Periodic safety wipe relaxed to
  10 minutes.
- `7267c32d3` — defer structural-nav cache drops by 150ms after
  children-changed events. Dynamic pages (Gmail, Twitter, Reddit)
  fire dense bursts of these; previously every event in the burst
  dropped the per-root match cache, so every H/K/B press during the
  burst paid a full tree walk. Now the cache keeps serving across
  the burst and is dropped once after it settles.

**Round-3 perf + correctness (commits `cf34bf0a4` through `740d84192`)**

Five more from the deferred list:
- `cf34bf0a4` — `SpeechServer.get_info()` in `speechdispatcherfactory.py`
  now prefers the live `_output_module` over the construction-time
  `self._id`, so the preferences display tracks the running output
  module after a `set_output_module()` call. Closes a long-standing
  TODO; `self._id` deliberately stays as the `_active_servers` key.
- `551110555` — lock `_latest_event` on read and clear paths in
  `event_manager.py`. Previously the AT-SPI dispatch thread wrote
  under `_gidle_lock` but the GLib main thread read without it, and
  `deactivate()` / `pause_queuing(clear_queue=True)` reassigned the
  dict wholesale, so cross-thread observers could see stale or
  orphan state. Now reads hold the lock and clears use `.clear()`.
- `ae5970ab9` — suppress duplicate `AXObject.clear_cache` calls
  within a single event scope. Multiple handlers in one event
  commonly clear the same obj; the first does real work, subsequent
  calls are D-Bus round-trips for nothing. Tracked via a new
  `cleared` set on the existing `event_scope` TLS. Recursive clears
  always run.
- `e031c1239` — rewrite `AXUtilities._is_descendant` to cache the
  resolved answer at every visited ancestor in a single walk, not
  just at the queried node. Also closes a redundancy where a True
  parent answer still fell through to a full `find_ancestor` walk
  on obj.
- `740d84192` — upgrade the 150ms nav-cache debounce from
  drop-and-recompute to background-rebuild. The debounce timer now
  schedules each stale entry's rebuild on `GLib.idle_add` instead of
  dropping it; reads keep serving the old list until the idle
  handler swaps the new one in. By the time the next H/K press
  lands the cache is typically already refreshed.

**Round-4/5/6 (commits `40c404eff` through `733ff2f99`)**

Smaller verified-real-bug fixes plus diagnostics from the holistic
audit and the upstream merge. See `FINDINGS.md` for the per-item
rationale and which items were skipped as false positives. Highlights:

- `40c404eff` — `HUNG_OBJECTS` lock (regression inherited from the
  upstream merge `c1d969e25`).
- `5d9954ebe` — D-Bus call-rate WARNING diagnostic (no behavior
  change; visibility only).
- `bf34212fd` — 10s BrlAPI NoOp probe so a hung brltty is caught
  during idle time instead of stalling the next real write.
- `282fa31ed` — Player.shutdown() releases bus watches + signal
  handlers so GStreamer can finalize.
- `9b0b9e039` — `rename_profile()` rolls back partial new-profile
  state if copy/metadata fails mid-flight.
- `8be6260cb` — 30s SSIP liveness probe; speech-dispatcher restart
  is caught during idle time so the next user-driven speak finds a
  healthy connection.
- Documentation: `FINDINGS.md` Parts 1-3 (holistic + cross-stack +
  subsystem audits).

**Round-7 (commits `f5b5131b9` through `d6465f7d2`)**

The "remaining 20%" plus the smaller P-tier findings:

- `f5b5131b9` — `notification_presenter` to `collections.deque` plus
  `_current_index` adjustment on truncation. Subsumes P1/P2/P3.
- `a7e6ede68` — `mouse_review` single-pending-timer pattern (P4).
- `ac44497b3` — `phonnames.py` defensive parse with English fallback
  (P7) so a malformed translation no longer breaks Orca startup.
- `9d0d24686` — `focus_manager.is_in_preferences_window()` broadened
  to cover descendant dialogs via same-application comparison. The
  root cause that the synth-revert fix routed around.
- `0adbbc56b` — `VoicesPreferencesGrid.revert_changes()` extended to
  cover rate/pitch/pitch-range/volume/family-* runtime overrides,
  not just the synthesizer combo.
- `94e36c02d` — `LONG_LIVED_STATES` reintroduces the long-lived state
  cache that `6445b86e4` had to revert. Stores `frozenset[int]`
  instead of `Atspi.StateSet`, so a defunct object can no longer
  cause a stale-pointer segfault inside libatspi. Reclaims roughly
  the 3 percentage points of cache hit rate that the earlier
  revert lost.
- `adcbbd3a3`, `d6465f7d2` — unit tests for the upstream-candidate
  patches (HUNG_OBJECTS sync, `_is_descendant` walk-and-cache,
  `_iter_text_with_language`, `LONG_LIVED_STATES`). 21 new tests,
  all green.

Two items from the round-7 plan were intentionally deferred:
- Caret-order pre-computation (multi-day; risk of regression on a
  critical user-path outweighs the marginal gain).
- True incremental web nav index (the existing background-rebuild
  in `740d84192` already extracts the perceived-speed benefit;
  going from background-rebuild to mutate-cached-list is a
  CPU-bandwidth win during idle, not a user-latency win, and the
  sort-order correctness risk is real).
Both are documented in the commit log; revisit if either becomes
a measured user pain point.

**Phase 1 architectural foundation (commits `62bbcc6b2` through
`45d90d2d6`)**

Per the comprehensive plan in this file (see "Architectural cleanup"
section below): build the shared infrastructure that later phases
will use. Five items, all additive or behavior-preserving:

- `62bbcc6b2` — `orca.util.debounce.DebouncedCallable`. Single helper
  consolidates the hand-rolled single-pending-timer pattern used by
  structural_navigator, mouse_review, flat_review_presenter, and
  speechdispatcherfactory. 8 unit tests; ~60 lines of duplicated
  timer-bookkeeping deleted.
- `0df794228` — `docs/caching.md`. Authoritative explanation of the
  five cache layers (event-scope, long-lived AT-SPI, state-change
  baselines, descendant-of, structural-nav matches) with
  invalidation rules and the file reference table. Reader can
  answer "which cache do I invalidate?" without reading
  ax_object.py end-to-end.
- `9194c07c0` — `orca.telemetry` D-Bus interface. Read-only counters
  published under `org.gnome.Orca1.Telemetry`: cache hit rate,
  event queue depth, hung/dead object counts, LL cache size, nav
  cache stats, speechd/braille connection status. Diagnoseable
  from `gdbus call` without `--debug-file`. `ax_object.py` gained
  `LIFETIME_CACHE_HITS / LL_CACHE_HITS / CACHE_MISSES` class
  counters fed unconditionally by the existing `_record_*` helpers.
- `45d90d2d6` — `orca.stress_mode`. `ORCA_STRESS=1` env-gated
  harness with five stressors (hung-object, speechd reset, brlapi
  reset, navcache invalidation, placeholder event-flood) that
  exercise the robustness paths we built in rounds 4-7. Off by
  default. Lets us regression-test concurrency / recovery work
  without waiting for real-world conditions.

**Phase 2 structural-nav registry + dispatcher (commits `991a25158`
through `019db4cd7`)**

Collapses the 23 hand-written element-type quadruples in
`structural_navigator.py` (`_get_all_X` / `previous_X` / `next_X` /
`list_X`, plus six heading-level variants of the trio) into a
data-driven registry plus a single generic dispatcher. Three commits,
all behavior-preserving:

- `991a25158` — `ElementType` + `ElementRegistry` skeleton. Frozen
  dataclass captures the matcher, the "no more" message, and the
  list-dialog metadata; registry holds the ordered table. No
  consumers yet. 15 unit tests for shape, validation, and singleton
  identity.
- `31f380911` — register all 29 builtins (24 base types +
  heading-level 1..6), wire `StructuralNavigator.__init__` to call
  `register_builtins(self)`. Heading-level variants share the
  "headings" cache slot via `cache_key` and lazy-format their
  per-level templates via `format_arg`. List row builders gain a
  leading `script` parameter so the dispatcher can pass it at
  invocation time without racy `last_script` capture. Also fixes
  meson wiring that step 1 forgot. 5 new tests; 113 total green.
- `019db4cd7` — generic dispatcher. Three helpers
  (`_dispatch_previous`, `_dispatch_next`, `_dispatch_list`) handle
  the boilerplate that every per-type method repeated verbatim. All
  85 `previous_X` / `next_X` / `list_X` bodies became one-line calls
  into the dispatcher. The `@dbus_service.command` decorator stays
  on every wrapper so the D-Bus surface and `command_manager`
  lookups are byte-identical. Landmark's `_present_landmark`
  specialization is hardcoded inside `_dispatch_directional` with a
  comment explaining why it's not another field on `ElementType`
  (one record would set it). Net diff: `structural_navigator.py`
  4378 → 3024 lines (-1354 / ~32%).

Pure refactor — zero runtime delta on its own. The win is structural:
adding a new element type now costs one `ElementType` record instead
of four ~50-line methods, and the registry is the natural foundation
for a future single-walk multi-type classifier (one tree traversal
classifies every node against every matcher in registration order)
if that ever becomes the bottleneck.

**Speech-prefs correctness (commit `e05d8868d`)**

Fixes a regression in the GSettings-based prefs system where changing
the speech synthesizer in Orca preferences and saving would silently
revert the choice. The combo selection was applied to the live speech
server but never persisted to dconf; `default.py:Script.activate()`
calls `update_synthesizer()` on every focus event, which reads dconf
(still the old value), and on save the dconf value won out. Symptom
was most visible switching to/from synthesizers with unique voice
names — `sd-piper`'s `en_US-ryan-medium` etc. — because the voice-name
side effect in speech-dispatcher's `SET SYNTHESIS_VOICE` would flip
modules. Fix follows the established `_on_punctuation_changed` pattern
(register a runtime override in the combo handler, read the combo
widget in save_settings, clear the override on Cancel).

See `ANALYSIS.md` for the full design report on the perf patches, and
individual commit messages for per-patch rationale and measured impact.
The synth fix is self-contained in the commit message of `e05d8868d`.

Two changes in this branch fix issues that were observed but never
reproduced in stock 50.1.2; treat them as caveats:

- The long-lived state-set cache was reverted (`6445b86e4`) because
  `Atspi.StateSet.contains()` segfaulted when called on a cached
  StateSet whose underlying object had become defunct between events.
  Caching state at the cross-event layer is unsafe without a different
  approach (probably storing primitive state bits, not the StateSet
  object).
- `AXObject._NAME_LL_CACHE_DISABLED = True` is set in the final commit
  as a diagnostic for a wrong-window-title-on-Alt-Tab issue. With the
  flag set and the held-key fix in place, the issue is gone. Whether
  the name cache was the root cause is undetermined.

## Building from this branch

This branch assumes Fedora 44 build dependencies. If you're on a different
distro, install the equivalents of:
`meson ninja-build at-spi2-core-devel at-spi2-atk-devel atk-devel python3-gobject-devel gettext itstool yelp-tools desktop-file-utils`.

```sh
git clone https://github.com/churst90/orca-perf.git
cd orca-perf
git checkout perf/atspi-event-cache
meson setup builddir --prefix="$HOME/.local" -Dmathcat=false
meson install -C builddir
```

The build installs to `$HOME/.local`, leaving the system Orca untouched.
To run the built Orca, prepend `~/.local/bin` to `PATH`, OR (when running
under systemd-user) add a drop-in like the one at:

`~/.config/systemd/user/orca.service.d/perflog.conf`
```
[Service]
Environment="ORCA_PERF_LOG=1"
ExecStart=
ExecStart=/home/<user>/.local/bin/orca --replace
```

The explicit `ExecStart=` is required because systemd-user's `DefaultPath`
resolves bare command names against `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`
only — it does *not* honor `~/.local/bin` even when the user's shell PATH
does. Without the override systemd silently runs the distro's `/usr/bin/orca`.

## Recovering to stock Orca

If a custom Orca build misbehaves:

```sh
systemctl --user stop orca.service
/usr/bin/orca --replace &
```

This brings up the distro-installed Orca; nothing on disk needs reverting.

## Keeping up with upstream

To merge in new upstream fixes:

```sh
cd ~/dev/orca
git fetch origin             # this fork's remote
git remote add upstream https://gitlab.gnome.org/GNOME/orca.git  # one time
git fetch upstream
git checkout main
git merge upstream/main      # pull in new upstream commits to your local main
git checkout perf/atspi-event-cache
git rebase main              # or git merge main, your preference
```

Review the rebased commits and the upstream changes side-by-side. Pay
particular attention to:
- Any new code in `src/orca/ax_object.py`, `event_manager.py`,
  `structural_navigator.py`, or `speech_presenter.py` — these are the files
  we've patched.
- Any upstream caching work — if upstream lands their own AT-SPI caching
  (an active topic per the GNOME accessibility mailing list), drop our
  matching patches to avoid divergence.
- Any upstream Spiel migration progress (`spiel.py`) — when the offset
  mapping TODOs at lines 467/476/483 are fixed, the in-process speech
  path becomes viable, which would be a bigger latency win than anything
  in our branch.

## Measured impact

On the author's Fedora 44 + MATE + X11 setup, in a VM:

| Metric | Stock | This branch |
|---|---|---|
| Per-event AT-SPI property reads cached | 0% | ~95% steady-state |
| Held-key crashes (systemd watchdog) | yes, ~6s after sustained hold | no |
| Held-key speech pile-up | yes | no |
| Alt-Tab wrong-window-title | sometimes (toolkit-dependent) | improved with name-cache disabled |

Speech-dispatcher startup latency dominates perceived response time;
this branch doesn't address it. Spiel migration would.

## Files

- `ANALYSIS.md` — synthesis of the four-subsystem code analysis
- `ANALYSIS_CORE.md` — event flow + AT-SPI access layer deep dive
- `ANALYSIS_SPEECH.md` — speech + braille pipeline deep dive
- `ANALYSIS_WEB.md` — web/document handling deep dive
- `ANALYSIS_CONFIG.md` — configuration + script system deep dive

## Open work

Things that are known-broken or known-incomplete in this branch and
worth picking up next:

All of the originally documented open work has been addressed in
rounds 7+ except where noted:

- **`focus_manager.is_in_preferences_window()` broadened** in
  `9d0d24686`. Now treats any window in the same application as the
  prefs root as "in preferences," so descendant dialogs (Voice
  Defaults, Global Voice Settings) no longer slip past the guard.
  The root cause that the synth-revert fix routed around.
- **`AXObject._NAME_LL_CACHE_DISABLED`** — resolved in `2176c4952`,
  cache is back on. Held-key coalesce in `ccda9d591` was the actual
  fix for the wrong-window-title symptom.
- **Long-lived state cache** — primitive-bits version landed in
  `94e36c02d`. Stores `frozenset[int]` instead of the StateSet object;
  defunct objects can no longer cause a stale-pointer crash.
- **Cancel-revert UX** for the speech grid now also reverts
  rate/pitch/pitch-range/volume/family-* runtime overrides, not just
  the synthesizer combo (`0adbbc56b`).
- **Same runtime-override gap on other prefs combos** — the
  is_in_preferences_window fix above eliminates the root cause for
  all combos at once, so this is no longer a separate hazard.
- **Spiel migration in `src/orca/spiel.py` has TODOs at lines 467, 476,
  483** for utterance-offset mapping — *intentionally not attempted.*
  The author uses Voxin as the primary TTS and Spiel has no Voxin
  provider, so completing the migration would force a fallback to
  espeak-ng. Revisit when (if) a Spiel-Voxin provider ships.

## Next-on-deck (after the round-7 commits above)

The personal-perf-branch work has reached a plateau. Remaining items
are either:

- **True incremental Web Navigation Index.** The round-3
  background-rebuild patch (`740d84192`) gets us most of the way
  there — by the time the next press lands the cache is usually
  fresh. The remaining gap is the recompute itself: each rebuild
  still walks the whole tree. A true incremental index would mutate
  the cached list on `children-changed:add`/`:remove` instead of
  re-walking. Multi-day; harder than expected because new matches
  must be inserted in document order (requires path comparisons
  during insert). **Intentionally deferred:** the round-3
  background-rebuild already extracts the perceived-speed benefit;
  going to true incremental is a CPU-bandwidth win during idle, not
  a user-latency win, and the sort-order correctness risk is real.
- **Caret-order pre-computation** for arrow-key navigation through
  text (WEB analysis #2). **Intentionally deferred:** critical
  user-path; risk of regression outweighs the marginal gain.
- **Fake role / synthetic role cleanup** — *done in `5e463573d`*.
- **Pidgin/Smuxi scripts** — keeping per user preference.

Architectural cleanup (not bugs; long-term hygiene):
- ~~Developer-facing caching architecture doc~~ — done in `0df794228`
  (`docs/caching.md`).
- ~~Shared `DebouncedCallable` helper~~ — done in `62bbcc6b2`;
  four ad-hoc debounce patterns migrated in `971951e54`.
- ~~`Telemetry` D-Bus interface~~ — done in `9194c07c0` /
  `5e58eeb8f`.
- ~~Structural-nav 23-element-type registry + dispatcher~~ — done
  in Phase 2 (`991a25158` → `019db4cd7`).
- Keybinding tuple → dataclass refactor (the cleanup item flagged
  in the original holistic review). Still open. Lower priority now
  that Phase 2 proved the dataclass-table-driven pattern works
  cleanly.
- ~~"Stress mode" diagnostic flag~~ — done in `45d90d2d6`.

Strategic / not recommended:
- **Full Spiel migration** — would give 20-30% speech-latency
  reduction by eliminating speech-dispatcher, but Spiel has no Voxin
  provider, so the author would lose Voxin. Revisit when (if) one
  ships, or build a Spiel-Piper provider as a side project.
- **AT-SPI batch query API** (`get_attributes_batch` proposed to
  upstream Atspi). Multi-quarter upstream effort with single-digit
  realistic gain after Orca's already-aggressive client-side caching.

## License and upstream

GNOME Orca is licensed under LGPL 2.1+. This fork inherits that
license. None of the patches change file headers. The branch is hosted
publicly at <https://github.com/churst90/orca-perf> for ease of
collaboration; it is not a hard fork — re-syncing with upstream is
expected (see "Keeping up with upstream" above).
