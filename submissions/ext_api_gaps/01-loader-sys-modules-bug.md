# Title

User extensions using `@dataclass` (Python 3.14) fail to load: extension_loader doesn't register the module in `sys.modules` before `exec_module`

# Description

## Summary

`extension_loader._load_from_file()` calls `spec.loader.exec_module(module)` without first adding `module` to `sys.modules`. Python's standard library `dataclasses` module, as of Python 3.14, does `sys.modules.get(cls.__module__).__dict__` during class creation. When the loaded extension's module is not yet in `sys.modules`, this dereference raises `AttributeError: 'NoneType' object has no attribute '__dict__'` and the entire extension silently fails to load.

This blocks any user extension that uses `@dataclass`, `@dataclass(frozen=True)`, or other class decorators that introspect `sys.modules[cls.__module__]` during class creation. Plain `class Foo:` definitions are unaffected.

## Steps to reproduce on upstream/main

`upstream/main` HEAD `b20d990c6` (or any newer commit; the loader code hasn't changed).

1. Create `~/.local/share/orca/extensions/dctest.py`:
   ```python
   from dataclasses import dataclass
   from orca.command import Command
   from orca.extension import Extension

   @dataclass(frozen=True)
   class Point:
       x: int
       y: int

   class DCTestExtension(Extension):
       GROUP_LABEL = "DCTest"
       def _get_commands(self):
           return []
   ```
2. Approve: `orca --approve-extension dctest.py`
3. Start Orca with `--debug --debug-file=/tmp/dctest.log --replace`
4. Look at the log:
   ```
   EXTENSION LOADER: Failed to load /home/<user>/.local/share/orca/extensions/dctest.py: 'NoneType' object has no attribute '__dict__'
   ```

The extension never reaches `_get_commands` and the user has no idea why; the only signal is a single warning line in a debug file most users won't read.

## Root cause

`src/orca/extension_loader.py:257-273`:

```python
@staticmethod
def _load_from_file(filepath: str, filename: str) -> Extension | None:
    module_name = f"orca_user_extension.{filename[:-3]}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, filepath)
        if spec is None or spec.loader is None:
            ...
            return None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)        # <-- module NOT in sys.modules here
    except Exception as error:
        ...
```

The standard importlib pattern for loading code by file path requires the module to be in `sys.modules` before `exec_module` runs. From `importlib` docs:

> If you want to import a module by name, use `importlib.import_module()`. If you must import from a file location, you must register the module in `sys.modules` between `module_from_spec()` and `exec_module()`, otherwise inner code that looks up `sys.modules[__name__]` (including `@dataclass` in Python 3.14+, and any `functools.singledispatch` usage, and any `typing.NamedTuple` introspection) will fail.

`dataclasses.py:814` in Python 3.14:
```python
ns = sys.modules.get(cls.__module__).__dict__
```

`sys.modules.get()` returns `None`; `.None.__dict__` is the crash.

## Proposed fix

One-line addition to `_load_from_file`:

```python
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module          # <-- ADD THIS LINE
try:
    spec.loader.exec_module(module)
except Exception:
    sys.modules.pop(module_name, None)     # <-- and clean up on failure
    raise
```

This is the canonical "execute a Python file as a module" pattern; see PEP 328, the importlib documentation, and standard implementations like `runpy._run_module_code`.

## Why this matters now

- Python 3.14 (current Fedora 44 default) ships the new `dataclasses` codepath that hits this.
- `@dataclass` is the modern, recommended Python pattern for data classes; the orca codebase itself uses it extensively (see `src/orca/command.py`, `src/orca/keybindings.py`, etc.).
- Other patterns that hit `sys.modules.get(__name__)` and crash the same way:
  - `typing.NamedTuple` subclasses
  - `functools.singledispatch` with type-based dispatch
  - `attr`/`attrs` library (not stdlib but common)
  - Some `pydantic` paths

Any user extension author who tries the modern Python style hits this immediately and the only error trail is a single debug-file line.

## Workaround for extension authors (until fix lands)

Self-register at the top of the extension file:

```python
import sys, types
if __name__ not in sys.modules:
    sys.modules[__name__] = types.ModuleType(__name__)
```

This works but is unintuitive and feels like a hack. Extension authors shouldn't need to know about loader internals.

## Use case

Encountered while converting the OCR extension (`github.com/churst90/orca-perf` → `~/.local/share/orca/extensions/ocr.py`) from a built-in to a user extension. The OCR module uses `@dataclass(frozen=True)` for `OCRWord` and `OCRLine` (immutable per-word recognition results). Conversion was blocked until the workaround above was applied.

## Suggested labels

- `2. Needs Information` -> `1. Bug` after confirmation
- `8. Accessibility`
