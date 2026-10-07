import {useEffect,useState} from 'react'
import {AppDrawer} from './AppDrawer'
import {graphql} from './graphql'

type Answer={label:string;probabilities?:Record<string,number>|null}
type Trace={endpoint:string;target_id:string;item:Record<string,unknown>;labels:Record<string,string>;
  request_fingerprint:string;checkpoint_fingerprint:string;
  request:{state:{classifiers:Record<string,{rubric:string;examples:Record<string,unknown>[];current_datetime?:string}>};questions:Record<string,{instructions:string;options:unknown}>};
  response:{model?:string;answers:Record<string,Record<string,Answer>>};
  outputs:Record<string,{label:string;actual_label:string;probabilities?:Record<string,number>|null}>;exchanges:unknown[]}
const readable=(value:unknown)=>typeof value==='string'?value:JSON.stringify(value,null,2)
const distribution=(values?:Record<string,number>|null)=>values?Object.entries(values).map(([label,value])=>`${label}: ${(value*100).toFixed(1)}%`).join(' · '):'No probability vector recorded'
function Fields({values}:{values:Record<string,unknown>}){
  return <dl className="space-y-2">{Object.entries(values).map(([key,value])=><div key={key}><dt className="text-xs font-semibold capitalize text-muted-foreground">{key.replaceAll('_',' ')}</dt><dd className="whitespace-pre-wrap break-words text-sm">{readable(value)}</dd></div>)}</dl>
}

export function MatchedItemTrace({runId,eventId,onClose}:{runId:string;eventId:number;onClose:()=>void}){
  const [trace,setTrace]=useState<Trace|null>(null),[loading,setLoading]=useState(true),[error,setError]=useState('')
  useEffect(()=>{
    let cancelled=false
    graphql<{matchedEvaluationTarget:Trace|null}>('query($run:ID!,$event:Int!){matchedEvaluationTarget(runId:$run,eventId:$event)}',{run:runId,event:eventId})
      .then(result=>{if(!cancelled){setTrace(result.matchedEvaluationTarget);setLoading(false)}})
      .catch(e=>{if(!cancelled){setError((e as Error).message);setLoading(false)}})
    return()=>{cancelled=true}
  },[runId,eventId])
  return <AppDrawer title="Comparison item trace" side="right" open onOpenChange={open=>{if(!open)onClose()}}>
    {error?<p role="alert">{error}</p>:loading?<p role="status">Loading recorded item exchange…</p>:!trace?<p>No item trace was recorded at this point. No request has been reconstructed or re-run.</p>:<>
      <p className="text-sm font-semibold capitalize">{trace.endpoint} · {trace.target_id}</p>
      <h3 className="font-semibold">Item</h3><Fields values={trace.item} />
      <h3 className="font-semibold">Protected label and final classifier</h3>
      {Object.entries(trace.outputs).map(([cid,result])=><section key={cid} className="rounded-md border p-3 text-sm"><h4 className="font-semibold">{cid}</h4><p>Human: {result.actual_label} · final: {result.label}</p><p className="text-xs text-muted-foreground">{distribution(result.probabilities)}</p></section>)}
      <h3 className="font-semibold">Structured decision request</h3>
      <p className="text-xs text-muted-foreground">Full joint state and questions for this target. Protected labels are scoring evidence, not part of the request.</p>
      {Object.entries(trace.request.state.classifiers).map(([cid,context])=><section key={cid} className="space-y-2 rounded-md border p-3"><h4 className="font-semibold">{cid}</h4><p className="text-xs font-semibold">Rubric</p><p className="whitespace-pre-wrap text-sm">{context.rubric||'Empty rubric'}</p>{context.current_datetime?<p className="text-xs">Current datetime: {context.current_datetime}</p>:null}<details><summary className="text-sm">{context.examples.length} actual labeled examples</summary><div className="space-y-3 py-2">{context.examples.map((example,index)=><section key={index} className="rounded-md border p-2"><Fields values={example} /></section>)}</div></details></section>)}
      {Object.entries(trace.request.questions).map(([key,question])=><section key={key} className="space-y-1 rounded-md border p-3"><h4 className="text-xs font-semibold">{key}</h4><p className="whitespace-pre-wrap text-sm">{question.instructions}</p><p className="text-xs text-muted-foreground">Options: {readable(question.options)}</p></section>)}
      <h3 className="font-semibold">Decision response</h3><p className="text-xs text-muted-foreground">Model: {trace.response.model??'Not recorded'}</p>
      {Object.entries(trace.response.answers).map(([cid,answers])=><section key={cid} className="space-y-2 rounded-md border p-3"><h4 className="font-semibold">{cid}</h4>{Object.entries(answers).map(([name,answer])=><div key={name}><p className="text-sm">{name}: {answer.label}</p><p className="text-xs text-muted-foreground">{distribution(answer.probabilities)}</p></div>)}</section>)}
      <details><summary className="text-xs">Exact joint request and response</summary><pre className="overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify({request:trace.request,response:trace.response},null,2)}</pre></details>
      <details><summary className="text-xs">Recorded provider exchanges ({trace.exchanges.length})</summary><pre className="overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify(trace.exchanges,null,2)}</pre></details>
      <details><summary className="text-xs">Pinned provenance</summary><Fields values={{request_fingerprint:trace.request_fingerprint,checkpoint_fingerprint:trace.checkpoint_fingerprint,protected_labels:trace.labels}} /></details>
    </>}
  </AppDrawer>
}
