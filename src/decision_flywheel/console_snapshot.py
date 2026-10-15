"""Versioned native console migration; never a cloud mutation coordinator.

Only the known native schema is accepted. Imported SQL is never executed. Physical
row ordinals preserve existing item order, command dispatch and latest-job rules.
"""
from hashlib import sha256

from .catalog_archive import CatalogArchive
from .catalog_snapshot import COLUMNS as CATALOG_COLUMNS, KEYS as CATALOG_KEYS
from .catalog_snapshot import budgets, decode, encode, validate as validate_catalog


SCHEMA='cyclotron-console/v1'
TABLES={**CATALOG_COLUMNS,
    'web_runs':('id','name','mode','status','created_at','config'),
    'web_events':('sequence','run_id','source_id','payload'),
    'web_jobs':('id','run_id','request_id','kind','payload','status','result'),
    'web_items':('run_id','id','payload','prediction','reviewed'),
    'web_summaries':('run_id','payload'),
    'cyclotrons':('id','name','active_revision'),
    'cyclotron_versions':('cyclotron_id','revision','run_id','parent_run_id','created_at'),
    'replay_feedback':('run_id','item_id','classifier_id','payload'),
    'cyclotron_checkpoints':('id','run_id','fingerprint','payload','created_at'),
    'matched_evaluation_inputs':('run_id','payload'),
    'matched_evaluation_authorizations':('request_id','run_id','payload'),
    'cyclotron_definitions':('id','revision','name','classifiers','settings','fingerprint','created_at'),
    'cyclotron_catalog':('id','active_revision'),
    'run_cyclotron_definitions':('run_id','cyclotron_id','definition_revision'),
}
KEYS={**CATALOG_KEYS,
    'web_runs':('id',),'web_events':('sequence',),'web_jobs':('id',),
    'web_items':('run_id','id'),'web_summaries':('run_id',),'cyclotrons':('id',),
    'cyclotron_versions':('cyclotron_id','revision'),'replay_feedback':('run_id','item_id','classifier_id'),
    'cyclotron_checkpoints':('id',),'matched_evaluation_inputs':('run_id',),
    'matched_evaluation_authorizations':('request_id',),'cyclotron_definitions':('id','revision'),
    'cyclotron_catalog':('id',),'run_cyclotron_definitions':('run_id',),
}
SEQUENCES={'item_feedback':'sequence','web_events':'sequence','cyclotron_checkpoints':'id'}
NULLABLE={('web_jobs','result'),('web_items','prediction'),('cyclotron_versions','parent_run_id')}
INTS={'revision','sequence','classifier_revision','item_revision','active_revision','definition_revision','reviewed'}
JSON_FIELDS={'web_runs':('config',),'web_events':('payload',),'web_jobs':('payload','result'),
    'web_items':('payload','prediction'),'web_summaries':('payload',),'replay_feedback':('payload',),
    'cyclotron_checkpoints':('payload',),'matched_evaluation_inputs':('payload',),
    'matched_evaluation_authorizations':('payload',),'cyclotron_definitions':('classifiers','settings')}


