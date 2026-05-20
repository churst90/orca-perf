# speechdispatcherfactory: `get_info()` reports the live output module

Closes #NNN. <!-- replace with issue number after filing -->

## What this changes

Adds an override of `get_info()` to the speech-dispatcher subclass of
`SpeechServer`. The override prefers `self._output_module` (the
"live" module set via `set_output_module()`) over `self._id` (the
construction-time identifier), falling back to `self._id` when
`_output_module` is `None`.

`self._id` is intentionally left untouched so that the
`SpeechServer._active_servers` registry — which is keyed on `self._id`
— continues to work, including during shutdown.

The comment block on `set_output_module()` is updated from a `TODO` to
a "why" explanation describing the split responsibility between
`self._id` (registry key) and `self._output_module` (live module).

## Why

Two TODO comments in the current tree, both by @joanmarie, document
the out-of-sync behavior:

- `src/orca/speechdispatcherfactory.py:576-578` — "TODO - JD: This
  updates the output module, but not the the value of self._id. That
  might be desired (e.g. self._id impacts what is shown in Orca
  preferences), but it can be confusing."
- `src/orca/speech_manager.py:2431-2432` — "TODO - JD: The result is
  not in sync with the current output module. Should it be?"

This MR answers "Should it be?" with "yes" and implements the sync via
the smallest possible change: a 13-line override that does not touch
the call path or the registry semantics.

The second TODO at `speech_manager.py:2432` ("why do we need
`get_current_speech_server_info()` at all?") is a separate refactor
question and is not addressed here.

## How it was developed

Per the discipline change adopted after issues #711 and #712: the
patch was developed on a branch forked directly from `upstream/main`
HEAD `b20d990c6`, not cherry-picked from any downstream tree. Every
function named in the analysis was read from `upstream/main` before
the patch was written.

- Branch: `upstream-fix/speechdispatcher-get-info-live-module`
- Base: `b20d990c6`
- `git apply --check` against `upstream/main` passes.
- `tests/unit_tests/test_speech_presenter.py` — 58/58 pass on the
  upstream-only tree.
- `tests/unit_tests/test_ax_object.py` +
  `tests/unit_tests/test_ax_utilities.py` — 298/298 pass on the
  upstream-only tree (sanity check that the patch does not break
  unrelated paths).

## How to verify

Requires speech-dispatcher with at least two output modules installed.

1. Apply the patch to a checkout of `upstream/main`.
2. Build and run Orca.
3. Open Orca preferences (`Orca+Space`) → **Voice** tab.
4. Note the value in the **Speech synthesizer** field.
5. Pick a different synthesizer from the dropdown, click **Apply**.
6. The **Speech synthesizer** field now reports the newly-selected
   module name. Without the patch, it continues to report the original
   module from process start until Orca is restarted.

## Behavior in the no-change case

`self._output_module` is initialized to `None` in `__init__()` and
stays `None` until `set_output_module()` is called. The override
therefore returns `[self._SERVER_NAMES.get(self._id, self._id),
self._id]` — byte-identical to the base implementation — for any
server whose output module has never been changed. Existing tests and
behavior are preserved.

## Diff size

```
 src/orca/speechdispatcherfactory.py | 19 ++++++++++++++++---
 1 file changed, 16 insertions(+), 3 deletions(-)
```

## Not in scope

- The second JD TODO at `speech_manager.py:2432`
  ("why do we need this method?") — separate API question.
- The Spiel backend (`src/orca/spiel.py`) — different active-server
  model; this MR only touches the speech-dispatcher backend.
- `self._id` mutation policy — kept as-is so `_active_servers`
  bookkeeping stays correct.
