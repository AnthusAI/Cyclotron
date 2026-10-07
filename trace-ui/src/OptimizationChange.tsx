import type {TraceEvent} from './graphql'

type RecordValue=Record<string,unknown>
const record=(value:unknown):RecordValue|undefined=>value!==null&&typeof value==='object'&&!Array.isArray(value)?value as RecordValue:undefined
const strings=(value:unknown)=>Array.isArray(value)?value.filter((entry):entry is string=>typeof entry==='string'):null
const text=(value:unknown)=>typeof value==='string'?value:null

function recordedExamples(events:TraceEvent[]){
  const examples=new Map<string,RecordValue>()
  for(const event of events){
    if(event.payload.kind!=='optimizer-request'||!Array.isArray(event.payload.messages))continue
    for(const message of event.payload.messages){
      const m=record(message)
      if(m?.role!=='user'||typeof m.content!=='string')continue
      try{
        const context=record(JSON.parse(m.content))
        if(!Array.isArray(context?.feedback))continue
        for(const entry of context.feedback){const row=record(entry);if(row&&typeof row.id==='string')examples.set(row.id,row)}
      }catch{/* An unstructured prompt is not a recorded example pool. */}
    }
  }
  return examples
}

function Snapshot({snapshot,label,examples}:{snapshot:RecordValue|undefined;label:'Before'|'After';examples:Map<string,RecordValue>}){
  const config=record(snapshot?.config),head=record(snapshot?.head)
  const ids=strings(config?.example_ids),tasks=Array.isArray(config?.tasks)?config.tasks:null
  const main=record(config?.task),features=strings(head?.feature_names),calibration=record(head?.calibration)
  return <section aria-label={`${label} optimization`} className="min-w-0 space-y-3 rounded-lg border border-border p-3">
    <h3 className="text-sm font-semibold">{label}</h3>
    {!snapshot?<p className="text-xs text-muted-foreground">{label} snapshot not recorded for this step</p>:<>
      <div><h4 className="text-xs font-medium">Rubric</h4><p className="whitespace-pre-wrap break-words text-sm">{text(config?.rubric)===null?'Rubric not recorded':config?.rubric===''?'Empty rubric':String(config?.rubric)}</p></div>
      <div><h4 className="text-xs font-medium">Classification questions {main&&tasks?`· ${tasks.length+1}`:''}</h4>
        {main?<p className="whitespace-pre-wrap text-sm">{text(main.instructions)??'Main question not recorded'}</p>:<p className="text-xs text-muted-foreground">Main question not recorded</p>}
        {main?<p className="text-xs text-muted-foreground">{text(main.name)} · {strings(main.labels)?.join(' / ')??'Classes not recorded'}</p>:null}
        {tasks?tasks.map((task,index)=>{const q=record(task);return <div key={index} className="mt-2"><p className="whitespace-pre-wrap text-sm">{text(q?.instructions)??'Question not recorded'}</p><p className="text-xs text-muted-foreground">{text(q?.name)} · {strings(q?.labels)?.join(' / ')??'Classes not recorded'}</p></div>}):<p className="text-xs text-muted-foreground">Supporting questions not recorded</p>}
      </div>
      <div><h4 className="text-xs font-medium">Few-shot examples {ids?`· ${ids.length}`:''}</h4>
        {ids?.length===0?<p className="text-xs text-muted-foreground">No examples</p>:ids?ids.map(id=>{
          const example=examples.get(id),values=record(example?.values)
          return <details key={id} className="disclosure"><summary>{text(values?.title)??id}</summary><p className="text-xs">{example?`Label: ${text(example.label)??'not recorded'}`:'Example content not recorded in this step'}</p>{values?<p className="whitespace-pre-wrap break-words text-sm">{text(values.abstract)??text(values.text)??'Text not recorded'}</p>:null}{text(example?.comment)?<p className="whitespace-pre-wrap text-xs text-muted-foreground">{String(example?.comment)}</p>:null}</details>
        }):<p className="text-xs text-muted-foreground">Example list not recorded</p>}
      </div>
      <div><h4 className="text-xs font-medium">Dynamic decision elements</h4><p className="text-xs">{strings(config?.dynamic_elements)?.join(', ')||(Array.isArray(config?.dynamic_elements)?'None':'Not recorded')}</p></div>
      <div><h4 className="text-xs font-medium">Final classifier</h4>
        <p className="text-sm">{snapshot.head===null?'Raw decision-model output':head?'Learned ML head':'ML head not recorded'}</p>
        {features?<ul className="mt-1 list-inside list-disc break-words text-xs">{features.map(feature=><li key={feature}>{feature}</li>)}</ul>:null}
        {head?<p className="text-xs text-muted-foreground">Calibration: {typeof calibration?.temperature==='number'?`temperature ${calibration.temperature}`:text(calibration?.method)??'not recorded'}{text(calibration?.fit_on)?` · fitted on ${String(calibration?.fit_on)}`:''}</p>:null}
      </div>
    </>}
  </section>
}

export function OptimizationChange({event,events}:{event:TraceEvent;events:TraceEvent[]}){
  const selected=event.payload
  if(typeof selected.step_id!=='string'||!selected.step_id)return null
  const scoped=events.filter(row=>row.payload.step_id===selected.step_id&&row.payload.classifier_id===selected.classifier_id&&
    (!selected.cycle_id||row.payload.cycle_id===selected.cycle_id)).sort((a,b)=>a.sequence-b.sequence)
  const start=scoped.find(row=>row.payload.kind==='step-started')
  const stage=selected.step_stage??selected.stage??start?.payload.step_stage
  if(!['rubric','examples','questions','classifier'].includes(String(stage)))return null
  const end=scoped.findLast(row=>['step-completed','step-paused'].includes(String(row.payload.kind)))
  const result=record(end?.payload.result)
  const outcome=end?.payload.status==='failed'||end?.payload.status==='partial'?'Failed or incomplete':end?.payload.kind==='step-paused'?'Paused':
    !end?'No final outcome recorded':result?.activated===true||result?.promoted===true?'Accepted':end.payload.status==='waiting'?'Waiting for evidence':
      result?.activated===false||result?.promoted===false?'No change accepted':'Outcome not recorded'
  const examples=recordedExamples(scoped)
  return <section aria-label="Recorded optimization change" className="space-y-3">
    <div><h2 className="text-sm font-semibold">Optimization change</h2><p className="text-xs font-medium">{outcome}</p>{text(result?.reason)?<p className="text-xs text-muted-foreground">{String(result?.reason)}</p>:null}</div>
    <div className="grid grid-cols-1 gap-3"><Snapshot label="Before" snapshot={record(start?.payload.classifier_snapshot)} examples={examples}/><Snapshot label="After" snapshot={record(end?.payload.classifier_snapshot)} examples={examples}/></div>
  </section>
}
