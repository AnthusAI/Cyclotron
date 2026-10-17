"""A store snapshot is one archive of every store file, with a digest of each."""
import tarfile

from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import DEFINITION, Model, item, run
from .store_snapshot import STORE_FILES, classifier_store_name, store_paths


def test_the_archive_lists_every_store_file_of_the_definition(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        run(cyclotron.decide(item("one", "one")))
        manifest = cyclotron.snapshot(tmp_path / "archive.tar.gz")
    names = {entry["name"] for entry in manifest["files"]}
    assert names == set(store_paths(tmp_path / "c", DEFINITION))
    assert set(STORE_FILES) < names and classifier_store_name("relevant") in names
    with tarfile.open(tmp_path / "archive.tar.gz") as archive:
        assert set(archive.getnames()) == names | {"manifest.json"}

