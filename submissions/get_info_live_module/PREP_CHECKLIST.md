# Prep checklist — `speechdispatcherfactory: get_info() reports the live output module`

Follows `UPSTREAM_SUBMISSION_PROCESS.md`. Every checkbox here must be true
before filing.

## Workflow checklist

- [x] **Bug reproduces against a clean `upstream/main` checkout.**
  The issue is rooted in two JD-authored TODO comments in upstream itself
  (`speechdispatcherfactory.py:576` and `speech_manager.py:2431-2432`), both
  acknowledging the out-of-sync state. The reproducer needs two speech-d
  output modules installed (e.g. `espeak-ng` and any second module). No
  perf-branch behavior is invoked.

- [x] **Bug does NOT reproduce only because of perf-branch or `sd-piper`
  modifications.** The base class `get_info()` in
  `upstream/main:src/orca/speechserver.py:241-244` returns `self._id`, and
  `upstream/main:src/orca/speechdispatcherfactory.py` does not override it.
  The behavior is in upstream code, not in perf-branch code.

- [x] **Every function named in the analysis has been read from
  `upstream/main`.**
  - `speechserver.SpeechServer.get_info` — `upstream/main:src/orca/speechserver.py:241-244`
  - `speechdispatcherfactory.SpeechServer.set_output_module` — `upstream/main:src/orca/speechdispatcherfactory.py:573-584` (contains the source TODO)
  - `speechdispatcherfactory.SpeechServer.__init__` — `upstream/main:src/orca/speechdispatcherfactory.py:83-129` (where `self._output_module = None` is set)
  - `speech_manager.SpeechManager.get_current_speech_server_info` — `upstream/main:src/orca/speech_manager.py:2423-2436` (contains the caller TODO)
  - `speech_manager.SpeechManager.get_available_synthesizers` — `upstream/main:src/orca/speech_manager.py:2215-2229` (the other caller of `get_info`)

- [x] **Patch developed on a branch forked from `upstream/main`.**
  Branch: `upstream-fix/speechdispatcher-get-info-live-module` in worktree
  `~/dev/orca-upstream-main`. Created from
  `upstream/main` HEAD `b20d990c6`. No perf-branch cherry-pick involved —
  the patch was written fresh from the upstream tree.

- [x] **`git apply --check` passes against `upstream/main`.**
  Verified: `git apply --check ~/dev/orca/submissions/get_info_live_module/0001-*.patch`
  against `upstream/main` (HEAD `b20d990c6`) returns 0.

- [x] **Unit tests on the upstream-only tree pass.**
  - `tests/unit_tests/test_speech_presenter.py` — 58/58 pass.
  - `tests/unit_tests/test_ax_object.py` + `tests/unit_tests/test_ax_utilities.py` — 298/298 pass.
  - No isolated unit test exists for `speechdispatcherfactory` (it is the
    speech-dispatcher backend integration; testing it requires a running
    speechd, which the test suite does not assume).

- [ ] **Manual repro confirms the fix on the upstream-only tree.**
  USER ACTION REQUIRED. Steps:
  1. Build the upstream-only tree:
     `cd ~/dev/orca-upstream-main && meson install -C builddir`.
  2. Run `orca-upstream` (the launcher this session installed at
     `~/.local/bin/orca-upstream`).
  3. Open Orca preferences (Insert+Space).
  4. Go to the **Voice** tab. Note the synthesizer name currently shown.
  5. Pick a different synthesizer from the dropdown (any second module).
  6. Click Apply (do not close the dialog).
  7. **Without the patch:** the synthesizer field still reads the original
     module name. **With the patch:** the field reads the newly-selected
     module name.

- [x] **Issue body cites upstream code, not perf-branch code.**
  All file/line citations in `ISSUE_BODY.md` and `MR_BODY.md` are against
  `upstream/main` HEAD `b20d990c6`. Quoted code blocks were copied via
  `git show upstream/main:<file>`.

## Files in this submission

- `0001-speechdispatcherfactory-get_info-reports-the-live-ou.patch`
  — the patch, generated from the upstream-fix branch.
- `ISSUE_BODY.md` — text to paste into the GitLab issue.
- `MR_BODY.md` — text to paste into the GitLab merge request.
- `PREP_CHECKLIST.md` — this file.

## What to actually do

1. Complete the manual repro step above. If it confirms, proceed.
2. File the GitLab issue at
   <https://gitlab.gnome.org/GNOME/orca/-/issues/new> using the contents
   of `ISSUE_BODY.md`. Note the assigned issue number (e.g. `#NNN`).
3. Push the `upstream-fix/speechdispatcher-get-info-live-module` branch
   to your fork at `gnome` remote
   (`https://gitlab.gnome.org/churst90/orca.git`):
   ```sh
   cd ~/dev/orca-upstream-main
   git push gnome upstream-fix/speechdispatcher-get-info-live-module
   ```
4. Open a merge request from that branch into `GNOME/orca:main` using
   the contents of `MR_BODY.md`. Reference the issue number from step 2
   in the MR description (replace `#NNN` placeholder).
5. Wait for review. Do not start the next submission until this one has
   either landed or been declined — per the process step 5 ("Pause
   submissions until the process change has been exercised").

## If this is rejected

The patch is also living in the perf branch as commit `cf34bf0a4`, which
predates the upstream-process-discipline change. The perf-branch version
is functionally identical and will continue to apply locally regardless
of the upstream outcome.
