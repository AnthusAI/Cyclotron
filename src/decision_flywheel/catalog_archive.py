"""Immutable catalog migration records, independent of cloud write coordination.

This transports a bounded validated catalog snapshot. It is NOT an implementation
of the live CatalogStorage mutation contract or an activation/head protocol.
"""
from dataclasses import dataclass
from hashlib import sha256

from .batch_storage import StorageScope
from .catalog_snapshot import COLUMNS, SCHEMA, budgets, decode, encode, validate
from .storage_payloads import PayloadRef

MANIFEST_SCHEMA='cyclotron-catalog-manifest/v1'


@dataclass(frozen=True)
class CatalogPublication:
    manifest: PayloadRef
    records: tuple[PayloadRef,...]


class CatalogArchive:
    """Fixed authorized scope; no credentials, index authority or mutable head.

    A caller may rebuild discovery from returned verified references. Importers
    must use a known canonical manifest, not infer membership from index results.
    Orphan immutable records after a failed publish are safe and not activated.
    """
    columns=COLUMNS
    snapshot_schema=SCHEMA
    manifest_schema=MANIFEST_SCHEMA
    schema_field='catalogSchema'
    metadata_field='feedbackSequence'
    snapshot_metadata_field='feedback_sequence'
    validator=staticmethod(validate)

    def _check_metadata(self,value):
        if type(value) is not int or value<0: raise ValueError('invalid catalog sequence watermark')

    def __init__(self, payloads, scope, *, max_records, max_bytes):
        budgets(max_records,max_bytes)
        if not isinstance(scope,StorageScope) or getattr(payloads,'scope',None)!=scope:
            raise PermissionError('fixed canonical catalog scope required')
        self.payloads,self.scope=payloads,scope
        self.max_records,self.max_bytes=max_records,max_bytes

    def publish(self, snapshot):
        snapshot=self.validator(snapshot,max_records=self.max_records,max_bytes=self.max_bytes)
        contents=[]
        records=[]
        for table in self.columns:
            for row in snapshot['tables'][table]:
                content=encode({'table':table,'row':row})
                # Compute references before publication so aggregate budgets fail
                # before side effects. Payloads independently verifies each put.
                reference=PayloadRef(self.scope,sha256(content).hexdigest(),len(content))
                contents.append(content)
                records.append({'table':table,'digest':reference.digest,'length':reference.length})
        manifest=encode({'schema':self.manifest_schema,self.schema_field:self.snapshot_schema,
            self.metadata_field:snapshot[self.snapshot_metadata_field],'records':records})
        if len(manifest)+sum(map(len,contents))>self.max_bytes:
            raise ValueError('canonical catalog aggregate byte budget exceeded')
        references=tuple(self.payloads.put(content) for content in contents)
        return CatalogPublication(self.payloads.put(manifest),references)

    def read(self, reference):
        if not isinstance(reference,PayloadRef) or reference.scope!=self.scope:
            raise PermissionError('canonical catalog scope refused')
        if reference.length>self.max_bytes: raise ValueError('canonical catalog aggregate byte budget exceeded')
        manifest=decode(self.payloads.get(reference))
        if (not isinstance(manifest,dict) or set(manifest)!={'schema',self.schema_field,self.metadata_field,'records'}
                or manifest['schema']!=self.manifest_schema or manifest[self.schema_field]!=self.snapshot_schema):
            raise ValueError('invalid canonical catalog manifest')
        self._check_metadata(manifest[self.metadata_field])
        rows=manifest['records']
        if not isinstance(rows,list) or len(rows)>self.max_records: raise ValueError('canonical catalog record budget exceeded')
        total=reference.length
        references=[]
        seen=set()
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'table','digest','length'} or not isinstance(row['table'],str) or row['table'] not in self.columns:
                raise ValueError('invalid canonical catalog membership')
            candidate=PayloadRef(self.scope,row['digest'],row['length'])
            if candidate.digest in seen: raise ValueError('duplicate canonical catalog membership')
            seen.add(candidate.digest)
            total+=candidate.length
            if total>self.max_bytes: raise ValueError('canonical catalog aggregate byte budget exceeded')
            references.append(candidate)
        tables={table:[] for table in self.columns}
        for member,candidate in zip(rows,references):
            envelope=decode(self.payloads.get(candidate))
            if not isinstance(envelope,dict) or set(envelope)!={'table','row'} or envelope['table']!=member['table']:
                raise ValueError('canonical catalog record/manifest mismatch')
            tables[member['table']].append(envelope['row'])
        return self.validator({'schema':self.snapshot_schema,'tables':tables,self.snapshot_metadata_field:manifest[self.metadata_field]},
            max_records=self.max_records,max_bytes=self.max_bytes)


class LegacyCatalogArchive(CatalogArchive):
    """Read known v1 manifests explicitly and upgrade their validated records."""
    columns={table:columns for table,columns in COLUMNS.items() if table!='priming_drafts'}
    snapshot_schema='cyclotron-catalog/v1'
    def publish(self,snapshot): raise ValueError('legacy archive is read-only; publish v2')
