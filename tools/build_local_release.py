#!/usr/bin/env python3
"""Build deterministic SPP client package archives and release metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CORE_SOURCE = ROOT / "packages/spp-client-core"
AUDIO_SOURCE = ROOT / "packages/spp-victory-audio"
DEFAULT_OUTPUT = ROOT / "release"
DOWNLOAD_ROOT = (
    "https://raw.githubusercontent.com/master1591/"
    "super-pro-players-mods/main/release"
)
ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
CORE_VERSION = "0.1.2-beta"
AUDIO_VERSION = "0.1.1-beta"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zip_info(relative: str, *, compressed: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(relative, ZIP_TIMESTAMP)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = (
        zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
    )
    return info


def build_archive(
    output: Path,
    files: list[tuple[str, Path]],
) -> tuple[int, str, list[dict[str, object]], int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    unpacked_size = 0
    with zipfile.ZipFile(output, "w") as archive:
        for relative, source in sorted(files):
            data = source.read_bytes()
            unpacked_size += len(data)
            records.append(
                {"path": relative, "size": len(data), "sha256": digest(data)}
            )
            archive.writestr(
                zip_info(
                    relative,
                    compressed=not relative.lower().endswith(".mp3"),
                ),
                data,
            )
    archive_bytes = output.read_bytes()
    return len(archive_bytes), digest(archive_bytes), records, unpacked_size


def package_record(
    *,
    package_id: str,
    name: str,
    version: str,
    archive_name: str,
    archive_data: tuple[int, str, list[dict[str, object]], int],
    entrypoint: str | None,
    load_order: int,
) -> dict[str, object]:
    size, archive_hash, files, unpacked_size = archive_data
    return {
        "id": package_id,
        "name": name,
        "version": version,
        "api_versions": [9],
        "minimum_build": 22796,
        "maximum_build": None,
        "download_url": f"{DOWNLOAD_ROOT}/{archive_name}",
        "download_size": size,
        "unpacked_size": unpacked_size,
        "archive_sha256": archive_hash,
        "files": files,
        "entrypoint": entrypoint,
        "start_callable": "start" if entrypoint else None,
        "requires_restart": True,
        "load_order": load_order,
    }


def build(output: Path, sequence: int) -> Path:
    core_names = (
        "spp_client_core.py",
        "protocol.py",
        "playback.py",
        "README.md",
    )
    core_files = [(name, CORE_SOURCE / name) for name in core_names]
    core_archive_name = f"spp-client-core-{CORE_VERSION}.zip"
    core_data = build_archive(output / core_archive_name, core_files)

    cue_names = tuple(
        f"{team}_round_{stage}.mp3"
        for team in ("super", "pro")
        for stage in (1, 2, 3)
    )
    missing = [
        name for name in cue_names
        if not (AUDIO_SOURCE / "audio" / name).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            "Prepare all six local cue files first: " + ", ".join(missing)
        )
    audio_files = [("catalog.json", AUDIO_SOURCE / "catalog.json")]
    audio_files.extend(
        (f"audio/{name}", AUDIO_SOURCE / "audio" / name)
        for name in cue_names
    )
    audio_archive_name = f"spp-victory-audio-{AUDIO_VERSION}.zip"
    audio_data = build_archive(output / audio_archive_name, audio_files)

    manifest = {
        "schema": 1,
        "release_sequence": sequence,
        "channel": "stable",
        "title": "SUPER PRO PLAYERS official packages",
        "packages": [
            package_record(
                package_id="spp-client-core",
                name="SPP Client Core",
                version=CORE_VERSION,
                archive_name=core_archive_name,
                archive_data=core_data,
                entrypoint="spp_client_core.py",
                load_order=10,
            ),
            package_record(
                package_id="spp-victory-audio",
                name="SPP Victory Audio",
                version=AUDIO_VERSION,
                archive_name=audio_archive_name,
                archive_data=audio_data,
                entrypoint=None,
                load_order=20,
            ),
        ],
    }
    manifest_path = output / "manifest.release.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sequence", type=int, default=4)
    args = parser.parse_args()
    if args.sequence < 1:
        parser.error("--sequence must be positive")
    manifest = build(args.output.resolve(), args.sequence)
    print(f"Built deterministic SPP release: {manifest}")
    print("Nothing is published until the generated files are reviewed and pushed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
