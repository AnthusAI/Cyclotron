import {useRef,useState} from 'react'
import {Button} from '@/components/ui/button'
import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import {AppDrawer} from './AppDrawer'
import {graphql} from './graphql'
import {ReliabilityCurve,type CalibrationCurve} from './ReliabilityCurve'
import {MatchedItemTrace} from './MatchedItemTrace'
import {ModelComparison,type DecisionModelComparison} from './ModelComparison'
import type {ClassRole} from './metricRates'

type Plan={fingerprint:string;sample_count:number;request_upper_bound:number;excluded_count:number;class_counts:Record<string,Record<string,number>>}
type Metrics={recall?:number|null;precision?:number|null;accuracy?:number|null;calibration?:CalibrationCurve;metric_aggregation?:'macro'|'positive-vs-rest';positive_labels?:string[];undefined_precision_classes?:string[];undefined_recall_classes?:string[];class_config?:ClassRole[];decision_model_comparison?:DecisionModelComparison}
type RecordResult={endpoint:string;classifier_id:string;item_id:string;actual_label?:string;label:string;decision_model_label:string;trace_event_id?:number}
export type ComparisonResult={sample_count:number;classifiers:Record<string,{before:Metrics;after:Metrics}>;requests?:number;new_requests?:number;records?:RecordResult[]}
const percent=(value:number|null|undefined)=>value==null?'—':`${(value*100).toFixed(1)}%`

