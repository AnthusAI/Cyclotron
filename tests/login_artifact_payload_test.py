"""Public source builds are compared by file payload, independent of zip encoding."""

import zipfile
import base64
import hashlib
import pytest

from scripts.verify_login_artifact import fingerprints, verify


def archive(path, rows):
    with zipfile.ZipFile(path, "w") as output:
        for name, body in rows:
            output.writestr(name, body)
    return path


def test_payload_identity_ignores_archive_order_but_detects_application_and_dependency_changes(
    tmp_path,
):
    rows = [
        ("decision_flywheel/example.py", b"synthetic app"),
        ("dependency.py", b"synthetic dependency"),
    ]
    first = fingerprints(archive(tmp_path / "one.zip", rows))
    assert fingerprints(archive(tmp_path / "two.zip", rows[::-1])) == first
    dep = fingerprints(
        archive(tmp_path / "dep.zip", [rows[0], ("dependency.py", b"changed")])
    )
    assert dep["application"] == first["application"] and dep["full"] != first["full"]
    app = fingerprints(
        archive(
            tmp_path / "app.zip",
            [("decision_flywheel/example.py", b"changed"), rows[1]],
        )
    )
    assert app["application"] != first["application"]


def test_unreviewed_payload_and_duplicate_members_cannot_pass_verification(tmp_path):
    path = archive(
        tmp_path / "wrong.zip", [("decision_flywheel/example.py", b"synthetic")]
    )
    with pytest.raises(ValueError, match="reviewed"):
        verify(path)
    with pytest.warns(UserWarning):
        path = archive(tmp_path / "duplicate.zip", [("one", b"a"), ("one", b"b")])
    with pytest.raises(ValueError, match="duplicate"):
        fingerprints(path)


def test_only_validated_installer_bookkeeping_is_normalized(tmp_path):
    record_name = "synthetic-1.dist-info/RECORD"
    requested_name = "synthetic-1.dist-info/REQUESTED"

    def records(requested=False, corrupt=False):
        body = b"synthetic dependency"
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")
        )
        record = f"dependency.py,sha256={digest},{len(body)}\n{record_name},,\n"
        rows = [("dependency.py", body)]
        if requested:
            empty_digest = (
                base64.urlsafe_b64encode(hashlib.sha256(b"").digest())
                .decode()
                .rstrip("=")
            )
            record += f"{requested_name},sha256={empty_digest},0\n"
            rows.append((requested_name, b""))
        if corrupt:
            record = record.replace(digest, "wrong")
        return rows + [(record_name, record.encode())]

    first = fingerprints(archive(tmp_path / "original.zip", records()))
    second = fingerprints(
        archive(tmp_path / "reinstalled.zip", records(requested=True))
    )
    assert first["stable"] == second["stable"] and first["full"] != second["full"]
    changed = records(requested=True)
    changed[1] = (requested_name, b"unexpected payload")
    with pytest.raises(ValueError, match="REQUESTED"):
        fingerprints(archive(tmp_path / "nonempty.zip", changed))
    with pytest.raises(ValueError, match="RECORD"):
        fingerprints(archive(tmp_path / "bad-record.zip", records(corrupt=True)))
