"""Checks that the documented public bootstrap stays immutable and verified."""

import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BUILDER_SPEC = importlib.util.spec_from_file_location(
    "spp_release_builder_tests", ROOT / "tools/build_local_release.py"
)
assert BUILDER_SPEC is not None and BUILDER_SPEC.loader is not None
BUILDER = importlib.util.module_from_spec(BUILDER_SPEC)
BUILDER_SPEC.loader.exec_module(BUILDER)


class ReleaseMetadataTests(unittest.TestCase):
    def test_readme_bootstrap_is_pinned_to_the_current_installer(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        block = re.search(r"```python\n([^\n]+)\n```", readme)
        self.assertIsNotNone(block)
        command = block.group(1)
        ast.parse(command)

        immutable_url = re.search(
            r"https://raw\.githubusercontent\.com/master1591/"
            r"super-pro-players-mods/([0-9a-f]{40})/install\.py",
            command,
        )
        self.assertIsNotNone(immutable_url)
        self.assertNotIn("/main/", command)

        digest = re.search(
            r"hexdigest\(\)==['\"]([0-9a-f]{64})['\"]", command
        )
        self.assertIsNotNone(digest)
        installer = (ROOT / "install.py").read_bytes()
        self.assertEqual(hashlib.sha256(installer).hexdigest(), digest.group(1))
        self.assertIn(".read(131073)", command)
        self.assertNotIn("_", command)
        self.assertIn("n=chr(95)+chr(95)", command)
        self.assertIn("{n+'name'+n:n+'main'+n}", command)

    def test_readme_bootstrap_survives_discord_and_sets_main(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        block = re.search(r"```python\n([^\n]+)\n```", readme)
        self.assertIsNotNone(block)
        command = block.group(1)
        self.assertNotIn("_", command)

        original_digest = re.search(
            r"hexdigest\(\)==['\"]([0-9a-f]{64})['\"]", command
        )
        self.assertIsNotNone(original_digest)
        payload = b"raise RuntimeError(__name__)\n"
        test_command = command.replace(
            original_digest.group(1), hashlib.sha256(payload).hexdigest()
        )
        with mock.patch(
            "urllib.request.urlopen", return_value=io.BytesIO(payload)
        ):
            with self.assertRaisesRegex(RuntimeError, "^__main__$"):
                exec(test_command, {})

    def test_readme_bootstrap_rejects_changed_installer(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        block = re.search(r"```python\n([^\n]+)\n```", readme)
        self.assertIsNotNone(block)
        with mock.patch(
            "urllib.request.urlopen", return_value=io.BytesIO(b"changed")
        ):
            with self.assertRaisesRegex(
                RuntimeError, "SPP installer security check failed"
            ):
                exec(block.group(1), {})

    def test_public_manifest_matches_every_release_archive_byte(self):
        manifest = json.loads(
            (ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["channel"], "stable")
        self.assertGreaterEqual(manifest["release_sequence"], 5)
        self.assertEqual(
            [item["id"] for item in manifest["packages"]],
            ["spp-client-core", "spp-victory-audio", "spp-shock-norris"],
        )

        for package in manifest["packages"]:
            archive_name = package["download_url"].rsplit("/", 1)[-1]
            archive_path = ROOT / "release" / archive_name
            archive_bytes = archive_path.read_bytes()
            self.assertEqual(len(archive_bytes), package["download_size"])
            self.assertEqual(
                hashlib.sha256(archive_bytes).hexdigest(),
                package["archive_sha256"],
            )
            expected = {item["path"]: item for item in package["files"]}
            with zipfile.ZipFile(archive_path, "r") as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                self.assertEqual({item.filename for item in members}, set(expected))
                for member in members:
                    data = archive.read(member)
                    record = expected[member.filename]
                    self.assertEqual(len(data), record["size"])
                    self.assertEqual(
                        hashlib.sha256(data).hexdigest(), record["sha256"]
                    )
                    # Audio is distributed only in its verified ZIP, not as
                    # duplicate loose MP3s in Git. Code/catalog sources ARE
                    # tracked and must match the release byte for byte.
                    if package["id"] != "spp-victory-audio" or member.filename == "catalog.json":
                        source = ROOT / "packages" / package["id"] / member.filename
                        self.assertEqual(data, source.read_bytes())

    def test_shock_norris_is_an_independent_compatible_restart_package(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        package = next(p for p in manifest["packages"] if p["id"] == "spp-shock-norris")
        self.assertEqual(package["version"], BUILDER.SHOCK_VERSION)
        self.assertEqual(package["api_versions"], [9])
        self.assertEqual(package["minimum_build"], 22796)
        self.assertEqual(package["entrypoint"], "spp_shock_norris.py")
        self.assertEqual(package["start_callable"], "start")
        self.assertEqual(package["load_order"], 5)
        self.assertTrue(package["requires_restart"])
        self.assertEqual(
            {item["path"] for item in package["files"]},
            {"spp_shock_norris.py", "README.md"},
        )

    def test_clean_checkout_rebuild_preserves_all_published_archive_bytes(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_source = root / "audio-source"
            audio_source.mkdir()
            (audio_source / "catalog.json").write_bytes(
                (ROOT / "packages/spp-victory-audio/catalog.json").read_bytes()
            )
            with mock.patch.object(BUILDER, "AUDIO_SOURCE", audio_source):
                built = BUILDER.build(root / "release", manifest["release_sequence"])
            self.assertEqual(json.loads(built.read_text(encoding="utf-8")), manifest)
            for package in manifest["packages"]:
                name = package["download_url"].rsplit("/", 1)[-1]
                self.assertEqual(
                    (root / "release" / name).read_bytes(),
                    (ROOT / "release" / name).read_bytes(),
                )

    def test_published_archive_cannot_be_overwritten_with_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            output = root / "package-1.zip"
            BUILDER.build_archive(output, [("source.py", source)])
            published = output.read_bytes()
            # An identical rebuild is harmless; changed code needs a version bump.
            BUILDER.build_archive(output, [("source.py", source)])
            source.write_text("VALUE = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "bump the package version"):
                BUILDER.build_archive(output, [("source.py", source)])
            self.assertEqual(output.read_bytes(), published)

    def test_audio_reuse_rejects_corruption_and_changed_catalog(self):
        manifest_bytes = (ROOT / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        record = next(p for p in manifest["packages"] if p["id"] == "spp-victory-audio")
        name = record["download_url"].rsplit("/", 1)[-1]
        reviewed = (ROOT / "release" / name).read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_bytes(manifest_bytes)
            (root / "release").mkdir()
            archive = root / "release" / name
            archive.write_bytes(reviewed[:-1] + bytes([reviewed[-1] ^ 1]))
            source = root / "audio-source"
            source.mkdir()
            catalog = source / "catalog.json"
            catalog.write_bytes((ROOT / "packages/spp-victory-audio/catalog.json").read_bytes())
            with mock.patch.object(BUILDER, "ROOT", root), mock.patch.object(
                BUILDER, "AUDIO_SOURCE", source
            ):
                with self.assertRaisesRegex(ValueError, "SHA-256/size"):
                    BUILDER.reuse_audio_archive(root / "output", name)
                archive.write_bytes(reviewed)
                catalog.write_text("{}\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "catalog changed"):
                    BUILDER.reuse_audio_archive(root / "output", name)
                self.assertFalse((root / "output" / name).exists())

    def test_core_release_version_matches_runtime_and_builder(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        core = next(p for p in manifest["packages"] if p["id"] == "spp-client-core")
        self.assertEqual(core["version"], "0.1.2-beta")
        for source in (
            ROOT / "packages/spp-client-core/spp_client_core.py",
            ROOT / "tools/build_local_release.py",
        ):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            versions = [
                ast.literal_eval(node.value)
                for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "CORE_VERSION"
                        for t in node.targets)
            ]
            self.assertEqual(versions, [core["version"]])

    def test_round_one_and_two_use_identical_teasers_and_finals_are_longer(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        package = next(p for p in manifest["packages"] if p["id"] == "spp-victory-audio")
        self.assertEqual(package["version"], "0.1.1-beta")
        archive_name = package["download_url"].rsplit("/", 1)[-1]
        with zipfile.ZipFile(ROOT / "release" / archive_name) as archive:
            catalog = json.loads(archive.read("catalog.json"))
            for team in ("super", "pro"):
                stages = catalog["tracks"][team]
                first = archive.read(stages["1"]["path"])
                self.assertEqual(first, archive.read(stages["2"]["path"]))
                self.assertEqual(stages["1"]["duration"], stages["2"]["duration"])
                self.assertTrue(9.0 <= stages["1"]["duration"] <= 11.0)
                self.assertTrue(19.0 <= stages["3"]["duration"] <= 21.0)
                self.assertNotEqual(first, archive.read(stages["3"]["path"]))


if __name__ == "__main__":
    unittest.main()
