import {Activity,LoaderCircle,TriangleAlert} from 'lucide-react'
import {Button} from '@/components/ui/button'
import type {TraceEvent} from './graphql'
import type {MetricClassifier} from './ClassifierMetrics'
import type {FeedbackJob} from './FeedbackControls'

const stages:Record<string,string>={rubric:'Rubric',examples:'Few-shot examples',questions:'Classifier questions',classifier:'ML optimization'}
type ActivityState={event:TraceEvent;status:string;failed:boolean;accepted:boolean}

/** Describe recorded phases, not guessed progress from elapsed time. */
export function latestOptimizationActivity(events:TraceEvent[]):ActivityState|null{
  let latest:ActivityState|null=null
  let latestCheck:ActivityState|null=null
  for(const event of events){
    if(latest&&event.sequence<=latest.event.sequence)continue
    const p=event.payload,kind=String(p.kind)
    // Routine checks remain inspectable without hiding the latest outcome.
    if((kind==='trigger-check'||kind==='trigger-evaluated')&&p.due===false){
      if(!latestCheck||event.sequence>latestCheck.event.sequence)latestCheck={event,status:'Trigger checked · not due',failed:false,accepted:false}
      continue
    }
    let status=''
    let failed=false,accepted=false
    if(kind==='trigger-check'||kind==='trigger-evaluated')status=p.due===true?'Trigger fired · stage queued':p.due===false?'Trigger checked · not due':'Trigger result not recorded'
    else if(kind==='optimization-stage-started'||kind==='step-started')status='Optimization started'
    else if(kind==='optimizer-request')status='Optimizer request sent'
    else if(kind==='optimizer-response')status='Response received · validation pending'
    else if(kind==='proposal-validated')status='Proposal validated · evaluation pending'
    else if(kind==='fit-started')status='ML fitting started'
    else if(kind==='fit-completed')status='ML fit completed · selection pending'
    else if(kind==='candidate-evaluated')status='Candidate evaluated · selection pending'
    else if(kind==='promoted'){status='Accepted';accepted=true}
    else if(kind==='candidate-rejected')status='Completed · no change accepted'
    else if(kind==='optimization-stage-completed'){
      accepted=p.activated===true||p.promoted===true
      status=accepted?'Accepted':'Completed · no change accepted'
    }else if(kind==='step-completed'){
      const result=p.result as Record<string,unknown>|undefined
      accepted=result?.activated===true||result?.promoted===true
      failed=p.status==='failed'||p.status==='partial'
      status=p.status==='failed'?'Optimization failed':p.status==='partial'?'Completed with failed trials · inspect activity':p.status==='waiting'?'Waiting for eligible evidence':accepted?'Accepted':'Completed · no change accepted'
    }else if(kind==='step-paused'||kind==='optimization-paused')status='Optimization paused · explicit resume required'
    else if(['optimization-stage-failed','optimizer-failed','round-failed','round-interrupted','step-failed'].includes(kind)){
      status=kind==='round-interrupted'?'Optimization interrupted':'Optimization failed';failed=true
    }
    if(status)latest={event,status,accepted,failed}
  }
  return latest??latestCheck
}

export function OptimizationStatus({events,classifiers,jobs,onInspect}:{
  events:TraceEvent[];classifiers:MetricClassifier[];jobs:FeedbackJob[];onInspect:(event:TraceEvent)=>void;
}){
  const activity=latestOptimizationActivity(events)
  const job=jobs[0]
  const commandFailed=job?.status==='failed'||job?.status==='interrupted'
  const working=jobs.some(row=>row.status==='pending'||row.status==='running')
  const payload=activity?.event.payload
  const stage=String(payload?.stage??payload?.step_stage??'')
  const classifier=classifiers.find(row=>row.id===payload?.classifier_id)
  const scope=[classifier?.name,stages[stage]??stage].filter(Boolean).join(' · ')
  const status=commandFailed?`Command ${job.status} · inspect activity before retrying`:activity?.status??'No optimization recorded yet'
  const result=payload?.result as Record<string,unknown>|undefined
  const reason=typeof payload?.reason==='string'?payload.reason:typeof result?.reason==='string'?result.reason:null
  const failure=commandFailed||activity?.failed
  return <section aria-label="Flywheel activity" className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border bg-card px-3 py-2 text-xs">
    {failure?<TriangleAlert aria-hidden="true" className="size-4 text-destructive"/>:working?<LoaderCircle aria-hidden="true" className="size-4 motion-safe:animate-spin"/>:<Activity aria-hidden="true" className="size-4 text-muted-foreground"/>}
    <div role="status" aria-live="polite" aria-atomic="true" className="min-w-0 flex-1">
      {scope?<p className="font-medium">{scope}</p>:null}
      <p className={failure?'text-destructive':activity?.accepted?'text-emerald-700 dark:text-emerald-400':'text-muted-foreground'}>{status}</p>
      {reason?<p className="text-muted-foreground">{reason}</p>:null}
    </div>
    {activity?<Button variant="ghost" size="sm" aria-label="Inspect latest optimization event" onClick={()=>onInspect(activity.event)}>Inspect activity</Button>:null}
  </section>
}
