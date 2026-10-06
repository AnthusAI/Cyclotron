"""Inspect one actual rubric proposal on private snapshots, without new decision calls."""
import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from decision_flywheel import ClassifierConfig, DecisionFlywheel, OptimizerAgent
from decision_flywheel.adapters.openai_optimizer import OpenAIOptimizer
from decision_flywheel.reviewer_core import reviewer_task
from decision_flywheel.reviewer_flywheel import ReviewerFlywheel
from decision_flywheel.reviewer_store import ReviewStore
from measure_arxiv_questions import backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--confirm-live', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    reviews, runtime = args.output / 'reviews.sqlite3', args.output / 'runtime.sqlite3'
    backup(Path('var/reviewer.sqlite3'), reviews)
    backup(Path('var/reviewer-runtime.sqlite3'), runtime)
    with sqlite3.connect(runtime) as db:
        identity = json.loads(db.execute("SELECT value FROM runtime_state WHERE key='contract'").fetchone()[0])['model']
    with sqlite3.connect(reviews) as db:
        metadata = dict(db.execute('SELECT key,value FROM study_metadata'))

    class CachedOnlyModel:
        model_identity = identity
        async def classify(self, *args, **kwargs):
            raise AssertionError('this trace must not make decision-model calls')

    def forbidden(_):
        raise AssertionError('preview must not call an optimizer')

    with ReviewStore(reviews, study_seed=metadata['study_seed'],
                     rolling_audit_rate=float(metadata['rolling_audit_rate']),
                     final_audit_rate=float(metadata['final_audit_rate'])) as store:
        optimizer = OpenAIOptimizer.from_environment(model='gpt-6-luna', max_calls=1) if args.confirm_live else forbidden
        wheel = DecisionFlywheel(runtime, ClassifierConfig(reviewer_task()), CachedOnlyModel(), OptimizerAgent(optimizer))
        try:
            reviewer = ReviewerFlywheel(store, wheel)
            reviewer.sync_optimizer_context()
            training, development, protected = reviewer.partitions()
            before = wheel.active.fingerprint
            preview = wheel.preview_optimizer_request('rubric', training, development, protected=protected)
            (args.output / 'request-preview.json').write_text(json.dumps(preview, indent=2)+'\n')
            cursor = wheel.trace_events(limit=1000)['cursor']
            # The copied history can exceed one page; use the actual durable tail.
            cursor = wheel.db.execute('SELECT COALESCE(MAX(id),0) FROM runtime_events').fetchone()[0]
            if args.confirm_live:
                result = asyncio.run(wheel.step('rubric', training, development, protected=protected,
                    propensities={r.item.id: 1. for r in training}, request_budget=0,
                    trigger='inspect-human-explanations'))
                events = wheel.trace_events(after_event_id=cursor, limit=1000)['events']
                (args.output / 'trace.json').write_text(json.dumps(events, indent=2)+'\n')
                (args.output / 'result.json').write_text(json.dumps(result, indent=2)+'\n')
                response = next((e for e in events if e['kind'] == 'optimizer-response'), None)
                print(json.dumps({'status': result['status'], 'optimizer_calls': optimizer.calls,
                    'decision_calls': wheel.requests, 'active_unchanged': wheel.active.fingerprint == before,
                    'proposal': json.loads(response['content']) if response else None,
                    'usage': response.get('usage') if response else None}, indent=2))
            else:
                print(json.dumps({'preview_only': True, 'context': wheel.optimizer_context,
                                  'training_count': len(training), 'development_count': len(development)}, indent=2))
        finally:
            wheel.close()


if __name__ == '__main__':
    main()
