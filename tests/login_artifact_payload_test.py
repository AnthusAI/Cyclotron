"""Public source builds are compared by file payload, independent of zip encoding."""

import zipfile
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