def validate(snapshot, *, max_records=10000,max_bytes=64000000):
    budgets(max_records,max_bytes)
    content=encode(snapshot)
    if len(content)>max_bytes: raise ValueError('console snapshot byte budget exceeded')
    snapshot=decode(content)
    if not isinstance(snapshot,dict) or set(snapshot)!={'schema','tables','sequences'} or snapshot['schema']!=SCHEMA:
        raise ValueError('unsupported console snapshot')
    tables=snapshot['tables']
    if not isinstance(tables,dict) or set(tables)!=set(TABLES) or not all(isinstance(rows,list) for rows in tables.values()):
        raise ValueError('complete native console tables required')
    if sum(map(len,tables.values()))>max_records: raise ValueError('console snapshot record budget exceeded')
    for table,columns in TABLES.items():
        ordinals,identities=set(),set()
        for row in tables[table]:
            if not isinstance(row,dict) or set(row)!=set(columns)|{'_rowid'}: raise ValueError('invalid console row columns')
            ordinal=row['_rowid']
            if type(ordinal) is not int or not 1<=ordinal<2**63 or ordinal in ordinals: raise ValueError('invalid console row ordinal')
            ordinals.add(ordinal)
            for column in columns:
                value=row[column]
                if value is None and (table,column) in NULLABLE: continue
                if column in INTS or (table,column)==('cyclotron_checkpoints','id'):
                    if type(value) is not int or not (0 if column=='reviewed' else 1)<=value<2**63:
                        raise ValueError('invalid native integer')
                    if column=='reviewed' and value not in (0,1): raise ValueError('invalid reviewed flag')
                elif not isinstance(value,str): raise ValueError('invalid native text')
            key=tuple(row[column] for column in KEYS[table])
            if key in identities: raise ValueError('duplicate console identity')
            identities.add(key)
            if table in SEQUENCES and ordinal!=row[SEQUENCES[table]]: raise ValueError('native sequence/ordinal mismatch')
            for column in JSON_FIELDS.get(table,()):
                if row[column] is not None: decode(row[column])
    sequences=snapshot['sequences']
    if not isinstance(sequences,dict) or set(sequences)!=set(SEQUENCES): raise ValueError('complete native sequence watermarks required')
    for table,column in SEQUENCES.items():
        value=sequences[table]
        if type(value) is not int or not max((row[column] for row in tables[table]),default=0)<=value<2**63:
            raise ValueError('invalid native sequence watermark')
    validate_catalog({'schema':'cyclotron-catalog/v1','feedback_sequence':sequences['item_feedback'],
        'tables':{table:[{key:value for key,value in row.items() if key!='_rowid'} for row in tables[table]] for table in CATALOG_COLUMNS}},
        max_records=max_records,max_bytes=max_bytes)
    runs={row['id']:row for row in tables['web_runs']}
    for row in runs.values():
        if row['mode'] not in ('live','recorded') or not isinstance(decode(row['config']),dict): raise ValueError('invalid frozen run')
    for table in ('web_events','web_jobs','web_items','web_summaries','replay_feedback','cyclotron_checkpoints','matched_evaluation_inputs','matched_evaluation_authorizations','run_cyclotron_definitions','cyclotron_versions'):
        for row in tables[table]:
            if row['run_id'] not in runs: raise ValueError('console record references unknown run')
    for table,columns in (('web_events',('run_id','source_id')),('web_jobs',('run_id','request_id')),
        ('cyclotron_versions',('run_id',)),('cyclotron_checkpoints',('run_id','fingerprint'))):
        values=[tuple(row[column] for column in columns) for row in tables[table]]
        if len(values)!=len(set(values)): raise ValueError('duplicate native operation identity')
    families={row['id']:row for row in tables['cyclotrons']}
    editions={(row['cyclotron_id'],row['revision']) for row in tables['cyclotron_versions']}
    for row in tables['cyclotron_versions']:
        if row['cyclotron_id'] not in families or row['parent_run_id'] is not None and row['parent_run_id'] not in runs:
            raise ValueError('invalid edition family/parent')
    for row in families.values():
        if (row['id'],row['active_revision']) not in editions: raise ValueError('unknown active run edition')
    classifiers={(row['id'],row['revision']) for row in tables['classifiers']}
    definitions={}
    for row in tables['cyclotron_definitions']:
        refs,settings=decode(row['classifiers']),decode(row['settings'])
        if not isinstance(refs,list) or not isinstance(settings,dict): raise ValueError('invalid frozen definition')
        ids=set()
        for ref in refs:
            if (not isinstance(ref,dict) or set(ref)!={'id','revision'} or not isinstance(ref['id'],str)
                    or type(ref['revision']) is not int or (ref['id'],ref['revision']) not in classifiers or ref['id'] in ids):
                raise ValueError('definition references unknown classifier revision')
            ids.add(ref['id'])
        # Native definition hashes use ensure_ascii=True; retain raw JSON text.
        from .definition_storage import encode as native_encode
        if sha256(native_encode({'name':row['name'],'classifiers':refs,'settings':settings}).encode()).hexdigest()!=row['fingerprint']:
            raise ValueError('definition fingerprint mismatch')
        definitions[row['id'],row['revision']]=row
    for row in tables['cyclotron_catalog']:
        if (row['id'],row['active_revision']) not in definitions: raise ValueError('unknown active definition')
    for row in tables['run_cyclotron_definitions']:
        if (row['cyclotron_id'],row['definition_revision']) not in definitions: raise ValueError('unknown run definition pin')
        from .cyclotrons import load_config
        config=load_config(runs[row['run_id']]['config'])
        if config.get('cyclotron_definition_revision',row['definition_revision'])!=row['definition_revision'] or config.get('cyclotron_id',row['cyclotron_id'])!=row['cyclotron_id']:
            raise ValueError('run configuration/definition pin mismatch')
        members=config.get('classifiers')
        if members is not None:
            if not isinstance(members,list) or any(not isinstance(member,dict) or 'id' not in member or 'revision' not in member for member in members):
                raise ValueError('invalid frozen classifier pin')
            pinned=decode(definitions[row['cyclotron_id'],row['definition_revision']]['classifiers'])
            if [{'id':member['id'],'revision':member['revision']} for member in members]!=pinned:
                raise ValueError('run classifier/definition pin mismatch')
    return snapshot


