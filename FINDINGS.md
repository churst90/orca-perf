# Orca / AT-SPI / speech-dispatcher findings

Sibling to `ANALYSIS.md` and the per-subsystem `ANALYSIS_*.md` docs.
Captures the holistic Orca review and cross-stack accessibility seam
audit done on the `perf/atspi-event-cache` branch on 2026-05-18, after
the round-3 perf commits and the `c1d969e25` upstream merge landed.

Items already on the `BRANCH_INFO.md` "Open work" or
"Next-on-deck" lists are not duplicated here.

---

## Part 1 — Holistic Orca findings (filtered)

Items below were surfaced by a targeted walk of subsystems
underserved by the existing `ANALYSIS_*.md` docs. Each one is
either a verified-likely bug, a tractable correctness gap, or a
clean candidate for upstream submission.

### A. Worth verifying then fixing on this branch

**A1. Braille worker race on queue/worker state checks** — *NOT A BUG
after audit.* Closer reading of `braille.py:430-433` and `569-577`
shows the worker thread never touches `_STATE.*`. The worker operates
on a local `task_queue` reference passed to `_brlapi_worker_loop`,
calls `task.func(task.brlapi)`, and defers all state reactions to the
GLib main thread via `GLib.idle_add`. All `_STATE.brlapi_*` writes
happen on main. The agent overcalled this finding. Skipped.

**A2. `HUNG_OBJECTS` dict access without lock** — *DONE in
`40c404eff`.* Take `AXObject._lock` around all reads, writes, and the
prune iteration. Switched `check_hung()` to `.get()` with sentinel to
avoid the membership-then-read race even within the locked section.
Inherited code from upstream merge `c1d969e25`; this commit is a good
candidate to send back to her once the synth-revert MR lands.

**A3. D-Bus surface lacks rate limiting / call coalescing** — *DONE
as diagnostic in `5d9954ebe`.* On reflection, hard-throttling would
break legitimate automation and silent setter coalescing would change
call semantics. Instead added a sliding-window call-rate monitor that
emits a throttled `WARNING` log when a method exceeds 50 calls/sec.
Never drops, delays, or coalesces. If the warning fires in real use,
we'll have a concrete signal to add real throttling; until then, no
behavior change.

**A4. BrlAPI health probe is missing** — *DONE in `bf34212fd`.* Added
a 10-second periodic NoOp probe that enqueues a cheap `displaySize`
read on the existing task queue. Re-uses the existing in-flight
timeout (5s) and `_mark_brlapi_dead` path. A hung brltty is now
discovered during idle time instead of stalling the next real write.

### B. Worth fixing eventually, lower priority

**B1. Sound player GStreamer bus watches not unregistered.**
`sound.py:177-178, 183` register signal watches in `__init__` but
no destructor unregisters them. Probably masked by Python GC on
normal shutdown; matters on crash or force-kill. **CLEAN, SMALL.**

**B2. Input event manager `_paused` flag not synchronized.**
`input_event_manager.py:64-69`. Technically safe on CPython due to
GIL but the intent is unclear and the design is fragile across
implementations. **CLEAN, SMALL.**

**B3. Profile rename non-atomic.** `gsettings_registry.py:549-572`
does copy-then-reset. Interrupting between the two leaves dconf
containing both old and new profile keys. **BUG, SMALL.**

**B4. Speechd reconnect-on-socket-close.** Orca's reconnect logic
fires lazily on the next speak attempt. If speechd is restarted
mid-session, the gap between socket close and next speak is dead
air. **CLEAN, SMALL.** Fix shape: socket-level epoll/EOF detection
on the SSIP client, proactive reconnect.

### C. Already known / on BRANCH_INFO.md "Open work" list

For completeness — not new findings, just noting they're tracked:
- Preferences-window guard (`focus_manager.is_in_preferences_window`)
  too narrow; doesn't catch child dialogs.
- Voice-family / language / rate / pitch / volume runtime overrides
  don't get reverted on Cancel (only synth combo does, per the
  `e05d8868d` fix).
- StateSet long-lived caching needs primitive-bits approach to be
  safe.
- Speech_generator fake-role cleanup (done in `5e463573d`).

### D. Test coverage gap

The threading-sensitive subsystems (braille worker, event-scope cache
under concurrent dispatch, hung-object pruning, structural-nav
background rebuild) have no unit tests. Integration tests cover the
happy path but not contention. **TEST, MEDIUM.** Adding good
concurrency tests is hard; useful but not blocking.

---

## Part 2 — Cross-stack accessibility seams

Where Orca, AT-SPI, speech-dispatcher, and the toolkit a11y bindings
meet, and where the cracks show. Maintained by different teams who
don't always coordinate, so seam bugs persist across releases.

### The end-to-end flow

