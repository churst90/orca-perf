# Orca
#
# Copyright 2026 Igalia, S.L.
# Author: Joanmarie Diggs <jdiggs@igalia.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., Franklin Street, Fifth Floor,
# Boston MA  02110-1301 USA.

"""Discovers, validates, and loads built-in and user extensions."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import sys
import tomllib
import types
from typing import TYPE_CHECKING

from . import debug, gsettings_registry
from .extension import Extension

if TYPE_CHECKING:
    from collections.abc import Callable

_SCHEMA = "extensions"
_KEY_DISABLED = "disabled-extensions"
_KEY_APPROVED = "approved-user-extensions"


@gsettings_registry.get_registry().gsettings_schema("org.gnome.Orca.Extensions", name="extensions")
class ExtensionLoader:
    """Discovers, validates, and loads built-in and user extensions."""

    def __init__(self) -> None:
        self._builtins: list[tuple[Callable[[], Extension], str]] = []
        self._user_extensions: list[Extension] = []

    @gsettings_registry.get_registry().gsetting(
        key="disabled-extensions",
        schema="extensions",
        gtype="as",
        default=[],
        summary="Extensions disabled by the user (by MODULE_NAME)",
    )
    def get_disabled_extensions(self) -> list[str]:
        """Returns the list of disabled extension MODULE_NAMEs."""

        return gsettings_registry.get_registry().layered_lookup(
            _SCHEMA,
            _KEY_DISABLED,
            "as",
            default=[],
        )

    def set_disabled_extensions(self, value: list[str]) -> bool:
        """Sets the list of disabled extension class names."""

        return gsettings_registry.get_registry().set_strv(
            _SCHEMA,
            _KEY_DISABLED,
            value,
        )

    @gsettings_registry.get_registry().gsetting(
        key="approved-user-extensions",
        schema="extensions",
        gtype="a{ss}",
        default={},
        summary="Approved user extensions (filename to SHA256 hash)",
    )
    def get_approved_extensions(self) -> dict[str, str]:
        """Returns the dict of approved extensions (filename -> sha256)."""

        return gsettings_registry.get_registry().layered_lookup(
            _SCHEMA,
            _KEY_APPROVED,
            "a{ss}",
            default={},
        )

    def set_approved_extensions(self, value: dict[str, str]) -> bool:
        """Sets the dict of approved extensions."""

        return gsettings_registry.get_registry().set_dict(
            _SCHEMA,
            _KEY_APPROVED,
            "a{ss}",
            value,
        )

    def approve_extension(self, filename: str, sha256_hash: str) -> None:
        """Approves an extension by recording its filename and hash."""

        approved = dict(self.get_approved_extensions())
        approved[filename] = sha256_hash
        self.set_approved_extensions(approved)

    def approve_extension_file(self, filepath: str) -> str:
        """Computes the hash and approves the extension. Returns the hash."""

        filename = os.path.basename(filepath)
        file_hash = self._compute_hash(filepath)
        self.approve_extension(filename, file_hash)
        return file_hash

    def revoke_extension(self, filename: str) -> None:
        """Revokes approval for an extension (single-file OR package).

        For packages, pass the directory name (the value stored in the
        approval dict). For single-file extensions, pass the .py
        filename as before.
        """

        approved = dict(self.get_approved_extensions())
        approved.pop(filename, None)
        self.set_approved_extensions(approved)

    def approve_package_extension(self, package_dir: str) -> str:
        """Computes the package hash and approves it. Returns the hash."""

        package_name = os.path.basename(os.path.normpath(package_dir))
        pkg_hash = self._compute_package_hash(package_dir)
        self.approve_extension(package_name, pkg_hash)
        return pkg_hash

    def install_orca_ext(
        self, archive_path: str, extensions_dir: str,
    ) -> "tuple[str | None, str | None]":
        """Install a .orca-ext archive into extensions_dir.

        Validates the archive, extracts to extensions_dir/<name>/
        (creating the directory), and auto-approves. The name comes
        from the manifest's extension.name field, not the archive
        filename, so a user can rename foo.orca-ext freely.

        Returns (name, error). Exactly one of the two is None.
          (name, None)   -> success; name is the installed package's
                            directory name (and approval key).
          (None, error)  -> failure; error is a human-readable string.
        """

        import zipfile  # pylint: disable=import-outside-toplevel
        import shutil   # pylint: disable=import-outside-toplevel

        if not os.path.isfile(archive_path):
            return None, f"file not found: {archive_path}"
        if not zipfile.is_zipfile(archive_path):
            return None, f"not a zip archive: {archive_path}"

        # Extract to a temp directory, read manifest, validate, then
        # move to its final destination. This way a malformed archive
        # never leaves debris in extensions_dir.
        import tempfile  # pylint: disable=import-outside-toplevel
        with tempfile.TemporaryDirectory(prefix="orca-ext-install-") as tmp:
            try:
                with zipfile.ZipFile(archive_path) as zf:
                    # Guard against path-traversal via "../foo" entries.
                    for member in zf.namelist():
                        normalized = os.path.normpath(member)
                        if normalized.startswith(("/", "..")) or os.path.isabs(normalized):
                            return None, f"unsafe archive member: {member}"
                    zf.extractall(tmp)
            except (zipfile.BadZipFile, OSError) as error:
                return None, f"extract failed: {error}"

            # The archive may either contain the package files at the
            # top level or wrap them in a single subdirectory. Detect
            # which.
            entries = os.listdir(tmp)
            if (
                len(entries) == 1
                and os.path.isdir(os.path.join(tmp, entries[0]))
                and os.path.isfile(os.path.join(tmp, entries[0], "manifest.toml"))
            ):
                source_dir = os.path.join(tmp, entries[0])
            elif os.path.isfile(os.path.join(tmp, "manifest.toml")):
                source_dir = tmp
            else:
                return None, "archive does not contain a manifest.toml at the top level"

            manifest = self._parse_manifest(source_dir)
            if manifest is None:
                return None, "manifest.toml is invalid or incomplete"

            name = manifest["extension"]["name"]
            # Names must be safe directory names: alphanumeric, dash,
            # underscore. Reject anything else (path traversal, weird
            # characters, etc.).
            if not name.replace("-", "_").replace("_", "").isalnum():
                return None, (
                    f"extension name '{name}' is not a valid directory "
                    "name (only letters, digits, dash, underscore allowed)"
                )

            os.makedirs(extensions_dir, exist_ok=True)
            dest_dir = os.path.join(extensions_dir, name)

            # Idempotent re-install: if a previous version of this
            # extension is already there, swap it out atomically.
            # Atomic = rename old aside, copy new in, then delete old.
            # If the copy fails we restore the old dir so a partial
            # update never leaves a half-installed extension behind.
            # Anything in dest_dir that DOESN'T look like an extension
            # (no manifest.toml) is refused to avoid clobbering
            # unrelated files an end user may have stashed there.
            backup_dir: str | None = None
            if os.path.exists(dest_dir):
                if not os.path.isfile(os.path.join(dest_dir, "manifest.toml")):
                    return None, (
                        f"destination {dest_dir} exists but does not look "
                        f"like an installed extension (no manifest.toml). "
                        f"Refusing to overwrite; remove it manually first."
                    )
                backup_dir = dest_dir + ".pre-install"
                # Clear any leftover backup from a previous aborted
                # install so the rename below can't fail.
                if os.path.exists(backup_dir):
                    try:
                        shutil.rmtree(backup_dir)
                    except OSError:
                        pass
                try:
                    os.rename(dest_dir, backup_dir)
                except OSError as error:
                    return None, (
                        f"could not move existing install aside: {error}"
                    )

            try:
                shutil.copytree(source_dir, dest_dir)
            except OSError as error:
                # Restore the backup if we displaced one.
                if backup_dir is not None and os.path.exists(backup_dir):
                    try:
                        if os.path.exists(dest_dir):
                            shutil.rmtree(dest_dir)
                        os.rename(backup_dir, dest_dir)
                    except OSError:
                        pass
                return None, f"copy to {dest_dir} failed: {error}"

            # Success: dispose of the backup.
            if backup_dir is not None and os.path.exists(backup_dir):
                try:
                    shutil.rmtree(backup_dir)
                except OSError:
                    pass

        # Auto-approve the freshly installed extension (re-approve in
        # the upgrade case so the new file set's hash is what's
        # recorded -- approval is keyed on the package's content hash,
        # so an upgrade legitimately changes it).
        self.approve_package_extension(dest_dir)
        return name, None

    def uninstall_extension(
        self, name: str, extensions_dir: str,
    ) -> "tuple[bool, str | None]":
        """Uninstall a package extension by name.

        Removes the directory, revokes approval, and removes the
        extension from the disabled-extensions list if present.

        Returns (True, None) on success, (False, error) on failure.
        """

        import shutil  # pylint: disable=import-outside-toplevel

        target = os.path.join(extensions_dir, name)
        if not os.path.isdir(target):
            return False, f"package extension '{name}' is not installed"

        try:
            shutil.rmtree(target)
        except OSError as error:
            return False, f"failed to remove {target}: {error}"

        self.revoke_extension(name)

        # Also clear from disabled list if present, so a future
        # reinstall doesn't load disabled.
        disabled = list(self.get_disabled_extensions())
        # Disabled list contains module_names (i.e. class names like
        # "OcrExtension"), not directory names. The directory name
        # may not match, so try both -- best effort.
        for candidate in (name,):
            if candidate in disabled:
                disabled.remove(candidate)
        if disabled != list(self.get_disabled_extensions()):
            self.set_disabled_extensions(disabled)

        return True, None

    def register_builtin(self, getter: Callable[[], Extension], group_label: str) -> None:
        """Registers a built-in extension with the loader."""

        self._builtins.append((getter, group_label))

    def get_user_extensions(self) -> list[Extension]:
        """Returns the list of loaded user extensions.

        Used by the preferences UI so it doesn't have to reach into the
        loader's private state to find which extensions are currently
        live (and therefore eligible for runtime enable/disable).
        """

        return list(self._user_extensions)

    def discover_and_load(self, extensions_dir: str) -> None:
        """Scans the extensions directory and loads approved user extensions.

        Two layouts are recognized:
          1. Single-file:  <extensions_dir>/<name>.py
                           hashed and approved per-file.
          2. Package:      <extensions_dir>/<name>/manifest.toml
                                            <entry-module>.py
                                            [other modules / data]
                           hashed as a deterministic SHA256 over
                           (sorted filename, file bytes) tuples;
                           approval key is the directory name.

        Files / dirs starting with "_" are ignored. Anything else
        (random files, .pyc, etc.) is also ignored.
        """

        if not os.path.isdir(extensions_dir):
            msg = f"EXTENSION LOADER: Extensions directory not found: {extensions_dir}"
            debug.print_message(debug.LEVEL_INFO, msg, True)
            return

        approved = self.get_approved_extensions()
        disabled = self.get_disabled_extensions()

        for entry in sorted(os.listdir(extensions_dir)):
            if entry.startswith("_") or entry.startswith("."):
                continue
            entry_path = os.path.join(extensions_dir, entry)

            if os.path.isfile(entry_path) and entry.endswith(".py"):
                self._discover_single_file(
                    entry_path, entry, approved, disabled,
                )
                continue

            if (
                os.path.isdir(entry_path)
                and os.path.isfile(os.path.join(entry_path, "manifest.toml"))
            ):
                self._discover_package(entry_path, entry, approved, disabled)
                continue

    def _discover_single_file(
        self,
        filepath: str,
        filename: str,
        approved: "dict[str, str]",
        disabled: "list[str]",
    ) -> None:
        """Original single-.py-file extension discovery, refactored out."""

        file_hash = self._compute_hash(filepath)
        approved_hash = approved.get(filename)

        if approved_hash is None:
            self._log_unapproved(filename, file_hash, is_package=False)
            return

        if approved_hash != file_hash:
            self._log_modified(filename, approved_hash, file_hash, is_package=False)
            return

        extension = self._load_from_file(filepath, filename)
        if extension is None:
            return

        if extension.module_name in disabled:
            msg = f"EXTENSION LOADER: Extension {extension.module_name} is disabled. Skipping."
            debug.print_message(debug.LEVEL_INFO, msg, True)
            return

        self._user_extensions.append(extension)
        msg = f"EXTENSION LOADER: Loaded extension {extension.module_name} from {filename}"
        debug.print_message(debug.LEVEL_INFO, msg, True)

    def _discover_package(
        self,
        package_dir: str,
        package_name: str,
        approved: "dict[str, str]",
        disabled: "list[str]",
    ) -> None:
        """Package-extension discovery (subdir with manifest.toml)."""

        pkg_hash = self._compute_package_hash(package_dir)
        approved_hash = approved.get(package_name)

        if approved_hash is None:
            self._log_unapproved(package_name, pkg_hash, is_package=True)
            return

        if approved_hash != pkg_hash:
            self._log_modified(package_name, approved_hash, pkg_hash, is_package=True)
            return

        extension = self._load_from_package(package_dir, package_name)
        if extension is None:
            return

        if extension.module_name in disabled:
            msg = f"EXTENSION LOADER: Extension {extension.module_name} is disabled. Skipping."
            debug.print_message(debug.LEVEL_INFO, msg, True)
            return

        self._user_extensions.append(extension)
        msg = (
            f"EXTENSION LOADER: Loaded extension {extension.module_name} "
            f"from package {package_name}"
        )
        debug.print_message(debug.LEVEL_INFO, msg, True)

    def _log_unapproved(
        self, name: str, current_hash: str, *, is_package: bool,
    ) -> None:
        kind = "package extension" if is_package else "extension"
        msg = (
            f"EXTENSION LOADER: New {kind} found: {name}. "
            f"Not approved. Hash: {current_hash}"
        )
        debug.print_message(debug.LEVEL_INFO, msg, True)
        profile = gsettings_registry.get_registry().get_active_profile()
        msg = (
            f"EXTENSION LOADER: To approve, run: dconf write "
            f"/org/gnome/orca/{profile}/extensions/"
            f"approved-user-extensions "
            f"\"{{'{name}': '{current_hash}'}}\""
        )
        debug.print_message(debug.LEVEL_INFO, msg, True)

    def _log_modified(
        self,
        name: str,
        approved_hash: str,
        current_hash: str,
        *,
        is_package: bool,
    ) -> None:
        kind = "Package extension" if is_package else "Extension"
        msg = (
            f"EXTENSION LOADER: {kind} modified: {name}. "
            f"Approved hash: {approved_hash} "
            f"Current hash: {current_hash}. "
            "Not loading until re-approved."
        )
        debug.print_message(debug.LEVEL_WARNING, msg, True)
        profile = gsettings_registry.get_registry().get_active_profile()
        msg = (
            f"EXTENSION LOADER: To re-approve, run: dconf write "
            f"/org/gnome/orca/{profile}/extensions/"
            f"approved-user-extensions "
            f"\"{{'{name}': '{current_hash}'}}\""
        )
        debug.print_message(debug.LEVEL_INFO, msg, True)

    def set_up_all_commands(self) -> None:
        """Calls set_up_commands on all enabled extensions (built-in and user)."""

        disabled = self.get_disabled_extensions()

        for getter, _group_label in self._builtins:
            ext = getter()
            if ext.module_name in disabled:
                ext.disable()
                continue
            msg = f"EXTENSION LOADER: Loading built-in extension {ext.module_name}"
            debug.print_message(debug.LEVEL_INFO, msg, True)
            ext.set_up_commands()

        for ext in self._user_extensions:
            if ext.module_name in disabled:
                ext.disable()
                continue
            msg = f"EXTENSION LOADER: Loading user extension {ext.module_name}"
            debug.print_message(debug.LEVEL_INFO, msg, True)
            ext.set_up_commands()

    @staticmethod
    def get_class_name(filepath: str) -> str | None:
        """Returns the Extension subclass name from a file without executing it."""

        try:
            with open(filepath, encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError):
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                if isinstance(base, ast.Attribute):
                    name = base.attr
                elif isinstance(base, ast.Name):
                    name = base.id
                else:
                    continue
                if name == "Extension":
                    return node.name

        return None

    @staticmethod
    def _compute_hash(filepath: str) -> str:
        """Returns the SHA256 hex digest of the file at filepath."""

        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    @staticmethod
    def _parse_manifest(package_dir: str) -> "dict | None":
        """Read and validate manifest.toml. Returns parsed dict or None."""

        manifest_path = os.path.join(package_dir, "manifest.toml")
        try:
            with open(manifest_path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as error:
            msg = f"EXTENSION LOADER: Cannot read {manifest_path}: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            return None

        ext = data.get("extension", {})
        entry = data.get("entry", {})

        required = [
            ("extension.name",  ext.get("name")),
            ("entry.module",    entry.get("module")),
            ("entry.class",     entry.get("class")),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            msg = (
                f"EXTENSION LOADER: {manifest_path} missing required "
                f"field(s): {', '.join(missing)}"
            )
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            return None

        return data

    @staticmethod
    def _compute_package_hash(package_dir: str) -> str:
        """SHA256 over (sorted relative-path, file bytes) tuples in the dir.

        Includes every regular file under package_dir, recursively.
        Excludes __pycache__/ and dotfiles so byte-identical sources
        always hash the same regardless of whether Python has been
        run since last hash.
        """

        h = hashlib.sha256()
        for root, dirs, files in os.walk(package_dir):
            # Mutate dirs in place to skip __pycache__ and dotdirs.
            dirs[:] = sorted(
                d for d in dirs
                if d != "__pycache__" and not d.startswith(".")
            )
            for filename in sorted(files):
                if filename.startswith(".") or filename.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, filename)
                rel = os.path.relpath(full, package_dir).encode("utf-8")
                h.update(rel)
                h.update(b"\0")
                try:
                    with open(full, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            h.update(chunk)
                except OSError:
                    # Unreadable file -- include its name in the hash
                    # anyway so a later read-succeeded hash differs.
                    h.update(b"<unreadable>")
                h.update(b"\0")
        return h.hexdigest()

    @staticmethod
    def _load_from_package(
        package_dir: str, package_name: str,
    ) -> Extension | None:
        """Load an Extension subclass from a manifest-described package."""

        manifest = ExtensionLoader._parse_manifest(package_dir)
        if manifest is None:
            return None
        entry_module = manifest["entry"]["module"]
        entry_class = manifest["entry"]["class"]
        entry_filename = f"{entry_module}.py"
        entry_path = os.path.join(package_dir, entry_filename)
        if not os.path.isfile(entry_path):
            msg = (
                f"EXTENSION LOADER: Package {package_name} declares "
                f"entry.module='{entry_module}' but {entry_filename} is missing"
            )
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            return None

        # Synthesize a parent package so the entry module can use
        # relative imports (`from . import helper`). The package's
        # __path__ tells Python's import machinery where to find
        # sibling modules.
        pkg_module_name = f"orca_user_extension.{package_name}"
        pkg_module = types.ModuleType(pkg_module_name)
        pkg_module.__path__ = [package_dir]  # type: ignore[attr-defined]
        sys.modules[pkg_module_name] = pkg_module

        # Now load the entry module as a child of the synthetic package.
        entry_module_name = f"{pkg_module_name}.{entry_module}"
        try:
            spec = importlib.util.spec_from_file_location(
                entry_module_name, entry_path,
            )
            if spec is None or spec.loader is None:
                msg = f"EXTENSION LOADER: Could not create spec for {entry_path}"
                debug.print_message(debug.LEVEL_WARNING, msg, True)
                sys.modules.pop(pkg_module_name, None)
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[entry_module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(entry_module_name, None)
                sys.modules.pop(pkg_module_name, None)
                raise
        except Exception as error:  # pylint: disable=broad-exception-caught
            msg = (
                f"EXTENSION LOADER: Failed to load package "
                f"{package_name} entry {entry_path}: {error}"
            )
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            return None

        cls = getattr(module, entry_class, None)
        if cls is None:
            msg = (
                f"EXTENSION LOADER: Package {package_name} entry "
                f"module declares entry.class='{entry_class}' but "
                f"that name is not in {entry_filename}"
            )
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            return None
        if not (isinstance(cls, type) and issubclass(cls, Extension)):
            msg = (
                f"EXTENSION LOADER: Package {package_name}: "
                f"{entry_class} is not an Extension subclass"
            )
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            return None

        try:
            instance = cls()
            instance.mark_as_user_extension()
            return instance
        except Exception as error:  # pylint: disable=broad-exception-caught
            msg = (
                f"EXTENSION LOADER: Failed to instantiate {entry_class} "
                f"from package {package_name}: {error}"
            )
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            return None

    @staticmethod
    def _load_from_file(filepath: str, filename: str) -> Extension | None:
        """Loads an Extension subclass from a Python file."""

        module_name = f"orca_user_extension.{filename[:-3]}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            if spec is None or spec.loader is None:
                msg = f"EXTENSION LOADER: Could not create spec for {filepath}"
                debug.print_message(debug.LEVEL_WARNING, msg, True)
                return None

            module = importlib.util.module_from_spec(spec)
            # Register in sys.modules BEFORE exec_module. Some stdlib
            # paths -- notably @dataclass on Python 3.14, also
            # typing.NamedTuple and functools.singledispatch with
            # type-based dispatch -- do `sys.modules.get(cls.__module__)`
            # during class creation. If the module isn't there yet, that
            # returns None and crashes at the next .__dict__ access.
            # This is the canonical "import a Python file" pattern; see
            # importlib docs.
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                # Roll back the registration on failure so a half-loaded
                # broken extension doesn't pollute sys.modules.
                sys.modules.pop(module_name, None)
                raise
        except Exception as error:  # pylint: disable=broad-exception-caught
            msg = f"EXTENSION LOADER: Failed to load {filepath}: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            return None

        for attr_name in dir(module):
            obj = module.__dict__.get(attr_name)
            if isinstance(obj, type) and issubclass(obj, Extension) and obj is not Extension:
                try:
                    instance = obj()
                    instance.mark_as_user_extension()
                    return instance
                except Exception as error:  # pylint: disable=broad-exception-caught
                    msg = (
                        f"EXTENSION LOADER: Failed to instantiate "
                        f"{attr_name} from {filepath}: {error}"
                    )
                    debug.print_message(debug.LEVEL_WARNING, msg, True)
                    return None

        msg = f"EXTENSION LOADER: No Extension subclass found in {filepath}"
        debug.print_message(debug.LEVEL_WARNING, msg, True)
        return None


_loader: ExtensionLoader = ExtensionLoader()


def get_loader() -> ExtensionLoader:
    """Returns the ExtensionLoader singleton."""

    return _loader
