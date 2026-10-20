"""Compare a public source-built zip to reviewed local payload commitments.

No deployed archive, credentials, per-file private metadata, or cloud access is
needed in CI. Fingerprints ignore compression/timestamps but include every byte
of every application and dependency file. No artifact is uploaded.
"""

import hashlib
import json
from pathlib import Path
import sys
import zipfile

EXPECTED_FULL = "1bddd15ad0972044a0196856be25e94c2086afb044e9c66ccbfffa2d0f0f5d38"
EXPECTED_APPLICATION = (
    "bb996ecf007b69541cad91bb2fa5effac5765020e86b62572787158c1c31a02d"
)


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
    }


def verify(path):
    value = fingerprints(path)
    if value != {
        "full": EXPECTED_FULL,
        "application": EXPECTED_APPLICATION,
        "members": 3808,
    }:
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