def _schema(db):
    actual={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if actual!=set(TABLES): raise ValueError('unsupported native console schema tables')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():
        raise ValueError('unsupported native console schema behavior')
    for table,columns in TABLES.items():
        if {row[1] for row in db.execute(f'PRAGMA table_info({table})')}!=set(columns):
            raise ValueError('unsupported native console schema columns')


def export_console(connect, *, max_records=10000,max_bytes=64000000):
    budgets(max_records,max_bytes)
    with connect() as db:
        db.execute('BEGIN')
        _schema(db)
        if sum(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in TABLES)>max_records:
            raise ValueError('console snapshot record budget exceeded')
        raw_bytes=0
        for table,columns in TABLES.items():
            expression='+'.join(f'COALESCE(length(CAST({column} AS BLOB)),0)' for column in columns)
            raw_bytes+=db.execute(f'SELECT COALESCE(SUM({expression}),0) FROM {table}').fetchone()[0]
        if raw_bytes>max_bytes: raise ValueError('console snapshot byte budget exceeded')
        tables={};used=0
        for table,columns in TABLES.items():
            rows=[]
            for row in db.execute(f'SELECT rowid AS _rowid,{",".join(columns)} FROM {table} ORDER BY rowid'):
                record=dict(row);used+=len(encode(record))
                if used>max_bytes: raise ValueError('console snapshot byte budget exceeded')
                rows.append(record)
            tables[table]=rows
        watermarks=dict(db.execute('SELECT name,seq FROM sqlite_sequence'))
        return validate({'schema':SCHEMA,'tables':tables,'sequences':{table:watermarks.get(table,0) for table in SEQUENCES}},
            max_records=max_records,max_bytes=max_bytes)


def restore_console(connect,snapshot, *, max_records=10000,max_bytes=64000000):
    snapshot=validate(snapshot,max_records=max_records,max_bytes=max_bytes)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _schema(db)
        if any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in TABLES):
            raise ValueError('console restore requires empty native composition')
        for table,columns in TABLES.items():
            names=('_rowid',*columns)
            db.executemany(f'INSERT INTO {table}(rowid,{",".join(columns)}) VALUES ({",".join("?" for _ in names)})',
                [tuple(row[name] for name in names) for row in snapshot['tables'][table]])
        for table,value in snapshot['sequences'].items():
            db.execute('DELETE FROM sqlite_sequence WHERE name=?',(table,))
            db.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)',(table,value))


class ConsoleArchive(CatalogArchive):
    """Same immutable record transport; console-specific versioned codec."""
    columns=TABLES
    snapshot_schema=SCHEMA
    manifest_schema='cyclotron-console-manifest/v1'
    schema_field='consoleSchema'
    metadata_field='sequences'
    snapshot_metadata_field='sequences'
    validator=staticmethod(validate)

    def _check_metadata(self,value):
        if not isinstance(value,dict) or set(value)!=set(SEQUENCES): raise ValueError('invalid console watermarks')
        if any(type(number) is not int or not 0<=number<2**63 for number in value.values()):
            raise ValueError('invalid console watermarks')
