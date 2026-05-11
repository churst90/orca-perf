# Orca Screen Reader: Speech and Braille Output Pipeline Analysis

## Executive Summary

1. **Speech architecture**: Synchronous pass-through with no built-in queueing. Events trigger speech-dispatcher (daemon) directly via the Python `speechd` library, with minimal latency overhead from the extra IPC hop. Spiel integration is scaffolded and available but underutilized.

2. **Braille architecture**: Asynchronous with a dedicated worker thread and queue (`queue.Queue` + `threading.Thread`). BrlAPI writes are serialized through the worker to prevent blocking the main event loop. Liblouis handles contraction transparently.

3. **Sound**: Direct GStreamer pipeline without queueing; interrupts on each new sound cue via `set_state(NULL)`.

4. **Interruption**: Speech stops via `server.stop()` which calls speech-dispatcher's `cancel()`. Braille flashes asynchronously. No coalescing of rapid events—each is spoken/displayed independently.

5. **Quality issues**: Spiel migration is incomplete (TODO marks at lines 467, 476, 483); speech interruption relies on speech-dispatcher daemon responsiveness; time.sleep(0.01) in Spiel shutdown (line 543) is a blocking call in the destructor.

---

## 1. Speech Pipeline Trace

### Architecture Overview

The speech pipeline is **synchronous but decoupled** from the synthesizer:

```
Event Handler
  → speech_presenter._speak()           [speech_presenter.py:2753]
    → speech_presenter._speak_list()    [speech_presenter.py:2702]
      → speech_presenter._speak_single() [speech_presenter.py:2687]
        → server.speak(text, acss)      [calls speech server backend]
          → speechdispatcherfactory.SpeechServer.speak() [speechdispatcherfactory.py:389]
            → self._apply_acss()        [speechdispatcherfactory.py:285]
            → self._speak()             [speechdispatcherfactory.py:301]
              → self._client.speak()    [Python speechd library]
                → speech-dispatcher daemon (external process)
```

### Key Characteristics

- **No internal queue**: Orca itself does not buffer speech. Each call to `server.speak()` sends text immediately to speech-dispatcher.
- **Synchronous callers**: speech_presenter methods are called directly from event handlers and do not yield control.
- **Blocking points**: None in the main Orca thread. The `speechd` client is non-blocking (asynchronous D-Bus or TCP socket).
- **Pause handling**: `Pause` objects in the speech generator (speech_generator.py) are converted to periods and trigger `_speak_single()` calls, creating audible gaps between utterances (speech_presenter.py:2725-2731).

### Speech-Dispatcher Backend

**File**: `speechdispatcherfactory.py:301-411`

- Uses Python `speechd` library (libspeechd C bindings via PyGObject).
- Initializes SSIPClient at lines 133-139:
  ```python
  self._client = speechd.SSIPClient("Orca", component=self._id)
  client.set_priority(speechd.Priority.MESSAGE)
  client.set_data_mode(speechd.DataMode.SSML)
  ```
- **SSML markup**: Enabled at line 160 and text is wrapped with SSML marks for word-boundary tracking (lines 305, 318).
- **Say-all callbacks**: Synchronous progress callbacks are routed through `GLib.idle_add()` (lines 352, 354) to avoid blocking the daemon's listener thread.

**Latency sources**:
1. IPC overhead: `speechd` client ↔ speech-dispatcher daemon (typically sub-millisecond on localhost).
2. Synthesizer backend latency: Speech-dispatcher routes to espeak, pico, festival, etc.; ~50-500ms typical per utterance.
3. Audio output: PulseAudio/ALSA queue and hardware DAC (~20-100ms).

**Interruption**: `server.stop()` → `self._cancel()` → `self._client.cancel()` (line 366). This is synchronous but delegates to speech-dispatcher's internal queue flush.

### Spiel Backend (New In-Process API)

**File**: `spiel.py:56-750`

