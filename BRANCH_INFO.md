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

This branch carries two categories of patches:

**Performance / stability (commits `d1b418a19` through `713b9cc29`)**

Aggressive caching of AT-SPI property reads (role, parent, name) within
and across events; a per-document cache of structural-navigation match
lists (headings, links, etc.); and held-key coalescing so that key
auto-repeat does not flood `script.present_object()` with overlapping
scroll-and-speech calls.

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

- **Same runtime-override gap may exist on other speech-prefs combos.**
  The synth fix (`e05d8868d`) only patches `_on_speech_synthesizer_changed`
  and `VoicesPreferencesGrid.save_settings`. The speech-system combo
  (Speech Dispatcher vs. Spiel) and the voice-family combos use the
  same architecture and probably exhibit the same revert-on-focus bug
  when changed during prefs. Reproducing requires multiple speech
  servers — the author only has Speech Dispatcher installed, so this
  hasn't been verified.
- **`focus_manager.is_in_preferences_window()` is too narrow.** It only
  matches the prefs root window, not its descendant dialogs (Global
  Voice Settings, Voice Defaults). That's why `update_synthesizer()`
  was reaching back into dconf in the first place. The runtime-override
  patch routes around the symptom; the underlying guard should be
  broadened to "any window owned by the Orca app while prefs is open."
- **`AXObject._NAME_LL_CACHE_DISABLED = True` was a diagnostic, not a
  fix.** With the held-key coalescing in place, the wrong-window-title
  symptom doesn't recur, but the name cache may be re-enableable. Worth
  toggling the flag back on and stress-testing Alt-Tab.
- **Long-lived StateSet cache reverted (`6445b86e4`).** The right
  approach is probably to cache primitive state bits (a frozenset of
  ints) instead of the live `Atspi.StateSet` object. Not attempted yet.
- **Spiel migration in `src/orca/spiel.py` has TODOs at lines 467, 476,
  483** for utterance-offset mapping. Closing those would unlock the
  in-process speech path, which is a bigger latency win than any of the
  perf patches on this branch. Upstream work.
- **Cancel-revert UX** for the speech grid only handles the synthesizer
  runtime override. The voice-family / language / rate / pitch /
  volume sliders set runtime values via `set_runtime_value` and don't
  get reverted either; they'll leak past Cancel until Orca restarts.
  Same fix pattern as `revert_changes` in `VoicesPreferencesGrid`.

## License and upstream

GNOME Orca is licensed under LGPL 2.1+. This fork inherits that
license. None of the patches change file headers. The branch is hosted
publicly at <https://github.com/churst90/orca-perf> for ease of
collaboration; it is not a hard fork — re-syncing with upstream is
expected (see "Keeping up with upstream" above).
