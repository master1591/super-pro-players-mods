"""Local audio backends for the SPP client package.

Android uses Ballistica's existing OS music-player bridge.  Windows uses the
system MCI MP3 decoder through ``winmm.dll`` so no executable or third-party
Python dependency is downloaded.
"""

from __future__ import print_function

import os
from collections import deque
import threading
import time


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
        self._path = None

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

    def prepare(self, path):
        """Open a decoder without starting playback (called by our worker)."""
        path = os.path.abspath(path)
        if '"' in path or "\n" in path or "\r" in path:
            raise PlaybackError("Unsafe audio path.")
        if self._open and self._path == path:
            return
        self.stop()
        self._open = True
        try:
            self._command(
                'open "%s" type mpegvideo alias %s' % (path, self._alias)
            )
            self._path = path
        except Exception:
            self.stop()
            raise

    def play(self, path, volume):
        self.prepare(path)
        self.start_prepared(volume)

    def start_prepared(self, volume):
        if not self._open:
            raise PlaybackError("Audio decoder is not ready.")
        self.set_volume(volume)
        self._command("play %s from 0" % self._alias)

    def pause(self):
        """Stop audio while keeping its decoder ready for the next round."""
        if self._open:
            self._command("stop %s" % self._alias)

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
            self._path = None


class QueuedWindowsMCIPlayer(object):
    """Serialize native decoder work away from BombSquad's logic thread.

    One replaceable command and a bounded completion mailbox keep repeated
    events from building a queue. Cached decoders never play during preload.
    Only plain paths/numbers cross threads; all engine calls remain in core.
    """

    MAX_PREPARED = 6
    MAX_START_DELAY = 3.0

    def __init__(self, ctypes_module=None, player_factory=None):
        self._factory = player_factory or (
            lambda: WindowsMCIPlayer(ctypes_module=ctypes_module)
        )
        self._condition = threading.Condition()
        self._generation = 0
        self._desired = (0, None, 0.8, 0.0)
        self._preload = ()
        self._events = deque(maxlen=16)
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="SPP victory audio", daemon=True
        )
        self._thread.start()

    @staticmethod
    def _validated_path(path):
        path = os.path.abspath(path)
        if '"' in path or "\n" in path or "\r" in path:
            raise PlaybackError("Unsafe audio path.")
        return path

    def prepare(self, paths):
        paths = tuple(dict.fromkeys(self._validated_path(path) for path in paths))
        if len(paths) > self.MAX_PREPARED:
            raise PlaybackError("Too many audio files to prepare.")
        with self._condition:
            if not self._closed:
                self._preload = paths
                self._condition.notify()

    def play(self, path, volume):
        path = self._validated_path(path)
        with self._condition:
            if self._closed:
                raise PlaybackError("Audio player has shut down.")
            self._generation += 1
            self._desired = (
                self._generation, path, _clamped_volume(volume), time.monotonic()
            )
            self._condition.notify()
            return self._generation

    def set_volume(self, volume):
        with self._condition:
            generation, path, _old_volume, requested = self._desired
            self._desired = (generation, path, _clamped_volume(volume), requested)
            self._condition.notify()

    def stop(self):
        with self._condition:
            self._generation += 1
            self._desired = (self._generation, None, 0.8, 0.0)
            self._condition.notify()

    def shutdown(self):
        # Never join a decoder thread from BombSquad's logic callback.
        with self._condition:
            self._closed = True
            self._generation += 1
            self._condition.notify()

    def poll_events(self):
        with self._condition:
            events = list(self._events)
            self._events.clear()
            return events

    def _is_current(self, generation):
        with self._condition:
            return not self._closed and self._desired[0] == generation

    def _report(self, kind, generation, detail):
        with self._condition:
            if not self._closed and self._desired[0] == generation:
                self._events.append((kind, generation, detail))
                self._condition.notify_all()

    def _run(self):
        prepared = {}
        attempted = set()
        last_preload = None
        processed = -1
        active = None
        active_volume = None

        def pause_active():
            nonlocal active
            if active is not None:
                try:
                    active.pause()
                except Exception:
                    try:
                        active.stop()
                    except Exception:
                        pass
                active = None

        def obtain(path):
            if path not in prepared:
                if len(prepared) >= self.MAX_PREPARED:
                    old_path = next(iter(prepared))
                    prepared.pop(old_path).stop()
                decoder = self._factory()
                try:
                    decoder.prepare(path)
                except Exception:
                    decoder.stop()
                    raise
                prepared[path] = decoder
            else:
                # A previous stop error may have closed this cached handle.
                prepared[path].prepare(path)
            return prepared[path]

        try:
            while True:
                with self._condition:
                    if self._closed:
                        break
                    generation, path, volume, requested = self._desired
                    preload = self._preload
                    if preload != last_preload:
                        attempted.clear()
                        last_preload = preload
                    # Never let a speculative decoder open delay a STOP for
                    # the cue already playing. Preload resumes between cues.
                    candidate = (
                        next((p for p in preload if p not in attempted), None)
                        if active is None else None
                    )
                    if generation == processed and (
                        active is None or volume == active_volume
                    ) and candidate is None:
                        self._condition.wait()
                        continue
                if generation != processed:
                    pause_active()
                    processed = generation
                    if path is not None:
                        try:
                            decoder = obtain(path)
                            if not self._is_current(generation):
                                continue
                            if time.monotonic() - requested > self.MAX_START_DELAY:
                                raise PlaybackError("Audio decoder was not ready in time.")
                            active = decoder
                            decoder.start_prepared(volume)
                            active_volume = volume
                            if not self._is_current(generation):
                                pause_active()
                                continue
                            self._report("started", generation, time.monotonic())
                        except Exception as exc:
                            pause_active()
                            self._report("error", generation, str(exc))
                    continue
                if active is not None and volume != active_volume:
                    try:
                        active.set_volume(volume)
                        active_volume = volume
                    except Exception as exc:
                        pause_active()
                        self._report("error", generation, str(exc))
                    continue
                if candidate is not None:
                    attempted.add(candidate)
                    try:
                        obtain(candidate)
                    except Exception:
                        # A failed speculative open is retried/reported if the
                        # user actually needs that cue; no startup spam.
                        pass
        finally:
            for decoder in prepared.values():
                try:
                    decoder.stop()
                except Exception:
                    pass


def create_player(platform, base_module, ctypes_module=None):
    """Create the supported backend for a normalized Ballistica platform."""
    platform = str(platform).lower()
    if platform == "android":
        return AndroidMusicPlayer(base_module)
    if platform in ("windows", "win"):
        return QueuedWindowsMCIPlayer(ctypes_module=ctypes_module)
    raise PlaybackError(
        "Victory music currently supports BombSquad on Android and Windows."
    )
