"""Compare a public source-built zip to reviewed local payload commitments.

No deployed archive, credentials, per-file private metadata, or cloud access is
needed in CI. Application and dependency payload bytes must match exactly.
Only validated pip-generated RECORD/empty REQUESTED bookkeeping is normalized.
No artifact is uploaded.
"""

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import zipfile

EXPECTED_FULL = "1bddd15ad0972044a0196856be25e94c2086afb044e9c66ccbfffa2d0f0f5d38"
EXPECTED_APPLICATION = (
    "bb996ecf007b69541cad91bb2fa5effac5765020e86b62572787158c1c31a02d"
)
EXPECTED_STABLE = "c27e669929782a97ab1d83f300d759f02c195ad9d2ca75154362b2e0758a5edf"
INSTALL_METADATA = re.compile(r"[^/]+\.dist-info/(RECORD|REQUESTED)")
EXCLUDED_BIN_REFERENCES = {
    "../../bin/" + name
    for name in (
        "cffi-gen-src",
        "fastapi",
        "httpx",
        "idna",
        "jp.py",
        "jsonschema",
        "strawberry",
    )
}


def validate_install_metadata(archive, names):
    for name in names:
        match = INSTALL_METADATA.fullmatch(name)
        if match and match[1] == "REQUESTED" and archive.read(name):
            raise ValueError("installer REQUESTED marker must be empty")
    for name in names:
        match = INSTALL_METADATA.fullmatch(name)
        if match is None or match[1] == "REQUESTED":
            continue
        body = archive.read(name)
        for row in csv.reader(io.StringIO(body.decode("utf-8"))):
            if len(row) != 3:
                raise ValueError("invalid installer RECORD")
            path, digest, size = row
            if path == name and not digest and not size:
                continue
            if path not in names:
                if path in EXCLUDED_BIN_REFERENCES:
                    continue  # Builder excludes these console scripts from Lambda.
                raise ValueError("installer RECORD names an absent payload")
            payload = archive.read(path)
            actual = (
                base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
                .decode()
                .rstrip("=")
            )
            if digest != "sha256=" + actual or size != str(len(payload)):
                raise ValueError("installer RECORD does not match packaged payload")


def fingerprints(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate archive member")
        if (
            len(names) > 10000
            or sum(i.file_size for i in archive.infolist()) > 240 * 1024 * 1024
        ):
            raise ValueError("bounded artifact required")
        validate_install_metadata(archive, set(names))
        rows = sorted((n, hashlib.sha256(archive.read(n)).hexdigest()) for n in names)

    def digest(values):
        return hashlib.sha256(
            json.dumps(values, separators=(",", ":")).encode()
        ).hexdigest()

    return {
        "full": digest(rows),
        "application": digest(
            [r for r in rows if r[0].startswith("decision_flywheel/")]
        ),
        "members": len(rows),
        "stable": digest([r for r in rows if not INSTALL_METADATA.fullmatch(r[0])]),
        "stableMembers": sum(not INSTALL_METADATA.fullmatch(r[0]) for r in rows),
        "recordFiles": sum(r[0].endswith(".dist-info/RECORD") for r in rows),
        "requestedMarkers": sum(r[0].endswith(".dist-info/REQUESTED") for r in rows),
    }


def verify(path):
    value = fingerprints(path)
    if (
        value["stable"] != EXPECTED_STABLE
        or value["application"] != EXPECTED_APPLICATION
        or value["stableMembers"] != 3763
        or value["recordFiles"] != 37
        or (value["members"], value["requestedMarkers"]) not in ((3808, 8), (3837, 37))
    ):
        raise ValueError(
            "source build does not match the reviewed candidate payload: "
            + json.dumps(value)
        )
    return value


if __name__ == "__main__":
    path = Path(sys.argv[1])
    try:
        result = verify(path)
    except ValueError:
        # CI supplies a freshly built PUBLIC source/PyPI artifact, never the
        # private deployed zip. Emit hashes only, so differing build metadata
        # can be compared locally without uploading a deployed file or body.
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                if not name.startswith("decision_flywheel/"):
                    print(
                        "public-build-file "
                        + json.dumps(
                            [name, hashlib.sha256(archive.read(name)).hexdigest()]
                        )
                    )
        raise
    print(json.dumps({"reviewedPayloadComparison": "PASS", **result}))