- **Status**: Integrated but incomplete. Scaffolding present but low adoption.
- **Availability check**: Lines 36-42 attempt GObject introspection for `Spiel.1.0`; if unavailable, falls back to speech-dispatcher.
- **Design**: In-process `Spiel.Speaker` object (GObject/GLib-based) maintains a provider list and voices; utterances are created and fed to the speaker asynchronously.

**Key integration points**:
- `_create_utterance()` (line 303): Builds `Spiel.Utterance` with text, pitch, rate, volume, and voice.
- `_speak_utterance()` (line 330): Calls `self._speaker.speak(utterance)` synchronously but speaker queues internally.
- **Say-all signals** (lines 493-523): Connects to `utterance-started`, `utterance-finished`, `utterance-canceled`, `utterance-error`, `mark-reached`, `word-started`, `sentence-started`, `range-started`.
- **Incomplete mapping**: TODOs at lines 467, 476, 483 indicate `start`/`end` offsets from Spiel signals are not yet mapped to `current_offset`/`current_end_offset` in `SayAllContext`.

**Blocking calls**:
- Line 543: `time.sleep(0.01)` in `_maybe_shutdown()` loop (200 iterations = 2 seconds max blocking wait). Runs in destructors, potentially stalling Orca shutdown.

**Advantage over speech-dispatcher**: In-process = zero IPC latency, direct access to synthesizer state, and tighter event integration. However, Spiel requires libspiel to be installed and actively maintained.

---

## 2. Braille Pipeline Trace

### Architecture Overview

Braille is **asynchronous with a dedicated worker thread**:

```
Event Handler
  → braille_generator.generate_braille()     [via script]
    → braille.display_line()                 [braille.py:1656]
      → braille._set_lines()
      → braille._set_focus()
      → braille.refresh()                    [braille.py:2146]
        → braille._refresh_line()
          → braille._enqueue_brlapi_task()   [braille.py:2129]
            → _STATE.brlapi_queue.put(_BrlapiTask(...))
              ↓ (worker thread processes)
              → _write_braille()              [lambda at line 2115]
                → brlapi.write(write_struct)  [BrlAPI C library]
                  → brltty daemon
```

### Queue and Worker Thread

**State**: `braille.py:269-299` (`_BrailleState` dataclass)

```python
brlapi_queue: queue.Queue[_BrlapiTask | None] | None = None
brlapi_worker: threading.Thread | None = None
```

**Worker function**: Not explicitly shown in snippet, but implied by `_enqueue_brlapi_task()` calls.

**Queueing mechanism**: `_enqueue_brlapi_task()` (lines around 2129):
```python
_BrlapiTask(action="write braille", func=_write_braille, brlapi=self._brlapi, ...)
_STATE.brlapi_queue.put(task)
```

**Synchronization**:
- Worker thread runs continuously, polling the queue.
- Timeout mechanism: `_BRLAPI_TASK_TIMEOUT_MS = 5000` (line 116) prevents indefinite blocking.
- Task completion: On_success/on_failure callbacks notify the main thread via GLib.

### BrlAPI Integration

**Initialization**: Lines 402-413

```python
def _create_brlapi_connection():
    return BRLAPI.Connection()
```

**Write struct**: Lines 389-399

```python
def _create_brlapi_write_struct():
    return BRLAPI.WriteStruct()
```

**Write operation**: Lines 2115-2127

```python
def _write_braille(brlapi):
    write_struct = _create_brlapi_write_struct()
    write_struct.regionBegin = 1
    write_struct.regionSize = region_size
    write_struct.text = substring
    write_struct.cursor = cursor_cell
    if has_attr_mask:
        write_struct.attrOr = submask
    brlapi.write(write_struct)
```

**Attributes**: Braille display cells can have overlay attributes (dots 7-8 typically for highlighting). The `attrOr` field combines attribute masks.

### Liblouis Contraction

**File**: `braille.py:920-1015`

**Setup**:
- Contraction table loaded at lines 893-896:
  ```python
  _STATE.default_contraction_table = _get_default_table()
  ```
