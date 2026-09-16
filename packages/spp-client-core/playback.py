"""Local audio backends for the SPP client package.

Android uses Ballistica's existing OS music-player bridge.  Windows uses the
system MCI MP3 decoder through ``winmm.dll`` so no executable or third-party
Python dependency is downloaded.
"""

from __future__ import print_function

import os


class PlaybackError(RuntimeError):
    """A local playback backend could not play a verified cue."""


def _clamped_volume(value):
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.8


class AndroidMusicPlayer(object):
    """Play an app-readable music file through Ballistica's Android bridge."""

    def __init__(self, base_module):
        self._base = base_module

    def play(self, path, volume):
        self.stop()
        self._base.music_player_set_volume(_clamped_volume(volume))
        self._base.music_player_play(path)

    def set_volume(self, volume):
        self._base.music_player_set_volume(_clamped_volume(volume))

    def stop(self):
        self._base.music_player_stop()


class WindowsMCIPlayer(object):
    """Play MP3 cues with the Windows multimedia MCI service."""

    def __init__(self, ctypes_module=None):
        if ctypes_module is None:
            import ctypes as ctypes_module  # pylint: disable=import-outside-toplevel
        self._ctypes = ctypes_module
        try:
            self._winmm = ctypes_module.windll.winmm
        except (AttributeError, OSError) as exc:
            raise PlaybackError("Windows multimedia playback is unavailable.") from exc
        # Declare the wide-character WinMM signatures when running with real
        # ctypes. Some offline tests use a deliberately tiny stand-in.
        try:
            self._winmm.mciSendStringW.argtypes = [
                ctypes_module.c_wchar_p,
                ctypes_module.c_wchar_p,
                ctypes_module.c_uint,
                ctypes_module.c_void_p,
            ]
            self._winmm.mciSendStringW.restype = ctypes_module.c_uint
            self._winmm.mciGetErrorStringW.argtypes = [
                ctypes_module.c_uint,
                ctypes_module.c_wchar_p,
                ctypes_module.c_uint,
            ]
            self._winmm.mciGetErrorStringW.restype = ctypes_module.c_bool
        except (AttributeError, TypeError):
            pass
        self._alias = "sppv%x" % id(self)
        self._open = False

    def _command(self, command):
        result = int(self._winmm.mciSendStringW(command, None, 0, None))
        if result:
            try:
                buffer = self._ctypes.create_unicode_buffer(256)
                self._winmm.mciGetErrorStringW(result, buffer, len(buffer))
                detail = str(buffer.value).strip()
            except Exception:
                detail = "MCI error %d" % result
            raise PlaybackError(detail or "MCI error %d" % result)

    def play(self, path, volume):
        path = os.path.abspath(path)
        if '"' in path or "\n" in path or "\r" in path:
            raise PlaybackError("Unsafe audio path.")
        self.stop()
        self._open = True
        try:
            self._command(
                'open "%s" type mpegvideo alias %s' % (path, self._alias)
            )
            self.set_volume(volume)
            self._command("play %s from 0" % self._alias)
        except Exception:
            self.stop()
            raise

    def set_volume(self, volume):
        if self._open:
            level = int(round(_clamped_volume(volume) * 1000.0))
            self._command(
                "setaudio %s volume to %d" % (self._alias, level)
            )

    def stop(self):
        if not self._open:
            return
        try:
            self._command("stop %s" % self._alias)
        except PlaybackError:
            pass
        try:
            self._command("close %s" % self._alias)
        except PlaybackError:
            pass
        finally:
            self._open = False


def create_player(platform, base_module, ctypes_module=None):
    """Create the supported backend for a normalized Ballistica platform."""
    platform = str(platform).lower()
    if platform == "android":
        return AndroidMusicPlayer(base_module)
    if platform in ("windows", "win"):
        return WindowsMCIPlayer(ctypes_module=ctypes_module)
    raise PlaybackError(
        "Victory music currently supports BombSquad on Android and Windows."
    )
