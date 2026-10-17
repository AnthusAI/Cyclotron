"""Native console composition over shared catalog, definition and run APIs."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .cyclotron_definitions import CyclotronDefinitions
from .cyclotrons import Cyclotrons
from .run_storage import _UNSET, SQLiteRunStorage
from .workspace_catalog import WorkspaceCatalog


class WebStore(WorkspaceCatalog,Cyclotrons,CyclotronDefinitions):
    def __init__(self, path, *, catalog_storage=None, definition_storage=None, run_storage=None, priming_storage=None):
        self.catalog_storage, self.definition_storage = catalog_storage, definition_storage
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # WebStore remains the native composition. Other domain repositories
        # still use its SQLite file; an injected domain is not hosted activation.
        native = SQLiteRunStorage(lambda: self.connect(), lambda *args: self.register_run_definition(*args))
        self.run_storage = run_storage if run_storage is not None else native
        self.initialize_catalog()
        from .priming_storage import SQLitePrimingStorage
        self.priming_storage = priming_storage if priming_storage is not None else SQLitePrimingStorage(self.connect)
        self.initialize_cyclotrons()
        self.initialize_cyclotron_definitions()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()


    # Legacy Cyclotrons composition uses these pure record decoders. Reuse the
    # adapter's helpers so the facade does not duplicate their implementation.
    _run = staticmethod(SQLiteRunStorage._run)
    _event = staticmethod(SQLiteRunStorage._event)
    _job = staticmethod(SQLiteRunStorage._job)

    def create_run(self, name, mode, config, *, items=(), frozen_feedback=(), initial_events=()):
        return self.run_storage.create_run(name, mode, config, items=items, frozen_feedback=frozen_feedback, initial_events=initial_events)

    def run(self, run_id):
        return self.run_storage.run(run_id)

    def runs(self):
        return self.run_storage.runs()

    def run_cyclotron_id(self, run_id):
        return self.run_storage.run_cyclotron_id(run_id)

    def labeling_access(self, run_id):
        return self.run_storage.labeling_access(run_id)

    def counts(self, run_id):
        return self.run_storage.counts(run_id)

    def set_status(self, run_id, status):
        return self.run_storage.set_status(run_id, status)

    def update_run_limits(self, run_id, max_requests, max_optimizer_calls):
        return self.run_storage.update_run_limits(run_id, max_requests, max_optimizer_calls)

    def summary(self, run_id):
        return self.run_storage.summary(run_id)

    def complete_run(self, run_id, summary):
        return self.run_storage.complete_run(run_id, summary)

    def append_batch(self, run_id, events):
        return self.run_storage.append_batch(run_id, events)

    def append_event(self, run_id, source_id, payload):
        return self.run_storage.append_event(run_id, source_id, payload)

    def events(self, run_id, *, after=0, limit=1000):
        return self.run_storage.events(run_id, after=after, limit=limit)

    def all_events(self, run_id):
        return self.run_storage.all_events(run_id)

    def event_cursor(self, run_id):
        return self.run_storage.event_cursor(run_id)

    def command(self, run_id, request_id, kind, payload):
        return self.run_storage.command(run_id, request_id, kind, payload)

    def resume_feedback_command(self, run_id, job_id):
        return self.run_storage.resume_feedback_command(run_id, job_id)

    def jobs(self, run_id):
        return self.run_storage.jobs(run_id)

    def claim_command(self):
        return self.run_storage.claim_command()

    def finish_command(self, job_id, status, result):
        return self.run_storage.finish_command(job_id, status, result)

    def recover_interrupted(self):
        return self.run_storage.recover_interrupted()

    def current_item(self, run_id):
        return self.run_storage.current_item(run_id)

    def update_item(self, run_id, item_id, *, prediction=_UNSET, reviewed=_UNSET):
        return self.run_storage.update_item(run_id, item_id, prediction=prediction, reviewed=reviewed)

    def items(self, run_id):
        return self.run_storage.items(run_id)

    def unreviewed_items(self, run_id):
        return self.run_storage.unreviewed_items(run_id)

    def export_storage(self, *, max_records=10000, max_bytes=64000000):
        """Native composition migration; not a hosted authority selector."""
        from .console_snapshot import export_console
        return export_console(self.connect,max_records=max_records,max_bytes=max_bytes)

    def restore_storage(self,snapshot, *, max_records=10000, max_bytes=64000000):
        from .console_snapshot import restore_console
        return restore_console(self.connect,snapshot,max_records=max_records,max_bytes=max_bytes)