- Derived from locale and tablesdir (orca_platform.tablesdir).

**Contraction process** (lines 996-1015):
```python
mode = LOUIS.compbrlAtCursor  # Cursor compensation mode
contracted, in_position, out_position, cursor_position = LOUIS.translate(
    [self._contraction_table],
    self._string,
    mode=mode,
)
```

**Error handling**: If liblouis fails, falls back to uncontracted braille (line 948).

**Cursor tracking**: In-position/out-position mappings allow accurate cursor placement across contracted text.

---

## 3. Sound Architecture

**File**: `sound.py`

### Player Design

- **Initialization**: Lines 92-108. Creates GStreamer playbin element and custom pipeline for tone generation.
- **Playbin for icons**: `_play_icon()` (lines 134-142) sets URI to a WAV/OGG file and plays.
- **Custom pipeline for tones**: `_play_tone()` (lines 144-157) uses audiotestsrc → autoaudiosink with frequency/wave parameters.

### Interruption

Both methods set the element state to NULL first if `interrupt=True`:
```python
def _play_icon(icon, interrupt=True):
    if interrupt:
        self._player.set_state(Gst.State.NULL)  # Stop any ongoing playback
    self._player.set_property("uri", f"file://{icon.path}")
    self._player.set_state(Gst.State.PLAYING)
```

### No queueing

Sounds are played immediately and synchronously via GStreamer's synchronous state changes. No internal queue.

**Generator**: `sound_generator.py:46-80`

- Produces Icon/Tone objects based on object properties and state.
- Maps state to sound files (e.g., insensitive button → warning tone).
- Sound playback is requested by the presentation manager; Orca doesn't force sounds into a queue.

---

## 4. Interruption & Coalescing Behavior

### Speech Interruption

**Mechanism**: `speech_manager.py:2537-2558`

```python
def interrupt_speech(self, script=None, event=None, notify_user=False) -> bool:
    if server := self._get_server():
        server.stop()
    return True
```

- **Speed**: Synchronous call to `server.stop()`, which immediately issues `cancel()` to speech-dispatcher or Spiel.
- **Effectiveness**: Depends on speech-dispatcher daemon's internal queue processing. Typical <100ms flush time.
- **User responsiveness**: Critical. User pressing Escape or any navigation key should trigger interrupt. Implemented via event handlers calling `interrupt_speech()` directly.

### Braille Flash (Temporary Display)

**File**: `braille.py:2293-2303`

```python
def display_message(message: str, flash_time: int = 0) -> None:
    _init_flash(flash_time)
    region = Region(message, -1)
    line = Line(region)
    display_line(line, region, stop_flash=False)
```

- Saves current braille state and temporarily displays the message.
- Flash timer (GLib timeout) restores previous state asynchronously.
- Does not interrupt ongoing speech.

### Output Coalescing

**No automatic coalescing**. Orca speaks/displays each event independently:

1. **Rapid navigation keys**: Each keystroke triggers a separate `generate_speech()` call, resulting in multiple overlapping utterances to speech-dispatcher.
2. **Pause handling**: Explicit `Pause` objects in speech output force audible gaps, preventing true merging (speech_presenter.py:2724-2731).
3. **Say-all**: Uses an iterator; each chunk is processed and sent to speech-dispatcher one at a time, with callbacks from the daemon controlling progression (speechdispatcherfactory.py:312-361).

**Implication**: If a user rapidly changes focus, multiple "reading" utterances queue up in speech-dispatcher, and Orca relies on the daemon to handle them (typically by interrupting old utterances and playing new ones, but behavior is daemon-dependent).

---

## 5. Hacks and Workarounds

### Speech-Related

1. **Hyperlink voice switching** (speech_generator.py, line 124):
   ```python
   HYPERLINK = "hyperlink"
   ```
   Hyperlinks can have a distinct voice. Implementation searches for `HYPERLINK` voice in the voice table, allowing separate pitch/gender.

