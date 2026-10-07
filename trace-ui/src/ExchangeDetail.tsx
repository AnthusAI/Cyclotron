import type {TraceEvent} from './graphql'
import {TraceDetail} from './TraceDetail'

/** Only use durable identities; temporal proximity is not proof of causation. */
export function exchangeEvents(selected:TraceEvent,events:TraceEvent[]):TraceEvent[]{
  const value=selected.payload
  const stage=value.stage??value.step_stage
  const optimizer=String(value.kind).startsWith('optimizer-')||value.kind==='proposal-validated'||
    (['rubric','examples','questions'].includes(String(stage))&&
      ['step-started','step-completed','step-failed','step-paused','optimization-stage-started','optimization-stage-completed','optimization-stage-failed'].includes(String(value.kind)))
  const family=optimizer?'optimizer':'decision'
  const candidates=events.filter(row=>{
    const other=row.payload
    if(![`${family}-request`,`${family}-response`,`${family}-failed`].includes(String(other.kind)))return false
    if(other.classifier_id!==value.classifier_id)return false
    if(optimizer&&value.briefing_fingerprint)return other.briefing_fingerprint===value.briefing_fingerprint&&(!value.step_id||other.step_id===value.step_id)
    if(!value.step_id||other.step_id!==value.step_id)return false
    return optimizer||Boolean(value.target_id&&other.target_id===value.target_id&&other.cycle_id===value.cycle_id)
  })
  // Explicit cache events contain their own exact request and answer; do not invent an API call.
  return candidates.sort((a,b)=>a.sequence-b.sequence)
}

export function ExchangeDetail({event,events}:{event:TraceEvent;events:TraceEvent[]}){
  const exchanges=exchangeEvents(event,events)
  return <section aria-label="Recorded model exchange" className="space-y-3">
    {!exchanges.some(row=>row.sequence===event.sequence)?<TraceDetail event={event}/>:null}
    {exchanges.map(row=><TraceDetail key={row.sequence} event={row}/>)}
    {!exchanges.length?<p className="text-xs text-muted-foreground">No correlated model exchange was recorded for this event. No nearby call has been substituted.</p>:null}
  </section>
}
