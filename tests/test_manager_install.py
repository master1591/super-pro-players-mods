"""Tests for loader installation and legacy Locale config recovery."""

import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest


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


if __name__ == "__main__":
    unittest.main()
