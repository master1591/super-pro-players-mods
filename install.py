"""One-command installer for SUPER PRO PLAYERS Mods Manager.

Run this file inside BombSquad. It downloads one pinned manager build,
verifies its SHA-256 digest and Python syntax, installs it atomically in the
game-provided user mods directory, and rolls back the previous manager if
activation does not finish successfully.
"""

from __future__ import print_function

import hashlib
import os
import stat
import sys
import tempfile
import types
import urllib.error
import urllib.parse
import urllib.request


INSTALLER_VERSION = "0.1.2-beta"
MANAGER_FILENAME = "super_pro_players_mod_manager.py"
MANAGER_MODULE = "super_pro_players_mod_manager"
MANAGER_URL = (
    "https://raw.githubusercontent.com/master1591/"
    "super-pro-players-mods/ff8639cebd191b28d21eb0420df00503395750df/"
    "super_pro_players_mod_manager.py"
)
MANAGER_SHA256 = (
    "c6e25aad390a99fe541f38dbc37443848c93fef9e5b06f839a45756af3606194"
)
MAX_MANAGER_BYTES = 2 * 1024 * 1024
NETWORK_TIMEOUT_SECONDS = 20


class InstallError(Exception):
    """A safe, user-displayable installation failure."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so the pinned URL cannot switch hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        raise InstallError("The SPP manager download unexpectedly redirected.")


def _screen(message, good=False):
    text = "[SPP Installer] " + str(message)
    print(text)
    color = (0.25, 1.0, 0.35) if good else (1.0, 0.35, 0.20)
    for module_name in ("babase", "ba", "_babase", "_ba"):
        try:
            module = __import__(module_name)
            module.screenmessage(text, color=color)
            break
        except Exception:
            pass


def _env_value(name):
    for module_name in ("babase", "ba"):
        try:
            module = __import__(module_name)
            env_object = module.app.env
            value = getattr(env_object, name, None)
            if value is not None:
                return value
        except Exception:
            pass
    for module_name in ("_babase", "_ba"):
        try:
            module = __import__(module_name)
            env_dict = module.env()
            value = env_dict.get(name)
            if value is not None:
                return value
        except Exception:
            pass
    return None


def _mods_directory():
    value = _env_value("python_directory_user")
    if not isinstance(value, str) or not value:
        raise InstallError(
            "BombSquad did not provide its user mods directory. "
            "Run this command inside BombSquad."
        )
    path = os.path.abspath(os.path.normpath(value))
    if os.path.lexists(path):
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise InstallError("BombSquad's mods path is not a normal folder.")
    else:
        os.makedirs(path, mode=0o700)
    return path


def _validate_api():
    value = _env_value("api_version")
    try:
        api_version = int(value)
    except (TypeError, ValueError):
        api_version = 0
    if api_version not in (6, 7, 8, 9):
        raise InstallError(
            "Unsupported BombSquad API %s; supported APIs are 6, 7, 8, and 9."
            % api_version
        )
    return api_version


def _plain_file_or_missing(path, label):
    if not os.path.lexists(path):
        return False
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise InstallError("%s is not a normal file: %s" % (label, path))
    return True


def _download_manager():
    parsed = urllib.parse.urlsplit(MANAGER_URL)
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != "raw.githubusercontent.com"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise InstallError("The built-in SPP manager URL is invalid.")

    request = urllib.request.Request(
        MANAGER_URL,
        headers={
            "User-Agent": "SPP-Mods-Installer/%s" % INSTALLER_VERSION,
            "Accept": "text/plain, application/octet-stream",
            "Accept-Encoding": "identity",
        },
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        response = opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS)
        with response:
            if response.getcode() != 200 or response.geturl() != MANAGER_URL:
                raise InstallError(
                    "The SPP download server returned an unexpected response."
                )
            encoding = response.headers.get(
                "Content-Encoding", "identity"
            ).lower()
            if encoding not in ("", "identity"):
                raise InstallError(
                    "The SPP download server used unexpected compression."
                )
            length_text = response.headers.get("Content-Length")
            if length_text is not None:
                try:
                    length = int(length_text)
                except (TypeError, ValueError):
                    raise InstallError("SPP download reported an invalid size.")
                if length < 1 or length > MAX_MANAGER_BYTES:
                    raise InstallError("SPP manager download is an unsafe size.")

            chunks = []
            total = 0
            digest = hashlib.sha256()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MANAGER_BYTES:
                    raise InstallError(
                        "SPP manager download exceeded its size limit."
                    )
                chunks.append(chunk)
                digest.update(chunk)
    except InstallError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise InstallError("Could not download SPP manager: %s" % exc)

    if total == 0:
        raise InstallError("SPP manager download was empty.")
    if digest.hexdigest() != MANAGER_SHA256:
        raise InstallError(
            "SPP manager security check failed; nothing was installed."
        )
    data = b"".join(chunks)
    try:
        compile(data, MANAGER_FILENAME, "exec")
    except Exception as exc:
        raise InstallError("Downloaded SPP manager is invalid Python: %s" % exc)
    return data


def _write_temporary(directory, prefix, payload):
    descriptor, path = tempfile.mkstemp(
        prefix=prefix, suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as outfile:
            descriptor = -1
            outfile.write(payload)
            outfile.flush()
            try:
                os.fsync(outfile.fileno())
            except OSError:
                pass
        return path
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _read_regular_file(path, label):
    if not _plain_file_or_missing(path, label):
        raise InstallError("%s disappeared during installation." % label)
    with open(path, "rb") as infile:
        return infile.read(MAX_MANAGER_BYTES + 1)


def _restore_interrupted(destination, pending):
    if not os.path.lexists(pending):
        return
    _plain_file_or_missing(pending, "Pending rollback")
    _plain_file_or_missing(destination, "Manager destination")
    os.replace(pending, destination)
    _screen("Recovered the previous manager from an interrupted installation.")


def _execute_verified(payload, destination):
    code = compile(payload, destination, "exec")
    module = types.ModuleType(MANAGER_MODULE)
    module.__file__ = destination
    module.__package__ = ""
    module.__spec__ = None
    missing = object()
    previous = sys.modules.get(MANAGER_MODULE, missing)
    sys.modules[MANAGER_MODULE] = module
    try:
        exec(code, module.__dict__)
        entrypoint = getattr(module, "install", None)
        if not callable(entrypoint):
            raise InstallError("Verified SPP manager has no install() function.")
        result = entrypoint()
    except Exception:
        if previous is missing:
            sys.modules.pop(MANAGER_MODULE, None)
        else:
            sys.modules[MANAGER_MODULE] = previous
        raise
    return result


def install():
    """Download, verify, atomically install, and activate the manager."""
    _validate_api()
    mods = _mods_directory()
    destination = os.path.join(mods, MANAGER_FILENAME)
    backup = destination + ".previous"
    pending = destination + ".rollback-pending"
    stage = None
    had_previous = False

    _plain_file_or_missing(destination, "Manager destination")
    _plain_file_or_missing(backup, "Manager backup")
    _plain_file_or_missing(pending, "Pending rollback")
    _restore_interrupted(destination, pending)
    _screen("Downloading and verifying SPP Mods Manager...")
    payload = _download_manager()

    stage = _write_temporary(mods, ".spp-manager-new-", payload)
    try:
        had_previous = os.path.lexists(destination)
        if had_previous:
            old_payload = _read_regular_file(destination, "Existing manager")
            if len(old_payload) > MAX_MANAGER_BYTES:
                raise InstallError("The existing SPP manager is unexpectedly large.")
            pending_temp = _write_temporary(
                mods, ".spp-manager-old-", old_payload
            )
            try:
                os.replace(pending_temp, pending)
            finally:
                if os.path.exists(pending_temp):
                    os.remove(pending_temp)

        os.replace(stage, destination)
        stage = None
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass

        try:
            _execute_verified(payload, destination)
        except Exception:
            if had_previous:
                os.replace(pending, destination)
            elif os.path.isfile(destination):
                current = _read_regular_file(destination, "New manager")
                if hashlib.sha256(current).hexdigest() == MANAGER_SHA256:
                    os.remove(destination)
            raise

        warning = ""
        if had_previous:
            try:
                os.replace(pending, backup)
            except OSError as exc:
                warning = (
                    " The previous-version backup remains at %s (%s)."
                    % (pending, exc)
                )
        _screen(
            "Installed successfully. Fully restart BombSquad once." + warning,
            good=True,
        )
        return destination
    finally:
        if stage is not None and os.path.exists(stage):
            try:
                os.remove(stage)
            except OSError:
                pass


if __name__ == "__main__":
    try:
        install()
    except Exception as error:
        _screen("Installation stopped safely: %s" % error)
        raise
