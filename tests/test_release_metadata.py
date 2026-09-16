"""Checks that the documented public bootstrap stays immutable and verified."""

import ast
import hashlib
import json
from pathlib import Path
import re
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


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
        self.assertIn("{'__name__':'__main__'}", command)

    def test_public_manifest_matches_every_release_archive_byte(self):
        manifest = json.loads(
            (ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["channel"], "stable")
        self.assertGreaterEqual(manifest["release_sequence"], 3)
        self.assertEqual(
            [item["id"] for item in manifest["packages"]],
            ["spp-client-core", "spp-victory-audio"],
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
                    if package["id"] == "spp-client-core" or member.filename == "catalog.json":
                        source = ROOT / "packages" / package["id"] / member.filename
                        self.assertEqual(data, source.read_bytes())

    def test_core_release_version_matches_runtime_and_builder(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        core = next(p for p in manifest["packages"] if p["id"] == "spp-client-core")
        self.assertEqual(core["version"], "0.1.1-beta")
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


if __name__ == "__main__":
    unittest.main()
