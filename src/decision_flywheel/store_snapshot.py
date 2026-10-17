"""Copy an embedded cyclotron's SQLite store to one archive file, and back.

The archive holds every store file of a cyclotron directory plus a manifest
with a digest of each file, so a restore refuses a damaged or foreign archive.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
from typing import Any, Mapping

from .embedded_records import CyclotronDefinition, FileLease, _now

STORE_SCHEMA = "cyclotron-store/v1"
STORE_FILES = ("cyclotron.sqlite3", "shared.sqlite3")
CLASSIFIER_DIRECTORY = "classifiers"


def classifier_store_name(classifier: str) -> str:
    return f"{CLASSIFIER_DIRECTORY}/" + hashlib.sha256(classifier.encode()).hexdigest() + ".sqlite3"


def store_paths(directory: Path, definition: CyclotronDefinition) -> dict[str, Path]:
    paths = {name: directory / name for name in STORE_FILES}
    for spec in definition.classifiers:
        name = classifier_store_name(spec.id)
        paths[name] = directory / name
    return paths


def write_store_snapshot(path, definition: CyclotronDefinition,
                         connections: Mapping[str, sqlite3.Connection]) -> dict[str, Any]:
    path = Path(path)
    manifest = {"schema": STORE_SCHEMA, "cyclotron": definition.id,
                "definition_fingerprint": definition.fingerprint, "created_at": _now(), "files": []}
    with tempfile.TemporaryDirectory() as scratch:
        with tarfile.open(Path(scratch) / "store.tar.gz", "w:gz") as archive:
            for name, connection in connections.items():
                copy = Path(scratch) / hashlib.sha256(name.encode()).hexdigest()
                target = sqlite3.connect(copy)
                with target:
                    connection.backup(target)
                target.close()
                archive.add(copy, arcname=name)
                manifest["files"].append({"name": name, "sha256": hashlib.sha256(copy.read_bytes()).hexdigest()})
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        os.replace(Path(scratch) / "store.tar.gz", partial)
        os.replace(partial, path)
    return manifest


def restore_store_snapshot(archive_path, directory, *, lease=None) -> dict[str, Any]:
    directory = Path(directory)
    if any((directory / name).exists() for name in STORE_FILES) or (directory / CLASSIFIER_DIRECTORY).exists():
        raise ValueError("restore needs a directory without a cyclotron store")
    lease = lease if lease is not None else FileLease(directory)
    lease.acquire()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            manifest = json.loads(archive.extractfile("manifest.json").read())
            if manifest.get("schema") != STORE_SCHEMA:
                raise ValueError("not a cyclotron store snapshot")
            allowed = {entry["name"]: entry["sha256"] for entry in manifest["files"]}
            for name, digest in allowed.items():
                if name not in STORE_FILES and not (name.startswith(f"{CLASSIFIER_DIRECTORY}/") and name.count("/") == 1
                                                    and name.endswith(".sqlite3") and ".." not in name):
                    raise ValueError(f"unexpected file in snapshot: {name}")
                data = archive.extractfile(name).read()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError(f"snapshot file is damaged: {name}")
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        return manifest
    finally:
        lease.release()
