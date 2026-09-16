"""SUPER PRO PLAYERS Mods Manager.

Version: 0.1.1-beta

This is the one downloadable bootstrap/core file for the SPP client mod
manager.  BombSquad requires plugins to target one exact API version, so this
file intentionally contains no plugin metadata of its own. Run ``install()``
from BombSquad's developer console; it detects API 6, 7, 8, or 9 and writes a
tiny matching loader beside this file. Run the same command again after any
BombSquad upgrade or downgrade that changes its plugin API.

Supported compatibility target:
    * BombSquad 1.5.23-1.7.1 (API 6; legacy UI fallback included)
    * BombSquad 1.7.2-1.7.19 (API 7; legacy UI fallback included)
    * BombSquad 1.7.20-1.7.36 (API 8)
    * BombSquad 1.7.37+ / 1.8.x (API 9)

Releases older than 1.5.23 predate the normal plugin system and are not
supported. Future APIs beyond 9 require a manager update. API 6/7/8
compatibility is best-effort until tested on actual devices; API 9 is the
primary release target.

Initial installation after copying this file to BombSquad's mods folder:

    import super_pro_players_mod_manager as spp; spp.install()

Then restart BombSquad.  Open Settings -> Advanced -> Plugins ->
SPPModManagerLoader -> Settings. On API 6 and very early API 7 builds the
window opens automatically each launch because those builds did not expose
plugin settings UI.

Security model for this beta:
    * HTTPS-only manifests and downloads.
    * SHA-256 verification for archives and every extracted file.
    * Exact manifest/archive file-set matching.
    * Strict archive path and size checks.
    * Staged installation, last-working fallback, and manual rollback.
    * Executable updates require a second confirmation click.

SHA-256 detects corruption or a changed download, but a checksum hosted next
to the download is not a publisher signature.  Do not describe this beta as
cryptographically signed or safe after a distribution-repository compromise.

The manager never scans personal storage, modifies BombSquad's packaged game
files, stores account credentials, or edits unrelated mods.
"""

from __future__ import print_function

import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import struct
import sys
import threading
import time
import types
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


MANAGER_NAME = "SUPER PRO PLAYERS Mods Manager"
MANAGER_VERSION = "0.1.1-beta"
STATE_SCHEMA = 1
MANIFEST_SCHEMA = 1
LOADER_FILENAME = "spp_mod_manager_loader.py"
CORE_MODULE_NAME = "super_pro_players_mod_manager"

# Public, credential-free client-package source.  Never put a private GitHub
# token in this file.  The manifest begins with no downloadable packages and
# will gain the SPP client/music packs only after those releases are tested.
DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/master1591/"
    "super-pro-players-mods/main/manifest.json"
)

NETWORK_TIMEOUT_SECONDS = 15
MAX_MANIFEST_BYTES = 512 * 1024
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_FILES = 256
MAX_ARCHIVE_MEMBERS = 512
MAX_PATH_DEPTH = 12
MAX_COMPRESSION_RATIO = 100
MAX_PACKAGES = 32
MAX_TRANSACTION_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_TRANSACTION_UNPACKED_BYTES = 256 * 1024 * 1024

_PACKAGE_ID_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._+-]{0,62}[A-Za-z0-9])?$"
)
_STORAGE_DIR_RE = re.compile(r"^release-[0-9a-f]{16}-[0-9a-f]{12}$")
_WINDOWS_RESERVED_NAMES = set(
    ["CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"]
    + ["COM%d" % value for value in range(1, 10)]
    + ["LPT%d" % value for value in range(1, 10)]
)
_BLOCKED_SUFFIXES = (
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".apk",
    ".jar",
    ".bat",
    ".cmd",
    ".com",
    ".scr",
    ".ps1",
    ".pyc",
    ".pyo",
)

_CACHED_PATHS = None
_CACHED_API_VERSION = None
_CACHED_BUILD_NUMBER = None
_CACHED_GAME_VERSION = None
_STATE_LOAD_CAN_PRUNE = False
_STATE_LOAD_ERROR = ""
# Import only what exists on the running BombSquad generation.  Keeping these
# imports optional also lets the non-UI/security portions be tested with normal
# Python outside the game.
try:
    import babase as _base
    import bauiv1 as _ui

    _MODERN_API = True
except ImportError:
    try:
        import ba as _base

        _ui = _base
        _MODERN_API = False
    except ImportError:
        _base = None
        _ui = None
        _MODERN_API = False


class SPPManagerError(Exception):
    """Expected, user-displayable manager failure."""


class ManifestError(SPPManagerError):
    """The remote manifest is invalid or incompatible."""


class PackageError(SPPManagerError):
    """A package could not be downloaded, verified, or installed."""


def _environment_dict():
    """Return the legacy environment dict on every supported API."""
    try:
        if _MODERN_API:
            import _babase

            return _babase.env()
        import _ba

        return _ba.env()
    except Exception:
        return {}


def _environment_object():
    if _base is None:
        return None
    try:
        return _base.app.env
    except Exception:
        return None


def _env_value(*names):
    env_object = _environment_object()
    env_dict = _environment_dict()
    for name in names:
        if env_object is not None:
            try:
                value = getattr(env_object, name)
                if value is not None:
                    return value
            except Exception:
                pass
        if name in env_dict and env_dict[name] is not None:
            return env_dict[name]
    return None


def _game_api_version():
    if _CACHED_API_VERSION is not None:
        return _CACHED_API_VERSION
    value = _env_value("api_version")
    if value is None and _base is not None:
        try:
            value = _base.app.api_version
        except Exception:
            value = None
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _game_build_number():
    if _CACHED_BUILD_NUMBER is not None:
        return _CACHED_BUILD_NUMBER
    value = _env_value("engine_build_number", "build_number")
    if value is None and _base is not None:
        try:
            value = _base.app.build_number
        except Exception:
            value = None
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _game_version_string():
    if _CACHED_GAME_VERSION is not None:
        return _CACHED_GAME_VERSION
    value = _env_value("engine_version", "version")
    if value is None and _base is not None:
        try:
            value = _base.app.version
        except Exception:
            value = "unknown"
    return str(value or "unknown")


def _python_user_directory():
    value = _env_value("python_directory_user")
    if not value:
        raise SPPManagerError(
            "BombSquad did not provide its user mods directory."
        )
    return os.path.abspath(str(value))


def _data_root():
    """Return a writable manager-data path outside the scanned mods tree."""
    config_path = _env_value("config_file_path")
    if config_path:
        return os.path.join(
            os.path.dirname(os.path.abspath(str(config_path))),
            "spp_mod_manager_data",
        )
    # Last-resort legacy fallback.  The leading dot makes this an invalid
    # Python module name so BombSquad's metadata scanner will ignore it.
    return os.path.join(_python_user_directory(), ".spp_mod_manager_data")


def _paths():
    global _CACHED_PATHS
    if _CACHED_PATHS is not None:
        return dict(_CACHED_PATHS)
    root = _data_root()
    _CACHED_PATHS = {
        "root": root,
        "state": os.path.join(root, "state.json"),
        "cache": os.path.join(root, "manifest-cache.json"),
        "staging": os.path.join(root, "staging"),
        "releases": os.path.join(root, "releases"),
        "logs": os.path.join(root, "manager.log"),
    }
    return dict(_CACHED_PATHS)


def _cache_runtime_values():
    """Capture game-owned values on the logic thread before worker use."""
    global _CACHED_API_VERSION
    global _CACHED_BUILD_NUMBER
    global _CACHED_GAME_VERSION
    if _CACHED_API_VERSION is None:
        _CACHED_API_VERSION = _game_api_version()
    if _CACHED_BUILD_NUMBER is None:
        _CACHED_BUILD_NUMBER = _game_build_number()
    if _CACHED_GAME_VERSION is None:
        _CACHED_GAME_VERSION = _game_version_string()
    _ensure_directories()


def _ensure_directories():
    paths = _paths()
    for key in ("root", "staging", "releases"):
        path = paths[key]
        if os.path.lexists(path):
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise SPPManagerError(
                    "Manager data path is not a plain directory: %s" % path
                )
        else:
            os.makedirs(path, mode=0o700)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return paths


def _default_state():
    return {
        "schema": STATE_SCHEMA,
        "source_url": DEFAULT_MANIFEST_URL,
        "auto_check": True,
        "music_enabled": True,
        "music_volume": 0.80,
        "active": {},
        "rollback_active": {},
        "rollback_repair_needed": [],
        "pending": None,
        "repair_needed": [],
        "last_release_sequence": 0,
        "last_check_time": 0,
        "last_error": "",
        "first_run_notice_shown": False,
        "prune_disabled": False,
    }


