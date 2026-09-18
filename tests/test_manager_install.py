"""Tests for loader installation and legacy Locale config recovery."""

import importlib.util
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "super_pro_players_mod_manager.py"


class _Config(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commits = 0

    def commit(self):
        self.commits += 1


class Locale:
    def __init__(self, long_value):
        self.long_value = long_value


def _load_manager(path):
    name = "super_pro_players_mod_manager"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ManagerInstallTests(unittest.TestCase):
    def _prepared_manager(self, directory, config, api_version=9):
        manager_path = Path(directory) / SOURCE.name
        manager_path.write_bytes(SOURCE.read_bytes())
        manager = _load_manager(manager_path)
        manager._base = types.SimpleNamespace(
            app=types.SimpleNamespace(config=config),
            screenmessage=lambda *args, **kwargs: None,
        )
        manager._game_api_version = lambda: api_version
        manager._python_user_directory = lambda: directory
        manager._append_log = lambda message: None
        return manager

    def test_clean_config_enables_and_commits_loader_for_all_apis(self):
        for api_version in (6, 7, 8, 9):
            with self.subTest(api_version=api_version):
                with tempfile.TemporaryDirectory() as directory:
                    config = _Config()
                    manager = self._prepared_manager(
                        directory, config, api_version
                    )
                    loader_path = manager.install()
                    loader_name = (
                        "spp_mod_manager_loader.SPPModManagerLoader"
                    )
                    self.assertTrue(Path(loader_path).is_file())
                    self.assertTrue(config["Plugins"][loader_name]["enabled"])
                    self.assertEqual(config.commits, 1)

    def test_known_legacy_locale_values_are_narrowly_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            untouched = object()
            config = _Config(
                {
                    "Lang": Locale("German"),
                    "O Target Trans Lang": Locale("SpanishSpain"),
                    "Y Target Trans Lang": Locale("French"),
                    "Unrelated": "unchanged",
                    "Unrelated Object Holder": {"value": untouched},
                }
            )
            manager = self._prepared_manager(directory, config)
            # Remove the deliberately invalid unrelated object for this
            # successful narrow-repair case after checking identity survives
            # the repair helper itself.
            repaired = manager._repair_known_legacy_locale_config(config)
            self.assertIs(
                config["Unrelated Object Holder"]["value"], untouched
            )
            del config["Unrelated Object Holder"]
            self.assertEqual(
                set(repaired),
                {"Lang", "O Target Trans Lang", "Y Target Trans Lang"},
            )
            manager.install()
            self.assertEqual(config["Lang"], "German")
            self.assertEqual(config["O Target Trans Lang"], "English")
            self.assertEqual(config["Y Target Trans Lang"], "English")
            self.assertEqual(config["Unrelated"], "unchanged")
            self.assertEqual(config.commits, 1)

    def test_unrelated_invalid_config_is_preserved_and_not_committed(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = object()
            config = _Config({"Other Mod": {"value": invalid}})
            manager = self._prepared_manager(directory, config)
            with self.assertRaises(manager.SPPManagerError):
                manager.install()
            self.assertIs(config["Other Mod"]["value"], invalid)
            self.assertEqual(config.commits, 0)


    def test_one_shot_app_timer_survives_without_python_reference(self):
        manager = _load_manager(SOURCE)
        scheduled = []
        called = []

        def apptimer(delay, call):
            scheduled.append((delay, call))

        manager._base = types.SimpleNamespace(apptimer=apptimer)
        manager._app_timer(2.0, lambda: called.append("healthy"))
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], 2.0)
        scheduled[0][1]()
        self.assertEqual(called, ["healthy"])

    def test_app_timer_fallback_is_retained_until_callback(self):
        manager = _load_manager(SOURCE)
        called = []

        class Timer:
            def __init__(self, delay, call):
                self.delay = delay
                self.call = call

        def unavailable(*_args, **_kwargs):
            raise AttributeError("one-shot helper unavailable")

        manager._base = types.SimpleNamespace(
            apptimer=unavailable,
            AppTimer=Timer,
        )
        timer = manager._app_timer(2.0, lambda: called.append("healthy"))
        self.assertIn(timer, manager._RETAINED_APP_TIMERS)
        timer.call()
        self.assertEqual(called, ["healthy"])
        self.assertNotIn(timer, manager._RETAINED_APP_TIMERS)


    def test_legacy_timer_fallback_releases_after_callback_error(self):
        manager = _load_manager(SOURCE)

        class Timer:
            def __init__(self, delay, call, timetype=None):
                self.delay = delay
                self.call = call
                self.timetype = timetype

        def unavailable(*_args, **_kwargs):
            raise AttributeError("timer helper unavailable")

        def fail():
            raise RuntimeError("test callback failure")

        manager._base = types.SimpleNamespace(
            apptimer=unavailable,
            AppTimer=unavailable,
            timer=Timer,
            TimeType=types.SimpleNamespace(REAL="real"),
        )
        timer = manager._app_timer(2.0, fail)
        self.assertIn(timer, manager._RETAINED_APP_TIMERS)
        with self.assertRaisesRegex(RuntimeError, "test callback failure"):
            timer.call()
        self.assertNotIn(timer, manager._RETAINED_APP_TIMERS)

    def test_confirmed_update_stays_active_after_next_state_load(self):
        manager = _load_manager(SOURCE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager._CACHED_PATHS = {
                "root": str(root),
                "state": str(root / "state.json"),
                "cache": str(root / "manifest-cache.json"),
                "staging": str(root / "staging"),
                "releases": str(root / "releases"),
                "logs": str(root / "manager.log"),
            }
            service = manager._ManagerService()
            service.state = manager._default_state()
            service.state["pending"] = {
                "previous_active": {},
                "new_active": {},
                "previous_repair_needed": [],
                "new_repair_needed": [],
                "fallback_active": {},
                "fallback_repair_needed": [],
                "boot_attempted": False,
                "clear_prune_on_healthy": False,
                "activated_at": 1,
                "attempt_time": 0,
            }
            service._process_pending_boot()
            self.assertTrue(service.state["pending"]["boot_attempted"])
            service._mark_pending_healthy()
            self.assertIsNone(service.state["pending"])
            self.assertIsNone(manager._load_state()["pending"])

    def test_existing_manager_adds_shock_once_and_preserves_music_and_rollback(self):
        manager = _load_manager(SOURCE)
        repository = SOURCE.parent
        manifest = manager._validate_manifest(
            json.loads((repository / "manifest.json").read_text(encoding="utf-8")),
            manager.DEFAULT_MANIFEST_URL,
            api_version=9,
            build_number=22796,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager._CACHED_PATHS = {
                "root": str(root), "state": str(root / "state.json"),
                "cache": str(root / "manifest-cache.json"),
                "staging": str(root / "staging"),
                "releases": str(root / "releases"),
                "logs": str(root / "manager.log"),
            }
            manager._game_api_version = lambda: 9
            manager._game_build_number = lambda: 22796
            manager._append_log = lambda _message: None

            def local_download(package, _source_url, target, _progress):
                name = package["download_url"].rsplit("/", 1)[-1]
                Path(target).write_bytes((repository / "release" / name).read_bytes())

            with mock.patch.object(manager, "_download_archive", local_download):
                service = manager._ManagerService()
                service.state = manager._default_state()
                service.manifest = copy.deepcopy(manifest)
                service.manifest["packages"] = [
                    package for package in manifest["packages"]
                    if package["id"] != "spp-shock-norris"
                ]
                source_url = service.state["source_url"]
                initial = service._prepare_transaction(
                    service._packages_needing_update(), source_url, None
                )
                service._activate_transaction(initial)
                service._process_pending_boot()
                service._mark_pending_healthy()
                previous = copy.deepcopy(service.state["active"])

                service.manifest = manifest
                updates = service._packages_needing_update()
                self.assertEqual([p["id"] for p in updates], ["spp-shock-norris"])
                transaction = service._prepare_transaction(updates, source_url, None)
                service._activate_transaction(transaction)
                self.assertEqual(service.state["pending"]["fallback_active"], previous)
                for package_id, record in previous.items():
                    self.assertEqual(service.state["active"][package_id], record)
                service._process_pending_boot()
                service._mark_pending_healthy()

                reloaded = manager._ManagerService()
                reloaded.state = manager._load_state()
                reloaded.manifest = manifest
                self.assertIsNone(reloaded.state["pending"])
                self.assertEqual(reloaded.state["rollback_active"], previous)
                self.assertEqual(reloaded._packages_needing_update(), [])
                self.assertTrue(manager._verify_installed_map(reloaded.state["active"])[0])

    def test_legacy_game_does_not_download_api9_character(self):
        manager = _load_manager(SOURCE)
        raw = json.loads((SOURCE.parent / "manifest.json").read_text(encoding="utf-8"))
        for api in (6, 7, 8):
            with self.subTest(api=api):
                manifest = manager._validate_manifest(
                    raw, manager.DEFAULT_MANIFEST_URL,
                    api_version=api, build_number=22837,
                )
                shock = next(p for p in manifest["packages"] if p["id"] == "spp-shock-norris")
                self.assertFalse(shock["compatible"])
                service = manager._ManagerService()
                service.state = manager._default_state()
                service.manifest = manifest
                self.assertEqual(service._packages_needing_update(), [])


if __name__ == "__main__":
    unittest.main()
