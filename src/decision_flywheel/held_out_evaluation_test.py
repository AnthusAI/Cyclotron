"""A held-out set is kept as its own record in the root store, in either storage adapter."""
import json

from .embedded_cyclotron import Cyclotron
from .held_out_test import RubricReadingModel, decide_and_review, learning_definition


def test_each_frozen_set_is_stored_as_its_own_record_with_an_index_in_the_local_store(tmp_path):
    with Cyclotron.open(tmp_path / "c", learning_definition(), RubricReadingModel(), max_requests=500) as cyclotron:
        decide_and_review(cyclotron, 30)
        first = cyclotron.freeze_held_out_set()
        decide_and_review(cyclotron, 30, prefix="later")
        second = cyclotron.freeze_held_out_set()
        stored = cyclotron.root_storage
        assert json.loads(stored.meta("held_out_set_ids:relevant")[0]) == [first["id"], second["id"]]
        assert json.loads(stored.meta(f"held_out_set:relevant:{first['id']}")[0])["id"] == first["id"]
        assert json.loads(stored.meta(f"held_out_set:relevant:{second['id']}")[0])["id"] == second["id"]
        assert stored.meta("held_out_sets:relevant") is None


def test_each_frozen_set_is_stored_as_its_own_record_with_an_index_in_the_cloud_store():
    from .s3_embedded_storage import S3CyclotronStores
    from .s3_journal_test import ConditionalS3, journal
    client = ConditionalS3()
    stores = S3CyclotronStores(journal(client, name="held-out-root"), max_records=10000,
                               journal_for=lambda domain, name: journal(client, name=domain + "-" + name))
    with Cyclotron.open(None, learning_definition(), RubricReadingModel(), storage=stores, max_requests=500) as cyclotron:
        decide_and_review(cyclotron, 3, audit_only=True)
        frozen = cyclotron.freeze_held_out_set()
        assert json.loads(cyclotron.root_storage.meta("held_out_set_ids:relevant")[0]) == [frozen["id"]]
        assert cyclotron.root_storage.meta(f"held_out_set:relevant:{frozen['id']}") is not None
        assert cyclotron.root_storage.meta("held_out_sets:relevant") is None


def test_sets_saved_as_one_list_per_classifier_stay_readable_and_new_sets_follow_them(tmp_path):
    with Cyclotron.open(tmp_path / "c", learning_definition(), RubricReadingModel(), max_requests=500) as cyclotron:
        decide_and_review(cyclotron, 30)
        first = cyclotron.freeze_held_out_set()
        stored = cyclotron.root_storage
        legacy_list = [json.loads(stored.meta(f"held_out_set:relevant:{first['id']}")[0])]
        with stored.transaction():
            stored.db.execute("DELETE FROM meta WHERE key LIKE 'held_out_set%'")
            stored.put_meta("held_out_sets:relevant", json.dumps(legacy_list))
        assert cyclotron.held_out_sets() == [first]
        assert cyclotron.freeze_held_out_set() == first
        decide_and_review(cyclotron, 30, prefix="later")
        second = cyclotron.freeze_held_out_set()
        assert [row["id"] for row in cyclotron.held_out_sets()] == [first["id"], second["id"]]
        assert cyclotron.held_out_request_bound(set_id=first["id"]) == 2 * first["items"]
