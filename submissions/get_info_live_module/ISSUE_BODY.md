# Speech-dispatcher `get_info()` reports stale server id after `set_output_module()`

## Summary

`SpeechServer.get_info()` in the speech-dispatcher backend returns the
construction-time `self._id` rather than the live output module. After
the user changes the synthesizer in Orca preferences (which calls
`set_output_module(new_module)`), any subsequent call to `get_info()`
still reports the original id from process start, so the preferences UI
and any other consumer sees stale information.

Two TODO comments already in the tree, both by JD, document this exact
state:

```python
# src/orca/speechdispatcherfactory.py, around line 573
def set_output_module(self, module_id: str) -> None:
    """Set the speech output module to the specified provider."""

    # TODO - JD: This updates the output module, but not the the value of self._id.
    # That might be desired (e.g. self._id impacts what is shown in Orca preferences),
    # but it can be confusing.
    self._output_module = module_id
    ...
```

```python
# src/orca/speech_manager.py, around line 2423
def get_current_speech_server_info(self) -> tuple[str, str]:
    """Returns the name and ID of the current speech server."""

    # TODO - JD: The result is not in sync with the current output module. Should it be?
    # TODO - JD: The only caller is the preferences dialog. And the useful functionality is in
    # the methods to get (and set) the output module. So why exactly do we need this?
    server = self._get_server()
    if server is None:
        return ("", "")

    server_name, server_id = server.get_info()
    ...
```

This issue is filed in response to the first TODO ("Should it be?") with
"yes" — and proposes a minimal fix. The second TODO ("Why do we need
this method?") is a separate API-cleanup question and is intentionally
out of scope here.

## Steps to reproduce on `upstream/main`

Tested against `upstream/main` commit `b20d990c6`. Requires
speech-dispatcher with at least two output modules installed (the
default `espeak-ng` plus any second module, e.g. `piper` or `voxin`).

1. Start Orca: `orca --replace`.
2. Open the Orca preferences (`Orca+Space`).
3. Go to the **Voice** tab.
4. Note the value in the **Speech synthesizer** field — for a freshly
   started Orca with a default speech server, this matches the module
   that speech-dispatcher started with.
5. Open the **Speech synthesizer** dropdown and select a different
   module.
6. Click **Apply**.
7. Without closing the dialog, look at the **Speech synthesizer** field
   again, or close and re-open the dialog.

**Expected:** the field reports the synthesizer the user just selected.

**Actual:** the field continues to report the synthesizer from process
start, until Orca is restarted.

## Root cause

The base `SpeechServer.get_info()` implementation
(`upstream/main:src/orca/speechserver.py:241-244`) returns `self._id`:

```python
def get_info(self) -> list[str]:
    """Returns [name, id] of the current speech server."""

    return [self._SERVER_NAMES.get(self._id, self._id), self._id]
```

`self._id` is set once in `SpeechServer.__init__()` and is intentionally
never mutated, because it is the key into `SpeechServer._active_servers`
(`upstream/main:src/orca/speechdispatcherfactory.py:129`) and changing
it would break shutdown cleanup.

`set_output_module()`
(`upstream/main:src/orca/speechdispatcherfactory.py:573-584`) writes the
new module name into `self._output_module`, but the speech-dispatcher
subclass does not override `get_info()`, so reads continue to come from
`self._id`. The two pieces of state drift apart from the first call to
`set_output_module()` onward.

The first call site of `get_info()` for this flow is
`SpeechManager.get_current_speech_server_info()`
(`upstream/main:src/orca/speech_manager.py:2423-2436`), which the
preferences dialog uses to display the active synthesizer.

## Proposed fix

Override `get_info()` in the speech-dispatcher subclass so it prefers
`self._output_module` when set, falling back to `self._id` otherwise.
Do not mutate `self._id` — `_active_servers` bookkeeping depends on it.

Patch is attached to the merge request. Diff is +16 / -3, single file
(`src/orca/speechdispatcherfactory.py`). Behavior is identical until
`set_output_module()` has been called; `self._output_module` is `None`
at `__init__()` time, so the fallback to `self._id` reproduces the
pre-patch behavior on a freshly-constructed server.

## Verification

- Patch developed on a branch forked from `upstream/main` HEAD
  `b20d990c6` (not cherry-picked from a downstream tree).
- `git apply --check` against `upstream/main` passes.
- Unit tests: `tests/unit_tests/test_speech_presenter.py` (58/58),
  `tests/unit_tests/test_ax_object.py` + `test_ax_utilities.py`
  (298/298) all pass on the upstream-only tree with the patch applied.
  No isolated unit test exists for the speech-dispatcher backend; it
  is exercised through the integration path.

## What this patch does NOT do

- It does **not** rename or remove `get_current_speech_server_info()` —
  that question is in the second JD TODO and is intentionally separate.
- It does **not** touch `self._id` mutation policy — `_id` stays the
  `_active_servers` key as before.
- It does **not** touch the Spiel backend (`src/orca/spiel.py`), which
  has a different model for "active synthesizer" tracking.
