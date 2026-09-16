"""Offline tests for manager-delivered SPP client packages."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "packages/spp-client-core"


def load_client_package():
    """Load the package under the same relative-import shape as the manager."""
    package_name = "_spp_client_package_tests"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PACKAGE_ROOT)]
    package.__package__ = package_name
    sys.modules[package_name] = package
    loaded = {}
    for short_name in ("protocol", "playback", "spp_client_core"):
        full_name = f"{package_name}.{short_name}"
        spec = importlib.util.spec_from_file_location(
            full_name, PACKAGE_ROOT / f"{short_name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = package_name
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        loaded[short_name] = module
    return loaded


MODULES = load_client_package()
PROTOCOL = MODULES["protocol"]
PLAYBACK = MODULES["playback"]
CORE = MODULES["spp_client_core"]


class ProtocolTests(unittest.TestCase):
    def test_round_trip_all_allowed_events(self):
        for team in ("SUPER", "PRO"):
            for stage in (1, 2, 3):
                payload = PROTOCOL.build_payload(
                    team,
                    stage,
                    "012345abcdef",
                    "0123456789abcdef",
                    "abcdef012345",
                )
                event = PROTOCOL.parse_chat_message(
                    f"SPP MUSIC: {payload}"
                )
                self.assertEqual(event["team"], team.lower())
                self.assertEqual(event["stage"], stage)
                self.assertEqual(event["target"], 3)

    def test_rejects_spoofed_or_mismatched_frames(self):
        valid = (
            "SPP MUSIC: SUPER wins round 1/3! | "
            "SPP1|SUPER|1|3|012345abcdef|0123456789abcdef|abcdef012345"
        )
        rejected = (
            "Player: " + valid,
            valid.replace("SPP MUSIC:", "Someone:"),
            valid.replace("SPP1|SUPER|1", "SPP1|PRO|1"),
            valid.replace("SPP1|SUPER|1", "SPP1|SUPER|3"),
            valid.replace("|3|012", "|9|012"),
            valid + " trailing",
            "SPP MUSIC: SPP1|SUPER|1|3|../../song.mp3",
        )
        for message in rejected:
            with self.subTest(message=message):
                self.assertIsNone(PROTOCOL.parse_chat_message(message))

    def test_builder_rejects_unknown_values(self):
        with self.assertRaises(ValueError):
            PROTOCOL.build_payload(
                "BLUE", 1, "012345abcdef", "0123456789abcdef", "abcdef012345"
            )
        with self.assertRaises(ValueError):
            PROTOCOL.build_payload(
                "SUPER", 4, "012345abcdef", "0123456789abcdef", "abcdef012345"
            )
        with self.assertRaises(ValueError):
            PROTOCOL.build_payload(
                "SUPER", 1, "bad", "0123456789abcdef", "abcdef012345"
            )
        with self.assertRaises(ValueError):
            PROTOCOL.build_hello("not-hex")


class CatalogTests(unittest.TestCase):
    def make_catalog(self, root: Path):
        audio = root / "audio"
        audio.mkdir()
        tracks = {}
        for team in ("super", "pro"):
            tracks[team] = {}
            for stage, duration in ((1, 4.0), (2, 7.0), (3, 18.0)):
                relative = f"audio/{team}_round_{stage}.mp3"
                (root / relative).write_bytes(b"ID3-test")
                tracks[team][str(stage)] = {
                    "path": relative,
                    "duration": duration,
                }
        (root / "catalog.json").write_text(
            json.dumps({"schema": 1, "protocol": "SPP1", "tracks": tracks}),
            encoding="utf-8",
        )

    def test_catalog_requires_all_six_contained_mp3_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_catalog(root)
            catalog = CORE._load_catalog(str(root))
            self.assertEqual(len(catalog), 6)
            self.assertEqual(catalog[("super", 3)]["duration"], 18.0)

            raw = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
            raw["tracks"]["pro"]["1"]["path"] = "../outside.mp3"
            (root / "catalog.json").write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValueError):
                CORE._load_catalog(str(root))

    def test_catalog_rejects_non_finite_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_catalog(root)
            raw = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
            raw["tracks"]["super"]["1"]["duration"] = float("nan")
            (root / "catalog.json").write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValueError):
                CORE._load_catalog(str(root))

    def test_chat_history_overlap_handles_native_rolling_window(self):
        old = ["a", "b", "c", "d"]
        new = ["c", "d", "e", "f"]
        self.assertEqual(CORE._new_messages(old, new), ["e", "f"])
        self.assertEqual(CORE._new_messages(old, ["new"]), ["new"])


class PlaybackTests(unittest.TestCase):
    def test_android_uses_internal_bridge_and_clamped_volume(self):
        calls = []
        base = types.SimpleNamespace(
            music_player_stop=lambda: calls.append(("stop",)),
            music_player_set_volume=lambda value: calls.append(("volume", value)),
            music_player_play=lambda path: calls.append(("play", path)),
        )
        player = PLAYBACK.AndroidMusicPlayer(base)
        player.play("/safe/cue.mp3", 2.0)
        self.assertEqual(
            calls,
            [("stop",), ("volume", 1.0), ("play", "/safe/cue.mp3")],
        )

    def test_windows_mci_uses_safe_unique_alias_and_never_a_shell(self):
        commands = []

        class WinMM:
            @staticmethod
            def mciSendStringW(command, _return, _length, _callback):
                commands.append(command)
                return 0

            @staticmethod
            def mciGetErrorStringW(_code, _buffer, _length):
                return 1

        fake_ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(winmm=WinMM()),
            create_unicode_buffer=lambda _size: types.SimpleNamespace(value=""),
        )
        player = PLAYBACK.WindowsMCIPlayer(ctypes_module=fake_ctypes)
        with mock.patch.object(PLAYBACK.os.path, "abspath", side_effect=lambda path: path):
            player.play("C:/BombSquad/cue.mp3", 0.75)
        player.stop()
        self.assertRegex(player._alias, r"^sppv[0-9a-f]+$")
        self.assertEqual(
            commands[0],
            'open "C:/BombSquad/cue.mp3" type mpegvideo alias %s'
            % player._alias,
        )
        self.assertIn(
            "setaudio %s volume to 750" % player._alias,
            commands,
        )
        self.assertEqual(commands[-1], "close %s" % player._alias)

    def test_windows_mci_open_failure_attempts_safe_alias_cleanup(self):
        commands = []

        class Buffer:
            def __init__(self):
                self.value = ""

            def __len__(self):
                return 256

        class WinMM:
            @staticmethod
            def mciSendStringW(command, _return, _length, _callback):
                commands.append(command)
                return 5 if command.startswith("open ") else 0

            @staticmethod
            def mciGetErrorStringW(_code, buffer, _length):
                buffer.value = "simulated MCI failure"
                return 1

        fake_ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(winmm=WinMM()),
            create_unicode_buffer=lambda _size: Buffer(),
        )
        player = PLAYBACK.WindowsMCIPlayer(ctypes_module=fake_ctypes)
        with mock.patch.object(
            PLAYBACK.os.path, "abspath", side_effect=lambda path: path
        ):
            with self.assertRaisesRegex(
                PLAYBACK.PlaybackError, "simulated MCI failure"
            ):
                player.play("C:/BombSquad/cue.mp3", 0.75)

        self.assertEqual(commands[-2:], [
            "stop %s" % player._alias,
            "close %s" % player._alias,
        ])
        self.assertFalse(player._open)


class RuntimeTests(unittest.TestCase):
    def test_start_twice_replaces_runtime_and_public_shutdown_cleans_up(self):
        events = []

        class FakeRuntime:
            def __init__(self, *_args):
                self.number = len([
                    event for event in events if event[0] == "start"
                ]) + 1

            def start(self):
                events.append(("start", self.number))

            def shutdown(self):
                events.append(("shutdown", self.number))

        context = {
            "package_id": "spp-client-core",
            "package_version": "test",
            "get_package_path": lambda _package_id: None,
            "get_music_settings": lambda: {
                "enabled": True,
                "volume": 0.8,
            },
        }
        CORE._runtime = None
        try:
            with mock.patch.object(
                CORE, "_import_game_modules", return_value=(object(), object(), True)
            ), mock.patch.object(CORE, "ClientRuntime", FakeRuntime):
                first = CORE.start(context)
                second = CORE.start(context)
                self.assertIsNot(first, second)
                CORE.shutdown()
            self.assertEqual(
                events,
                [
                    ("start", 1),
                    ("shutdown", 1),
                    ("start", 2),
                    ("shutdown", 2),
                ],
            )
            self.assertIsNone(CORE._runtime)
        finally:
            CORE._runtime = None

    def test_partial_start_failure_shuts_down_runtime(self):
        events = []

        class FailingRuntime:
            def __init__(self, *_args):
                pass

            def start(self):
                events.append("start")
                raise RuntimeError("simulated startup failure")

            def shutdown(self):
                events.append("shutdown")

        context = {
            "package_id": "spp-client-core",
            "package_version": "test",
            "get_package_path": lambda _package_id: None,
            "get_music_settings": lambda: {
                "enabled": True,
                "volume": 0.8,
            },
        }
        CORE._runtime = None
        try:
            with mock.patch.object(
                CORE, "_import_game_modules", return_value=(object(), object(), True)
            ), mock.patch.object(CORE, "ClientRuntime", FailingRuntime):
                with self.assertRaisesRegex(
                    RuntimeError, "simulated startup failure"
                ):
                    CORE.start(context)
            self.assertEqual(events, ["start", "shutdown"])
            self.assertIsNone(CORE._runtime)
        finally:
            CORE._runtime = None

    def test_android_stop_preserves_a_newer_activity_soundtrack(self):
        calls = []
        mode = object()
        music = types.SimpleNamespace(
            _music_mode=mode,
            music_types={mode: "New Activity Music"},
            do_play_music=lambda *_args, **_kwargs: self.fail(
                "older cue restored over newer activity music"
            ),
        )
        base = types.SimpleNamespace(
            app=types.SimpleNamespace(
                classic=types.SimpleNamespace(music=music),
                config={"Music Volume": 0.35},
            ),
            music_player_set_volume=lambda volume: calls.append(
                ("volume", volume)
            ),
        )
        runtime = CORE.ClientRuntime(
            {
                "package_id": "spp-client-core",
                "package_version": "test",
                "get_package_path": lambda _package_id: None,
                "get_music_settings": lambda: {
                    "enabled": True,
                    "volume": 0.8,
                },
            },
            base,
            types.SimpleNamespace(get_chat_messages=lambda: []),
            True,
        )
        runtime._platform = "android"
        runtime._player = types.SimpleNamespace(
            stop=lambda: self.fail("newer Android soundtrack was stopped")
        )
        runtime._playing = True
        runtime._music_restore = (music, mode, "Old Music", 0.9)

        runtime.stop_playback()

        self.assertFalse(runtime._playing)
        self.assertEqual(calls, [])

    def test_new_valid_event_plays_once_and_setting_can_stop_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            CatalogTests().make_catalog(root)
            chat = ["Old line"]
            timers = []
            outgoing = []

            class Timer:
                def __init__(self, delay, callback, repeat=False):
                    self.delay = delay
                    self.callback = callback
                    self.repeat = repeat
                    timers.append(self)

            regular_mode = object()

            class Music:
                def __init__(self):
                    self._music_mode = regular_mode
                    self.music_types = {regular_mode: "Scores"}
                    self.calls = []

                def do_play_music(self, value):
                    self.calls.append(value)
                    self.music_types[self._music_mode] = value

            music = Music()
            base = types.SimpleNamespace(
                AppTimer=Timer,
                screenmessage=lambda *_args, **_kwargs: None,
                config={"Music Volume": 0.9},
                app=types.SimpleNamespace(
                    classic=types.SimpleNamespace(music=music),
                    config={"Music Volume": 0.9},
                ),
            )
            scene = types.SimpleNamespace(
                get_chat_messages=lambda: list(chat),
                get_connection_to_host_info=lambda: {
                    "name": "🥊super pro players🥊",
                    "port": 43210,
                },
                chatmessage=outgoing.append,
            )
            settings = {"enabled": True, "volume": 0.65}
            context = {
                "package_id": "spp-client-core",
                "package_version": "test",
                "get_package_path": lambda package_id: (
                    str(root) if package_id == "spp-victory-audio" else None
                ),
                "get_music_settings": lambda: dict(settings),
            }
            played = []

            class FakePlayer:
                def play(self, path, volume):
                    played.append(("play", Path(path).name, volume))

                def set_volume(self, volume):
                    played.append(("volume", volume))

                def stop(self):
                    played.append(("stop",))

            runtime = CORE.ClientRuntime(context, base, scene, True)
            runtime._client_nonce = "0123456789abcdef"
            runtime._player = FakePlayer()
            runtime._ensure_catalog()
            runtime._poll_timer = Timer(0.2, runtime._poll, repeat=True)
            payload = PROTOCOL.build_payload(
                "SUPER",
                2,
                "012345abcdef",
                runtime._client_nonce,
                "abcdef012345",
            )
            chat.append("SPP MUSIC: " + payload)
            runtime._poll()
            self.assertEqual(played[0], ("play", "super_round_2.mp3", 0.65))
            self.assertEqual(music.calls, [None])
            self.assertEqual(
                outgoing, ["/sppmod hello SPP1 0123456789abcdef"]
            )

            runtime._poll()
            self.assertEqual([row for row in played if row[0] == "play"], [played[0]])

            settings["enabled"] = False
            runtime._poll()
            self.assertIn(("stop",), played)
            self.assertEqual(music.calls, [None, "Scores"])

    def test_events_are_ignored_on_another_server(self):
        chat = []

        class Timer:
            def __init__(self, *_args, **_kwargs):
                pass

        base = types.SimpleNamespace(
            AppTimer=Timer,
            screenmessage=lambda *_args, **_kwargs: None,
            app=types.SimpleNamespace(classic=None),
        )
        scene = types.SimpleNamespace(
            get_chat_messages=lambda: list(chat),
            get_connection_to_host_info=lambda: {"name": "Different Server"},
            chatmessage=lambda _message: self.fail("hello sent to another server"),
        )
        runtime = CORE.ClientRuntime(
            {
                "package_id": "spp-client-core",
                "package_version": "test",
                "get_package_path": lambda _package_id: None,
                "get_music_settings": lambda: {"enabled": True, "volume": 0.8},
            },
            base,
            scene,
            True,
        )
        runtime._player = types.SimpleNamespace(
            play=lambda *_args: self.fail("event played on another server"),
            set_volume=lambda *_args: None,
            stop=lambda: None,
        )
        runtime.start()
        chat.append(
            "SPP MUSIC: "
            + PROTOCOL.build_payload(
                "PRO",
                1,
                "012345abcdef",
                runtime._client_nonce,
                "abcdef012345",
            )
        )
        runtime._poll()


if __name__ == "__main__":
    unittest.main()
