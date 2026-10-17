"""Complete per-invocation S3 storage for the existing console WorkspaceSession.

The caller owns an explicitly authorized, fenced command. No timeout takeover,
local path, SQLite fallback or DynamoDB authority is introduced.
"""
from hashlib import sha256
from uuid import uuid4

from .journal import encode
from .s3_batch_storage import S3BatchStorage
from .s3_embedded_storage import S3EmbeddedStorage
from .s3_runtime_storage import S3RuntimeStores


class S3WorkspaceStores:
    def __init__(self,root_journal,*,journal_for,max_records,execution_guard=None):
        if not callable(journal_for):raise ValueError('fixed scoped journal factory required')
        self.root=S3EmbeddedStorage(root_journal,max_records=max_records,cache_owned=True)
        self.journal_for,self.max_records=journal_for,max_records
        self.execution_guard=execution_guard
        self.owner=str(uuid4());self.runtimes={};self.shared=None
        self.name=sha256(root_journal.namespace.encode()).hexdigest()

    def _journal(self,domain,name):
        journal=self.journal_for(domain,name)
        original=self.root.journal
        if journal.boundary!=original.boundary:raise ValueError('workspace storage scope mismatch')
        return journal

    def acquire(self,config):
        self.root.initialize();self.root.acquire(self.owner)
        try:
            if self.execution_guard is not None:self.execution_guard()
            raw=encode({k:v for k,v in config.items() if k not in ('max_requests','max_optimizer_calls')}).decode();existing=self.root.meta('workspace-config')
            if existing is not None and existing!=(raw,):raise ValueError('frozen workspace configuration changed')
            if existing is None:self.root.put_meta('workspace-config',raw)
            self.shared=S3BatchStorage(self._journal('shared-batches',self.name),max_records=self.max_records)
            self.shared.initialize()
        except BaseException:
            self.close();raise

    def runtime(self,identifier):
        if self.root.authority is None or self.shared is None:raise ValueError('workspace ownership required')
        if identifier not in self.runtimes:
            name=sha256((self.name+'\0'+identifier).encode()).hexdigest()
            runtime=S3RuntimeStores(self._journal('runtime',name),max_records=self.max_records,cache_owned=True)
            runtime.initialize();self.runtimes[identifier]=runtime
            runtime.acquire(self.owner)
            if self.execution_guard is not None:self.execution_guard()
        return self.runtimes[identifier]

    def refresh(self):
        self.root.refresh()
        for runtime in self.runtimes.values():runtime.refresh()

    def close(self):
        errors=[]
        for runtime in self.runtimes.values():
            try:runtime.close()
            except BaseException as error:errors.append(error)
        try:self.root.release()
        except BaseException as error:errors.append(error)
        if errors:raise errors[0]