export function MatchedComparison({runs,initialRunId,onCreated,onClose}:{runs:{id:string;name:string}[];initialRunId:string;onCreated:(id:string)=>void;onClose:()=>void}){
  const [before,setBefore]=useState(runs.find(run=>run.id!==initialRunId)?.id??'')
  const [after,setAfter]=useState(initialRunId),[plan,setPlan]=useState<Plan|null>(null)
  const [ceiling,setCeiling]=useState(400),[confirmed,setConfirmed]=useState(false)
  const [busy,setBusy]=useState(false),[error,setError]=useState('')
  const generation=useRef(0)
  const change=(side:'before'|'after',value:string)=>{
    generation.current++;setPlan(null);setConfirmed(false);setError('')
    if(side==='before')setBefore(value);else setAfter(value)
  }
  const preflight=async()=>{
    const version=++generation.current
    setBusy(true);setError('');setPlan(null);setConfirmed(false)
    try{
      const result=await graphql<{matchedRunPreflight:Plan}>('query($before:ID!,$after:ID!){matchedRunPreflight(beforeRunId:$before,afterRunId:$after,limit:200)}',{before,after})
      if(generation.current===version){setPlan(result.matchedRunPreflight);setCeiling(result.matchedRunPreflight.request_upper_bound)}
    }catch(e){if(generation.current===version)setError((e as Error).message)}finally{if(generation.current===version)setBusy(false)}
  }
  const execute=async()=>{
    if(!plan||!confirmed||!Number.isInteger(ceiling)||ceiling<plan.request_upper_bound)return
    setBusy(true);setError('')
    try{
      const result=await graphql<{createMatchedComparison:{id:string}}>('mutation($before:ID!,$after:ID!,$fingerprint:String!,$ceiling:Int!,$confirmed:Boolean!){createMatchedComparison(name:"Protected matched comparison",beforeRunId:$before,afterRunId:$after,approvedFingerprint:$fingerprint,maxRequests:$ceiling,confirmed:$confirmed){id}}',{before,after,fingerprint:plan.fingerprint,ceiling,confirmed:true})
      onCreated(result.createMatchedComparison.id)
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  return <AppDrawer title="Compare runs on protected items" side="right" open onOpenChange={open=>{if(!open)onClose()}}>
    <p className="text-sm text-muted-foreground">Score frozen versions on the same protected labels. No training, optimizer calls, or active-version changes. Preflight makes no model calls.</p>
    {(['before','after'] as const).map(side=><div key={side} className="space-y-1"><Label htmlFor={`compare-${side}`}>{side==='before'?'Before run':'After run'}</Label><NativeSelect id={`compare-${side}`} value={side==='before'?before:after} disabled={busy} onChange={event=>change(side,event.target.value)}><NativeSelectOption value="">Choose run</NativeSelectOption>{runs.map(run=><NativeSelectOption key={run.id} value={run.id}>{run.name}</NativeSelectOption>)}</NativeSelect></div>)}
    <Button variant="outline" disabled={busy||!before||!after||before===after} onClick={preflight}>{busy?'Working…':'Check protected samples'}</Button>
    {plan?<section className="space-y-3 rounded-lg border p-3">
      <p className="font-semibold">{plan.sample_count} matched items · at most {plan.request_upper_bound} requests</p>
      <p className="text-xs text-muted-foreground">{plan.excluded_count} items excluded. At most 200 targets; counts below disclose each classifier's balance.</p>
      {Object.entries(plan.class_counts).map(([cid,counts])=><p key={cid} className="text-xs"><strong>{cid}</strong>: {Object.entries(counts).map(([label,count])=>`${label}: ${count}`).join(' · ')}</p>)}
      <Label htmlFor="comparison-ceiling">Maximum requests</Label><Input id="comparison-ceiling" type="number" min={plan.request_upper_bound} value={ceiling} disabled={busy} onChange={event=>{setCeiling(Number(event.target.value));setConfirmed(false)}} />
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={event=>setConfirmed(event.target.checked)} />Authorize paid comparison requests</label>
      <Button disabled={busy||!confirmed||!plan.sample_count||!Number.isInteger(ceiling)||ceiling<plan.request_upper_bound} onClick={execute}>Run matched comparison</Button>
    </section>:null}
    {error?<p role="alert" className="text-sm text-destructive">{error}</p>:null}
  </AppDrawer>
}

export function MatchedComparisonResult({status,result,runId}:{status:string;result?:ComparisonResult|null;runId?:string}){
  const [selected,setSelected]=useState<number|null>(null),[disagreements,setDisagreements]=useState(false)
  if(!result?.classifiers)return <section role="status" className="p-4 text-sm">Comparison {status}. {status==='failed'||status==='interrupted'?'No automatic paid retry. Inspect the failed job before resuming.':'Results appear after the protected evaluation completes.'}</section>
  return <section aria-label="Protected comparison results" className="min-h-0 flex-1 space-y-4 overflow-auto rounded-xl border p-4">
    <p className="text-sm">{result.sample_count} shared protected items · frozen versions · no fitting or promotion</p>
    <p className="text-xs text-muted-foreground">{result.requests??'—'} total request attempts · {result.new_requests??'—'} new attempts in this execution. Calibration curves use recorded probability vectors, not labels reconstructed as confidence.</p>
    {Object.entries(result.classifiers).map(([cid,outputs])=><section key={cid} className="space-y-2"><h2 className="font-semibold">{cid}</h2>
      {outputs.before.metric_aggregation==='macro'?<p className="text-xs text-muted-foreground">Recall and precision: macro average across configured classes.</p>:outputs.before.metric_aggregation==='positive-vs-rest'?<p className="text-xs text-muted-foreground">Recall and precision: positive versus rest ({outputs.before.positive_labels?.join(', ')}).</p>:<p className="text-xs text-muted-foreground">Metric aggregation was not recorded in this older result.</p>}
      {(['before','after'] as const).flatMap(side=>(['recall','precision'] as const).map(metric=>outputs[side][`undefined_${metric}_classes`]?.length?<p key={`${side}:${metric}`} className="text-xs text-muted-foreground">{side==='before'?'Before':'After'} {metric} undefined for: {outputs[side][`undefined_${metric}_classes`]!.join(', ')}.</p>:null))}
      <div className="overflow-x-auto"><table className="w-full text-left text-sm tabular-nums"><thead><tr>{['Endpoint','Recall','Precision','Accuracy','ECE','Brier'].map(label=><th scope="col" key={label} className="p-2">{label}</th>)}</tr></thead><tbody>{(['before','after'] as const).map(side=><tr key={side} className="border-t"><th scope="row" className="p-2 capitalize">{side}</th>{[outputs[side].recall,outputs[side].precision,outputs[side].accuracy,outputs[side].calibration?.ece].map((value,index)=><td key={index} className="p-2">{percent(value)}</td>)}<td className="p-2">{outputs[side].calibration?.brier?.toFixed(3)??'—'}</td></tr>)}</tbody></table></div>
      <div className="grid gap-4 sm:grid-cols-2">{(['before','after'] as const).map(side=><div key={side}><h3 className="text-xs font-semibold capitalize">{side} calibration</h3><ReliabilityCurve curve={outputs[side].calibration} /></div>)}</div>
      {(['before','after'] as const).map(side=><details key={side} className="disclosure"><summary>{side==='before'?'Before':'After'} raw decision model vs final classifier</summary><ModelComparison comparison={outputs[side].decision_model_comparison} classes={outputs[side].class_config} /></details>)}
    </section>)}
    {result.records?.length?<section className="space-y-2"><h2 className="font-semibold">Individual predictions</h2><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={disagreements} onChange={event=>setDisagreements(event.target.checked)} />Only disagreements</label>
      <div className="max-h-80 overflow-auto"><table className="w-full text-left text-xs"><thead><tr>{['Endpoint','Item','Classifier','Human','Raw model','Final classifier','Trace'].map(label=><th scope="col" key={label} className="p-2">{label}</th>)}</tr></thead><tbody>{result.records.filter(row=>!disagreements||(row.actual_label!=null&&row.actual_label!==row.label)).map(row=><tr key={`${row.endpoint}:${row.item_id}:${row.classifier_id}`} className="border-t"><td className="p-2">{row.endpoint}</td><td className="p-2">{row.item_id}</td><td className="p-2">{row.classifier_id}</td><td className="p-2">{row.actual_label??'Not recorded'}</td><td className="p-2">{row.decision_model_label}</td><td className={`p-2 ${row.actual_label==null?'text-muted-foreground':row.actual_label===row.label?'text-green-600':'text-destructive'}`}>{row.label}</td><td className="p-2"><Button size="sm" variant="outline" disabled={!runId||row.trace_event_id==null} aria-label={`Inspect ${row.endpoint} · ${row.item_id} · ${row.classifier_id}`} onClick={()=>setSelected(row.trace_event_id!)}>Inspect</Button></td></tr>)}</tbody></table></div>
    </section>:null}
    {runId&&selected!=null?<MatchedItemTrace key={`${runId}:${selected}`} runId={runId} eventId={selected} onClose={()=>setSelected(null)} />:null}
  </section>
}
