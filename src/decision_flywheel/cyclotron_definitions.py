"""Definition API shared by local, remote and embedded consumers."""
from .definition_storage import SQLiteDefinitionStorage


class CyclotronDefinitions:
    def initialize_cyclotron_definitions(self):
        if self.definition_storage is None:
            self.definition_storage = SQLiteDefinitionStorage(lambda: self.connect(), lambda *args: self.classifier(*args))

    def run_cyclotron_definition(self, run_id):
        return self.definition_storage.run_cyclotron_definition(run_id)

    def cyclotron_definition_comparison(self, identifier, before_revision, after_revision):
        return self.definition_storage.cyclotron_definition_comparison(identifier, before_revision, after_revision)

    def save_cyclotron_definition(self, identifier, name, classifiers, settings):
        return self.definition_storage.save_cyclotron_definition(identifier, name, classifiers, settings)

    def cyclotron_definition(self, identifier, revision=None):
        return self.definition_storage.cyclotron_definition(identifier, revision)

    def cyclotron_definitions(self):
        return self.definition_storage.cyclotron_definitions()

    def cyclotron_definition_versions(self, identifier):
        return self.definition_storage.cyclotron_definition_versions(identifier)

    def activate_cyclotron_definition(self, identifier, revision):
        return self.definition_storage.activate_cyclotron_definition(identifier, revision)

    def register_run_definition(self, db, run_id, identifier, name, config):
        """Legacy native composition; never a portable SQL interface."""
        hook = getattr(self.definition_storage, "register_run_definition", None)
        if hook is None: raise NotImplementedError("native definition composition unavailable")
        return hook(db, run_id, identifier, name, config)

    def advance_classifier_definitions(self, db, identifier, revision):
        """Legacy native composition; never a portable SQL interface."""
        hook = getattr(self.definition_storage, "advance_classifier_definitions", None)
        if hook is None: raise NotImplementedError("native definition composition unavailable")
        return hook(db, identifier, revision)
