"""Known embedded/reviewer/evaluation codecs over one shared migration engine."""
import json
import sqlite3
from .catalog_archive import CatalogArchive
from .native_domain_snapshot import NativeDomainCodec


def _embedded_records(snapshot):
    tables=snapshot['tables']
    items={row['id'] for row in tables['items']}
    decisions={row['id'] for row in tables['decisions']}
    usages={row['id'] for row in tables['usage']}
    for row in tables['decisions']:
        if row['item_id'] not in items: raise ValueError('embedded decision references unknown item')
        payload=json.loads(row['payload'])
        if payload.get('decisionId')!=row['id'] or payload.get('itemId')!=row['item_id']:
            raise ValueError('embedded decision identity mismatch')
    for row in tables['reviews']:
        if row['decision_id'] not in decisions: raise ValueError('embedded review references unknown decision')
        payload=json.loads(row['payload'])
        if payload.get('review_id')!=row['id'] or payload.get('decision_id')!=row['decision_id'] or payload.get('classifier')!=row['classifier']:
            raise ValueError('embedded review identity mismatch')
    if any(row['decision_id'] not in decisions for row in tables['usage']): raise ValueError('embedded usage references unknown decision')
    if any(row['usage_id'] not in usages for row in tables['usage_shares']): raise ValueError('embedded usage share references unknown request')


def embedded_codec():
    from .embedded_storage import SCHEMA_SQL
    return NativeDomainCodec('cyclotron-embedded-root/v1',lambda db:db.executescript(SCHEMA_SQL),
        json_columns={'meta':('value',),'items':('item_values',),'decisions':('payload',),
                      'reviews':('payload',),'events':('payload',)},validate_records=_embedded_records)


def review_codec():
    from .review_storage import SQLiteReviewStorage
    def create(db):
        db.row_factory=sqlite3.Row
        adapter=object.__new__(SQLiteReviewStorage)
        adapter.db=db
        adapter._create_schema()
    return NativeDomainCodec('cyclotron-review-source/v1',create,
        json_columns={'articles':('categories_json',)},flexible_columns=True)


def evaluation_codec():
    def create(db):
        db.executescript('''CREATE TABLE execution(plan TEXT PRIMARY KEY, clock TEXT NOT NULL);
            CREATE TABLE trace(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, payload TEXT NOT NULL);''')
    return NativeDomainCodec('cyclotron-evaluation/v1',create,json_columns={'trace':('payload',)},sequences={'trace':'id'})


class DomainArchive(CatalogArchive):
    """Reuse fixed-scope immutable records/manifests, with a known native codec."""
    def __init__(self,codec,payloads,scope,*,max_records,max_bytes):
        if not isinstance(codec,NativeDomainCodec): raise ValueError('known native codec required')
        self.codec=codec
        self.columns=codec.columns
        self.snapshot_schema=codec.schema
        self.manifest_schema=codec.schema.removesuffix('/v1')+'-manifest/v1'
        self.schema_field='domainSchema'
        self.metadata_field='nativeMetadata'
        self.snapshot_metadata_field='metadata'
        self.validator=codec.validate
        super().__init__(payloads,scope,max_records=max_records,max_bytes=max_bytes)

    def _check_metadata(self,value):
        if not isinstance(value,dict) or set(value)!={'sequences'} or not isinstance(value['sequences'],dict):
            raise ValueError('invalid native domain metadata')
        if set(value['sequences'])!=set(self.codec.sequences): raise ValueError('invalid native domain sequences')
        if any(type(number) is not int or not 0<=number<2**63 for number in value['sequences'].values()):
            raise ValueError('invalid native domain sequence watermark')
