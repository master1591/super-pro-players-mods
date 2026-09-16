"""SUPER PRO PLAYERS client runtime.

Loaded by SUPER PRO PLAYERS Mods Manager through its verified package ABI.
"""

from __future__ import print_function

from collections import deque
import json
import math
import os
import time
import uuid

from .playback import PlaybackError, create_player
from .protocol import build_hello, parse_chat_message


CORE_VERSION = "0.1.0-beta"
AUDIO_PACKAGE_ID = "spp-victory-audio"
CATALOG_FILENAME = "catalog.json"
POLL_SECONDS = 0.20
MAX_CATALOG_BYTES = 65536
MAX_SEEN_EVENTS = 256
MIN_EVENT_GAP_SECONDS = 1.0
HELLO_REFRESH_SECONDS = 45.0
PUBLIC_PARTY_NAME = "🥊super pro players🥊"

_runtime = None


def _import_game_modules():
    try:
        import babase as base
        import bascenev1 as scene

        return base, scene, True
    except ImportError:
        import ba as base

        return base, base, False


def _platform_name(modern):
    try:
        if modern:
            import _babase as internal
        else:
            import _ba as internal
        env = internal.env()
    except Exception:
        return "unknown"
    return str(env.get("platform", "unknown")).lower()


def _screenmessage(base, message, color=(0.7, 0.3, 1.0)):
    print("[SPP Client] %s" % message)
    try:
        base.screenmessage(message, color=color)
    except Exception:
        pass


def _make_timer(base, delay, callback, repeat=False):
    try:
        return base.AppTimer(delay, callback, repeat=repeat)
    except (AttributeError, TypeError):
        try:
            return base.Timer(
                delay,
                callback,
                repeat=repeat,
                timetype=base.TimeType.REAL,
            )
        except (AttributeError, TypeError):
            if repeat:
                raise RuntimeError("This BombSquad build has no app timer.")
            return base.apptimer(delay, callback)


def _connection_details(scene):
    for name in ("get_connection_to_host_info_2", "get_connection_to_host_info"):
        getter = getattr(scene, name, None)
        if getter is None:
            continue
        try:
            info = getter()
        except Exception:
            continue
        if isinstance(info, dict):
            name_value = str(info.get("name", ""))
            key = repr(sorted((str(key), repr(value)) for key, value in info.items()))
            return key, name_value
        name_value = str(getattr(info, "name", ""))
        stable = tuple(
            getattr(info, attr, None)
            for attr in ("name", "address", "port", "build_number")
        )
        return repr(stable), name_value
    return "disconnected", ""


def _new_messages(previous, current):
    """Return appended chat entries across the native 40-line rolling list."""
    if not previous:
        return list(current)
    maximum = min(len(previous), len(current))
    for overlap in range(maximum, 0, -1):
        if previous[-overlap:] == current[:overlap]:
            return list(current[overlap:])
    return list(current)


def _safe_audio_path(root, relative):
    if not isinstance(relative, str) or not relative.endswith(".mp3"):
        raise ValueError("Cue paths must name packaged MP3 files.")
    if "\\" in relative or relative.startswith("/"):
        raise ValueError("Unsafe cue path.")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Unsafe cue path.")
    root = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root, *parts))
    try:
        contained = os.path.commonpath((root, candidate)) == root
    except (AttributeError, ValueError):
        contained = candidate == root or candidate.startswith(root + os.sep)
    if not contained or not os.path.isfile(candidate):
        raise ValueError("Cue file is missing or outside its package.")
    return candidate


def _load_catalog(package_path):
    catalog_path = os.path.join(package_path, CATALOG_FILENAME)
    if os.path.getsize(catalog_path) > MAX_CATALOG_BYTES:
        raise ValueError("Victory-audio catalog is too large.")
    with open(catalog_path, "r", encoding="utf-8") as infile:
        raw = json.load(infile)
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise ValueError("Unsupported victory-audio catalog.")
    if raw.get("protocol") != "SPP1":
        raise ValueError("Victory-audio protocol mismatch.")
    tracks = raw.get("tracks")
    if not isinstance(tracks, dict) or set(tracks) != set(("super", "pro")):
        raise ValueError("Victory-audio catalog must contain both teams.")
    result = {}
    for team in ("super", "pro"):
        stages = tracks.get(team)
        if not isinstance(stages, dict) or set(stages) != set(("1", "2", "3")):
            raise ValueError("Each team must contain three victory cues.")
        for stage in (1, 2, 3):
            cue = stages[str(stage)]
            if not isinstance(cue, dict):
                raise ValueError("Invalid victory cue record.")
            try:
                duration = float(cue.get("duration"))
            except (TypeError, ValueError):
                raise ValueError("Invalid victory cue duration.")
            if not math.isfinite(duration) or duration < 1.0 or duration > 30.0:
                raise ValueError("Victory cue duration is outside safety limits.")
            result[(team, stage)] = {
                "path": _safe_audio_path(package_path, cue.get("path")),
                "duration": duration,
            }
    return result


