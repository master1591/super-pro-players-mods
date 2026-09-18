"""Character registration regressions without importing a live game."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "spp_shock_norris_tests_module",
    ROOT / "packages/spp-shock-norris/spp_shock_norris.py",
)
SHOCK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHOCK)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.bones = types.SimpleNamespace(
            **{field: object() for field in SHOCK.APPEARANCE_FIELDS}
        )
        self.bones.attack_sounds = ["bones1", "bones2"]
        self.registry = {"Bones": self.bones, "Spaz": object()}
        self.original_bones = vars(self.bones).copy()
        registry = self.registry

        class Appearance:
            def __init__(self, name):
                self.name = name
                registry[name] = self

        self.appearance_type = Appearance

    def test_registers_only_new_alias_and_preserves_native_handles_and_bones(self):
        result = SHOCK.register_appearance(self.registry, self.appearance_type)
        self.assertEqual(set(self.registry), {"Bones", "Spaz", "Shock Norris"})
        self.assertEqual(vars(self.bones), self.original_bones)
        for field in SHOCK.APPEARANCE_FIELDS:
            if field != "color_mask_texture":
                self.assertIs(getattr(result, field), getattr(self.bones, field))
        self.assertEqual(result.color_mask_texture, "white")
        self.assertEqual(result.attack_sounds, ["shieldHit"])
        self.assertEqual(result.death_sounds, ["shieldDown"])
        self.assertEqual(result.default_color, SHOCK.DEFAULT_COLOR)
        self.assertNotEqual(result.attack_sounds, self.bones.attack_sounds)

    def test_registration_is_idempotent_without_duplicate_objects(self):
        first = SHOCK.register_appearance(self.registry, self.appearance_type)
        second = SHOCK.register_appearance(self.registry, self.appearance_type)
        self.assertIs(first, second)

    def test_foreign_mod_name_is_never_overwritten(self):
        other = object()
        self.registry[SHOCK.CHARACTER_NAME] = other
        with self.assertRaisesRegex(RuntimeError, "Another mod"):
            SHOCK.register_appearance(self.registry, self.appearance_type)
        self.assertIs(self.registry[SHOCK.CHARACTER_NAME], other)

    def test_missing_native_media_never_leaves_partial_appearance(self):
        del self.bones.head_mesh
        with self.assertRaises(AttributeError):
            SHOCK.register_appearance(self.registry, self.appearance_type)
        self.assertNotIn(SHOCK.CHARACTER_NAME, self.registry)

    def test_setter_failure_removes_only_our_partial_registration(self):
        registry = self.registry

        class BrokenAppearance(self.appearance_type):
            def __setattr__(self, name, value):
                if name == "default_color":
                    raise RuntimeError("Simulated engine API mismatch")
                object.__setattr__(self, name, value)

        with self.assertRaisesRegex(RuntimeError, "API mismatch"):
            SHOCK.register_appearance(registry, BrokenAppearance)
        self.assertEqual(set(registry), {"Bones", "Spaz"})

    def test_manager_abi_registers_without_network_music_or_game_file_writes(self):
        base = types.ModuleType("babase")
        base.app = types.SimpleNamespace(
            classic=types.SimpleNamespace(spaz_appearances=self.registry)
        )
        appearances = types.ModuleType("bascenev1lib.actor.spazappearance")
        appearances.Appearance = self.appearance_type
        modules = {
            "babase": base,
            "bascenev1lib": types.ModuleType("bascenev1lib"),
            "bascenev1lib.actor": types.ModuleType("bascenev1lib.actor"),
            "bascenev1lib.actor.spazappearance": appearances,
        }
        with mock.patch.dict(sys.modules, modules), mock.patch("builtins.open") as opened:
            SHOCK.start({"api_version": 9})
        opened.assert_not_called()
        self.assertIn(SHOCK.CHARACTER_NAME, self.registry)

    def test_legacy_api_is_rejected_before_engine_import(self):
        for version in (6, 7, 8, 10, None):
            with self.subTest(api=version), self.assertRaisesRegex(RuntimeError, "API 9"):
                SHOCK.start({"api_version": version})


if __name__ == "__main__":
    unittest.main()
