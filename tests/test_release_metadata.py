"""Checks that the documented public bootstrap stays immutable and verified."""

import ast
import hashlib
from pathlib import Path
import re
import unittest


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


if __name__ == "__main__":
    unittest.main()