class ClientRuntime(object):
    """Poll validated server events and play only verified packaged cues."""

    def __init__(self, context, base, scene, modern):
        self._context = dict(context)
        self._base = base
        self._scene = scene
        self._platform = _platform_name(modern)
        self._get_music_settings = context["get_music_settings"]
        self._get_package_path = context["get_package_path"]
        self._player = None
        self._catalog = None
        self._catalog_path = None
        self._poll_timer = None
        self._stop_timer = None
        self._play_token = 0
        self._playing = False
        self._last_volume = None
        self._last_event_time = -1000.0
        self._last_hello_time = -1000.0
        self._seen_events = set()
        self._seen_order = deque()
        self._connection, self._connection_name = _connection_details(scene)
        self._client_nonce = uuid.uuid4().hex[:16]
        self._messages = self._chat_messages()
        self._warned = set()
        self._active = True
        self._music_restore = None

    def start(self):
        try:
            self._player = create_player(self._platform, self._base)
        except PlaybackError as exc:
            self._warn_once("backend", str(exc))
        if self._player is not None:
            self._ensure_catalog()
        self._poll_timer = _make_timer(
            self._base, POLL_SECONDS, self._poll, repeat=True
        )
        self._send_hello(force=True)
        print(
            "[SPP Client] core %s ready on %s"
            % (CORE_VERSION, self._platform)
        )

    def _warn_once(self, key, message):
        if key in self._warned:
            return
        self._warned.add(key)
        _screenmessage(self._base, "SPP: %s" % message, color=(1.0, 0.35, 0.2))

    def _chat_messages(self):
        getter = getattr(self._scene, "get_chat_messages", None)
        if getter is None:
            return []
        try:
            messages = getter()
        except Exception:
            return []
        return [str(message) for message in messages][-40:]

    def _settings(self):
        try:
            raw = self._get_music_settings()
            enabled = bool(raw.get("enabled", True))
            volume = min(1.0, max(0.0, float(raw.get("volume", 0.8))))
            return enabled, volume
        except Exception:
            return True, 0.8

    def _is_spp_connection(self):
        return self._connection_name.casefold() == PUBLIC_PARTY_NAME.casefold()

    def _send_hello(self, force=False):
        if (
            not self._is_spp_connection()
            or self._player is None
            or self._catalog is None
        ):
            return
        now = time.monotonic()
        if not force and now - self._last_hello_time < HELLO_REFRESH_SECONDS:
            return
        sender = getattr(self._scene, "chatmessage", None)
        if sender is None:
            return
        try:
            sender(build_hello(self._client_nonce))
            self._last_hello_time = now
        except Exception:
            pass

    def _ensure_catalog(self):
        package_path = self._get_package_path(AUDIO_PACKAGE_ID)
        if not package_path:
            self._catalog = None
            self._catalog_path = None
            return False
        package_path = os.path.realpath(package_path)
        if self._catalog is not None and package_path == self._catalog_path:
            return True
        try:
            self._catalog = _load_catalog(package_path)
            self._catalog_path = package_path
            return True
        except Exception as exc:
            self._catalog = None
            self._catalog_path = None
            self._warn_once("catalog", "victory-audio package is invalid: %s" % exc)
            return False

    def _remember_event(self, event_key):
        if event_key in self._seen_events:
            return False
        self._seen_events.add(event_key)
        self._seen_order.append(event_key)
        while len(self._seen_order) > MAX_SEEN_EVENTS:
            self._seen_events.discard(self._seen_order.popleft())
        return True

    def _silence_game_music(self):
        """Pause regular scene music and retain enough state to restore it."""
        self._music_restore = None
        try:
            classic = self._base.app.classic
            music = classic.music
            mode = music._music_mode
            current = music.music_types.get(mode)
            try:
                original_volume = min(
                    1.0,
                    max(0.0, float(self._base.app.config.get("Music Volume", 1.0))),
                )
            except Exception:
                original_volume = 1.0
            try:
                music.do_play_music(None, mode=mode)
            except TypeError:
                music.do_play_music(None)
            self._music_restore = (music, mode, current, original_volume)
        except Exception:
            pass

    def _restore_game_music(self):
        state = self._music_restore
        self._music_restore = None
        if state is None:
            return
        music, mode, previous, original_volume = state
        try:
            still_owns_music_slot = (
                music._music_mode is mode
                and music.music_types.get(mode) is None
            )
        except Exception:
            still_owns_music_slot = False
        if self._platform == "android" and still_owns_music_slot:
            try:
                restore_volume = min(
                    1.0,
                    max(
                        0.0,
                        float(
                            self._base.app.config.get(
                                "Music Volume", original_volume
                            )
                        ),
                    ),
                )
                self._base.music_player_set_volume(restore_volume)
            except Exception:
                pass
        try:
            # A new activity may have selected its own music while the cue was
            # playing. Never overwrite that newer choice.
            if still_owns_music_slot:
                try:
                    music.do_play_music(previous, mode=mode)
                except TypeError:
                    music.do_play_music(previous)
        except Exception:
            pass

    def _play_event(self, event, volume):
        if self._player is None or not self._ensure_catalog():
            return
        if event.get("client_nonce") != self._client_nonce:
            return
        event_key = (event["session_nonce"], event["event_id"])
        if not self._remember_event(event_key):
            return
        now = time.monotonic()
        if now - self._last_event_time < MIN_EVENT_GAP_SECONDS:
            return
        cue = self._catalog.get((event["team"], event["stage"]))
        if cue is None:
            return
        self.stop_playback()
        self._silence_game_music()
        try:
            self._player.play(cue["path"], volume)
        except Exception as exc:
            self._restore_game_music()
            self._warn_once("playback", "could not play victory music: %s" % exc)
            return
        self._last_event_time = now
        self._last_volume = volume
        self._playing = True
        self._play_token += 1
        token = self._play_token
        self._stop_timer = _make_timer(
            self._base,
            cue["duration"] + 0.15,
            lambda: self._stop_if_current(token),
        )

    def _stop_if_current(self, token):
        if token == self._play_token:
            self.stop_playback()

    def stop_playback(self):
        self._play_token += 1
        self._stop_timer = None
        stop_owned_audio = True
        if self._platform == "android" and self._music_restore is not None:
            # Android victory cues share Ballistica's OS music player. If a
            # newer activity has already selected its own soundtrack, the cue
            # has been replaced and calling stop() here would silence that
            # newer music.
            music, mode, _previous, _volume = self._music_restore
            try:
                stop_owned_audio = (
                    music._music_mode is mode
                    and music.music_types.get(mode) is None
                )
            except Exception:
                stop_owned_audio = False
        if self._player is not None and self._playing and stop_owned_audio:
            try:
                self._player.stop()
            except Exception:
                pass
        self._playing = False
        self._restore_game_music()

    def shutdown(self):
        self._active = False
        self.stop_playback()
        self._poll_timer = None

    def _poll(self):
        if not self._active:
            return
        enabled, volume = self._settings()
        if not enabled:
            self.stop_playback()
        elif self._playing and volume != self._last_volume:
            try:
                self._player.set_volume(volume)
                self._last_volume = volume
            except Exception:
                pass

        connection, connection_name = _connection_details(self._scene)
        current = self._chat_messages()
        if connection != self._connection:
            self.stop_playback()
            self._connection = connection
            self._connection_name = connection_name
            self._client_nonce = uuid.uuid4().hex[:16]
            self._last_hello_time = -1000.0
            self._messages = current
            self._seen_events.clear()
            self._seen_order.clear()
            self._send_hello(force=True)
            return

        self._connection_name = connection_name
        self._send_hello()

        additions = _new_messages(self._messages, current)
        self._messages = current
        if not enabled or not self._is_spp_connection():
            return
        for message in additions:
            event = parse_chat_message(message)
            if event is not None:
                self._play_event(event, volume)


def shutdown():
    """Stop the active runtime when a manager supports package lifecycles."""
    global _runtime
    if _runtime is not None:
        _runtime.shutdown()
        _runtime = None


def start(context):
    """Start the package through the manager's stable context contract."""
    global _runtime
    required = (
        "package_id",
        "package_version",
        "get_package_path",
        "get_music_settings",
    )
    if not isinstance(context, dict) or any(name not in context for name in required):
        raise RuntimeError("Invalid SPP Mods Manager package context.")
    if context["package_id"] != "spp-client-core":
        raise RuntimeError("SPP client core loaded under the wrong package id.")
    base, scene, modern = _import_game_modules()
    shutdown()
    runtime = ClientRuntime(context, base, scene, modern)
    try:
        runtime.start()
    except Exception:
        runtime.shutdown()
        raise
    _runtime = runtime
    return runtime
