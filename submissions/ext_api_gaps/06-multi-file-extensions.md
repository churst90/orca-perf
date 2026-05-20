# Title

User extensions should support multi-file / package layouts, not just single .py files

# Description

## Summary

`extension_loader.discover_and_load()` scans
`~/.local/share/orca/extensions/` for `.py` files only and treats
each one as an independent extension candidate (hashed for
approval, loaded via importlib, scanned for an `Extension`
subclass).

This pushes any non-trivial extension to consolidate everything
into a single .py file. The OCR extension's "user-extension
version" is one such consolidation: four logically separate
modules from the perf-branch built-in (`ocr_buffer`,
`ocr_capture`, `ocr_engine`, `ocr_presenter`) collapsed into a
single 40 KB file at `~/.local/share/orca/extensions/ocr.py`. The
audio-themes-for-orca community extension (a non-trivial NVDA-
style add-on) does the opposite -- splits across six modules --
which is why it can't use the user-extensions framework at all
today and still uses the legacy `orca-customizations.py`
monkey-patch pattern.

## Proposed: support package-style extensions

In addition to today's flat-`.py`-file pattern, recognize
`~/.local/share/orca/extensions/<addon-name>/` as a Python
package when it contains both:

- An `__init__.py` (standard package marker), AND
- A `manifest.toml` (extension metadata file -- see "Manifest"
  below)

The loader's responsibilities:

1. Discover candidates: iterate `~/.local/share/orca/extensions/`,
   include both `*.py` files (today's behavior) and
   `*/manifest.toml`-bearing subdirectories.
2. For each candidate, compute a hash. For package extensions,
   the hash is over all `.py` files in the package directory plus
   the manifest, sorted deterministically. (Alternative: hash the
   manifest only -- defer to your security preference.)
3. The package's main extension class is named in
   `manifest.toml`. The loader imports the package as
   `orca_user_extension.<addon-name>` and looks for the named
   class.

## Manifest format proposal

`manifest.toml`:

```toml
[extension]
name = "ocr"
display-name = "OCR (Optical Character Recognition)"
version = "0.1.0"
author = "Cody Hurst <codythurst@gmail.com>"
license = "LGPL-2.1-or-later"
url = "https://github.com/churst90/orca-perf"
description = """
Recognize and interact with text in any window, including
inaccessible applications. NVDA-style content recognition.
"""

[entry]
module = "ocr.presenter"        # imports the named module from
                                # the package
class = "OcrExtension"          # the Extension subclass to load

[compatibility]
min-orca = "51.alpha"
last-tested-orca = "51.alpha"
```

Why TOML: GNOME-native (`meson.build`, more recent gnome-project
metadata), built-in to Python 3.11+ as `tomllib`, friendlier to
human editing than JSON, more structured than INI.

## Why "support both" rather than "only packages"

- Backward compatibility: existing single-file extensions
  (today's only supported shape) keep working.
- Trivial extensions stay one file -- the "hello world" extension
  from the docs is six lines; making the author create a
  directory and a manifest is overkill.
- Package shape kicks in only for extensions that opt into it by
  providing a `manifest.toml`.

## Why the manifest at all (vs. just the `__init__.py`)

- **Version compatibility**: extensions break when Orca's
  internal API changes. The recent `**args` -> explicit-kwargs
  refactor in `present_object` would break any extension that
  imported the old signatures; a `min-orca` declaration lets the
  loader warn the user "this extension was last tested with
  51.alpha; you're on 52.beta; load anyway?" instead of crashing
  at runtime. NVDA addons have `minimumNVDAVersion` and
  `lastTestedNVDAVersion` for the same reason.
- **Human-readable display name**: the keybindings dialog and
  future extension-management UI can show "OCR (Optical Character
  Recognition)" instead of "orca_user_extension.ocr_v2_final".
- **License + author**: gives the user something to read before
  approving.
- **Entry point indirection**: lets the package author rearrange
  internal module structure without breaking the loader.

## Use case

The OCR extension's perf-branch source is four modules. Forcing
a single-file consolidation cost ~200 lines of duplicated import
boilerplate and made the file ~40 KB. The user-extension version
at `~/.local/share/orca/extensions/ocr.py` is provably-correct
but harder to maintain than the four-module original.

Heath Toby's audio-themes-for-orca is the broader case: six
Python modules plus a `themes/` data directory plus a gschema
file plus icons. Today it installs via a custom shell script
that drops files into `~/.local/share/orca/audio_themes/` and
mutates `orca-customizations.py`. With package-extension
support, the entire thing could live at
`~/.local/share/orca/extensions/audio-themes/` and be installed
by a single `orca --install-extension audio-themes.orca-addon`
command (see the related "extension distribution" discussion).

## Adjacent / future

- Once package extensions exist, the natural next step is a
  ZIP-based distribution format (`.orca-addon`), `orca
  --install-extension foo.orca-addon` that extracts to the
  extensions dir and pre-approves it.
- This is also a prerequisite for shipping language packs
  (extension-local locale/ directory with .mo files for
  translation) -- the user-extensions framework otherwise has no
  way to expose a translation file.

## Suggested labels

- `1. Feature`
- `8. Accessibility`

## Related

Companion to the controller-API issues (focus window, clipboard,
mouse event, modal keys). Where those are about WHAT extensions
can do, this issue is about HOW extensions are structured.

Encountered while consolidating the perf-branch built-in OCR
into a single user-extension file. Concretely: a 40 KB single
file is awkward to maintain compared to the four 5-10 KB
modules of the source-tree version, and there is no extension-
local config / data / locale story today.
