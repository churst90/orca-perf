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

### B. Round-5 outcomes

**B1. Sound player GStreamer bus watches not unregistered** — *DONE
in `282fa31ed`.* Player.init() called `bus.add_signal_watch()` and
`bus.connect("message", ...)` on both the playbin and the custom
tone pipeline, but shutdown() only set the elements to NULL state.
Track bus references and handler ids; disconnect handlers and call
`remove_signal_watch()` in shutdown(); drop element refs so
GStreamer can finalize.

**B2. Input event manager `_paused` flag not synchronized** — *NOT
A BUG after audit.* `pause_key_watcher` is only called from
`event_manager.pause_queuing` (main thread), and
`process_keyboard_event` runs via GObject `key-pressed`/`key-released`
signal dispatch (also main thread). Single-threaded access; no race
possible. The agent's "fragile across implementations" hedge was the
warning sign here. Skipped.

**B3. Profile rename non-atomic** — *DONE in `9b0b9e039`.*
`rename_profile()` was a three-phase sequence (copy each schema,
write metadata on new, reset old) with no error handling between
phases. Wrap copy + metadata in try/except; on failure, reset the
partial new profile so the user keeps the old one, then re-raise.
Only reset the old after both phases succeed. Doesn't make dconf
transactional but auto-cleans the common in-process failure modes.

**B4. Speechd reconnect-on-socket-close** — *DONE in `8be6260cb`.*
Used the same probe pattern as the BrlAPI fix in `bf34212fd`. A
30-second cheap SSIP round-trip (`get_output_module`) goes through
the existing `_send_command` path, which already turns
`SSIPCommunicationError` into a `reset()` + `_init()` cycle. So if
speech-dispatcher restarts while Orca is idle, the probe discovers
it within 30 seconds and reconnects before the user's next speak.

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

---

## Part 3 — Subsystem audit (round 6)

Targeted pass through subsystems not covered by ANALYSIS_*.md or
earlier rounds. Conclusion up front: **nothing here is urgent.** A
few minor inconsistencies and one real-but-low-impact UX bug; bulk
of the surface is clean and single-threaded.

### Findings

**P1. `notification_presenter._current_index` is not adjusted when
the queue is truncated.** `notification_presenter.py:108-110`. When
a new notification arrives and the list is at `_max_size` (55), the
oldest entry is dropped. If the user is browsing at a positive
index (`_current_index == 5`, say), their pointer now refers to a
different message after the truncation. Symptom is "previous
notification" jumping unexpectedly. Rare (requires browsing at
exactly the moment the list hits cap), low impact, deterministic.
**BUG, SMALL.** Fix shape: subtract `to_remove` from
`_current_index` when positive, clamp to 0.

**P2. `notification_presenter` `IndexError` except clauses mask the
real condition.** Lines 205 and 252 catch `IndexError` defensively
after a `not self._notifications` guard. The only path to the
`except` is a stale `_current_index`. Better to clamp the index up
front (related to P1) and remove the defensive handler. **CLEAN,
SMALL.**

**P3. `notification_presenter` uses `list` slice-append for a
bounded queue.** Lines 108-110 do an O(n) slice-copy on every
append once the list reaches cap. `collections.deque(maxlen=55)`
is the idiomatic answer; O(1) append + natural truncation, and
trivially preserves a single positive index since deque indexing
matches list semantics. **CLEAN, SMALL.** Same change addresses
P1/P2 in passing.

**P4. `mouse_review` schedules a 50ms `GLib.timeout_add` per
event.** `mouse_review.py:851, 877`. Each AT-SPI mouse event
schedules a new timer; the timer source is never stored or
cancelled. The processing logic (`_process_event`) intentionally
discards events when the queue still has pending items behind it,
so behavior is correct (coalesces to the latest), but you can have
hundreds of pending GLib timer sources during rapid mouse movement.
Wasted machinery, not a leak (sources auto-clean on fire). **CLEAN,
SMALL.** Fix shape: track a single `_pending_timer_id`, skip
scheduling if one is already armed — same pattern we used for the
structural-nav debounce.

**P5. `flat_review_presenter._listener` has a leftover TODO.**
`flat_review_presenter.py:109` — `# TODO - JD: Implement support
to invalidate individual objects.` The current behavior drops the
flat-review context wholesale on changes. Per-object invalidation
would let the user keep their position when an unrelated part of
the window updates. Touches the same event-scope infrastructure we
built. **CLEAN, MEDIUM.** Worth flagging to Joanmarie if she opens
a conversation about caching.

**P6. `flat_review_presenter.say_all` doesn't preserve the user's
location.** Line 1411 captures `location` but the function discards
it without restoring after speaking. After `say_all` the user is at
the END of the window content; if they were reviewing mid-document
they lose their place. Probably intentional behavior carried over
from older code, but worth confirming. **POSSIBLE BUG, SMALL.**

**P7. `phonnames.py` builds its dict at module import without
exception handling.** If a translation has a malformed entry
(missing colon, extra colon), the import raises and Orca fails to
start. Defensive `try/except` plus a fallback to the English NATO
alphabet would harden against bad translation files. **CLEAN,
SMALL.** Low impact (would be caught in translation review), but
trivial to harden.

### What does not need work

- **`bypass_mode_manager.py`** — 106 lines, single-threaded boolean
  toggle. Clean.
- **`table_navigator.py`** (982 lines) and **`caret_navigator.py`**
  (1003 lines) — mostly state-tracking around `_last_input_event`.
  No threading, no caches that could go stale, no exception
  swallowing. Repeated `self._last_input_event = event` at every
  command method is verbose but not wrong.
- **`flat_review_presenter.py`** main flow — uses the right single
  `_idle_id` pattern for event coalescing. Properly removes the
  source on quit.

### Verdict

Six small findings; P1 is the only real bug (and very rare). P3
collapses P1/P2 into a single deque conversion. P4 is a clean-up
that mirrors a pattern we already use elsewhere. Total work:
maybe half a day if you want to land all of them. None worth
prioritizing over the larger items still pending (multi-day web
index, `is_in_preferences_window` broadening).
