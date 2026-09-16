"""Regression tests for the SPP one-command bootstrap installer."""

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load_installer():
    name = "_spp_installer_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "install.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, data, content_length=None):
        self._stream = io.BytesIO(data)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.url = None

    def read(self, size=-1):
        return self._stream.read(size)

    def getcode(self):
        return 200

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _Opener:
    def __init__(self, response):
        self.response = response

    def open(self, request, timeout=None):
        del timeout
        self.response.url = request.full_url
        return self.response


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_installer()

    def test_verified_download(self):
        data = b"print('verified')\n"
        self.module.MANAGER_SHA256 = hashlib.sha256(data).hexdigest()
        opener = _Opener(_Response(data, len(data)))
        with mock.patch.object(
            self.module.urllib.request, "build_opener", return_value=opener
        ):
            self.assertEqual(self.module._download_manager(), data)

    def test_hash_mismatch_fails_closed(self):
        data = b"print('changed')\n"
        self.module.MANAGER_SHA256 = "0" * 64
        opener = _Opener(_Response(data, len(data)))
        with mock.patch.object(
            self.module.urllib.request, "build_opener", return_value=opener
        ):
            with self.assertRaisesRegex(
                self.module.InstallError, "security check failed"
            ):
                self.module._download_manager()

    def test_announced_oversize_is_rejected(self):
        opener = _Opener(
            _Response(b"x", self.module.MAX_MANAGER_BYTES + 1)
        )
        with mock.patch.object(
            self.module.urllib.request, "build_opener", return_value=opener
        ):
            with self.assertRaisesRegex(
                self.module.InstallError, "unsafe size"
            ):
                self.module._download_manager()

    def test_streamed_oversize_is_rejected(self):
        data = b"x" * (self.module.MAX_MANAGER_BYTES + 1)
        opener = _Opener(_Response(data))
        with mock.patch.object(
            self.module.urllib.request, "build_opener", return_value=opener
        ):
            with self.assertRaisesRegex(
                self.module.InstallError, "exceeded its size limit"
            ):
                self.module._download_manager()

    def test_success_keeps_previous_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / self.module.MANAGER_FILENAME
            destination.write_bytes(b"old manager")
            payload = b"new manager"
            with mock.patch.object(
                self.module, "_validate_api", return_value=9
            ), mock.patch.object(
                self.module, "_mods_directory", return_value=directory
            ), mock.patch.object(
                self.module, "_download_manager", return_value=payload
            ), mock.patch.object(
                self.module, "_execute_verified", return_value="loader"
            ):
                result = self.module.install()
            self.assertEqual(Path(result), destination)
            self.assertEqual(destination.read_bytes(), b"new manager")
            self.assertEqual(
                Path(str(destination) + ".previous").read_bytes(), b"old manager"
            )

    def test_activation_failure_restores_previous_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / self.module.MANAGER_FILENAME
            destination.write_bytes(b"old manager")
            with mock.patch.object(
                self.module, "_validate_api", return_value=9
            ), mock.patch.object(
                self.module, "_mods_directory", return_value=directory
            ), mock.patch.object(
                self.module, "_download_manager", return_value=b"new manager"
            ), mock.patch.object(
                self.module,
                "_execute_verified",
                side_effect=RuntimeError("activation failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "activation failed"):
                    self.module.install()
            self.assertEqual(destination.read_bytes(), b"old manager")
            self.assertFalse(
                Path(str(destination) + ".rollback-pending").exists()
            )

    def test_activation_failure_removes_first_install(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / self.module.MANAGER_FILENAME
            payload = b"new manager"
            self.module.MANAGER_SHA256 = hashlib.sha256(payload).hexdigest()
            with mock.patch.object(
                self.module, "_validate_api", return_value=9
            ), mock.patch.object(
                self.module, "_mods_directory", return_value=directory
            ), mock.patch.object(
                self.module, "_download_manager", return_value=payload
            ), mock.patch.object(
                self.module,
                "_execute_verified",
                side_effect=RuntimeError("activation failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "activation failed"):
                    self.module.install()
            self.assertFalse(destination.exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.py"
            target.write_text("safe", encoding="utf-8")
            destination = Path(directory) / self.module.MANAGER_FILENAME
            os.symlink(str(target), str(destination))
            with mock.patch.object(
                self.module, "_validate_api", return_value=9
            ), mock.patch.object(
                self.module, "_mods_directory", return_value=directory
            ):
                with self.assertRaisesRegex(
                    self.module.InstallError, "not a normal file"
                ):
                    self.module.install()
            self.assertEqual(target.read_text(encoding="utf-8"), "safe")

    def test_full_flow_uses_game_mods_directory_for_each_api(self):
        for api_version in (6, 7, 8, 9):
            with self.subTest(api_version=api_version):
                with tempfile.TemporaryDirectory() as directory:
                    payload = b"manager"
                    with mock.patch.object(
                        self.module,
                        "_validate_api",
                        return_value=api_version,
                    ), mock.patch.object(
                        self.module, "_mods_directory", return_value=directory
                    ), mock.patch.object(
                        self.module, "_download_manager", return_value=payload
                    ), mock.patch.object(
                        self.module, "_execute_verified", return_value="loader"
                    ) as loader:
                        self.module.install()
                    manager_path = Path(directory) / self.module.MANAGER_FILENAME
                    self.assertEqual(manager_path.read_bytes(), payload)
                    loader.assert_called_once_with(payload, str(manager_path))

    def test_pinned_manager_hash_matches_repository_file(self):
        manager_data = (ROOT / self.module.MANAGER_FILENAME).read_bytes()
        self.assertEqual(
            hashlib.sha256(manager_data).hexdigest(),
            self.module.MANAGER_SHA256,
        )


if __name__ == "__main__":
    unittest.main()