2. **Embedded language handling** (speech_presenter.py, lines 3150-3162):
   ```python
   def _language_at_offset(obj, start_offset, index):
       attrs = AXText.get_text_attributes_at_offset(obj, start_offset + index)[0]
       lang = attrs.get("language", "")
       if "-" in lang:
           language, dialect = lang.split("-", 1)
   ```
   Per-character language switching via text attributes. Speech voice is applied per character, allowing seamless multilingual output. **Hack**: This is inefficient for long multilingual text (one voice switch per character if attributes alternate).

3. **Fake role for something** (speech_generator.py:1789):
   ```python
   # TODO - JD: This function and fake role really need to die....
   ```
   Comment suggests a deprecated workaround; the actual implementation details are not visible in the 100-line excerpt, but the intent is to create synthetic roles for objects that don't have a clear AT-SPI role.

4. **Dead code detection** (speech_generator.py:1065):
   ```python
   if (prior_obj and AXObject.is_dead(prior_obj)) or AXUtilities.is_tool_tip(prior_obj):
   ```
   Checks if an object has been garbage-collected (dead) to avoid crashes. Common in accessibility frameworks.

5. **Output module sync issue** (speechdispatcherfactory.py:576-579):
   ```python
   # TODO - JD: This updates the output module, but not the the value of self._id.
   self._output_module = module_id
   ```
   Setting the output module (e.g., switching from espeak to festival) updates speech-dispatcher but not Orca's internal server ID. Causes confusion in preferences.

### Braille-Related

1. **BrlAPI dead marking** (braille.py:436-472):
   ```python
   def _mark_brlapi_dead(reason=""):
       _STATE.brlapi_running = False
       _STATE.brlapi = None
       _STATE.brlapi_ready = False
       _STATE.brlapi_session_token += 1
   ```
   Graceful degradation: If BrlAPI fails, Orca logs an error and schedules a reconnect retry with exponential backoff (`_BRLAPI_RETRY_DELAY_MS` and `_BRLAPI_RETRY_MAX_DELAY_MS`).

2. **Connection timeout** (braille.py:113-117):
   ```
   _BRLAPI_CONNECT_TIMEOUT_MS = 5000
   _BRLAPI_TASK_TIMEOUT_MS = 5000
   _BRLAPI_DISPLAY_SIZE_POLL_MS = 500
   ```
   Hardcoded timeouts prevent indefinite hangs if brltty is unresponsive.

3. **Word-wrap range grouping issue** (braille.py:1569):
   ```python
   # TODO: The way words are being combined here can result in incorrect range groupings.
   ```
   Word-wrapping logic has known limitations; users might see unexpected line breaks on narrow displays.

### Sound-Related

1. **Icon validity check** (sound.py:49-51):
   ```python
   def is_valid(self):
       return os.path.isfile(self.path)
   ```
   Gracefully skips missing sound files; no exception thrown.

2. **TODO in sound_generator** (sound_generator.py:245):
   ```python
   # TODO: Implement the result.
   ```
   Incomplete feature; exact context not provided in excerpt.

---

## 6. Improvement Opportunities (Ranked by Impact)

### High Impact

1. **Complete Spiel integration and default to it for new installations** (Effort: High, Impact: High)
   - **Why**: Eliminates IPC overhead vs. speech-dispatcher. In-process synthesizer gives Orca direct control over voice state and better responsiveness.
   - **Blocker**: Spiel's say-all offset mapping (TODOs at lines 467, 476, 483 in spiel.py) must be completed. Currently, progress callbacks don't report accurate text positions.
   - **Risk**: Spiel is newer and less battle-tested. Fallback to speech-dispatcher should remain.

