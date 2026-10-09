#!/usr/bin/env python3
"""Hash-gated, resumable prequential Cyclotron study over a frozen editorial corpus.

The 400-item recording is the default; ``--operational-items`` runs a longer
corpus with the same plan, models, and cadences. Live calls need --confirm-live.
"""
import argparse, asyncio, hashlib, json, os, sqlite3
from pathlib import Path

from decision_flywheel.adapters.openai_decision import OpenAIDecisionAdapter, OpenAIDecisionConfiguration
from decision_flywheel.adapters.openai_optimizer import OpenAIOptimizer
from decision_flywheel.classifier_config import ClassifierConfig
from decision_flywheel.cycle_replay import run_cycle_replay
from decision_flywheel.flywheel import DecisionFlywheel
from decision_flywheel.models import DecisionTask, Item, LabeledItem
from decision_flywheel.optimizer_agent import OptimizerAgent
from decision_flywheel.replay import ReplayPlan
from decision_flywheel.replay_feedback_policy import (ReplayFeedbackPolicy, onboarding_all_then_half,
    onboarding_publish_priority_taper, onboarding_first_hundred_then_half)
from decision_flywheel.trace_artifact import read_trace, render_trace

DEFAULT_MODES=('all','reject_half','casual_ten_percent')
BASELINE_RUBRIC=("Publish only when the article clearly reports an achieved, concrete benefit or constructive outcome "
 "for people, communities, knowledge, safety, health, fairness, or the environment. Reject routine coverage, "
 "campaign promises or plans without demonstrated results, sports or celebrity results without wider public benefit, "
 "and articles primarily about tragedy, violence, conflict, wrongdoing, or unresolved harm. When the evidence is mixed, "
 "do not assume a benefit that the article does not establish.")

def rows(path):
    return tuple(LabeledItem(Item(r['id'],{'text':r['text']}),r.get('simulated_label',r.get('label')),
        context={'human_feedback':r.get('simulated_rationale','reviewed editorial label')})
        for r in map(json.loads,path.read_text().splitlines()))

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def event_counts(database):
    if not database.exists(): return {'decision_requests':0,'optimizer_requests':0,'completed_cycles':0,'unfinished_cycles':0}
    connection=sqlite3.connect(f'file:{database}?mode=ro',uri=True)
    events=[json.loads(row[0]) for row in connection.execute('SELECT payload FROM runtime_events ORDER BY id')]
    connection.close()
    starts={event.get('cycle_id') for event in events if event.get('kind')=='cycle-started'}
    closed={event.get('cycle_id') for event in events if event.get('kind') in {'cycle-completed','cycle-failed'}}
    return {'decision_requests':sum(event.get('kind')=='features-requested' for event in events),
            'optimizer_requests':sum(event.get('kind')=='optimizer-request' for event in events),
            'completed_cycles':sum(event.get('kind')=='cycle-completed' for event in events),
            'unfinished_cycles':len(starts-closed)}

def write_budget_ledger(output, decision_cap, optimizer_cap, modes):
    modes={mode:event_counts(output/mode/'runtime.sqlite3') for mode in modes}
    totals={'decision_requests':sum(row['decision_requests'] for row in modes.values()),
            'optimizer_requests':sum(row['optimizer_requests'] for row in modes.values())}
    ledger={'ledger_type':'editorial-study-global-cap-audit','version':1,
            'source_of_truth':'immutable features-requested and optimizer-request events in each mode runtime.sqlite3',
            'caps':{'decision_requests':decision_cap,'optimizer_requests':optimizer_cap},
            'consumed':totals,
            'remaining':{'decision_requests':decision_cap-totals['decision_requests'],
                         'optimizer_requests':optimizer_cap-totals['optimizer_requests']},
            'modes':modes}
    if min(ledger['remaining'].values())<0: raise RuntimeError('immutable run ledger already exceeds the authorized cap')
    (output/'global_budget_ledger.json').write_text(json.dumps(ledger,indent=2)+'\n')
    return ledger