```
App (GTK/Qt/Chromium/Electron) emits a11y info
  → ATK API (GTK3) or native AT-SPI2 backend (GTK4) or QtA11y (Qt)
    or AXIs / AXPlatformNode (Chromium)
  → at-spi2-atk bridge (GTK3 only): translates ATK calls into
    AT-SPI2 D-Bus messages
  → at-spi2-registryd: brokers between apps and assistive techs
  → libatspi (in Orca's process): caches accessibles, dispatches
    events
  → Orca event_manager: priority queue, script dispatch
  → script: generates speech / braille
  → speech-dispatcher daemon (separate process)
  → speech-dispatcher module (subprocess of daemon)
  → audio output (Pulse / PipeWire / ALSA / OSS)

Parallel braille flow:
  → braille_generator → braille.py worker thread → BrlAPI
  → brltty daemon → display hardware
```

### Seam issues

**S1. ATK → AT-SPI2 bridge is the biggest legacy hazard.**
`at-spi2-atk` translates ATK calls into D-Bus events. ATK is being
deprecated; GTK4 dropped it for a native AT-SPI2 backend
(libgtk-newatk). The bridge has known property-change timing issues
— name changes emit at different points than role changes, for
instance. Orca has workarounds scattered through `ax_object.py`
(`LAST_KNOWN_NAME` tracking is partly to detect bridge-introduced
lies). **Fix shape:** long-term, toolkit-side migration to native
AT-SPI2. Short-term, document Orca's workarounds clearly so future
refactors don't accidentally remove them.

**S2. at-spi2-registryd is a single point of contention.** All
events flow through one daemon. Under load (rapid focus, Chromium
streaming) the registry is the bottleneck and has no backpressure
mechanism — if Orca can't drain fast enough, the registry buffers
indefinitely. **Fix shape:** registry-side changes (libatspi
maintainers). Or Orca could expose queue depth and ask the registry
to drop low-priority events. Multi-team coord.

**S3. Three independent cache layers, no coherence protocol.** Apps
cache accessibility state internally; libatspi caches accessibles
client-side; Orca has its own caches (`AXObject.LONG_LIVED_*`,
`AXUtilitiesEvent.LAST_KNOWN_*`, structural-nav match cache). Each
invalidates independently. When one layer thinks an object is alive
and another thinks it's dead, defunct crashes follow. **Fix shape:**
libatspi should publish cache events; Orca subscribes and invalidates
in lockstep. Doesn't exist today; would be an Atspi-2 extension.

**S4. SSIP has no cancel acknowledgment.** When Orca sends `STOP`,
speechd ACKs the command but the in-flight utterance may still be
in the audio backend buffer. Orca has no way to know when speech
*actually* stops. Rapid arrow nav (cancel-then-speak pattern) is
racy by design — the user occasionally hears the tail of the prior
utterance under the start of the new one. **Fix shape:** SSIP
extension for stop+ack with audio-flush confirmation. Needs
Brailcom buy-in (Samuel Thibault et al).

**S5. BrlAPI has timeout but no liveness probe.** See A4 above.
Orca-side; tractable.

**S6. speechd restart orphans Orca's connection.** `systemctl
restart speech-dispatcher` drops the SSIP socket. Orca reconnects
lazily on next speak. Gap = dead air. **Fix shape:** see B4.
Orca-side; tractable.

**S7. No system-wide a11y tracing.** Debugging a multi-component
issue requires `orca --debug-file`, speechd `LogLevel 5`, brltty
log, and toolkit-side AT-SPI logging, none correlated. **Fix
shape:** a centralized journald accessibility namespace, or an
`a11y-trace` wrapper command. New tool; multi-team coord.

**S8. Toolkit a11y quality varies wildly with no shared conformance
test.** GTK3+ATK is solid-ish, GTK4 native has gaps, Qt6 a11y is
incomplete, Chromium has documented quirks (upstream commit
`23aaa0d86` is one such workaround). No shared test runner across
toolkits. **Fix shape:** an `at-spi2-conformance` suite. Long-term;
the building blocks (`pyatspi`, Orca's integration tests) exist.

**S9. speechd module capabilities are inconsistently advertised.**
Some modules support index marks (espeak-ng), some don't (`sd_generic`
with most engines), some partially (voxin). Orca queries at connect
time but doesn't always degrade gracefully — say-all on a no-index
module reports wrong positions. **Fix shape:** mandate a
capabilities reply format in speechd's module protocol; have Orca
require it. Partly done speechd-side already.

**S10. `org.a11y.Screen Reader` D-Bus interface is undocumented
for third parties.** Magnifier apps, alternative braille drivers,
alt speech frontends all want "is the screen reader running?"
There's a name on the bus but no published contract. **Fix shape:**
write the contract. Smaller than it sounds.

---

## What to do with these findings

In rough order of effort and impact:

1. **A2 (HUNG_OBJECTS lock)** — half-hour fix, regression we just
   inherited, clean upstream PR candidate.
2. **A1 (braille worker race)** — audit then fix, ~half-day.
3. **A3 (D-Bus rate limit)** — small contained patch.
4. **A4 / S5 (BrlAPI health probe)** — small contained patch.
5. **B4 / S6 (speechd reconnect on socket close)** — small contained
   patch.
6. **D (test coverage)** — adds tests for our existing concurrency
   work and the new fixes. Medium.

The seam issues (S1, S2, S3, S4, S7, S8, S9, S10) are multi-team
efforts that need raising with the right maintainers separately. Not
branch material — they're conversation starters.