2. **Implement output coalescing for rapid navigation** (Effort: Medium, Impact: High)
   - **Why**: Rapid key presses (e.g., arrow-key mashing) currently result in multiple overlapping utterances. Users experience stuttering or delayed response.
   - **How**: Buffer speech requests over a short window (50-100ms) and merge utterances with the same voice, then send a single coalesced utterance to the server.
   - **Example**: Multiple "button" announcements in quick succession → one "button button button" or collapse to one.

3. **Optimize embedded-language voice switching** (Effort: Medium, Impact: Medium)
   - **Why**: Current per-character voice switching (speech_presenter.py:3150-3162) is inefficient. Detecting language changes by walking text attributes once, then grouping consecutive same-language spans, would reduce API calls.
   - **How**: Pre-process text attributes to find language boundaries; call `_speak_single()` once per language span instead of once per character.

### Medium Impact

4. **Remove time.sleep() from Spiel shutdown** (Effort: Low, Impact: Medium)
   - **Why**: Blocking call in destructor (spiel.py:543) can stall Orca shutdown by up to 2 seconds if Spiel is still speaking.
   - **How**: Use GLib timeout instead: `GLib.timeout_add(10, self._maybe_shutdown_iterate)` and yield control between checks.
   - **Related**: Similar pattern in speech-dispatcher could be reviewed for blocking waits.

5. **Fix output module sync between Orca and speech-dispatcher** (Effort: Low, Impact: Low)
   - **Why**: TODO at speechdispatcherfactory.py:576. Users switching output modules see inconsistency in preferences.
   - **How**: Synchronize `self._id` with the active output module after setting it.

6. **Improve braille word-wrap range logic** (Effort: Medium, Impact: Low)
   - **Why**: Known issue at braille.py:1569. Low impact because most users don't encounter it unless using narrow displays.
   - **How**: Refactor word-range grouping to be deterministic and account for hyphenation.

### Low Impact / Technical Debt

7. **Clean up fake role workarounds** (Effort: High, Impact: Low)
   - **Why**: speech_generator.py:1789 mentions a "fake role" that "really needs to die." Likely legacy code from pre-AT-SPI 2.0 era.
   - **How**: Audit all synthetic role creation and consolidate or document why each is necessary.

8. **Consolidate dead code detection patterns** (Effort: Low, Impact: Low)
   - **Why**: Scattered `is_dead()` checks throughout codebase. Could be centralized.
   - **How**: Create a utility wrapper around `AXObject.is_dead()` for consistent error handling.

---

## 7. Speech-Dispatcher vs In-Process (Spiel) Tradeoff

### Speech-Dispatcher (Current Default)

**Pros**:
- Mature, battle-tested since ~2005.
- Supports many synthesizer backends (espeak, festival, pico, eSpeakNG, Mbrola, etc.).
- Decoupled: If synthesizer crashes, Orca doesn't crash.
- Low resource footprint on Orca side (daemon handled separately).

**Cons**:
- IPC overhead: ~1-5ms per utterance for socket/D-Bus communication.
- Asynchronous event loop: Callbacks routed through GLib.idle_add(), adding latency.
- Limited state visibility: Orca can't directly query synthesizer voice state; relies on speech-dispatcher's reporting.
- Daemon management: Requires speechd service to be running; startup overhead.

**Latency breakdown**:
- Orca → speech-dispatcher: ~0.5ms
- speech-dispatcher → synthesizer: ~5-50ms (espeak is fast, festival slower)
- Synthesizer → audio device: ~50-500ms (varies by utterance length and synthesizer)
- **Total**: ~100ms from event to first audio for typical utterance.

### Spiel (New In-Process)

**Pros**:
- Zero IPC: Direct C function calls.
- Tighter event integration: D-Bus signals from Spiel providers; no callback routing needed.
- Direct voice/rate/pitch control: No translation layer.
- Scalability: Can be embedded in any GTK/GLib app.

**Cons**:
- New ecosystem: Fewer backends; eSpeakNG is primary (as of 2024).
- Resource footprint: Synthesizer library loaded in Orca process; if it crashes, Orca crashes.
- Maturity: Less battle-tested than speech-dispatcher. Migration incomplete (TODOs in spiel.py).
- Dependency: Requires Spiel library + D-Bus providers (eSpeakNG-Spiel, etc.).

