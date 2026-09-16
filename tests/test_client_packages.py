"""Offline tests for manager-delivered SPP client packages."""

from __future__ import annotations

import importlib.util
from enum import Enum
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
    def test_strict_discovery_and_ready_frames(self):
        self.assertEqual(
            PROTOCOL.parse_control_message("SPP MUSIC: SPP1|DISCOVER|012345abcdef"),
            {"kind": "discover", "session_nonce": "012345abcdef"},
        )
        ready = "SPP MUSIC: SPP1|READY|012345abcdef|0123456789abcdef"
        self.assertEqual(PROTOCOL.parse_control_message(ready)["kind"], "ready")
        for message in (
            "Player: " + ready, ready + "\n", ready + " extra",
            ready.replace("SPP MUSIC:", "Player:"),
            ready.replace("012345abcdef", "ABCDEF012345"),
            ready.replace("0123456789abcdef", "short"),
            "SPP MUSIC: SPP1|DISCOVER|../../song.mp3", None, 12,
        ):
            self.assertIsNone(PROTOCOL.parse_control_message(message))

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
            chat.insert(-1, "SPP MUSIC: SPP1|READY|012345abcdef|0123456789abcdef")
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


class HandshakeRuntimeTests(unittest.TestCase):
    """Use real client code with a native-chat-shaped test transport."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        CatalogTests().make_catalog(self.root)
        self.chat = []
        self.outgoing = []
        self.notices = []
        self.audio = []
        self.timers = []
        self.now = 100.0
        self.host = {"name": "Old Witchly Name", "port": 43210}
        self.settings = {"enabled": True, "volume": 0.65}

        class Mode(Enum):
            REGULAR = "regular"
            TEST = "test"

        self.mode = Mode

        class Music:
            def __init__(inner):
                inner._music_mode = Mode.REGULAR
                inner.music_types = {Mode.REGULAR: "Scores", Mode.TEST: None}
                inner.calls = []

            def do_play_music(inner, value, continuous=False,
                              mode=Mode.REGULAR, testsoundtrack=None):
                inner.calls.append((value, continuous, mode, testsoundtrack))
                inner.music_types[mode] = value

        self.music = Music()

        def timer(delay, callback, repeat=False):
            result = types.SimpleNamespace(delay=delay, callback=callback, repeat=repeat)
            self.timers.append(result)
            return result

        self.base = types.SimpleNamespace(
            AppTimer=timer,
            screenmessage=lambda message, **_kwargs: self.notices.append(message),
            music_player_set_volume=lambda volume: self.audio.append(("os-volume", volume)),
            app=types.SimpleNamespace(
                config={"Music Volume": 0.9},
                classic=types.SimpleNamespace(music=self.music),
            ),
        )
        self.scene = types.SimpleNamespace(
            get_connection_to_host_info_2=lambda: self.host,
            get_chat_messages=lambda: list(self.chat),
            chatmessage=self.outgoing.append,
        )
        self.runtime = CORE.ClientRuntime(
            {
                "package_id": "spp-client-core", "package_version": CORE.CORE_VERSION,
                "get_package_path": lambda _package_id: str(self.root),
                "get_music_settings": lambda: dict(self.settings),
            }, self.base, self.scene, True,
        )
        self.runtime._platform = "windows"
        self.runtime._player = types.SimpleNamespace(
            play=lambda path, volume: self.audio.append(("play", Path(path).name, volume)),
            stop=lambda: self.audio.append(("stop",)),
            set_volume=lambda volume: self.audio.append(("volume", volume)),
        )
        self.runtime._ensure_catalog()
        self.runtime._client_nonce = "0123456789abcdef"
        patcher = mock.patch.object(CORE.time, "monotonic", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.runtime.shutdown)

    def add(self, message):
        self.chat.append(message)
        self.chat[:] = self.chat[-40:]
        self.runtime._poll()

    def connect(self):
        self.add("SPP MUSIC: SPP1|DISCOVER|012345abcdef")
        self.add("SPP MUSIC: SPP1|READY|012345abcdef|" + self.runtime._client_nonce)

    def event(self, team="SUPER", stage=1, event_id="abcdef012345",
              session="012345abcdef", client_nonce=None):
        self.add("SPP MUSIC: " + PROTOCOL.build_payload(
            team, stage, session, client_nonce or self.runtime._client_nonce, event_id
        ))

    def test_name_mismatch_discovery_ack_and_all_six_cues(self):
        self.connect()
        self.assertTrue(self.runtime._is_spp_connection())
        self.assertEqual(self.notices, ["SPP music connected"])
        for team_index, team in enumerate(("SUPER", "PRO")):
            for stage in (1, 2, 3):
                self.now += 2
                self.event(team, stage, "%012x" % (team_index * 3 + stage))
                self.assertEqual(self.audio[-1], (
                    "play", "%s_round_%d.mp3" % (team.lower(), stage), 0.65
                ))
        self.assertEqual(len([row for row in self.audio if row[0] == "play"]), 6)
        self.assertEqual(len(self.outgoing), 1)

    def test_no_arbitrary_server_hello_or_unsolicited_ack_authority(self):
        self.add("Player: SPP MUSIC: SPP1|DISCOVER|012345abcdef")
        self.add("SPP MUSIC: SPP1|READY|012345abcdef|0123456789abcdef")
        self.event()
        self.assertEqual(self.outgoing, [])
        self.assertEqual(self.audio, [])
        self.assertFalse(self.runtime._is_spp_connection())

    def test_matching_name_still_requires_ack(self):
        self.host["name"] = CORE.PUBLIC_PARTY_NAME
        self.runtime._poll()
        self.assertEqual(len(self.outgoing), 1)
        self.event()
        self.assertEqual(self.audio, [])
        self.add("SPP MUSIC: SPP1|READY|012345abcdef|" + self.runtime._client_nonce)
        self.event(event_id="abcdef012346")
        self.assertTrue(self.audio)

    def test_initial_registration_retries_after_delayed_account_auth(self):
        self.add("SPP MUSIC: SPP1|DISCOVER|012345abcdef")
        for advance in (1, 1, 1, 1, 1):
            self.now += advance
            self.add("SPP MUSIC: SPP1|DISCOVER|012345abcdef")
        self.assertEqual(len(self.outgoing), 1)
        self.now += 1
        self.runtime._poll()
        self.assertEqual(len(self.outgoing), 2)
        self.add("SPP MUSIC: SPP1|READY|012345abcdef|" + self.runtime._client_nonce)
        self.now += 7
        self.runtime._poll()
        self.assertEqual(len(self.outgoing), 2)
        self.now += 38
        self.runtime._poll()
        self.assertEqual(len(self.outgoing), 3)
        self.add("SPP MUSIC: SPP1|READY|012345abcdef|" + self.runtime._client_nonce)
        self.assertEqual(self.notices.count("SPP music connected"), 1)

    def test_timeout_warns_once_and_slows_retry_cadence(self):
        self.add("SPP MUSIC: SPP1|DISCOVER|012345abcdef")
        for delta in (6, 6, 6, 6):
            self.now += delta
            self.runtime._poll()
        self.assertEqual(len(self.outgoing), 3)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("waiting for server confirmation", self.notices[0])
        self.now += 33
        self.runtime._poll()
        self.assertEqual(len(self.outgoing), 4)

    def test_wrong_nonce_session_duplicate_and_stale_frames_do_not_play(self):
        self.add("SPP MUSIC: SPP1|DISCOVER|012345abcdef")
        self.add("SPP MUSIC: SPP1|READY|012345abcdef|ffffffffffffffff")
        self.add("SPP MUSIC: SPP1|READY|ffffffffffff|0123456789abcdef")
        self.event()
        self.assertEqual(self.audio, [])
        self.add("SPP MUSIC: SPP1|READY|012345abcdef|0123456789abcdef")
        self.event(client_nonce="ffffffffffffffff")
        self.event(session="ffffffffffff")
        self.assertEqual(self.audio, [])
        self.event()
        self.now += 2
        self.event()
        self.assertEqual(len([row for row in self.audio if row[0] == "play"]), 1)

    def test_disconnect_and_reconnect_ignore_retained_history(self):
        self.connect()
        self.event()
        old_nonce = self.runtime._client_nonce
        self.host = None
        self.runtime._poll()
        self.assertFalse(self.runtime._playing)
        self.assertFalse(self.runtime._is_spp_connection())
        self.add("SPP MUSIC: SPP1|DISCOVER|012345abcdef")
        self.assertEqual(len(self.outgoing), 1)
        self.host = {"name": "Old Witchly Name", "port": 43210}
        self.runtime._poll()
        self.runtime._poll()
        self.assertEqual(len(self.outgoing), 1)
        self.assertNotEqual(self.runtime._client_nonce, old_nonce)
        self.add("SPP MUSIC: SPP1|READY|012345abcdef|" + old_nonce)
        self.assertFalse(self.runtime._is_spp_connection())
        self.connect()
        self.event(client_nonce=old_nonce, event_id="abcdef012349")
        self.assertEqual(len([row for row in self.audio if row[0] == "play"]), 1)

    def test_scene_music_is_deferred_for_cue_then_latest_request_restored(self):
        for platform in ("windows", "android"):
            with self.subTest(platform=platform):
                self.runtime._platform = platform
                self.connect()
                self.now += 2
                self.event(event_id="00000000000" + ("1" if platform == "windows" else "2"))
                calls_before = len(self.music.calls)
                self.music.do_play_music("Scores delayed", continuous=True)
                self.music.do_play_music("Next Activity", continuous=True,
                                         testsoundtrack={"custom": "keep"})
                self.assertEqual(len(self.music.calls), calls_before)
                self.assertTrue(self.runtime._playing)
                self.runtime.stop_playback()
                self.assertEqual(self.music.calls[-1], (
                    "Next Activity", False, self.mode.REGULAR, {"custom": "keep"}
                ))
                self.assertNotIn("do_play_music", self.music.__dict__)

    def test_deferred_music_preserves_other_modes_and_positional_arguments(self):
        self.connect()
        self.event()
        self.music.do_play_music("Next regular", True, self.mode.REGULAR, {"a": 1})
        self.music.do_play_music("Test track", mode=self.mode.TEST)
        self.runtime.stop_playback()
        self.assertEqual(self.music.calls[-1], (
            "Next regular", False, self.mode.REGULAR, {"a": 1}
        ))
        self.assertEqual(self.music.music_types[self.mode.TEST], "Test track")

    def test_changed_active_music_mode_restores_its_own_request(self):
        self.connect()
        self.event()
        self.music.do_play_music("Next regular", mode=self.mode.REGULAR)
        self.music._music_mode = self.mode.TEST
        self.music.do_play_music("Test track", mode=self.mode.TEST)
        self.runtime.stop_playback()
        self.assertEqual(self.music.calls[-1][0], "Test track")
        self.assertIs(self.music.calls[-1][2], self.mode.TEST)
        self.assertEqual(self.music.music_types[self.mode.REGULAR], "Next regular")

    def test_timer_failure_and_normal_duration_both_release_music_guard(self):
        self.connect()
        self.event(stage=3)
        self.assertEqual(self.timers[-1].delay, 18.15)
        self.timers[-1].callback()
        self.assertFalse(self.runtime._playing)
        self.assertNotIn("do_play_music", self.music.__dict__)
        self.now += 2
        with mock.patch.object(CORE, "_make_timer", side_effect=RuntimeError("timer failure")):
            self.event(event_id="abcdef012349")
        self.assertFalse(self.runtime._playing)
        self.assertNotIn("do_play_music", self.music.__dict__)
        self.assertIn(("stop",), self.audio)
        self.assertTrue(any("timer is unavailable" in message for message in self.notices))

    def test_guard_respects_newer_third_party_hook(self):
        self.runtime._platform = "android"
        self.connect()
        self.event()
        external_hook = mock.Mock()
        self.music.do_play_music = external_hook
        self.runtime.stop_playback()
        self.assertIs(self.music.do_play_music, external_hook)
        external_hook.assert_not_called()
        self.assertNotIn(("stop",), self.audio)

    def test_captured_guard_becomes_passthrough_after_stop_or_shutdown(self):
        for finish in (self.runtime.stop_playback, self.runtime.shutdown):
            with self.subTest(finish=finish.__name__):
                self.connect()
                self.now += 2
                self.event(event_id="00000000000" + ("1" if finish.__name__ == "stop_playback" else "2"))
                captured = self.music.do_play_music
                def wrap(previous):
                    def newer(*args, **kwargs):
                        return previous(*args, **kwargs)
                    return newer
                newer_wrapper = wrap(captured)
                self.music.do_play_music = newer_wrapper
                finish()
                self.music.do_play_music("After cue", continuous=True)
                self.assertEqual(self.music.calls[-1][0:2], ("After cue", True))
                self.assertIs(self.music.do_play_music, newer_wrapper)

    def test_captured_guard_becomes_passthrough_after_playback_failure(self):
        self.connect()
        captured = []
        def fail_playback(_path, _volume):
            old_hook = self.music.do_play_music
            def newer_wrapper(*args, **kwargs):
                return old_hook(*args, **kwargs)
            captured.append(newer_wrapper)
            self.music.do_play_music = newer_wrapper
            raise RuntimeError("playback failure after another mod wrapped music")
        self.runtime._player.play = fail_playback
        self.event()
        self.assertFalse(self.runtime._playing)
        self.music.do_play_music("After failure")
        self.assertEqual(self.music.calls[-1][0], "After failure")
        self.assertIs(self.music.do_play_music, captured[0])

    def test_playback_error_restores_music_and_removes_guard(self):
        self.connect()
        self.runtime._player.play = mock.Mock(side_effect=RuntimeError("test failure"))
        self.event()
        self.assertFalse(self.runtime._playing)
        self.assertNotIn("do_play_music", self.music.__dict__)
        self.assertEqual(self.music.calls[-1][0], "Scores")
        self.assertTrue(any("could not play" in message for message in self.notices))

    def test_disabled_music_and_shutdown_remove_guard(self):
        self.connect()
        self.event()
        self.settings["enabled"] = False
        self.runtime._poll()
        self.assertNotIn("do_play_music", self.music.__dict__)
        self.assertFalse(self.runtime._playing)
        self.settings["enabled"] = True
        self.now += 2
        self.event(event_id="abcdef012349")
        self.runtime.shutdown()
        self.assertNotIn("do_play_music", self.music.__dict__)
        self.assertFalse(self.runtime._playing)


if __name__ == "__main__":
    unittest.main()