def _atomic_write_bytes(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = path + ".tmp-" + uuid.uuid4().hex
    try:
        with open(temporary, "xb") as outfile:
            outfile.write(data)
            outfile.flush()
            try:
                os.fsync(outfile.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


def _atomic_write_json(path, value):
    raw = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    _atomic_write_bytes(path, raw)


def _load_state():
    global _STATE_LOAD_CAN_PRUNE
    global _STATE_LOAD_ERROR
    paths = _ensure_directories()
    state = _default_state()
    if not os.path.isfile(paths["state"]):
        _STATE_LOAD_CAN_PRUNE = False
        try:
            has_preserved_releases = any(
                name.startswith("pkg-")
                for name in os.listdir(paths["releases"])
            )
        except OSError:
            has_preserved_releases = True
        if has_preserved_releases:
            state["prune_disabled"] = True
            state["last_error"] = (
                "State metadata is missing; installed releases were preserved."
            )
            _STATE_LOAD_ERROR = "State metadata is missing."
        else:
            _STATE_LOAD_ERROR = ""
        return state
    try:
        if os.path.getsize(paths["state"]) > MAX_STATE_BYTES:
            raise ValueError("state file is too large")
        with open(paths["state"], "r", encoding="utf-8") as infile:
            loaded = json.load(infile)
        if not isinstance(loaded, dict):
            raise ValueError("state root is not an object")
        if loaded.get("schema") != STATE_SCHEMA:
            raise ValueError("unsupported state schema")
        for key in state:
            if key in loaded:
                state[key] = loaded[key]
        state["source_url"] = (
            state["source_url"] if isinstance(state["source_url"], str) else ""
        )
        state["auto_check"] = bool(state["auto_check"])
        state["music_enabled"] = bool(state["music_enabled"])
        try:
            state["music_volume"] = min(
                1.0, max(0.0, float(state["music_volume"]))
            )
        except (TypeError, ValueError):
            state["music_volume"] = 0.8
        for key in ("last_release_sequence", "last_check_time"):
            value = state.get(key)
            state[key] = value if isinstance(value, int) and not isinstance(value, bool) else 0
        state["last_error"] = (
            state["last_error"] if isinstance(state["last_error"], str) else ""
        )
        state["first_run_notice_shown"] = bool(
            state["first_run_notice_shown"]
        )
        state["prune_disabled"] = bool(state.get("prune_disabled", False))
        state["active"] = _validated_installed_map(
            state.get("active"), "active"
        )
        state["rollback_active"] = _validated_installed_map(
            state.get("rollback_active"), "rollback_active"
        )
        state["rollback_repair_needed"] = _validated_package_id_list(
            state.get("rollback_repair_needed")
        )
        state["pending"] = _validated_pending_state(state.get("pending"))
        repair_needed = state.get("repair_needed")
        state["repair_needed"] = sorted(
            set(
                value
                for value in repair_needed
                if isinstance(value, str) and _PACKAGE_ID_RE.match(value)
            )
        ) if isinstance(repair_needed, list) else []
        state["repair_needed"] = sorted(
            set(state["repair_needed"]) & set(state["active"])
        )
        state["rollback_repair_needed"] = sorted(
            set(state["rollback_repair_needed"])
            & set(state["rollback_active"])
        )
        if (
            state["pending"] is not None
            and state["pending"]["new_active"] != state["active"]
        ):
            # A pending record and the active pointer are committed together.
            # If they disagree, restore the deeply validated previous pointer
            # instead of guessing which untrusted value is correct.
            state["active"] = state["pending"]["previous_active"]
            state["repair_needed"] = state["pending"][
                "previous_repair_needed"
            ]
            state["pending"] = None
            state["last_error"] = "Discarded inconsistent pending update state."
        _STATE_LOAD_CAN_PRUNE = True
        _STATE_LOAD_ERROR = ""
        return state
    except Exception as exc:
        _append_log("Could not read state; using safe defaults: %s" % exc)
        _STATE_LOAD_CAN_PRUNE = False
        _STATE_LOAD_ERROR = str(exc)
        # Preserve the invalid file for manual recovery and, critically, do
        # not prune releases when we no longer know which ones are referenced.
        try:
            quarantine = paths["state"] + ".invalid-" + uuid.uuid4().hex
            os.replace(paths["state"], quarantine)
            _append_log("Preserved invalid state at %s" % quarantine)
        except OSError as quarantine_exc:
            _append_log("Could not preserve invalid state: %s" % quarantine_exc)
        safe_state = _default_state()
        safe_state["last_error"] = (
            "State metadata was invalid; installed releases were preserved."
        )
        safe_state["prune_disabled"] = True
        return safe_state


def _save_state(state):
    state["schema"] = STATE_SCHEMA
    _atomic_write_json(_paths()["state"], state)


def _append_log(message):
    try:
        paths = _ensure_directories()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        line = "[%s UTC] %s\n" % (stamp, str(message).replace("\n", " "))
        with open(paths["logs"], "a", encoding="utf-8") as outfile:
            outfile.write(line)
        # Avoid an endlessly growing log on low-storage phones.
        if os.path.getsize(paths["logs"]) > 256 * 1024:
            with open(paths["logs"], "rb") as infile:
                infile.seek(-128 * 1024, os.SEEK_END)
                tail = infile.read()
            _atomic_write_bytes(paths["logs"], tail)
    except Exception:
        pass


def _screen_message(message, color=(0.75, 0.35, 1.0)):
    print("[SPP Mods Manager] %s" % message)
    if _base is None:
        return
    try:
        _base.screenmessage(message, color=color)
    except Exception:
        try:
            import _babase

            _babase.screenmessage(message, color=color)
        except Exception:
            pass


def _push_from_thread(call):
    if _base is None:
        call()
        return
    try:
        _base.pushcall(call, from_other_thread=True)
    except Exception:
        try:
            if _MODERN_API:
                import _babase as internal_base
            else:
                import _ba as internal_base
            internal_base.pushcall(call, from_other_thread=True)
        except Exception:
            _append_log("Unable to return work to game thread safely.")


def _app_timer(delay, call):
    if _base is None:
        return None
    try:
        return _base.AppTimer(delay, call)
    except Exception:
        try:
            return _base.apptimer(delay, call)
        except Exception:
            try:
                return _base.timer(
                    delay, call, timetype=_base.TimeType.REAL
                )
            except Exception:
                return None


def _loader_source(api_version):
    if api_version in (6, 7):
        import_line = "import ba"
        base_name = "ba.Plugin"
        export_name = "plugin"
    elif api_version in (8, 9):
        import_line = "import babase"
        base_name = "babase.Plugin"
        export_name = "babase.Plugin"
    else:
        raise SPPManagerError(
            "Unsupported BombSquad API %s; supported APIs are 6, 7, 8, and 9."
            % api_version
        )

    # Build metadata strings indirectly so this metadata-free core is not
    # itself mistaken for an exact-API plugin by BombSquad's source scanner.
    meta_prefix = "# " + "ba_meta"
    lines = [
        meta_prefix + " require api %d" % api_version,
        import_line,
        "import %s as _spp" % CORE_MODULE_NAME,
        "",
        meta_prefix + " export " + export_name,
        "class SPPModManagerLoader(%s):" % base_name,
        (
            "    def on_app_launch(self):"
            if api_version == 6
            else "    def on_app_running(self):"
        ),
        "        _spp._plugin_on_app_running()",
        "",
        "    def has_settings_ui(self):",
        "        return True",
        "",
        "    def show_settings_ui(self, source_widget):",
        "        _spp.show_manager(source_widget)",
        "",
        "    def on_app_shutdown(self):",
        "        _spp._plugin_on_app_shutdown()",
        "",
    ]
    return "\n".join(lines)


def _locale_long_value(value):
    """Return a stable config string for a BombSquad Locale value."""
    if value.__class__.__name__ != "Locale":
        return None
    long_value = getattr(value, "long_value", None)
    if isinstance(long_value, str) and long_value:
        return long_value
    return None


def _repair_known_legacy_locale_config(config):
    """Repair Locale values written by old game/mod versions.

    This deliberately touches only three well-known language keys.  It never
    coerces or deletes arbitrary settings owned by BombSquad or other mods.
    """
    repaired = []
    language = config.get("Lang")
    language_value = _locale_long_value(language)
    if language_value is not None:
        config["Lang"] = language_value
        repaired.append("Lang")

    # Old releases of the community Translate plugin stored Locale objects in
    # these keys.  Translate expects one of its own language-name strings, and
    # English is the safe fallback used by its fixed releases.
    for key in ("O Target Trans Lang", "Y Target Trans Lang"):
        if key in config and _locale_long_value(config.get(key)) is not None:
            config[key] = "English"
            repaired.append(key)
    return repaired


def install():
    """Install the tiny exact-API loader after this file is copied in."""
    if _base is None:
        raise SPPManagerError("Run install() from inside BombSquad.")
    api_version = _game_api_version()
    if api_version not in (6, 7, 8, 9):
        message = (
            "This beta supports BombSquad API 6, 7, 8, and 9; detected API %s."
            % api_version
        )
        _screen_message(message, color=(1.0, 0.2, 0.2))
        raise SPPManagerError(message)
    mods_directory = _python_user_directory()
    core_path = os.path.abspath(__file__)
    expected_core_path = os.path.join(
        mods_directory, CORE_MODULE_NAME + ".py"
    )
    if os.path.normcase(core_path) != os.path.normcase(expected_core_path):
        message = (
            "First place this file in BombSquad's mods folder as %s.py."
            % CORE_MODULE_NAME
        )
        _screen_message(message, color=(1.0, 0.35, 0.2))
        raise SPPManagerError(message)

    # Do not depend on the user's "Auto Enable New Plugins" setting. This is
    # the same config shape BombSquad's PluginSpec uses. Builds before 1.8.0
    # can fail asynchronously while saving if an old mod placed a Locale (or
    # another non-JSON object) in the shared app config, so preflight the full
    # config before asking BombSquad to commit it.
    repaired_config_keys = []
    try:
        config = _base.app.config
        if not isinstance(config, dict):
            raise TypeError("BombSquad app config is not a dictionary")
        repaired_config_keys = _repair_known_legacy_locale_config(config)
        try:
            json.dumps(dict(config))
        except (TypeError, ValueError) as exc:
            message = (
                "SPP manager downloaded, but installation stopped because "
                "another mod left invalid BombSquad settings. Update old "
                "mods (especially Translate), then run spp.install() again."
            )
            _append_log("Skipped app-config commit: %s" % exc)
            _screen_message(message, color=(1.0, 0.35, 0.2))
            raise SPPManagerError(message)
        plugins = config.setdefault("Plugins", {})
        if not isinstance(plugins, dict):
            raise TypeError("BombSquad Plugins config is not a dictionary")
        loader_class = "spp_mod_manager_loader.SPPModManagerLoader"
        loader_config = plugins.setdefault(loader_class, {})
        if not isinstance(loader_config, dict):
            raise TypeError("SPP loader config is not a dictionary")
        loader_config["enabled"] = True
        loader_path = os.path.join(mods_directory, LOADER_FILENAME)
        _atomic_write_bytes(
            loader_path, _loader_source(api_version).encode("utf-8")
        )
        config.commit()
    except Exception as exc:
        if isinstance(exc, SPPManagerError):
            raise
        _append_log("Could not pre-enable loader: %s" % exc)
        message = (
            "SPP installation stopped before enabling the loader: %s"
            % exc
        )
        _screen_message(message, color=(1.0, 0.35, 0.2))
        raise SPPManagerError(message)
    message = (
        "SPP Mods Manager installed for API %d. Restart BombSquad once."
        % api_version
    )
    if repaired_config_keys:
        message += " Repaired old language settings."
    _append_log("Installed loader for API %d at %s" % (api_version, loader_path))
    _screen_message(message, color=(0.25, 1.0, 0.35))
    return loader_path


def _validate_https_url(url, field_name):
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise ManifestError("%s must be a non-empty URL." % field_name)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ManifestError("%s must use HTTPS." % field_name)
    if not parsed.hostname or parsed.username or parsed.password:
        raise ManifestError("%s has an invalid host." % field_name)
    if parsed.port not in (None, 443):
        raise ManifestError("%s must use the standard HTTPS port." % field_name)
    if parsed.fragment:
        raise ManifestError("%s must not contain a URL fragment." % field_name)
    return parsed


def _hosts_are_compatible(source_url, download_url):
    source_host = _validate_https_url(source_url, "Manifest URL").hostname.lower()
    download_host = _validate_https_url(
        download_url, "Package URL"
    ).hostname.lower()
    if source_host == download_host:
        return True
    source_is_github = source_host == "github.com" or source_host.endswith(
        ".githubusercontent.com"
    )
    download_is_github = download_host == "github.com" or download_host.endswith(
        ".githubusercontent.com"
    )
    if source_is_github and download_is_github:
        return True
    return False


def _normalise_package_path(value, field_name="file path"):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ManifestError("Invalid %s." % field_name)
    if "\\" in value or "\x00" in value or ":" in value:
        raise ManifestError("Unsafe %s: %s" % (field_name, value))
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ManifestError("Control character in %s." % field_name)
    if value.startswith("/") or value.startswith("//"):
        raise ManifestError("Absolute %s is forbidden." % field_name)
    if unicodedata.normalize("NFC", value) != value:
        raise ManifestError("%s must use normalised Unicode." % field_name)
    parts = value.split("/")
    if len(parts) > MAX_PATH_DEPTH:
        raise ManifestError("%s is nested too deeply." % field_name)
    for part in parts:
        if part in ("", ".", "..") or part.endswith((" ", ".")):
            raise ManifestError("Unsafe %s: %s" % (field_name, value))
        if part.casefold() == "__pycache__":
            raise ManifestError("Python cache path is forbidden: %s" % value)
        if part.lower().startswith(".spp-"):
            raise ManifestError("Manager-reserved name in %s: %s" % (field_name, value))
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise ManifestError("Reserved name in %s: %s" % (field_name, value))
    if value.lower().endswith(_BLOCKED_SUFFIXES):
        raise ManifestError("Blocked executable file type: %s" % value)
    return "/".join(parts)


def _validated_file_list(raw_files):
    if not isinstance(raw_files, list) or not raw_files:
        raise ManifestError("Each package must list its exact files.")
    if len(raw_files) > MAX_PACKAGE_FILES:
        raise ManifestError("Package contains too many files.")
    result = []
    seen = set()
    paths = []
    total = 0
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ManifestError("Package file entry must be an object.")
        path = _normalise_package_path(raw.get("path"))
        collision_key = unicodedata.normalize("NFC", path).casefold()
        if collision_key in seen:
            raise ManifestError("Duplicate/colliding package path: %s" % path)
        seen.add(collision_key)
        size = raw.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestError("Invalid size for %s." % path)
        if size > MAX_MEMBER_BYTES:
            raise ManifestError("Package member is too large: %s" % path)
        sha256 = str(raw.get("sha256", "")).lower()
        if not _SHA256_RE.match(sha256):
            raise ManifestError("Invalid SHA-256 for %s." % path)
        total += size
        if total > MAX_UNPACKED_BYTES:
            raise ManifestError("Package expands beyond the safety limit.")
        result.append({"path": path, "size": size, "sha256": sha256})
        paths.append(path)

    path_set = set(unicodedata.normalize("NFC", value).casefold() for value in paths)
    for path in paths:
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent_key = unicodedata.normalize(
                "NFC", "/".join(parts[:index])
            ).casefold()
            if parent_key in path_set:
                raise ManifestError("File/directory conflict at %s." % path)
    return result, total


def _validated_package_id(value):
    if not isinstance(value, str) or not _PACKAGE_ID_RE.match(value):
        raise ManifestError("Invalid package id.")
    if value in ("spp-manager", "spp_mod_manager"):
        raise ManifestError("The beta manager cannot update itself.")
    return value


def _validated_installed_record(package_id, raw):
    """Return a safe, normalized record loaded from persistent state."""
    package_id = _validated_package_id(package_id)
    if not isinstance(raw, dict):
        raise ManifestError("Invalid installed record for %s." % package_id)
    name = raw.get("name", package_id)
    if not isinstance(name, str) or not name or len(name) > 80:
        raise ManifestError("Invalid installed package name.")
    version = raw.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ManifestError("Invalid installed version for %s." % package_id)
    storage_dir = raw.get("storage_dir")
    if not isinstance(storage_dir, str) or not _STORAGE_DIR_RE.match(storage_dir):
        raise ManifestError("Invalid storage directory for %s." % package_id)
    files, _ = _validated_file_list(raw.get("files"))
    entrypoint = raw.get("entrypoint")
    start_callable = raw.get("start_callable")
    if entrypoint is not None:
        entrypoint = _normalise_package_path(entrypoint, "entrypoint")
        if "/" in entrypoint:
            raise ManifestError("Entrypoint must be at the package root.")
        if entrypoint not in set(item["path"] for item in files):
            raise ManifestError("Installed entrypoint is not listed.")
        if not entrypoint.lower().endswith(".py"):
            raise ManifestError("Installed entrypoint must be Python.")
        if (
            not isinstance(start_callable, str)
            or not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", start_callable)
        ):
            raise ManifestError("Invalid installed start callable.")
    else:
        start_callable = None
    load_order = raw.get("load_order", 100)
    if (
        not isinstance(load_order, int)
        or isinstance(load_order, bool)
        or not -10000 <= load_order <= 10000
    ):
        raise ManifestError("Invalid installed load order.")
    archive_sha256 = str(raw.get("archive_sha256", "")).lower()
    if not _SHA256_RE.match(archive_sha256):
        raise ManifestError("Invalid installed archive SHA-256.")
    source_url = raw.get("source_url")
    _validate_https_url(source_url, "Installed source URL")
    api_versions = raw.get("api_versions")
    if not isinstance(api_versions, list) or not api_versions:
        raise ManifestError("Installed package has no API compatibility list.")
    clean_apis = []
    for value in api_versions:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
            or value > 100
        ):
            raise ManifestError("Invalid installed API compatibility list.")
        if value not in clean_apis:
            clean_apis.append(value)
    minimum_build = raw.get("minimum_build", 0)
    maximum_build = raw.get("maximum_build")
    if (
        not isinstance(minimum_build, int)
        or isinstance(minimum_build, bool)
        or minimum_build < 0
    ):
        raise ManifestError("Invalid installed minimum build.")
    if maximum_build is not None and (
        not isinstance(maximum_build, int)
        or isinstance(maximum_build, bool)
        or maximum_build < minimum_build
    ):
        raise ManifestError("Invalid installed maximum build.")
    return {
        "name": name,
        "version": version,
        "storage_dir": storage_dir,
        "files": files,
        "entrypoint": entrypoint,
        "start_callable": start_callable,
        "load_order": load_order,
        "archive_sha256": archive_sha256,
        "source_url": source_url,
        "api_versions": clean_apis,
        "minimum_build": minimum_build,
        "maximum_build": maximum_build,
    }


def _validated_installed_map(raw, label="installed packages"):
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_PACKAGES:
        raise ManifestError("Invalid %s map." % label)
    result = {}
    seen = set()
    for package_id, record in raw.items():
        normalized_id = _validated_package_id(package_id)
        folded = normalized_id.casefold()
        if folded in seen:
            raise ManifestError("Colliding package id in %s." % label)
        seen.add(folded)
        result[normalized_id] = _validated_installed_record(
            normalized_id, record
        )
    return result


def _validated_package_id_list(raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_PACKAGES:
        raise ManifestError("Invalid package-id list in state.")
    return sorted(set(_validated_package_id(value) for value in raw))


def _validated_pending_state(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("Invalid pending update state.")
    boot_attempted = raw.get("boot_attempted", False)
    if not isinstance(boot_attempted, bool):
        raise ManifestError("Invalid pending boot flag.")
    clear_prune_on_healthy = raw.get("clear_prune_on_healthy", False)
    if not isinstance(clear_prune_on_healthy, bool):
        raise ManifestError("Invalid pending recovery flag.")
    activated_at = raw.get("activated_at", 0)
    attempt_time = raw.get("attempt_time", 0)
    for value in (activated_at, attempt_time):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ManifestError("Invalid pending update timestamp.")
    previous_active = _validated_installed_map(
        raw.get("previous_active"), "pending previous packages"
    )
    new_active = _validated_installed_map(
        raw.get("new_active"), "pending new packages"
    )
    previous_repair = _validated_package_id_list(
        raw.get("previous_repair_needed")
    )
    new_repair = _validated_package_id_list(raw.get("new_repair_needed"))
    fallback_active = _validated_installed_map(
        raw.get("fallback_active", previous_active),
        "pending fallback packages",
    )
    fallback_repair = _validated_package_id_list(
        raw.get("fallback_repair_needed", previous_repair)
    )
    return {
        "previous_active": previous_active,
        "new_active": new_active,
        "previous_repair_needed": sorted(
            set(previous_repair) & set(previous_active)
        ),
        "new_repair_needed": sorted(set(new_repair) & set(new_active)),
        "fallback_active": fallback_active,
        "fallback_repair_needed": sorted(
            set(fallback_repair) & set(fallback_active)
        ),
        "boot_attempted": boot_attempted,
        "clear_prune_on_healthy": clear_prune_on_healthy,
        "activated_at": activated_at,
        "attempt_time": attempt_time,
    }


def _managed_join(root, *parts):
    """Join below a manager-owned root and reject symlink escapes."""
    root = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root, *parts))
    try:
        if os.path.commonpath([root, candidate]) != root:
            raise PackageError("Managed path escaped its storage root.")
        root_real = os.path.realpath(root)
        candidate_real = os.path.realpath(candidate)
        if os.path.commonpath([root_real, candidate_real]) != root_real:
            raise PackageError("Managed path resolves outside its storage root.")
    except ValueError:
        raise PackageError("Managed path is on an unexpected filesystem.")

    current = root
    relative = os.path.relpath(candidate, root)
    if relative != ".":
        for part in relative.split(os.sep):
            current = os.path.join(current, part)
            if os.path.lexists(current) and stat.S_ISLNK(os.lstat(current).st_mode):
                raise PackageError("Symlink found in manager-owned storage.")
    return candidate


def _package_root(package_id):
    package_id = _validated_package_id(package_id)
    return _managed_join(_paths()["releases"], "pkg-" + package_id)


def _release_path(package_id, record):
    if not isinstance(record, dict):
        raise PackageError("Invalid installed package record.")
    storage_dir = record.get("storage_dir")
    if not isinstance(storage_dir, str) or not _STORAGE_DIR_RE.match(storage_dir):
        raise PackageError("Invalid package storage directory.")
    return _managed_join(_package_root(package_id), storage_dir)


def _new_storage_dir(version):
    digest = hashlib.sha256(version.encode("utf-8")).hexdigest()[:16]
    return "release-%s-%s" % (digest, uuid.uuid4().hex[:12])


def _record_is_compatible(record):
    api_version = _game_api_version()
    build_number = _game_build_number()
    return (
        api_version in record.get("api_versions", [])
        and build_number >= int(record.get("minimum_build", 0))
        and (
            record.get("maximum_build") is None
            or build_number <= int(record["maximum_build"])
        )
    )


def _referenced_release_keys(state):
    result = set()
    maps = [
        state.get("active", {}),
        state.get("rollback_active", {}),
    ]
    pending = state.get("pending")
    if isinstance(pending, dict):
        maps.extend(
            [
                pending.get("previous_active", {}),
                pending.get("new_active", {}),
                pending.get("fallback_active", {}),
            ]
        )
    for installed in maps:
        if not isinstance(installed, dict):
            continue
        for package_id, record in installed.items():
            try:
                package_id = _validated_package_id(package_id)
                storage_dir = record["storage_dir"]
                if _STORAGE_DIR_RE.match(storage_dir):
                    result.add((package_id, storage_dir))
            except Exception:
                continue
    return result


def _prune_storage(state):
    """Remove only strict manager-named, unreferenced temporary data."""
    paths = _ensure_directories()
    referenced = _referenced_release_keys(state)
    try:
        for name in os.listdir(paths["staging"]):
            if not re.match(r"^transaction-[0-9a-f]{32}$", name):
                continue
            target = _managed_join(paths["staging"], name)
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target, ignore_errors=True)
    except OSError as exc:
        _append_log("Could not prune staging storage: %s" % exc)

    try:
        for package_name in os.listdir(paths["releases"]):
            if not package_name.startswith("pkg-"):
                continue
            package_id = package_name[4:]
            try:
                _validated_package_id(package_id)
                package_root = _managed_join(paths["releases"], package_name)
            except Exception:
                continue
            if not os.path.isdir(package_root) or os.path.islink(package_root):
                continue
            for storage_dir in os.listdir(package_root):
                if not _STORAGE_DIR_RE.match(storage_dir):
                    continue
                if (package_id, storage_dir) in referenced:
                    continue
                target = _managed_join(package_root, storage_dir)
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target, ignore_errors=True)
            try:
                if not os.listdir(package_root):
                    os.rmdir(package_root)
            except OSError:
                pass
    except OSError as exc:
        _append_log("Could not prune release storage: %s" % exc)