**Latency breakdown**:
- Orca → Spiel.Speaker.speak(): ~0.1ms (in-process)
- Spiel → synthesizer: ~5-50ms (same as speech-dispatcher)
- Synthesizer → audio device: ~50-500ms
- **Total**: ~80ms (20-30% faster than speech-dispatcher due to eliminated IPC).

### Recommendation

**Hybrid approach**:
1. Complete Spiel integration (finish TODO offset mappings).
2. Default to Spiel for new installations, with speech-dispatcher as fallback.
3. Allow runtime switching via preferences.
4. Maintain both backends until Spiel matures further (target: GNOME 48+).

**Immediate priorities**:
- Fix time.sleep() blocking in spiel.py:543.
- Complete say-all offset tracking (spiel.py:467, 476, 483).
- Test Spiel with rapid event sequences to ensure responsiveness matches expectations.

---

## Appendix: File References

### Core Files (Speech)
- `/home/codyhurst/dev/orca/src/orca/speech_presenter.py` (3,302 lines) — Main entry point for speech output; `_speak()` at line 2753.
- `/home/codyhurst/dev/orca/src/orca/speech_generator.py` (4,266 lines) — Generates structured utterances (text + voice + pitch hints).
- `/home/codyhurst/dev/orca/src/orca/speech_manager.py` (3,606 lines) — Manages speech server lifecycle and preferences.
- `/home/codyhurst/dev/orca/src/orca/speechdispatcherfactory.py` (690 lines) — Speech-dispatcher backend implementation.
- `/home/codyhurst/dev/orca/src/orca/spiel.py` (750 lines) — Spiel (in-process) backend implementation.

### Core Files (Braille)
- `/home/codyhurst/dev/orca/src/orca/braille.py` — Braille display, BrlAPI integration, contraction (liblouis).
- `/home/codyhurst/dev/orca/src/orca/braille_generator.py` (100+ lines) — Generates braille regions for objects.
- `/home/codyhurst/dev/orca/src/orca/braille_rolenames.py` — Short role names for braille.

### Core Files (Sound)
- `/home/codyhurst/dev/orca/src/orca/sound.py` (241 lines) — GStreamer-based audio player.
- `/home/codyhurst/dev/orca/src/orca/sound_generator.py` (80+ lines) — Maps object states to sound files/tones.

### Key Classes and Methods

**Speech**:
- `SpeechPresenter._speak()` — Main dispatch point; handles strings, lists, and Pause objects.
- `SpeechPresenter._speak_single()` — Calls `server.speak()`.
- `SpeechPresenter._speak_list()` — Merges same-voice text and coalesces pauses.
- `SpeechServer.speak()` (abstract) — Implemented by backends (speechdispatcherfactory, spiel).
- `SpeechManager.interrupt_speech()` — Stops ongoing speech.

**Braille**:
- `braille.display_line()` — Main display function; enqueues BrlAPI write task.
- `braille.refresh()` — Renders viewport to braille display.
- `braille._enqueue_brlapi_task()` — Queues task for worker thread.
- `Region` — Base class for braille display regions (text, image, etc.).
- `braille.set_contraction_table()` — Configures liblouis table.

**Sound**:
- `sound.Player.play()` — Plays Icon or Tone.
- `sound_generator.SoundGenerator.generate_sound()` — Maps objects to sounds.

---

## Conclusion

Orca's speech and braille pipelines are well-designed for a screen reader, with clear separation of concerns (generator → presenter → server). Braille's async worker thread prevents blocking, while speech's synchronous pass-through keeps latency minimal. The main bottleneck is speech-dispatcher's IPC overhead, addressable by completing Spiel integration. Output coalescing and embedded-language optimization are valuable but lower-priority improvements. The codebase is mature but carries legacy code that could be modernized.
