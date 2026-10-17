"""Existing catalog API, delegating persistence to a domain storage contract."""
from .catalog_storage import SQLiteCatalogStorage


class WorkspaceCatalog:
    def initialize_catalog(self):
        if self.catalog_storage is None:
            self.catalog_storage = SQLiteCatalogStorage(
                lambda: self.connect(),
                lambda db, identifier, revision: self.advance_classifier_definitions(db, identifier, revision))

    def save_classifier(self, identifier, name, config):
        return self.catalog_storage.save_classifier(identifier, name, config)

    def classifier(self, identifier, revision=None):
        return self.catalog_storage.classifier(identifier, revision)

    def classifiers(self):
        return self.catalog_storage.classifiers()

    def classifier_versions(self, identifier):
        return self.catalog_storage.classifier_versions(identifier)

    def save_item_list(self, identifier, name):
        return self.catalog_storage.save_item_list(identifier, name)

    def item_lists(self):
        return self.catalog_storage.item_lists()

    def upsert_list_items(self, list_id, items):
        return self.catalog_storage.upsert_list_items(list_id, items)

    def list_items(self, list_id, *, after=0, limit=200):
        return self.catalog_storage.list_items(list_id, after=after, limit=limit)

    def item_revision(self, list_id, identifier, revision):
        return self.catalog_storage.item_revision(list_id, identifier, revision)

    def snapshot_items(self, list_id):
        return self.catalog_storage.snapshot_items(list_id)

    def label_item(self, classifier_id, classifier_revision, list_id, item_id, item_revision, label, comment, request_id):
        return self.catalog_storage.label_item(classifier_id, classifier_revision, list_id, item_id, item_revision, label, comment, request_id)

    def item_labels(self, list_id, item_id, item_revision):
        return self.catalog_storage.item_labels(list_id, item_id, item_revision)

    def retract_item_label(self, feedback_request_id, request_id):
        return self.catalog_storage.retract_item_label(feedback_request_id, request_id)

    def record_item_prediction(self, run_id, list_id, item_id, item_revision, prediction):
        return self.catalog_storage.record_item_prediction(run_id, list_id, item_id, item_revision, prediction)

    def item_results(self, list_id, item_id, item_revision):
        return self.catalog_storage.item_results(list_id, item_id, item_revision)