def _maybe_prune_storage(state):
    if not state.get("prune_disabled", False):
        _prune_storage(state)


def _validate_package(raw, source_url, api_version, build_number):
    if not isinstance(raw, dict):
        raise ManifestError("Package entry must be an object.")
    package_id = _validated_package_id(raw.get("id"))
    name = raw.get("name", package_id)
    if not isinstance(name, str) or not name or len(name) > 80:
        raise ManifestError("Invalid package name for %s." % package_id)
    version = raw.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ManifestError("Invalid version for %s." % package_id)
    api_versions = raw.get("api_versions")
    if not isinstance(api_versions, list) or not api_versions:
        raise ManifestError("%s must list compatible API versions." % package_id)
    clean_apis = []
    for value in api_versions:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
            or value > 100
        ):
            raise ManifestError("Invalid API list for %s." % package_id)
        if value not in clean_apis:
            clean_apis.append(value)
    minimum_build = raw.get("minimum_build", 0)
    maximum_build = raw.get("maximum_build")
    if (
        not isinstance(minimum_build, int)
        or isinstance(minimum_build, bool)
        or minimum_build < 0
    ):
        raise ManifestError("Invalid minimum build for %s." % package_id)
    if maximum_build is not None and (
        not isinstance(maximum_build, int) or isinstance(maximum_build, bool)
    ):
        raise ManifestError("Invalid maximum build for %s." % package_id)
    if maximum_build is not None and maximum_build < minimum_build:
        raise ManifestError("Maximum build precedes minimum for %s." % package_id)
    load_order = raw.get("load_order", 100)
    if (
        not isinstance(load_order, int)
        or isinstance(load_order, bool)
        or load_order < -10000
        or load_order > 10000
    ):
        raise ManifestError("Invalid load order for %s." % package_id)

    download_url = raw.get("download_url")
    _validate_https_url(download_url, "Package URL")
    if not _hosts_are_compatible(source_url, download_url):
        raise ManifestError(
            "%s downloads from a different unapproved host." % package_id
        )
    download_size = raw.get("download_size")
    if (
        not isinstance(download_size, int)
        or isinstance(download_size, bool)
        or download_size <= 0
        or download_size > MAX_ARCHIVE_BYTES
    ):
        raise ManifestError("Invalid download size for %s." % package_id)
    archive_sha256 = str(raw.get("archive_sha256", "")).lower()
    if not _SHA256_RE.match(archive_sha256):
        raise ManifestError("Invalid archive SHA-256 for %s." % package_id)
    files, computed_unpacked = _validated_file_list(raw.get("files"))
    unpacked_size = raw.get("unpacked_size", computed_unpacked)
    if unpacked_size != computed_unpacked:
        raise ManifestError("Unpacked size mismatch for %s." % package_id)

    entrypoint = raw.get("entrypoint")
    start_callable = raw.get("start_callable", "start")
    if entrypoint is not None:
        entrypoint = _normalise_package_path(entrypoint, "entrypoint")
        if "/" in entrypoint:
            raise ManifestError(
                "Entrypoint must be at the package root for %s." % package_id
            )
        if entrypoint not in set(item["path"] for item in files):
            raise ManifestError("Entrypoint is not listed for %s." % package_id)
        if not entrypoint.lower().endswith(".py"):
            raise ManifestError("Entrypoint must be Python for %s." % package_id)
        if (
            not isinstance(start_callable, str)
            or not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", start_callable)
        ):
            raise ManifestError("Invalid start callable for %s." % package_id)
    else:
        start_callable = None

    compatible = (
        api_version in clean_apis
        and build_number >= minimum_build
        and (maximum_build is None or build_number <= maximum_build)
    )
    return {
        "id": package_id,
        "name": name,
        "version": version,
        "api_versions": clean_apis,
        "minimum_build": minimum_build,
        "maximum_build": maximum_build,
        "download_url": download_url,
        "download_size": download_size,
        "unpacked_size": unpacked_size,
        "archive_sha256": archive_sha256,
        "files": files,
        "entrypoint": entrypoint,
        "start_callable": start_callable,
        "requires_restart": bool(raw.get("requires_restart", True)),
        "load_order": load_order,
        "compatible": compatible,
    }


