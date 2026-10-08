#!/usr/bin/env python3
"""Explicitly bounded live smoke for the OpenAI decision adapter and feedback policies."""
import argparse
import asyncio
import json
from pathlib import Path

from decision_flywheel.adapters.openai_decision import OpenAIDecisionAdapter, OpenAIDecisionConfiguration
from decision_flywheel.classifier_config import ClassifierConfig
from decision_flywheel.flywheel import DecisionFlywheel
from decision_flywheel.models import DecisionTask, Item
from decision_flywheel.replay_feedback_policy import ReplayFeedbackPolicy


async def run(args):
    if args.calls_per_policy != 10 or args.max_total_calls != 30:
        raise ValueError('this smoke is intentionally fixed at 10 calls per policy and 30 total calls')
    args.output.mkdir(parents=True, exist_ok=False)
    task=DecisionTask('editorial',('include','exclude'),'Classify whether this editorial item should be included.')
    corpus=[json.loads(line) for line in args.corpus.read_text().splitlines()]
    if len(corpus) < args.max_total_calls:
        raise ValueError('corpus needs at least 30 records')
    adapter=OpenAIDecisionAdapter.from_environment(configuration=OpenAIDecisionConfiguration(args.model))
    wheel=DecisionFlywheel(args.output/'runtime.sqlite3',ClassifierConfig(task),adapter,max_requests=args.max_total_calls,
                            calibration_method='auto')
    records=[]
    try:
        for mode_number,mode in enumerate(('all','reject_half','casual_ten_percent')):
            policy=ReplayFeedbackPolicy(mode,args.seed)
            for index in range(args.calls_per_policy):
                source=corpus[mode_number*args.calls_per_policy+index]
                item=Item(source['id'],{'text':source['text']})
                result=await wheel.predict(item,())
                selection=policy.select(item_id=item.id,predicted_label=result.label,negative_label='exclude')
                records.append({'mode':mode,'item_id':item.id,'label':result.label,'probabilities':result.probabilities,
                                'feedback_selected':selection.selected,'selection_propensity':selection.propensity})
        usage=[]
        for event in wheel.history(1000):
            if event['kind']=='prediction': usage.append(event.get('usage') or {})
        args.output.joinpath('results.json').write_text(json.dumps({'model':adapter.model_identity,'records':records,
            'attempted_calls':wheel.requests,'optimizer_calls':0,'usage':usage,
            'corpus':str(args.corpus)},indent=2)+'\n')
    finally:
        wheel.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--corpus',type=Path,required=True)
    parser.add_argument('--model',default='gpt-4.1-mini')
    parser.add_argument('--seed',default='openai-policy-smoke-v1')
    parser.add_argument('--calls-per-policy',type=int,default=10)
    parser.add_argument('--max-total-calls',type=int,default=30)
    args=parser.parse_args(argv)
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
