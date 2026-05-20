# Example user extensions

Reference extensions developed in the perf branch. Two layouts are
shown:

  - **`ocr/`** -- a *package* extension: a directory containing
    `manifest.toml` + multiple `.py` modules. The loader recognizes
    the package because of the manifest, hashes the whole tree for
    approval, and imports the entry module as a child of a
    synthetic `orca_user_extension.<name>` package so relative
    imports between sibling modules work.
  - **`ocr.orca-ext`** -- the same package, zipped into the
    distribution archive format. Install with
    `orca --install-extension ocr.orca-ext`.

## ocr/

NVDA-style OCR / content recognition. Press Orca+R on any window to
capture its pixels, recognize the text via Tesseract, and enter a
virtual buffer navigable with NumPad keys.

| File             | Purpose                                                   |
|------------------|-----------------------------------------------------------|
| `manifest.toml`  | Extension metadata (name, version, compat, entry point)   |
| `__init__.py`    | Empty marker so Python tooling recognizes the package     |
| `ocr.py`         | Entry module -- the `OcrExtension` class                  |
| `buffer.py`      | `OCRWord` / `OCRLine` / `OCRBuffer` dataclasses           |
| `capture.py`     | Three screen-capture backends: Gdk, ImageMagick, portal   |
| `engine.py`      | Tesseract subprocess wrapper + TSV parser                 |

**Requires** `tesseract` and at least one language data pack
(`tesseract-langpack-eng` on Fedora, `tesseract-ocr-eng` on Debian).
Without tesseract, pressing Orca+R speaks an error; no other Orca
behavior is affected.

## Installing the .orca-ext

```sh
orca --install-extension /path/to/ocr.orca-ext
# Output: Installed extension: ocr
```

Behind the scenes:

1. The archive is extracted to a temp directory.
2. The manifest is validated.
3. The contents are moved to `~/.local/share/orca/extensions/ocr/`.
4. The package's hash is computed (deterministic SHA256 over all
   files in the directory) and registered in dconf's
   `approved-user-extensions`.
5. On next Orca start, the package loads automatically.

Restart Orca (`pkill orca && orca --replace &`) and try Orca+R on
any window.

## Uninstalling

```sh
orca --uninstall-extension ocr
# Output: Uninstalled extension: ocr
```

Removes `~/.local/share/orca/extensions/ocr/`, revokes approval,
and clears the disabled-extensions entry if present.

## Building the .orca-ext from source

```sh
./build-orca-ext.sh ocr ocr.orca-ext
```

The script zips the package directory into a deterministic archive
(sorted entries, no extended attributes), suitable for distribution.

## Controller API surface used

This extension is a clean reference for what the user-extension
framework can do with only public API:

| Used                                            | From                                       |
|-------------------------------------------------|--------------------------------------------|
| `self.controller.present_message_internal`      | `docs/user-extensions.md` (existing)       |
| `self.controller.get_active_window`             | perf commit `766a2e96a`                    |
| `self.controller.get_active_window_screen_rect` | perf commit `766a2e96a`                    |
| `self.controller.set_clipboard_text`            | perf commit `766a2e96a`                    |
| `self.controller.synthesize_mouse_event`        | perf commit `766a2e96a`                    |
| `self.controller.enter_modal_mode`              | perf commit `98d2b2914`                    |
| `self.controller.exit_modal_mode`               | perf commit `98d2b2914`                    |

**Zero direct internal imports.** Every Orca-internal capability
goes through `self.controller.*`. The remaining `from orca import`
lines are for sanctioned surfaces (`debug`, `keybindings`,
`command.{Command,KeyboardCommand}`, `extension.Extension`).

## Manifest format

`manifest.toml` is TOML. Required fields:

```toml
[extension]
name = "ocr"             # alphanumeric + dash + underscore;
                         # becomes the install-dir name
                         # AND the dconf approval key

[entry]
module = "ocr"           # imports <module>.py from the package
class  = "OcrExtension"  # the Extension subclass within that module
```

Optional fields (for human consumption / future tooling):

```toml
[extension]
display-name = "OCR (Optical Character Recognition)"
version = "0.2.0"
author = "..."
license = "LGPL-2.1-or-later"
url = "..."
description = "..."

[compatibility]
min-orca = "51.alpha"
last-tested-orca = "51.alpha"

[dependencies]
system = ["tesseract"]   # informational; not auto-checked yet
```

## Companion built-in

The perf branch also carries OCR as a *built-in* extension at
`src/orca/ocr_presenter.py` (plus `ocr_buffer.py`, `ocr_capture.py`,
`ocr_engine.py`). The user-extension package here is the same
feature wearing the user-extension framework's clothes; the built-in
is the source of record. If you install both, disable the built-in
to avoid keybinding collisions:

```sh
dconf write /org/gnome/orca/profiles/default/extensions/disabled-extensions \
    "['OCRPresenter']"
```

(Reverse with `dconf reset`.)