def _validate_manifest(
    raw, source_url, api_version=None, build_number=None
):
    if not isinstance(raw, dict):
        raise ManifestError("Manifest root must be an object.")
    if raw.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError("Unsupported manifest schema.")
    sequence = raw.get("release_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ManifestError("Manifest release_sequence must be a positive integer.")
    channel = raw.get("channel", "stable")
    if channel != "stable":
        raise ManifestError("This beta accepts only the stable channel.")
    raw_packages = raw.get("packages")
    if not isinstance(raw_packages, list) or len(raw_packages) > MAX_PACKAGES:
        raise ManifestError("Manifest has an invalid package list.")
    packages = []
    seen_ids = set()
    if api_version is None:
        api_version = _game_api_version()
    if build_number is None:
        build_number = _game_build_number()
    for raw_package in raw_packages:
        package = _validate_package(
            raw_package,
            source_url,
            api_version,
            build_number,
        )
        if package["id"] in seen_ids:
            raise ManifestError("Duplicate package id: %s" % package["id"])
        seen_ids.add(package["id"])
        packages.append(package)
    return {
        "schema": MANIFEST_SCHEMA,
        "release_sequence": sequence,
        "channel": channel,
        "title": str(raw.get("title", "SUPER PRO PLAYERS"))[:80],
        "packages": packages,
    }


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject unsafe redirects before urllib connects to their destination."""

    def __init__(self, source_url):
        urllib.request.HTTPRedirectHandler.__init__(self)
        self._source_url = source_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute_url = urllib.parse.urljoin(req.full_url, newurl)
        if not _hosts_are_compatible(self._source_url, absolute_url):
            raise urllib.error.HTTPError(
                absolute_url,
                code,
                "Redirect to an unapproved host was blocked.",
                headers,
                fp,
            )
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, absolute_url
        )


def _urlopen_checked(url, source_url):
    if not _hosts_are_compatible(source_url, url):
        raise SPPManagerError("Download host is not approved for this source.")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SPP-Mods-Manager/%s" % MANAGER_VERSION,
            "Accept": "application/json, application/zip, application/octet-stream",
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler(source_url))
    response = opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS)
    final_url = response.geturl()
    if not _hosts_are_compatible(source_url, final_url):
        response.close()
        raise SPPManagerError("Download redirected to an unapproved host.")
    return response


def _fetch_manifest(
    source_url, api_version=None, build_number=None, cache_path=None
):
    _validate_https_url(source_url, "Manifest URL")
    try:
        with _urlopen_checked(source_url, source_url) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > MAX_MANIFEST_BYTES:
                raise ManifestError("Manifest is too large.")
            data = response.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            raise ManifestError("Manifest is too large.")
        raw = json.loads(data.decode("utf-8"))
        manifest = _validate_manifest(
            raw, source_url, api_version, build_number
        )
        _atomic_write_bytes(cache_path or _paths()["cache"], data)
        return manifest
    except SPPManagerError:
        raise
    except (urllib.error.URLError, OSError, ValueError, UnicodeError) as exc:
        raise ManifestError("Could not download manifest: %s" % exc)


def _download_archive(package, source_url, target_path, progress=None):
    expected_size = package["download_size"]
    hasher = hashlib.sha256()
    received = 0
    try:
        with _urlopen_checked(package["download_url"], source_url) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) != expected_size:
                raise PackageError(
                    "%s download size changed." % package["name"]
                )
            with open(target_path, "xb") as outfile:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > expected_size or received > MAX_ARCHIVE_BYTES:
                        raise PackageError(
                            "%s exceeded its download limit." % package["name"]
                        )
                    hasher.update(chunk)
                    outfile.write(chunk)
                    if progress is not None:
                        progress(package["name"], received, expected_size)
                outfile.flush()
                try:
                    os.fsync(outfile.fileno())
                except OSError:
                    pass
        if received != expected_size:
            raise PackageError("%s download is incomplete." % package["name"])
        if hasher.hexdigest() != package["archive_sha256"]:
            raise PackageError("%s archive SHA-256 failed." % package["name"])
    except SPPManagerError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise PackageError("Could not download %s: %s" % (package["name"], exc))


def _zip_entry_is_special(info):
    mode = (info.external_attr >> 16) & 0xFFFF
    if not mode:
        return False
    kind = stat.S_IFMT(mode)
    return kind not in (0, stat.S_IFREG, stat.S_IFDIR)


def _preflight_zip_entry_count(archive_path):
    """Read the small ZIP end record before ZipFile allocates entry objects."""
    try:
        file_size = os.path.getsize(archive_path)
        if file_size < 22 or file_size > MAX_ARCHIVE_BYTES:
            raise PackageError("Package ZIP has an invalid size.")
        tail_size = min(file_size, 22 + 65535)
        with open(archive_path, "rb") as infile:
            infile.seek(file_size - tail_size)
            tail = infile.read(tail_size)
        signature = b"PK\x05\x06"
        search_end = len(tail)
        values = None
        while True:
            offset = tail.rfind(signature, 0, search_end)
            if offset < 0:
                break
            if offset + 22 <= len(tail):
                candidate = struct.unpack_from("<4s4H2IH", tail, offset)
                comment_length = candidate[-1]
                if offset + 22 + comment_length == len(tail):
                    values = candidate
                    break
            search_end = offset
        if values is None:
            raise PackageError("Package ZIP has no valid end record.")
        (
            _,
            disk_number,
            central_disk,
            disk_entries,
            total_entries,
            central_size,
            central_offset,
            _,
        ) = values
        if disk_number != 0 or central_disk != 0 or disk_entries != total_entries:
            raise PackageError("Multi-disk ZIP packages are forbidden.")
        if (
            total_entries == 0xFFFF
            or central_size == 0xFFFFFFFF
            or central_offset == 0xFFFFFFFF
        ):
            raise PackageError("ZIP64 packages are forbidden.")
        if total_entries > MAX_ARCHIVE_MEMBERS:
            raise PackageError("Archive contains too many entries.")
        absolute_eocd = file_size - tail_size + offset
        if central_offset + central_size != absolute_eocd:
            raise PackageError("Package ZIP central directory is invalid.")

        # Do not trust the EOCD count alone: ZipFile allocates one ZipInfo per
        # central record based on central_size. Stream the fixed headers first
        # and prove the real count/shape before constructing ZipFile.
        actual_entries = 0
        consumed = 0
        with open(archive_path, "rb") as infile:
            infile.seek(central_offset)
            while consumed < central_size:
                if actual_entries >= MAX_ARCHIVE_MEMBERS:
                    raise PackageError("Archive contains too many entries.")
                fixed = infile.read(46)
                if len(fixed) != 46:
                    raise PackageError("Truncated ZIP central directory.")
                fields = struct.unpack("<4s6H3I5H2I", fixed)
                if fields[0] != b"PK\x01\x02":
                    raise PackageError("Invalid ZIP central-directory record.")
                filename_length = fields[10]
                extra_length = fields[11]
                comment_length = fields[12]
                if filename_length < 1 or filename_length > 1024:
                    raise PackageError("ZIP filename length is unsafe.")
                if extra_length > 4096 or comment_length > 1024:
                    raise PackageError("ZIP metadata field is too large.")
                variable_length = (
                    filename_length + extra_length + comment_length
                )
                record_length = 46 + variable_length
                if consumed + record_length > central_size:
                    raise PackageError("ZIP central-directory size mismatch.")
                infile.seek(variable_length, os.SEEK_CUR)
                consumed += record_length
                actual_entries += 1
        if consumed != central_size or actual_entries != total_entries:
            raise PackageError("Archive entry count is inconsistent.")
        return actual_entries
    except SPPManagerError:
        raise
    except (OSError, struct.error) as exc:
        raise PackageError("Could not inspect package ZIP: %s" % exc)


def _compile_python_file(path, display_path):
    try:
        with open(path, "rb") as infile:
            source = infile.read()
        compile(source, display_path, "exec")
    except Exception as exc:
        raise PackageError("Python check failed for %s: %s" % (display_path, exc))


def _safe_extract_archive(archive_path, destination, package):
    expected_list = package["files"]
    expected = dict((item["path"], item) for item in expected_list)
    extracted = set()
    extracted_casefold = set()
    total_written = 0
    try:
        declared_entries = _preflight_zip_entry_count(archive_path)
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            if len(infos) != declared_entries:
                raise PackageError("Archive entry count is inconsistent.")
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise PackageError("Archive contains too many entries.")
            if sum(len(item.filename.encode("utf-8")) for item in infos) > 64 * 1024:
                raise PackageError("Archive filenames exceed the safety limit.")
            file_infos = [item for item in infos if not item.is_dir()]
            if len(file_infos) != len(expected_list):
                raise PackageError("Archive file count does not match manifest.")
            for info in infos:
                raw_name = info.filename
                if _zip_entry_is_special(info):
                    raise PackageError("Links/devices are forbidden in packages.")
                if info.is_dir():
                    # Directory records are unnecessary. Validate, then ignore.
                    candidate = raw_name.rstrip("/")
                    if candidate:
                        _normalise_package_path(candidate, "archive directory")
                    continue
                name = _normalise_package_path(raw_name, "archive path")
                folded = unicodedata.normalize("NFC", name).casefold()
                if name in extracted or folded in extracted_casefold:
                    raise PackageError("Archive contains duplicate paths.")
                extracted.add(name)
                extracted_casefold.add(folded)
                if name not in expected:
                    raise PackageError("Unexpected archive file: %s" % name)
                wanted = expected[name]
                if info.flag_bits & 0x1:
                    raise PackageError("Encrypted ZIP members are forbidden.")
                if info.compress_type not in (
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                ):
                    raise PackageError("Unsupported ZIP compression for %s." % name)
                if info.file_size != wanted["size"]:
                    raise PackageError("Size mismatch for %s." % name)
                if info.file_size > MAX_MEMBER_BYTES:
                    raise PackageError("Archive member is too large: %s" % name)
                if info.compress_size > 0 and info.file_size > 0:
                    ratio = float(info.file_size) / float(info.compress_size)
                    if ratio > MAX_COMPRESSION_RATIO:
                        raise PackageError("Suspicious compression ratio for %s." % name)

                output_path = _managed_join(destination, *name.split("/"))
                output_parent = os.path.dirname(output_path)
                if not os.path.isdir(output_parent):
                    os.makedirs(output_parent)
                hasher = hashlib.sha256()
                written = 0
                with archive.open(info, "r") as source, open(output_path, "xb") as target:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        total_written += len(chunk)
                        if written > wanted["size"] or total_written > MAX_UNPACKED_BYTES:
                            raise PackageError("Package exceeded extraction limit.")
                        hasher.update(chunk)
                        target.write(chunk)
                    target.flush()
                    try:
                        os.fsync(target.fileno())
                    except OSError:
                        pass
                if written != wanted["size"]:
                    raise PackageError("Incomplete extracted file: %s" % name)
                if hasher.hexdigest() != wanted["sha256"]:
                    raise PackageError("SHA-256 failed for %s." % name)
                if name.lower().endswith(".py"):
                    _compile_python_file(output_path, name)

        if extracted != set(expected):
            missing = sorted(set(expected) - extracted)
            raise PackageError("Archive is missing: %s" % ", ".join(missing[:3]))
        if total_written != package["unpacked_size"]:
            raise PackageError("Extracted package size does not match manifest.")
    except SPPManagerError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise PackageError("Invalid package archive: %s" % exc)


def _verify_installed_record(package_id, record):
    try:
        record = _validated_installed_record(package_id, record)
        release_path = _release_path(package_id, record)
        if not os.path.lexists(release_path):
            return False, "release directory is missing"
        release_mode = os.lstat(release_path).st_mode
        if stat.S_ISLNK(release_mode) or not stat.S_ISDIR(release_mode):
            return False, "release path is not a plain directory"

        marker_path = _managed_join(release_path, ".spp-install.json")
        if not os.path.lexists(marker_path):
            return False, "installation marker is missing"
        marker_mode = os.lstat(marker_path).st_mode
        if stat.S_ISLNK(marker_mode) or not stat.S_ISREG(marker_mode):
            return False, "installation marker is not a plain file"
        if os.path.getsize(marker_path) > 16 * 1024:
            return False, "installation marker is too large"
        with open(marker_path, "r", encoding="utf-8") as infile:
            marker = json.load(infile)
        if not isinstance(marker, dict) or (
            marker.get("id") != package_id
            or marker.get("version") != record["version"]
            or marker.get("archive_sha256") != record["archive_sha256"]
            or marker.get("storage_dir") != record["storage_dir"]
        ):
            return False, "installation marker does not match state"

        # Bytecode caches are generated by Python, not supplied by a package.
        # Remove them before exact file-set verification so they can never be
        # used instead of the hash-checked source files.
        walked_entries = 0
        for current_root, directories, filenames in os.walk(
            release_path, topdown=True, followlinks=False
        ):
            walked_entries += len(directories) + len(filenames)
            if walked_entries > MAX_ARCHIVE_MEMBERS + 1:
                return False, "installed package contains too many entries"
            for directory in list(directories):
                full_directory = _managed_join(current_root, directory)
                mode = os.lstat(full_directory).st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    return False, "non-directory or symlink found in package"
                if directory == "__pycache__":
                    shutil.rmtree(full_directory)
                    directories.remove(directory)

        actual_files = set()
        for current_root, directories, filenames in os.walk(
            release_path, topdown=True, followlinks=False
        ):
            for directory in directories:
                full_directory = _managed_join(current_root, directory)
                mode = os.lstat(full_directory).st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    return False, "non-directory or symlink found in package"
            for filename in filenames:
                full_path = _managed_join(current_root, filename)
                mode = os.lstat(full_path).st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    return False, "non-file or symlink found in package"
                relative = os.path.relpath(full_path, release_path).replace(
                    os.sep, "/"
                )
                if relative == ".spp-install.json":
                    continue
                relative = _normalise_package_path(relative)
                actual_files.add(relative)

        expected_files = set(item["path"] for item in record["files"])
        if actual_files != expected_files:
            missing = sorted(expected_files - actual_files)
            extra = sorted(actual_files - expected_files)
            if missing:
                return False, "%s is missing" % missing[0]
            return False, "unexpected file found: %s" % extra[0]

        for item in record["files"]:
            relative = item["path"]
            full_path = _managed_join(release_path, *relative.split("/"))
            if os.path.getsize(full_path) != item["size"]:
                return False, "%s size changed" % relative
            hasher = hashlib.sha256()
            with open(full_path, "rb") as infile:
                while True:
                    chunk = infile.read(64 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
            if hasher.hexdigest() != item["sha256"]:
                return False, "%s hash changed" % relative
        return True, "ok"
    except Exception as exc:
        return False, "invalid installed package: %s" % exc


def _verify_installed_map(
    installed, require_compatible=True, skip_ids=None
):
    try:
        installed = _validated_installed_map(installed)
    except Exception as exc:
        return False, "invalid package state: %s" % exc
    skip_ids = set(skip_ids or [])
    for package_id, record in installed.items():
        if package_id in skip_ids:
            continue
        if require_compatible and not _record_is_compatible(record):
            return False, "%s is incompatible with this game build" % package_id
        valid, reason = _verify_installed_record(package_id, record)
        if not valid:
            return False, "%s: %s" % (package_id, reason)
    return True, "ok"


class _ManagerService(object):
    def __init__(self):
        self._lock = threading.RLock()
        self.state = None
        self.manifest = None
        self.status = "NOT STARTED"
        self.detail = ""
        self.busy = False
        self.progress = ""
        self.windows = []
        self.started = False
        self.loaded_modules = []
        self.repair_needed = set()

    def start(self):
        with self._lock:
            if self.started:
                return
            self.started = True
            _cache_runtime_values()
            self.state = _load_state()
            self.repair_needed = set(
                item
                for item in self.state.get("repair_needed", [])
                if isinstance(item, str) and _PACKAGE_ID_RE.match(item)
            )
            self.status = "STARTING"
        try:
            rolled_back = self._process_pending_boot()
            if _STATE_LOAD_CAN_PRUNE and not self.state.get("prune_disabled"):
                _prune_storage(self.state)
            self._load_active_packages()
            # Builds before 1.6.0 do not dispatch plugin shutdown callbacks.
            # Once every package starter has returned successfully, commit the
            # boot immediately there to avoid false rollback on a quick exit.
            if (
                _game_api_version() == 6
                and _game_build_number() < 20357
                and isinstance(self.state.get("pending"), dict)
                and self.state["pending"].get("boot_attempted")
            ):
                self._mark_pending_healthy()
            if rolled_back:
                self._set_status(
                    "ROLLED BACK",
                    "A pending update did not finish booting; restored last working pack.",
                )
            elif self.repair_needed:
                self._set_status(
                    "REPAIR NEEDED",
                    "Damaged packages were kept disabled. Open the manager to repair them.",
                )
            elif _STATE_LOAD_ERROR or self.state.get("prune_disabled"):
                self._set_status(
                    "STATE RECOVERY NEEDED",
                    "Invalid manager state was preserved; package files were not deleted.",
                )
            elif self.state.get("active"):
                self._set_status("READY", "Installed SPP pack loaded.")
            elif self.state.get("source_url"):
                self._set_status("READY", "No SPP packages installed yet.")
            else:
                self._set_status(
                    "SET SOURCE URL", "Add the public SPP manifest URL to continue."
                )
        except Exception as exc:
            _append_log("Startup failure: %s" % exc)
            self._rollback_after_load_failure(str(exc))

        pending = self.state.get("pending")
        if isinstance(pending, dict) and pending.get("boot_attempted"):
            _app_timer(2.0, self._mark_pending_healthy)

        if self.state.get("auto_check") and self.state.get("source_url"):
            _app_timer(2.0, self.check_for_updates)

        if not self.state.get("first_run_notice_shown"):
            new_state = copy.deepcopy(self.state)
            new_state["first_run_notice_shown"] = True
            _save_state(new_state)
            self.state = new_state
            if _game_api_version() == 6 or (
                _game_api_version() == 7 and _game_build_number() < 20909
            ):
                notice = "SPP Mods Manager will open automatically each launch."
            else:
                notice = (
                    "SPP Mods Manager ready. Open Settings > Advanced > Plugins."
                )
            _screen_message(notice, color=(0.65, 0.3, 1.0))
        # API 6 and early API-7 builds did not expose plugin settings buttons,
        # so open the small manager window once per game launch there.
        if _game_api_version() == 6 or (
            _game_api_version() == 7 and _game_build_number() < 20909
        ):
            _app_timer(2.5, lambda: show_manager(None))

    def _process_pending_boot(self):
        pending = self.state.get("pending")
        if not isinstance(pending, dict):
            return False
        if pending.get("boot_attempted"):
            previous = pending["fallback_active"]
            valid, reason = _verify_installed_map(
                previous, skip_ids=pending["fallback_repair_needed"]
            )
            if not valid:
                raise PackageError(
                    "Pending update did not finish and fallback is invalid: %s"
                    % reason
                )
            new_state = copy.deepcopy(self.state)
            new_state["active"] = copy.deepcopy(previous)
            new_state["repair_needed"] = list(
                pending["fallback_repair_needed"]
            )
            new_state["pending"] = None
            new_state["last_error"] = "Automatic rollback after incomplete boot."
            _save_state(new_state)
            self.state = new_state
            self.repair_needed = set(new_state["repair_needed"])
            _maybe_prune_storage(self.state)
            return True
        new_state = copy.deepcopy(self.state)
        new_state["pending"]["boot_attempted"] = True
        new_state["pending"]["attempt_time"] = int(time.time())
        _save_state(new_state)
        self.state = new_state
        return False

    def _mark_pending_healthy(self):
        with self._lock:
            pending = self.state.get("pending")
            if not isinstance(pending, dict) or not pending.get("boot_attempted"):
                return
            new_state = copy.deepcopy(self.state)
            new_state["rollback_active"] = copy.deepcopy(
                pending["fallback_active"]
            )
            new_state["rollback_repair_needed"] = list(
                pending["fallback_repair_needed"]
            )
            new_state["repair_needed"] = list(
                pending["new_repair_needed"]
            )
            new_state["pending"] = None
            new_state["last_error"] = ""
            if pending.get("clear_prune_on_healthy"):
                new_state["prune_disabled"] = False
            _save_state(new_state)
            self.state = new_state
            self.repair_needed = set(new_state["repair_needed"])
        _maybe_prune_storage(self.state)
        self._set_status("READY", "Updated SPP pack started successfully.")

    def _rollback_after_load_failure(self, reason):
        pending = self.state.get("pending") if self.state else None
        if isinstance(pending, dict):
            fallback = pending["fallback_active"]
            fallback_repair = pending["fallback_repair_needed"]
        else:
            fallback = self.state.get("rollback_active", {}) if self.state else {}
            fallback_repair = (
                self.state.get("rollback_repair_needed", [])
                if self.state
                else []
            )
        valid, fallback_reason = _verify_installed_map(
            fallback, skip_ids=fallback_repair
        )
        if valid and (fallback or isinstance(pending, dict)):
            new_state = copy.deepcopy(self.state)
            current = copy.deepcopy(new_state.get("active", {}))
            new_state["active"] = copy.deepcopy(fallback)
            new_state["repair_needed"] = list(fallback_repair)
            new_state["pending"] = None
            if not isinstance(pending, dict):
                new_state["rollback_active"] = current
                new_state["rollback_repair_needed"] = sorted(
                    self.repair_needed
                )
            new_state["last_error"] = "Package startup failed: %s" % reason
            _save_state(new_state)
            self.state = new_state
            self.repair_needed = set(fallback_repair)
            self._set_status(
                "ROLLED BACK",
                "New package failed. Previous version restored; restart BombSquad.",
            )
        else:
            self._set_status(
                "ERROR",
                "Package startup failed: %s; fallback unavailable: %s"
                % (reason, fallback_reason),
            )

    def _load_active_packages(self):
        active = self.state.get("active", {})
        valid, reason = _verify_installed_map(
            active, skip_ids=self.repair_needed
        )
        if not valid:
            raise PackageError(reason)
        ordered = sorted(
            active.items(), key=lambda item: int(item[1].get("load_order", 100))
        )
        old_dont_write_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            for package_id, record in ordered:
                if package_id in self.repair_needed:
                    _append_log(
                        "Skipped damaged package %s pending repair." % package_id
                    )
                    continue
                entrypoint = record.get("entrypoint")
                if not entrypoint:
                    continue
                version = record["version"]
                package_path = _release_path(package_id, record)
                entry_path = _managed_join(
                    package_path, *entrypoint.split("/")
                )
                namespace = "_spp_package_" + hashlib.sha256(
                    (package_id + "\0" + record["storage_dir"]).encode("utf-8")
                ).hexdigest()[:24]
                package_module = types.ModuleType(namespace)
                package_module.__path__ = [package_path]
                package_module.__package__ = namespace
                sys.modules[namespace] = package_module
                module_name = namespace + ".entrypoint"
                spec = importlib.util.spec_from_file_location(
                    module_name, entry_path
                )
                if spec is None or spec.loader is None:
                    raise PackageError("Could not load %s." % package_id)
                module = importlib.util.module_from_spec(spec)
                module.__package__ = namespace
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                callable_name = record["start_callable"]
                starter = getattr(module, callable_name, None)
                if not callable(starter):
                    raise PackageError(
                        "%s has no callable %s()." % (package_id, callable_name)
                    )
                context = {
                    "manager_name": MANAGER_NAME,
                    "manager_version": MANAGER_VERSION,
                    "api_version": _game_api_version(),
                    "build_number": _game_build_number(),
                    "package_id": package_id,
                    "package_version": version,
                    "package_path": package_path,
                    "get_package_path": self.get_package_path,
                    "get_music_settings": self.get_music_settings,
                }
                starter(context)
                self.loaded_modules.append(module)
                _append_log("Loaded package %s %s" % (package_id, version))
        finally:
            sys.dont_write_bytecode = old_dont_write_bytecode

    def get_package_path(self, package_id):
        if package_id in self.repair_needed:
            return None
        record = self.state.get("active", {}).get(package_id)
        if not isinstance(record, dict):
            return None
        if not _record_is_compatible(record):
            return None
        try:
            return _release_path(package_id, record)
        except Exception:
            return None

    def get_music_settings(self):
        return {
            "enabled": bool(self.state.get("music_enabled", True)),
            "volume": float(self.state.get("music_volume", 0.8)),
        }

    def _set_status(self, status, detail="", progress=""):
        with self._lock:
            self.status = status
            self.detail = detail
            self.progress = progress
        self._notify_windows()

    def _set_busy(self, value):
        with self._lock:
            self.busy = bool(value)
        self._notify_windows()

    def register_window(self, window):
        self.windows.append(window)

    def unregister_window(self, window):
        try:
            self.windows.remove(window)
        except ValueError:
            pass

    def _notify_windows(self):
        for window in list(self.windows):
            try:
                window.refresh()
            except Exception:
                try:
                    self.windows.remove(window)
                except ValueError:
                    pass

    def snapshot(self):
        with self._lock:
            active = copy.deepcopy(self.state.get("active", {}) if self.state else {})
            manifest = copy.deepcopy(self.manifest)
            return {
                "status": self.status,
                "detail": self.detail,
                "progress": self.progress,
                "busy": self.busy,
                "source_url": self.state.get("source_url", "") if self.state else "",
                "music_enabled": bool(
                    self.state.get("music_enabled", True) if self.state else True
                ),
                "music_volume": float(
                    self.state.get("music_volume", 0.8) if self.state else 0.8
                ),
                "active": active,
                "manifest": manifest,
            }

    def set_source_url(self, source_url):
        source_url = str(source_url).strip()
        if source_url:
            _validate_https_url(source_url, "Manifest URL")
        with self._lock:
            if self.busy:
                raise SPPManagerError(
                    "Wait for the current manager task before changing source."
                )
            if isinstance(self.state.get("pending"), dict):
                raise SPPManagerError(
                    "Restart BombSquad before changing the package source."
                )
            changed = source_url != self.state.get("source_url", "")
            new_state = copy.deepcopy(self.state)
            new_state["source_url"] = source_url
            if changed:
                new_state["last_release_sequence"] = 0
                self.manifest = None
            _save_state(new_state)
            self.state = new_state
        if source_url:
            self._set_status("SOURCE SAVED", "Checking official SPP manifest...")
            self.check_for_updates()
        else:
            self._set_status(
                "SET SOURCE URL", "Add the public SPP manifest URL to continue."
            )

    def set_music_enabled(self, enabled):
        with self._lock:
            self.state["music_enabled"] = bool(enabled)
            _save_state(self.state)
        self._notify_windows()

    def adjust_music_volume(self, amount):
        with self._lock:
            current = float(self.state.get("music_volume", 0.8))
            current = min(1.0, max(0.0, round(current + amount, 2)))
            self.state["music_volume"] = current
            _save_state(self.state)
        self._notify_windows()

    def _run_background(self, status, detail, work, success):
        with self._lock:
            if self.busy:
                _screen_message("SPP Mods Manager is already working.")
                return False
            self.busy = True
        self._set_status(status, detail)

        def worker():
            try:
                result = work()
            except Exception as exc:
                message = str(exc)
                _append_log("Background task failed: %s" % message)

                def failure_call():
                    self._set_busy(False)
                    self.state["last_error"] = message
                    _save_state(self.state)
                    self._set_status("ERROR", message)
                    _screen_message("SPP Manager: %s" % message, color=(1.0, 0.2, 0.2))

                _push_from_thread(failure_call)
                return

            def success_call():
                self._set_busy(False)
                try:
                    success(result)
                except Exception as exc:
                    _append_log("Finishing task failed: %s" % exc)
                    self._set_status("ERROR", str(exc))

            _push_from_thread(success_call)

        thread = threading.Thread(target=worker, name="SPPModsManagerWorker")
        thread.daemon = True
        thread.start()
        return True

    def check_for_updates(self):
        if isinstance(self.state.get("pending"), dict):
            self._set_status(
                "RESTART REQUIRED",
                "Restart BombSquad before checking for another update.",
            )
            return
        source_url = str(self.state.get("source_url", "")).strip()
        if not source_url:
            self._set_status(
                "SET SOURCE URL", "Add the public SPP manifest URL to continue."
            )
            return

        def work():
            return _fetch_manifest(
                source_url,
                _game_api_version(),
                _game_build_number(),
                _paths()["cache"],
            )

        def success(manifest):
            if source_url != self.state.get("source_url", ""):
                raise ManifestError("Source changed while the check was running.")
            if manifest["release_sequence"] < int(
                self.state.get("last_release_sequence", 0)
            ):
                raise ManifestError("Manifest release sequence moved backwards.")
            self.manifest = manifest
            self.state["last_release_sequence"] = manifest["release_sequence"]
            self.state["last_check_time"] = int(time.time())
            self.state["last_error"] = ""
            _save_state(self.state)
            updates = self._packages_needing_update(manifest)
            incompatible = [
                item for item in manifest["packages"] if not item["compatible"]
            ]
            if updates or self._active_set_needs_sync(manifest):
                self._set_status(
                    "UPDATE AVAILABLE",
                    "%d package change(s) available."
                    % (len(updates) + int(self._active_set_needs_sync(manifest))),
                )
            elif incompatible and not [
                item for item in manifest["packages"] if item["compatible"]
            ]:
                self._set_status(
                    "GAME UPDATE REQUIRED",
                    "The available SPP pack does not support this game build.",
                )
            else:
                self._set_status("READY", "All compatible SPP packages are current.")

        self._run_background(
            "CHECKING", "Downloading the SPP package list...", work, success
        )

    def _packages_needing_update(self, manifest=None):
        manifest = manifest or self.manifest
        if not manifest:
            return []
        active = self.state.get("active", {})
        result = []
        for package in manifest["packages"]:
            if not package["compatible"]:
                continue
            installed = active.get(package["id"])
            if (
                package["id"] in self.repair_needed
                or not isinstance(installed, dict)
                or installed.get("version") != package["version"]
                or installed.get("archive_sha256")
                != package["archive_sha256"]
                or installed.get("source_url")
                != self.state.get("source_url", "")
                or not _record_is_compatible(installed)
            ):
                result.append(package)
        return result

    def _active_set_needs_sync(self, manifest=None):
        manifest = manifest or self.manifest
        if not manifest:
            return False
        desired = set(
            item["id"] for item in manifest["packages"] if item["compatible"]
        )
        return bool(set(self.state.get("active", {})) - desired)

    def install_all(self):
        if isinstance(self.state.get("pending"), dict):
            self._set_status(
                "RESTART REQUIRED",
                "Restart BombSquad before installing another update.",
            )
            return
        if not self.manifest:
            self._set_status(
                "CHECK FIRST", "Press CHECK and wait for the package list."
            )
            return
        packages = self._packages_needing_update()
        if not packages and not self._active_set_needs_sync():
            self._set_status("READY", "Nothing needs to be installed.")
            return
        source_url = self.state.get("source_url", "")
        progress_state = {"time": 0.0, "percent": -1, "name": ""}

        def progress(name, received, total):
            percent = int((float(received) / float(total)) * 100.0) if total else 0
            now = time.time()
            if (
                name == progress_state["name"]
                and percent == progress_state["percent"]
                and now - progress_state["time"] < 0.25
            ):
                return
            progress_state["time"] = now
            progress_state["percent"] = percent
            progress_state["name"] = name

            def update():
                self._set_status(
                    "DOWNLOADING",
                    "%s: %d%%" % (name, percent),
                    "%d / %d bytes" % (received, total),
                )

            _push_from_thread(update)

        def work():
            return self._prepare_transaction(packages, source_url, progress)

        def success(transaction):
            self._activate_transaction(transaction)

        self._run_background(
            "PREPARING UPDATE",
            "Downloading to a safe staging area...",
            work,
            success,
        )

    def _prepare_transaction(self, packages, source_url, progress):
        paths = _ensure_directories()
        total_download = sum(item["download_size"] for item in packages)
        total_unpacked = sum(item["unpacked_size"] for item in packages)
        if total_download > MAX_TRANSACTION_DOWNLOAD_BYTES:
            raise PackageError("Update download is larger than the safety limit.")
        if total_unpacked > MAX_TRANSACTION_UNPACKED_BYTES:
            raise PackageError("Update expands beyond the safety limit.")
        transaction_root = os.path.join(
            paths["staging"], "transaction-" + uuid.uuid4().hex
        )
        os.makedirs(transaction_root)
        prepared = []
        try:
            for package in packages:
                storage_dir = _new_storage_dir(package["version"])
                archive_path = os.path.join(
                    transaction_root, "pkg-" + package["id"] + ".zip.part"
                )
                package_stage = os.path.join(
                    transaction_root, "pkg-" + package["id"]
                )
                os.makedirs(package_stage)
                _download_archive(package, source_url, archive_path, progress)
                _safe_extract_archive(archive_path, package_stage, package)
                os.remove(archive_path)
                marker = {
                    "id": package["id"],
                    "version": package["version"],
                    "archive_sha256": package["archive_sha256"],
                    "storage_dir": storage_dir,
                    "installed_at": int(time.time()),
                }
                _atomic_write_json(os.path.join(package_stage, ".spp-install.json"), marker)
                prepared.append(
                    {
                        "package": copy.deepcopy(package),
                        "stage": package_stage,
                        "storage_dir": storage_dir,
                    }
                )
            desired_ids = sorted(
                item["id"]
                for item in self.manifest["packages"]
                if item["compatible"]
            )
            return {
                "root": transaction_root,
                "prepared": prepared,
                "source_url": source_url,
                "desired_ids": desired_ids,
            }
        except Exception:
            shutil.rmtree(transaction_root, ignore_errors=True)
            raise

    def _activate_transaction(self, transaction):
        if transaction.get("source_url") != self.state.get("source_url"):
            raise PackageError("Manifest source changed; discard and check again.")
        if isinstance(self.state.get("pending"), dict):
            raise PackageError(
                "Restart BombSquad before installing another update."
            )
        old_active = copy.deepcopy(self.state.get("active", {}))
        desired_ids = set(transaction.get("desired_ids", []))
        new_active = dict(
            (key, copy.deepcopy(value))
            for key, value in old_active.items()
            if key in desired_ids
        )
        transaction_root = transaction["root"]
        promoted = []
        try:
            for item in transaction["prepared"]:
                package = item["package"]
                package_root = _package_root(package["id"])
                if os.path.lexists(package_root):
                    mode = os.lstat(package_root).st_mode
                    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                        raise PackageError("Invalid package storage directory.")
                else:
                    os.makedirs(package_root, mode=0o700)
                destination = _managed_join(
                    package_root, item["storage_dir"]
                )
                if os.path.lexists(destination):
                    raise PackageError("Unique package destination already exists.")
                os.replace(item["stage"], destination)
                promoted.append(destination)
                new_active[package["id"]] = {
                    "name": package["name"],
                    "version": package["version"],
                    "storage_dir": item["storage_dir"],
                    "files": package["files"],
                    "entrypoint": package["entrypoint"],
                    "start_callable": package["start_callable"],
                    "load_order": package["load_order"],
                    "archive_sha256": package["archive_sha256"],
                    "source_url": self.state["source_url"],
                    "api_versions": package["api_versions"],
                    "minimum_build": package["minimum_build"],
                    "maximum_build": package["maximum_build"],
                }
            new_repair_needed = set(self.repair_needed)
            for item in transaction["prepared"]:
                new_repair_needed.discard(item["package"]["id"])
            new_repair_needed &= set(new_active)
            # Extraction already verified every byte in the worker thread;
            # normalize the complete state record here without hashing the
            # files a second time on BombSquad's logic thread.
            new_active = _validated_installed_map(
                new_active, "new active packages"
            )
            new_state = copy.deepcopy(self.state)
            new_state["active"] = new_active
            previous_repair_needed = sorted(self.repair_needed)
            if previous_repair_needed and self.state.get("rollback_active"):
                fallback_active = copy.deepcopy(
                    self.state["rollback_active"]
                )
                fallback_repair_needed = list(
                    self.state.get("rollback_repair_needed", [])
                )
            else:
                fallback_active = copy.deepcopy(old_active)
                fallback_repair_needed = previous_repair_needed
            new_state["pending"] = {
                "previous_active": old_active,
                "new_active": copy.deepcopy(new_active),
                "previous_repair_needed": previous_repair_needed,
                "new_repair_needed": sorted(new_repair_needed),
                "fallback_active": fallback_active,
                "fallback_repair_needed": fallback_repair_needed,
                "boot_attempted": False,
                "clear_prune_on_healthy": bool(
                    self.state.get("prune_disabled")
                ),
                "activated_at": int(time.time()),
                "attempt_time": 0,
            }
            new_state["repair_needed"] = sorted(new_repair_needed)
            new_state["last_error"] = ""
            _save_state(new_state)
            self.state = new_state
            self.repair_needed = new_repair_needed
            promoted = []
            self._set_status(
                "RESTART REQUIRED",
                "Update installed safely. Restart BombSquad to activate it.",
            )
            _screen_message(
                "SPP update installed. Restart BombSquad once.",
                color=(0.25, 1.0, 0.35),
            )
        except Exception:
            # Before the one atomic state commit, promoted immutable releases
            # are unreachable. Remove only those exact validated destinations.
            for destination in promoted:
                try:
                    if os.path.isdir(destination) and not os.path.islink(destination):
                        shutil.rmtree(destination)
                except OSError:
                    pass
            raise
        finally:
            try:
                safe_root = _managed_join(_paths()["staging"], os.path.basename(transaction_root))
                if safe_root == os.path.abspath(transaction_root):
                    shutil.rmtree(safe_root, ignore_errors=True)
            except Exception:
                pass

    def repair(self):
        if isinstance(self.state.get("pending"), dict):
            self._set_status(
                "RESTART REQUIRED",
                "Restart BombSquad before running Repair.",
            )
            return
        active = copy.deepcopy(self.state.get("active", {}))
        if not active:
            self._set_status("NOT INSTALLED", "There are no SPP packages to repair.")
            return

        def work():
            failures = []
            for package_id, record in active.items():
                valid, reason = _verify_installed_record(package_id, record)
                if not valid:
                    failures.append((package_id, reason))
            return failures

        def success(failures):
            if failures:
                self.repair_needed = set(item[0] for item in failures)
                self.state["repair_needed"] = sorted(self.repair_needed)
                _save_state(self.state)
                text = ", ".join("%s (%s)" % item for item in failures[:3])
                self._set_status(
                    "REPAIR NEEDED",
                    text + ". Press INSTALL/UPDATE twice to download clean files.",
                )
            else:
                self.repair_needed = set()
                self.state["repair_needed"] = []
                _save_state(self.state)
                self._set_status("READY", "All installed SPP files passed SHA-256.")

        self._run_background(
            "VERIFYING", "Checking every installed SPP file...", work, success
        )

    def rollback(self):
        if self.busy:
            _screen_message("Wait for the current SPP Manager task to finish.")
            return
        pending = self.state.get("pending")
        if isinstance(pending, dict):
            previous = pending["fallback_active"]
            previous_repair = pending["fallback_repair_needed"]
        else:
            previous = self.state.get("rollback_active")
            previous_repair = self.state.get("rollback_repair_needed", [])
        if not isinstance(previous, dict) or (
            not previous and not isinstance(pending, dict)
        ):
            self._set_status("NO ROLLBACK", "No older working pack is available.")
            return
        valid, reason = _verify_installed_map(
            previous, skip_ids=previous_repair
        )
        if not valid:
            self._set_status("ROLLBACK FAILED", reason)
            return
        current = copy.deepcopy(self.state.get("active", {}))
        current_repair = sorted(self.repair_needed)
        new_state = copy.deepcopy(self.state)
        new_state["active"] = copy.deepcopy(previous)
        new_state["repair_needed"] = list(previous_repair)
        if not isinstance(pending, dict):
            new_state["rollback_active"] = current
            new_state["rollback_repair_needed"] = current_repair
        new_state["pending"] = None
        new_state["last_error"] = "Manual rollback selected."
        _save_state(new_state)
        self.state = new_state
        self.repair_needed = set(previous_repair)
        self._set_status(
            "RESTART REQUIRED", "Previous SPP pack restored. Restart BombSquad."
        )
        _screen_message("SPP rollback ready. Restart BombSquad once.")


_SERVICE = _ManagerService()


def _plugin_on_app_running():
    _SERVICE.start()


def _plugin_on_app_shutdown():
    # A clean shutdown during the short boot-health window is success, not a
    # crash. This prevents a quick normal exit from triggering rollback.
    if _SERVICE.started and _SERVICE.state is not None:
        try:
            _SERVICE._mark_pending_healthy()
        except Exception as exc:
            _append_log("Could not finalize pending update at shutdown: %s" % exc)


def _widget_exists(widget):
    try:
        return bool(widget.exists())
    except Exception:
        return False


_WindowBase = getattr(_ui, "Window", object) if _ui is not None else object


class _ManagerWindow(_WindowBase):
    """Small conservative UI shared by API 7, 8, and 9."""

    def __init__(self, source_widget=None):
        if _ui is None:
            raise SPPManagerError("Manager UI is available only inside BombSquad.")
        self._source_widget = source_widget
        self._confirm_until = 0.0
        self._reset_confirm_timer = None
        self._width = 760
        self._height = 570
        scale = self._window_scale()
        kwargs = {
            "size": (self._width, self._height),
            "transition": "in_scale",
            "scale": scale,
        }
        if source_widget is not None:
            try:
                kwargs["scale_origin_stack_offset"] = (
                    source_widget.get_screen_space_center()
                )
            except Exception:
                pass
        root = _ui.containerwidget(**kwargs)
        _WindowBase.__init__(self, root_widget=root)
        self._root_widget = root
        self._status_text = None
        self._detail_text = None
        self._package_text = None
        self._volume_text = None
        self._source_field = None
        self._install_button = None
        self._check_button = None
        self._repair_button = None
        self._rollback_button = None
        self._music_checkbox = None
        self._build()
        _SERVICE.register_window(self)
        self.refresh()

    @staticmethod
    def _window_scale():
        try:
            if _MODERN_API:
                value = _ui.app.ui_v1.uiscale
            else:
                value = _ui.app.ui.uiscale
            name = str(getattr(value, "name", value)).lower()
            if "small" in name:
                return 1.30
            if "medium" in name:
                return 1.05
        except Exception:
            pass
        return 0.90

    def _build(self):
        root = self._root_widget
        _ui.textwidget(
            parent=root,
            position=(35, 520),
            size=(690, 34),
            text="SUPER PRO PLAYERS",
            color=(0.72, 0.25, 1.0),
            scale=1.35,
            h_align="center",
            v_align="center",
            maxwidth=620,
        )
        _ui.textwidget(
            parent=root,
            position=(35, 490),
            size=(690, 26),
            text="MODS MANAGER  •  %s" % MANAGER_VERSION,
            color=(0.70, 0.70, 0.80),
            scale=0.80,
            h_align="center",
            v_align="center",
        )
        close_button = _ui.buttonwidget(
            parent=root,
            position=(690, 510),
            size=(45, 42),
            label="X",
            color=(0.32, 0.12, 0.42),
            on_activate_call=self._close,
            autoselect=True,
        )
        _ui.containerwidget(
            edit=root,
            cancel_button=close_button,
            on_cancel_call=self._close,
        )

        self._status_text = _ui.textwidget(
            parent=root,
            position=(40, 442),
            size=(680, 36),
            text="STARTING",
            color=(0.95, 0.75, 1.0),
            scale=1.05,
            h_align="center",
            v_align="center",
        )
        self._detail_text = _ui.textwidget(
            parent=root,
            position=(55, 404),
            size=(650, 40),
            text="",
            color=(0.78, 0.78, 0.86),
            scale=0.65,
            h_align="center",
            v_align="center",
            maxwidth=640,
            max_height=38,
        )

        _ui.textwidget(
            parent=root,
            position=(45, 369),
            size=(150, 24),
            text="Manifest URL",
            color=(0.75, 0.55, 0.90),
            scale=0.68,
            h_align="left",
            v_align="center",
        )
        self._source_field = _ui.textwidget(
            parent=root,
            position=(45, 327),
            size=(570, 42),
            text="",
            editable=True,
            max_chars=400,
            color=(0.95, 0.95, 1.0),
            h_align="left",
            v_align="center",
            autoselect=True,
        )
        _ui.buttonwidget(
            parent=root,
            position=(625, 327),
            size=(90, 42),
            label="SAVE",
            color=(0.46, 0.15, 0.68),
            on_activate_call=self._save_source,
            autoselect=True,
        )

        self._package_text = _ui.textwidget(
            parent=root,
            position=(55, 211),
            size=(650, 104),
            text="",
            color=(0.86, 0.86, 0.92),
            scale=0.68,
            h_align="left",
            v_align="top",
            maxwidth=640,
            max_height=100,
        )

        self._install_button = _ui.buttonwidget(
            parent=root,
            position=(45, 145),
            size=(350, 56),
            label="INSTALL / UPDATE ALL",
            color=(0.54, 0.12, 0.82),
            textcolor=(1.0, 1.0, 1.0),
            on_activate_call=self._install_clicked,
            autoselect=True,
        )
        self._check_button = _ui.buttonwidget(
            parent=root,
            position=(410, 145),
            size=(145, 56),
            label="CHECK",
            color=(0.28, 0.18, 0.38),
            on_activate_call=_SERVICE.check_for_updates,
            autoselect=True,
        )
        self._repair_button = _ui.buttonwidget(
            parent=root,
            position=(570, 145),
            size=(145, 56),
            label="REPAIR",
            color=(0.28, 0.18, 0.38),
            on_activate_call=_SERVICE.repair,
            autoselect=True,
        )

        self._music_checkbox = _ui.checkboxwidget(
            parent=root,
            position=(50, 91),
            size=(190, 40),
            text="Victory music",
            value=True,
            textcolor=(0.86, 0.82, 0.94),
            on_value_change_call=_SERVICE.set_music_enabled,
            autoselect=True,
        )
        _ui.buttonwidget(
            parent=root,
            position=(255, 91),
            size=(48, 40),
            label="−",
            color=(0.28, 0.18, 0.38),
            on_activate_call=lambda: _SERVICE.adjust_music_volume(-0.10),
            autoselect=True,
        )
        self._volume_text = _ui.textwidget(
            parent=root,
            position=(307, 91),
            size=(90, 40),
            text="80%",
            color=(0.90, 0.90, 0.95),
            h_align="center",
            v_align="center",
            scale=0.72,
        )
        _ui.buttonwidget(
            parent=root,
            position=(401, 91),
            size=(48, 40),
            label="+",
            color=(0.28, 0.18, 0.38),
            on_activate_call=lambda: _SERVICE.adjust_music_volume(0.10),
            autoselect=True,
        )
        self._rollback_button = _ui.buttonwidget(
            parent=root,
            position=(535, 91),
            size=(180, 40),
            label="ROLL BACK",
            color=(0.38, 0.16, 0.25),
            on_activate_call=_SERVICE.rollback,
            autoselect=True,
        )
        _ui.textwidget(
            parent=root,
            position=(45, 34),
            size=(670, 40),
            text=(
                "Beta security: HTTPS + SHA-256 + staged rollback. "
                "No credentials. Updates never install during a match."
            ),
            color=(0.52, 0.52, 0.62),
            scale=0.52,
            h_align="center",
            v_align="center",
            maxwidth=660,
        )
        _ui.containerwidget(edit=root, selected_child=self._install_button)

    def _save_source(self):
        try:
            value = _ui.textwidget(query=self._source_field)
            _SERVICE.set_source_url(value)
        except Exception as exc:
            _screen_message(str(exc), color=(1.0, 0.2, 0.2))

    def _install_clicked(self):
        if _SERVICE.busy:
            _screen_message("SPP Mods Manager is already working.")
            return
        now = time.time()
        if now > self._confirm_until:
            self._confirm_until = now + 8.0
            _ui.buttonwidget(
                edit=self._install_button,
                label="PRESS AGAIN TO CONFIRM",
                color=(0.78, 0.20, 0.35),
            )
            _SERVICE._set_status(
                "CONFIRM INSTALL",
                "Packages can contain executable Python. Press again within 8 seconds.",
            )
            self._reset_confirm_timer = _app_timer(8.1, self._reset_confirmation)
            return
        self._confirm_until = 0.0
        self._reset_confirmation()
        _SERVICE.install_all()

    def _reset_confirmation(self):
        self._confirm_until = 0.0
        if _widget_exists(self._install_button):
            _ui.buttonwidget(
                edit=self._install_button,
                label="INSTALL / UPDATE ALL",
                color=(0.54, 0.12, 0.82),
            )

    def refresh(self):
        if not _widget_exists(self._root_widget):
            _SERVICE.unregister_window(self)
            return
        snapshot = _SERVICE.snapshot()
        status_colors = {
            "READY": (0.30, 1.0, 0.42),
            "UPDATE AVAILABLE": (1.0, 0.78, 0.20),
            "RESTART REQUIRED": (0.35, 0.78, 1.0),
            "ERROR": (1.0, 0.25, 0.25),
            "REPAIR NEEDED": (1.0, 0.45, 0.20),
            "GAME UPDATE REQUIRED": (1.0, 0.45, 0.20),
            "ROLLED BACK": (0.40, 0.85, 1.0),
        }
        color = status_colors.get(snapshot["status"], (0.95, 0.75, 1.0))
        _ui.textwidget(
            edit=self._status_text,
            text=snapshot["status"],
            color=color,
        )
        detail = snapshot["detail"]
        if snapshot["progress"]:
            detail += "  " + snapshot["progress"]
        _ui.textwidget(edit=self._detail_text, text=detail)
        current_source = _ui.textwidget(query=self._source_field)
        if not current_source:
            _ui.textwidget(edit=self._source_field, text=snapshot["source_url"])
        _ui.checkboxwidget(
            edit=self._music_checkbox, value=snapshot["music_enabled"]
        )
        _ui.textwidget(
            edit=self._volume_text,
            text="%d%%" % int(snapshot["music_volume"] * 100),
        )
        active = snapshot["active"]
        manifest = snapshot["manifest"]
        available = {}
        if manifest:
            available = dict(
                (item["id"], item)
                for item in manifest["packages"]
                if item["compatible"]
            )
        package_ids = ["spp-client-core", "spp-victory-audio"]
        for key in sorted(set(active) | set(available)):
            if key not in package_ids:
                package_ids.append(key)
        lines = [
            "Manager: %s     BombSquad: %s  |  API %d  |  build %d"
            % (
                MANAGER_VERSION,
                _game_version_string(),
                _game_api_version(),
                _game_build_number(),
            )
        ]
        for package_id in package_ids[:5]:
            installed = active.get(package_id, {}).get("version", "not installed")
            remote = available.get(package_id, {}).get("version", "—")
            name = available.get(package_id, {}).get(
                "name", active.get(package_id, {}).get("name", package_id)
            )
            lines.append("%s: %s   |   available: %s" % (name, installed, remote))
        _ui.textwidget(edit=self._package_text, text="\n".join(lines))

    def _close(self):
        _SERVICE.unregister_window(self)
        if _widget_exists(self._root_widget):
            _ui.containerwidget(edit=self._root_widget, transition="out_scale")


def show_manager(source_widget=None):
    """Open the manager window from a plugin loader or developer console."""
    if not _SERVICE.started:
        _SERVICE.start()
    return _ManagerWindow(source_widget)


# A tiny public helper for troubleshooting very old API-7 builds whose Plugins
# screen did not yet expose per-plugin Settings buttons.
def open_manager():
    return show_manager(None)


if __name__ == "__main__":
    print(MANAGER_NAME)
    print("Copy this file into BombSquad's mods folder, then run:")
    print("import super_pro_players_mod_manager as spp; spp.install()")