def policy_for(mode, seed):
    if mode in {'all','reject_half','casual_ten_percent'}: return ReplayFeedbackPolicy(mode,seed)
    if mode=='first_50_all_then_half': return onboarding_all_then_half(seed=seed)
    if mode=='publish_priority_taper': return onboarding_publish_priority_taper(seed=seed)
    if mode=='first_100_all_then_half': return onboarding_first_hundred_then_half(seed=seed)
    raise ValueError(f'unknown feedback mode: {mode}')

def build_plan(task, operational, seed):
    # All operational items are eligible for feedback; a fixed stratified dev
    # slice is for candidate comparison only, never hidden from the all arm.
    dev=[]; training=[]
    for label in task.labels:
        group=sorted((r for r in operational if r.label==label),key=lambda r:hashlib.sha256(f'{seed}:{r.item.id}'.encode()).hexdigest())
        dev.extend(group[:20]); training.extend(group[20:])
    return ReplayPlan(tuple(operational),tuple(training),tuple(dev),(),(len(operational),),seed)

async def main_async(args):
    if digest(args.operational_corpus)!=args.operational_sha256: raise ValueError('operational corpus hash mismatch')
    if digest(args.bootstrap_corpus)!=args.bootstrap_sha256: raise ValueError('bootstrap corpus hash mismatch')
    task=DecisionTask('editorial',('publish','reject'),'Include items with a meaningful constructive or public-benefit outcome; reject harm-dominant or routine items.')
    operational,bootstrap=rows(args.operational_corpus),rows(args.bootstrap_corpus)
    count=args.operational_items
    if len(operational)!=count or len({r.item.id for r in operational})!=count: raise ValueError(f'operational corpus must contain exactly {count} unique rows')
    if {r.item.id for r in operational}&{r.item.id for r in bootstrap}: raise ValueError('bootstrap IDs overlap operational stream')
    if len(bootstrap)<12 or any(sum(r.label==label for r in bootstrap)<6 for label in task.labels): raise ValueError('bootstrap needs at least six labels per class')
    if args.resume:
        if not args.output.exists(): raise ValueError('--resume needs the existing study output directory')
        old=json.loads((args.output/'protocol.json').read_text())
        for key,value in {'operational_sha256':args.operational_sha256,'bootstrap_sha256':args.bootstrap_sha256,
                          'decision_cap_total':args.max_decision_calls,'optimizer_cap_total':args.max_optimizer_calls,
                          'operational_items':count}.items():
            if old.get(key)!=value: raise ValueError(f'--resume protocol mismatch for {key}')
        if tuple(old.get('modes',()))!=args.modes: raise ValueError('--resume protocol mismatch for modes')
        if any(event_counts(args.output/mode/'runtime.sqlite3')['unfinished_cycles'] for mode in args.modes):
            raise RuntimeError('a mode has an unfinished cycle; wait for its owning process to reach a terminal state')
    else:
        args.output.mkdir(parents=True,exist_ok=False)
    plan=build_plan(task,operational,args.seed)
    protocol={'operational_sha256':args.operational_sha256,'bootstrap_sha256':args.bootstrap_sha256,'modes':args.modes,
      'operational_items':count,'decision_model':args.model,'optimizer_model':args.model,'baseline_rubric':BASELINE_RUBRIC if args.baseline_rubric=='curated' else '','baseline_provenance':'curated from the product intent and v1 failure analysis; not derived from operational labels at runtime' if args.baseline_rubric=='curated' else 'none: an empty rubric, as in the first 400-cycle recording',
      'feedback_disclosure':f'all reveals all {count} post-prediction labels; selective policies disclose their realized rate','decision_cap_total':args.max_decision_calls,'optimizer_cap_total':args.max_optimizer_calls,
      'rubric_trigger':{'basis':args.rubric_trigger_basis,'every':args.rubric_changes_every,
                        'max_attempts':args.max_rubric_optimizations}}
    if not args.resume: (args.output/'protocol.json').write_text(json.dumps(protocol,indent=2)+'\n')
    ledger=write_budget_ledger(args.output,args.max_decision_calls,args.max_optimizer_calls,args.modes)
    if not args.confirm_live: return
    lock=args.output/'.recovery.lock'
    try:
        descriptor=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY); os.close(descriptor)
    except FileExistsError:
        raise RuntimeError('another recovery process owns this study output') from None
    try:
      for mode in args.modes:
        out=args.output/mode
        current=event_counts(out/'runtime.sqlite3')
        if current['completed_cycles']==len(operational): continue
        ledger=write_budget_ledger(args.output,args.max_decision_calls,args.max_optimizer_calls,args.modes)
        remaining_decisions=ledger['remaining']['decision_requests']
        remaining_optimizers=ledger['remaining']['optimizer_requests']
        if remaining_decisions<1 or remaining_optimizers<1: raise RuntimeError('global authorized budget is exhausted')
        out.mkdir(exist_ok=True); adapter=OpenAIDecisionAdapter.from_environment(configuration=OpenAIDecisionConfiguration(args.model,300))
        transport=OpenAIOptimizer.from_environment(model=args.model,max_calls=remaining_optimizers)
        wheel=DecisionFlywheel(out/'runtime.sqlite3',ClassifierConfig(task,BASELINE_RUBRIC if args.baseline_rubric=='curated' else ''),adapter,OptimizerAgent(transport),max_requests=remaining_decisions)
        try:
            report=await run_cycle_replay(wheel,plan,optimize_every=200,retrain_every=200,stages=('rubric',),
              feedback_policy=policy_for(mode,args.seed),negative_label='reject',max_rubric_optimizations=(args.max_rubric_optimizations or remaining_optimizers),
              initial_training=bootstrap[:8],initial_development=bootstrap[8:12],resume=args.resume and current['completed_cycles']>0,
              retry_failed_requests=args.retry_failed_requests,rubric_changes_every=args.rubric_changes_every,
              rubric_trigger_basis=args.rubric_trigger_basis)
            prior=(out/'results.json')
            if report.get('resumed_from_cycles') and prior.exists():
                old_cycles=json.loads(prior.read_text()).get('cycles',[])
                report['cycles']=[*old_cycles,*report['cycles']]
            report['disclosure']={'operational_items':count,'feedback_revealed':sum(x['feedback_selected'] for x in report['cycles']),
              'dev_role_items':len(plan.development),'scoreboard_role_items':0}
            (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
            if args.playback:
                events=read_trace(out/'runtime.sqlite3')
                (out/'playback.html').write_text(render_trace(events,class_config=[{'label':'publish','role':'positive'},{'label':'reject','role':'negative'}]))
        finally: wheel.close()
        write_budget_ledger(args.output,args.max_decision_calls,args.max_optimizer_calls,args.modes)
    finally:
      lock.unlink(missing_ok=True)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--operational-corpus',type=Path,required=True); p.add_argument('--operational-sha256',required=True); p.add_argument('--bootstrap-corpus',type=Path,required=True); p.add_argument('--bootstrap-sha256',required=True); p.add_argument('--output',type=Path,required=True); p.add_argument('--seed',default='editorial-prequential-v1'); p.add_argument('--model',default='gpt-4.1-mini'); p.add_argument('--max-decision-calls',type=int,default=6000); p.add_argument('--max-optimizer-calls',type=int,default=60); p.add_argument('--max-rubric-optimizations',type=int); p.add_argument('--rubric-changes-every',type=int,default=20); p.add_argument('--rubric-trigger-basis',choices=('label_transitions','revealed_feedback_count'),default='label_transitions'); p.add_argument('--confirm-live',action='store_true'); p.add_argument('--resume',action='store_true'); p.add_argument('--retry-failed-requests',action='store_true'); p.add_argument('--modes',default=','.join(DEFAULT_MODES)); p.add_argument('--operational-items',type=int,default=400); p.add_argument('--no-playback',dest='playback',action='store_false'); p.add_argument('--baseline-rubric',choices=('curated','none'),default='curated',help='none starts from an empty rubric, as the first 400-cycle recording did'); args=p.parse_args(argv); args.modes=tuple(part.strip() for part in args.modes.split(',') if part.strip());
    if not args.modes or len(set(args.modes))!=len(args.modes): p.error('--modes must be a non-empty unique comma-separated list')
    asyncio.run(main_async(args))
if __name__=='__main__': main()
