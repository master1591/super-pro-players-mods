"""Regressions for nonblocking audio and soundtrack ownership across a session."""

from enum import Enum
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest import mock

import test_client_packages as package_tests

CORE = package_tests.CORE
PLAYBACK = package_tests.PLAYBACK


class QueuedPlaybackTests(unittest.TestCase):
    def make_player(self, block_path=None, fail=False):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        commands = []

        class Decoder:
            def __init__(self):
                self.path = None

            def prepare(self, path):
                self.path = path
                commands.append(("prepare", path, threading.get_ident()))
                if path == block_path:
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("test decoder wait expired")
                if fail:
                    raise RuntimeError("decoder unavailable")

            def start_prepared(self, volume):
                commands.append(("play", self.path, threading.get_ident(), volume))

            def pause(self):
                commands.append(("pause", self.path, threading.get_ident()))

            def set_volume(self, volume):
                commands.append(("volume", self.path, threading.get_ident(), volume))

            def stop(self):
                commands.append(("close", self.path, threading.get_ident()))

        player = PLAYBACK.QueuedWindowsMCIPlayer(player_factory=Decoder)

        def cleanup():
            release.set()
            player.shutdown()
            player._thread.join(3)
            self.assertFalse(player._thread.is_alive())

        self.addCleanup(cleanup)
        return player, entered, release, commands

    def wait_events(self, player):
        with player._condition:
            # Native work is short except for explicit test gates. Waiting on
            # a condition avoids a timing-dependent sleep or busy loop.
            self.assertTrue(player._condition.wait_for(
                lambda: bool(player._events), timeout=2
            ))
        return player.poll_events()

    def test_open_does_not_block_caller_and_queue_replaces_stale_requests(self):
        path_a, path_b = "/a.mp3", "/b.mp3"
        player, entered, release, commands = self.make_player(block_path=path_a)
        player.play(path_a, 0.5)
        self.assertTrue(entered.wait(2))
        # The decoder is still blocked, but all public methods return. The
        # thousand requests occupy one desired-command slot, not a queue.
        for _ in range(1000):
            last = player.play(path_b, 0.7)
        player.set_volume(0.3)
        self.assertFalse(release.is_set())
        self.assertEqual(len(player._desired), 4)
        release.set()
        events = self.wait_events(player)
        self.assertEqual([(kind, token) for kind, token, _ in events], [("started", last)])
        plays = [command for command in commands if command[0] == "play"]
        self.assertEqual([(c[1], c[3]) for c in plays], [(path_b, 0.3)])
        self.assertTrue(all(command[2] != threading.get_ident() for command in commands))

    def test_disconnect_during_open_never_starts_stale_audio(self):
        player, entered, release, commands = self.make_player(block_path="/a.mp3")
        player.play("/a.mp3", 0.8)
        self.assertTrue(entered.wait(2))
        player.stop()
        player.shutdown()
        self.assertFalse(release.is_set())
        release.set()
        player._thread.join(2)
        self.assertFalse(player._thread.is_alive())
        self.assertFalse(any(command[0] == "play" for command in commands))
        self.assertTrue(any(command[0] == "close" for command in commands))
        self.assertEqual(player.poll_events(), [])

    def test_preload_is_silent_bounded_and_worker_closes_handles(self):
        player, entered, release, commands = self.make_player(block_path="/a.mp3")
        player.prepare(["/a.mp3", "/b.mp3"])
        self.assertTrue(entered.wait(2))
        self.assertFalse(any(command[0] == "play" for command in commands))
        with self.assertRaises(PLAYBACK.PlaybackError):
            player.prepare(["/%d.mp3" % number for number in range(7)])
        token = player.play("/a.mp3", 0.8)
        release.set()
        self.assertEqual(self.wait_events(player)[0][:2], ("started", token))
        player.shutdown()
        player._thread.join(2)
        opened = {command[1] for command in commands if command[0] == "prepare"}
        closed = {command[1] for command in commands if command[0] == "close"}
        self.assertEqual(opened, closed)

    def test_async_errors_are_reported_for_current_generation(self):
        player, _entered, _release, commands = self.make_player(fail=True)
        token = player.play("/a.mp3", 0.8)
        events = self.wait_events(player)
        self.assertEqual(events, [("error", token, "decoder unavailable")])
        self.assertTrue(any(command[0] == "close" for command in commands))
        with self.assertRaises(PLAYBACK.PlaybackError):
            player.play('/unsafe".mp3', 0.8)

    def test_speculative_preload_cannot_delay_stopping_an_active_cue(self):
        player, entered, release, commands = self.make_player(block_path="/b.mp3")
        token = player.play("/a.mp3", 0.8)
        self.assertEqual(self.wait_events(player)[0][:2], ("started", token))
        player.prepare(["/b.mp3"])
        # A preload of B would deliberately block our native worker. While A
        # plays it must stay postponed, leaving that worker available for STOP.
        self.assertFalse(entered.wait(0.05))
        player.stop()
        self.assertTrue(entered.wait(2))
        self.assertIn("pause", [command[0] for command in commands])
        self.assertLess(
            next(i for i, command in enumerate(commands) if command[0] == "pause"),
            next(i for i, command in enumerate(commands)
                 if command[:2] == ("prepare", "/b.mp3")),
        )
        release.set()


class SessionPlaybackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        package_tests.CatalogTests().make_catalog(self.root)
        self.host = {"name": CORE.PUBLIC_PARTY_NAME, "port": 43210}
        self.chat = []
        self.settings = {"enabled": True, "volume": 0.8}
        self.calls = []
        self.audio = []
        self.timers = []
        self.now = 100.0

        class Mode(Enum):
            REGULAR = "regular"
            TEST = "test"

        self.Mode = Mode
        calls = self.calls

        class Music:
            def __init__(self):
                self._music_mode = Mode.REGULAR
                self.music_types = {Mode.REGULAR: "Scores", Mode.TEST: None}

            def do_play_music(self, value, continuous=False, mode=Mode.REGULAR,
                              testsoundtrack=None):
                self.music_types[mode] = value
                calls.append(("music", value, continuous, mode, testsoundtrack))

            def music_volume_changed(self, volume):
                calls.append(("game-volume", volume))

            def set_music_play_mode(self, mode):
                self._music_mode = mode
                if mode == Mode.REGULAR:
                    self.do_play_music(self.music_types[mode])

        self.music = Music()

        def timer(delay, callback, repeat=False):
            result = types.SimpleNamespace(delay=delay, callback=callback)
            self.timers.append(result)
            return result

        self.base = types.SimpleNamespace(
            AppTimer=timer, screenmessage=lambda *_args, **_kwargs: None,
            music_player_set_volume=lambda value: self.audio.append(("os-volume", value)),
            app=types.SimpleNamespace(
                classic=types.SimpleNamespace(music=self.music), config={"Music Volume": 0.6}
            ),
        )
        self.scene = types.SimpleNamespace(
            get_connection_to_host_info_2=lambda: self.host,
            get_chat_messages=lambda: list(self.chat), chatmessage=lambda _value: None,
        )
        self.runtime = CORE.ClientRuntime({
            "get_music_settings": lambda: dict(self.settings),
            "get_package_path": lambda _package: str(self.root),
        }, self.base, self.scene, True)
        self.runtime._platform = "windows"
        self.runtime._player = types.SimpleNamespace(
            play=lambda path, volume: self.audio.append(("play", path, volume)),
            stop=lambda: self.audio.append(("stop",)),
            set_volume=lambda value: self.audio.append(("volume", value)),
        )
        self.runtime._ensure_catalog()
        self.runtime._client_nonce = "0123456789abcdef"
        self.runtime._hello_attempts = 1
        patcher = mock.patch.object(CORE.time, "monotonic", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.runtime.shutdown)
        self.runtime._handle_control({
            "kind": "ready", "session_nonce": "012345abcdef",
            "client_nonce": self.runtime._client_nonce,
        })

    def event(self, event_id="abcdef012345"):
        self.runtime._play_event({
            "session_nonce": "012345abcdef", "event_id": event_id,
            "client_nonce": self.runtime._client_nonce, "team": "super", "stage": 1,
        }, 0.8)

    def test_original_music_stays_muted_before_between_and_after_cues(self):
        self.assertEqual(self.calls[0][:2], ("music", None))
        self.music.do_play_music("Match")
        self.event()
        self.runtime.stop_playback()
        self.music.do_play_music("Next Match", continuous=True)
        self.settings["enabled"] = False
        self.runtime._poll()
        self.music.do_play_music("Victory")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.runtime._playing)
        self.music.do_play_music("Menu", continuous=True)
        self.host = None
        self.runtime._poll()
        self.assertEqual(self.calls[-1][:3], ("music", "Menu", False))
        self.assertNotIn("do_play_music", self.music.__dict__)
        self.assertNotIn("music_volume_changed", self.music.__dict__)

    def test_registration_keeps_retrying_during_first_minute_of_account_readiness(self):
        outgoing = []
        self.runtime._server_nonce = None
        self.runtime._hello_attempts = 0
        self.runtime._last_hello_time = -1000
        self.runtime._first_hello_time = None
        self.scene.chatmessage = outgoing.append
        for offset in range(10):
            self.now = 100.0 + 6 * offset
            self.runtime._send_hello()
        self.assertEqual(len(outgoing), 10)
        self.now += 6
        self.runtime._send_hello()
        self.assertEqual(len(outgoing), 10)
        self.now += 39
        self.runtime._send_hello()
        self.assertEqual(len(outgoing), 11)

    def test_known_name_mutes_before_ack_but_does_not_authorize_playback(self):
        self.runtime._restore_game_music()
        self.runtime._server_nonce = None
        self.calls.clear()
        self.runtime._poll()
        self.assertEqual(self.calls[0][:2], ("music", None))
        self.assertTrue(self.runtime._is_spp_music_scope())
        self.assertFalse(self.runtime._is_spp_connection())
        self.event()
        self.assertEqual(self.audio, [])
        self.settings["enabled"] = False
        self.runtime._poll()
        self.assertIn("do_play_music", self.music.__dict__)

    def test_only_fresh_host_discovery_mutes_an_unrecognized_name(self):
        self.host = {"name": "Old Witchly Name", "port": 43210}
        self.runtime._poll()
        self.assertFalse(self.runtime._is_spp_music_scope())
        self.assertNotIn("do_play_music", self.music.__dict__)
        self.calls.clear()
        self.chat.append("Player: SPP MUSIC: SPP1|DISCOVER|012345abcdef")
        self.runtime._poll()
        self.assertEqual(self.calls, [])
        self.chat.append("SPP MUSIC: SPP1|DISCOVER|012345abcdef")
        self.runtime._poll()
        self.assertEqual(self.calls[0][:2], ("music", None))
        self.assertFalse(self.runtime._is_spp_connection())
        self.event()
        self.assertFalse(any(call[0] == "play" for call in self.audio))
        self.host = {"name": "Unrelated Server", "port": 54321}
        self.runtime._poll()
        self.assertNotIn("do_play_music", self.music.__dict__)
        self.assertFalse(self.runtime._is_spp_music_scope())

    def test_mode_and_volume_callbacks_cannot_restart_soundtrack_or_override_cue(self):
        self.runtime._platform = "android"
        self.event()
        self.music.set_music_play_mode(self.Mode.TEST)
        self.music.do_play_music("Preview", mode=self.Mode.TEST)
        self.music.set_music_play_mode(self.Mode.REGULAR)
        self.base.app.config["Music Volume"] = 0.25
        self.music.music_volume_changed(0.25)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any(call[0] == "os-volume" for call in self.audio))
        self.host = None
        self.runtime._poll()
        self.assertIn(("game-volume", 0.25), self.calls)
        self.assertIn(("os-volume", 0.25), self.audio)

    def test_matching_stop_cancels_pending_audio_stale_stop_does_not(self):
        self.runtime._player.play = lambda *_args: 47
        self.runtime._player.poll_events = lambda: []
        self.event()
        self.assertIsNone(self.runtime._stop_timer)
        stop = {"kind": "stop", "session_nonce": "012345abcdef",
                "client_nonce": self.runtime._client_nonce, "event_id": "abcdef012345"}
        for key, value in (("client_nonce", "f" * 16), ("session_nonce", "f" * 12),
                           ("event_id", "f" * 12)):
            wrong = dict(stop, **{key: value})
            self.runtime._handle_control(wrong)
            self.assertTrue(self.runtime._playing)
        self.runtime._handle_control(stop)
        self.assertFalse(self.runtime._playing)
        self.assertIn(("stop",), self.audio)
        self.assertIn("do_play_music", self.music.__dict__)
        self.runtime._player.poll_events = lambda: [("started", 47, 100.0)]
        self.runtime._poll_player()
        self.assertIsNone(self.runtime._stop_timer)

    def test_async_timer_counts_from_native_start_and_ignores_old_notifications(self):
        self.runtime._player.play = lambda *_args: 47
        self.runtime._player.poll_events = lambda: []
        self.event()
        self.assertIsNone(self.runtime._stop_timer)
        self.now = 101.0
        self.runtime._player.poll_events = lambda: [("started", 46, 100.0), ("started", 47, 100.8)]
        self.runtime._poll_player()
        self.assertAlmostEqual(self.runtime._stop_timer.delay, 3.95)
        callback = self.runtime._stop_timer.callback
        self.runtime.stop_playback()
        self.runtime._player.play = lambda *_args: 48
        self.now = 103.0
        self.event("abcdef012346")
        callback()
        self.assertTrue(self.runtime._playing)

    def test_async_failure_stops_cue_but_keeps_session_quiet(self):
        self.runtime._player.play = lambda *_args: 47
        self.runtime._player.poll_events = lambda: [("error", 47, "decoder error")]
        self.event()
        self.runtime._poll_player()
        self.assertFalse(self.runtime._playing)
        self.assertIn("do_play_music", self.music.__dict__)
        self.assertEqual(len(self.calls), 1)

    def test_external_wrapper_chain_is_released_on_disconnect(self):
        previous = self.music.do_play_music
        previous_volume = self.music.music_volume_changed
        self.music.do_play_music = lambda *args, **kwargs: previous(*args, **kwargs)
        self.music.music_volume_changed = lambda *args, **kwargs: previous_volume(*args, **kwargs)
        wrapper = self.music.do_play_music
        self.runtime.stop_playback()
        self.music.do_play_music("Still on server")
        self.assertEqual(len(self.calls), 1)
        self.host = None
        self.runtime._poll()
        self.music.do_play_music("Another server")
        self.music.music_volume_changed(0.3)
        self.assertEqual(self.calls[-2][:2], ("music", "Another server"))
        self.assertEqual(self.calls[-1], ("game-volume", 0.3))
        self.assertIs(self.music.do_play_music, wrapper)


if __name__ == "__main__":
    unittest.main()
