"""Explicitly bounded, read-only endpoint audit of a completed private replay."""
import argparse
import asyncio
import json
import sqlite3
from pathlib import Path
from decision_flywheel.adapters.jev import JevAdapter, JevConfiguration
from decision_flywheel.classifier_config import ClassifierConfig
from decision_flywheel.flywheel import DecisionFlywheel, FittedClassifier, _restore
from decision_flywheel.optimizer_agent import OptimizerAgent
from decision_flywheel.reviewer_store import ReviewStore
from decision_flywheel.reviewer_core import reviewer_labeled_items, reviewer_task
from decision_flywheel.run_comparison import compare_endpoints
from decision_flywheel.trace_artifact import read_trace


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--max-requests',type=int,default=36)
    parser.add_argument('--confirm-live',action='store_true')
    args=parser.parse_args(argv)
    manifest=json.loads((args.run/'manifest.json').read_text())
    with sqlite3.connect((args.run/'reviews.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as db:
        metadata=dict(db.execute('SELECT key,value FROM study_metadata'))
    with ReviewStore(args.run/'reviews.sqlite3',study_seed=metadata['study_seed'],
        rolling_audit_rate=float(metadata['rolling_audit_rate']),final_audit_rate=float(metadata['final_audit_rate'])) as store:
        rows={row.item.id:row for row in reviewer_labeled_items(store.learning_feedback(),store.article)}
    training=tuple(rows[row['id']] for row in manifest['records'] if row['role']=='training')
    audit=tuple(rows[row['id']] for row in manifest['records'] if row['role']=='scoreboard')
    bound=2*len(audit)
    print(json.dumps({'audit_items':len(audit),'request_upper_bound':bound,'optimizer_calls':0}),flush=True)
    if not args.confirm_live:
        return 0
    if args.max_requests<bound:
        parser.error('approved ceiling does not cover matched evaluation')
    events=read_trace(args.run/'runtime.sqlite3')
    final=_restore(next(e['classifier_snapshot'] for e in reversed(events) if e['kind']=='cycle-completed'))
    initial=FittedClassifier(ClassifierConfig(reviewer_task()))
    def no_optimizer(*args,**kwargs):
        raise AssertionError('endpoint evaluation must never call an optimizer')
    adapter=JevAdapter.from_environment(configuration=JevConfiguration(model=manifest['execution']['decisions_model']))
    wheel=DecisionFlywheel(args.run/'endpoint-audit.sqlite3',initial.config,adapter,
        OptimizerAgent(no_optimizer),max_requests=args.max_requests)
    try:
        report=asyncio.run(compare_endpoints(wheel,initial,final,audit,training,
            class_config=[{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]))
        report['decision_requests']=wheel.requests
        (args.run/'run-comparison.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({key:{metric:report[key][metric] for metric in ('accuracy','precision','recall')} for key in ('before','after')}),flush=True)
    finally:
        wheel.close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
