#!/usr/bin/env python3
"""External arXiv example importer: refresh a local mirror, upsert through GraphQL.

Accepts normalized reviewer JSONL or raw arXiv metadata JSONL. Hugging Face
downloads use an explicitly selected JSONL file at a resolved immutable revision.
This imports items, never labels, and does not start optimization or model calls.
"""
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
from decision_flywheel.web_store import WebStore


def article_item(row, provenance):
    versions=row.get('versions',[])
    submitted=row.get('submitted_at')
    if not submitted and versions:
        submitted=parsedate_to_datetime(versions[0]['created']).isoformat()
    if not submitted: raise ValueError('article submission date required')
    identifier=str(row['id'])
    if not identifier.startswith('arxiv:'): identifier='arxiv:'+identifier
    categories=row.get('categories',[])
    if isinstance(categories,str): categories=categories.split()
    values={key:row.get(key) for key in ('title','abstract','authors','journal_ref')}
    values['journal_ref']=row.get('journal_ref',row.get('journal-ref'))
    values.update(categories=categories,submitted_at=submitted)
    updated=row.get('updated_at')
    if not updated and versions: updated=parsedate_to_datetime(versions[-1]['created']).isoformat()
    values['updated_at']=updated
    if not isinstance(values['title'],str) or not isinstance(values['abstract'],str):
        raise ValueError('article title and abstract required')
    values['text']='\n'.join(f'{key}: {value}' for key,value in values.items() if value is not None)
    return {'id':identifier,'occurred_at':submitted,'values':values,'provenance':provenance}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--source',type=Path,help='existing local JSONL; no dataset network call')
    source.add_argument('--dataset',help='Hugging Face dataset repository ID')
    parser.add_argument('--filename',help='JSONL metadata file in the dataset repository')
    parser.add_argument('--revision',default='main',help='resolved and recorded before download')
    parser.add_argument('--mirror',type=Path,default=Path('var/arxiv-item-mirror.sqlite3'))
    parser.add_argument('--list-id',default='arxiv-papers')
    parser.add_argument('--list-name',default='arXiv research articles')
    parser.add_argument('--endpoint',default='http://127.0.0.1:8782/graphql')
    parser.add_argument('--from-date',default='2025-01-01')
    args=parser.parse_args(argv)
    earliest=datetime.fromisoformat(args.from_date).replace(tzinfo=timezone.utc)
    if args.dataset:
        if not args.filename: parser.error('--dataset requires --filename (a JSONL metadata file)')
        from huggingface_hub import HfApi,hf_hub_download
        revision=HfApi().dataset_info(args.dataset,revision=args.revision).sha
        path=Path(hf_hub_download(repo_id=args.dataset,repo_type='dataset',filename=args.filename,revision=revision))
        provenance={'dataset':args.dataset,'revision':revision,'filename':args.filename}
    else:
        path=args.source
        provenance={'source':'local-jsonl','filename':path.name}
        manifest=path.with_suffix(path.suffix+'.manifest.json')
        if manifest.exists(): provenance['source_manifest']=json.loads(manifest.read_text())
    mirror=WebStore(args.mirror)
    mirror.save_item_list(args.list_id,args.list_name)
    batch=[];totals=dict(inserted=0,updated=0,unchanged=0)
    def flush():
        for key,count in mirror.upsert_list_items(args.list_id,batch).items(): totals[key]+=count
        batch.clear()
    with path.open() as stream:
        for line in stream:
            if not line.strip(): continue
            item=article_item(json.loads(line),provenance)
            when=datetime.fromisoformat(item['occurred_at'].replace('Z','+00:00'))
            if when.tzinfo is None: when=when.replace(tzinfo=timezone.utc)
            if when<earliest: continue
            batch.append(item)
            if len(batch)==200: flush()
    if batch: flush()
    from dotenv import load_dotenv
    load_dotenv(override=False)
    import httpx
    token=os.environ.get('FLYWHEEL_WEB_TOKEN')
    headers={'Authorization':f'Bearer {token}'} if token else {}
    def post(query,variables):
        response=httpx.post(args.endpoint,json={'query':query,'variables':variables},headers=headers,timeout=30,trust_env=False)
        response.raise_for_status()
        result=response.json()
        if result.get('errors'): raise RuntimeError('item API rejected import; local mirror retained for retry')
        return result['data']
    post('mutation($id:String!,$name:String!){saveItemList(identifier:$id,name:$name)}',{'id':args.list_id,'name':args.list_name})
    published=0;api_totals=dict(inserted=0,updated=0,unchanged=0)
    while page:=mirror.list_items(args.list_id,after=published):
        items=[{key:value for key,value in row.items() if key not in ('revision','fingerprint')} for row in page]
        result=post('mutation($list:ID!,$items:JSON!){upsertListItems(listId:$list,items:$items)}',{'list':args.list_id,'items':items})
        for key,count in result['upsertListItems'].items(): api_totals[key]+=count
        published+=len(page)
    print(json.dumps({'list_id':args.list_id,'local':totals,'api':api_totals,'items':published,'model_calls':0}))
    return 0


if __name__=='__main__': raise SystemExit(main())
