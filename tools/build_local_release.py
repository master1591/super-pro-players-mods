#!/usr/bin/env python3
"""Build deterministic SPP client package archives and release metadata."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CORE_SOURCE = ROOT / "packages/spp-client-core"
AUDIO_SOURCE = ROOT / "packages/spp-victory-audio"
SHOCK_SOURCE = ROOT / "packages/spp-shock-norris"
DEFAULT_OUTPUT = ROOT / "release"
DOWNLOAD_ROOT = (
    "https://raw.githubusercontent.com/master1591/"
    "super-pro-players-mods/main/release"
)
ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
CORE_VERSION = "0.1.2-beta"
AUDIO_VERSION = "0.1.1-beta"
SHOCK_VERSION = "0.1.0-beta"


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


def write_archive(output: Path, data: bytes) -> None:
    """Never replace a published version's archive with different bytes."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.read_bytes() != data:
            raise FileExistsError(
                f"Refusing to replace {output.name}; bump the package version first."
            )
        return
    with output.open("xb") as stream:
        stream.write(data)


def build_archive(
    output: Path,
    files: list[tuple[str, Path]],
) -> tuple[int, str, list[dict[str, object]], int]:
    records: list[dict[str, object]] = []
    unpacked_size = 0
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
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
    archive_bytes = buffer.getvalue()
    write_archive(output, archive_bytes)
    return len(archive_bytes), digest(archive_bytes), records, unpacked_size


def reuse_audio_archive(
    output: Path, archive_name: str
) -> tuple[int, str, list[dict[str, object]], int]:
    """Reuse reviewed audio when a clean checkout has no ignored MP3 sources."""
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    records = [
        record for record in manifest["packages"]
        if record["id"] == "spp-victory-audio"
        and record["version"] == AUDIO_VERSION
    ]
    if len(records) != 1:
        raise ValueError("No matching reviewed victory-audio release in manifest.")
    record = records[0]
    if record["download_url"].rsplit("/", 1)[-1] != archive_name:
        raise ValueError("Reviewed victory-audio archive name does not match.")
    data = (ROOT / "release" / archive_name).read_bytes()
    if len(data) != record["download_size"] or digest(data) != record["archive_sha256"]:
        raise ValueError("Reviewed victory-audio archive failed SHA-256/size verification.")
    expected = {item["path"]: item for item in record["files"]}
    unpacked_size = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != set(expected):
            raise ValueError("Reviewed victory-audio archive file list does not match.")
        for name in names:
            member = archive.read(name)
            wanted = expected[name]
            if len(member) != wanted["size"] or digest(member) != wanted["sha256"]:
                raise ValueError(f"Reviewed victory-audio member failed verification: {name}")
            unpacked_size += len(member)
        if archive.read("catalog.json") != (AUDIO_SOURCE / "catalog.json").read_bytes():
            raise ValueError("Victory catalog changed; prepare the audio sources and bump its version.")
    if unpacked_size != record["unpacked_size"]:
        raise ValueError("Reviewed victory-audio unpacked size does not match.")
    write_archive(output / archive_name, data)
    return len(data), digest(data), record["files"], unpacked_size


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
    audio_archive_name = f"spp-victory-audio-{AUDIO_VERSION}.zip"
    if len(missing) == len(cue_names):
        audio_data = reuse_audio_archive(output, audio_archive_name)
    elif missing:
        raise FileNotFoundError(
            "Prepare all six local cue files first: " + ", ".join(missing)
        )
    else:
        audio_files = [("catalog.json", AUDIO_SOURCE / "catalog.json")]
        audio_files.extend(
            (f"audio/{name}", AUDIO_SOURCE / "audio" / name)
            for name in cue_names
        )
        audio_data = build_archive(output / audio_archive_name, audio_files)

    shock_archive_name = f"spp-shock-norris-{SHOCK_VERSION}.zip"
    shock_data = build_archive(
        output / shock_archive_name,
        [(name, SHOCK_SOURCE / name) for name in ("spp_shock_norris.py", "README.md")],
    )

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
            package_record(
                package_id="spp-shock-norris",
                name="Shock Norris",
                version=SHOCK_VERSION,
                archive_name=shock_archive_name,
                archive_data=shock_data,
                entrypoint="spp_shock_norris.py",
                load_order=5,
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
    parser.add_argument("--sequence", type=int, default=5)
    args = parser.parse_args()
    if args.sequence < 1:
        parser.error("--sequence must be positive")
    manifest = build(args.output.resolve(), args.sequence)
    print(f"Built deterministic SPP release: {manifest}")
    print("Nothing is published until the generated files are reviewed and pushed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
