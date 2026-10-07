import type {CalibrationCurve} from './ReliabilityCurve'
import {useEffect,useState} from 'react'
import {metricRates,type ClassRole,type RateMetrics} from './metricRates'

type OutputMetrics=RateMetrics&{accuracy:number|null;calibration:CalibrationCurve}
export type DecisionModelComparison={count:number;missing_raw_count:number;missing_paired_probability_count?:number;raw:OutputMetrics;final:OutputMetrics}
const percent=(value:number|null)=>value==null?'—':`${(value*100).toFixed(1)}%`
export function ModelComparison({comparison,classes}:{comparison?:DecisionModelComparison;classes?:ClassRole[]}){
  if(!comparison?.count)return <p className="text-xs text-muted-foreground">No matched raw decision-model and final-classifier predictions recorded yet.</p>
  const outputs=[{name:'Raw decision model',metrics:comparison.raw,color:'#3b82f6'},{name:'Final classifier',metrics:comparison.final,color:'#22c55e'}]
  const config=classes
  const metric=(metrics:OutputMetrics,key:'recall'|'precision')=>metricRates(metrics,config)[key]
  return <div className="space-y-3">
    <p className="text-xs text-muted-foreground">Same {comparison.count} reviewed items · pre-vote predictions · not held-out.</p>
    <p className="text-xs text-muted-foreground">Recall and precision: {config?.some(row=>row.role==='positive')?`positive versus rest (${config.filter(row=>row.role==='positive').map(row=>row.label).join(', ')})`:'macro average across classes'}.</p>
    <table className="w-full text-xs tabular-nums"><thead><tr>{['Output','Recall','Precision','Accuracy'].map(label=><th scope="col" key={label} className="text-left font-medium p-1">{label}</th>)}</tr></thead><tbody>{outputs.map(({name,metrics,color})=><tr key={name}><th scope="row" className="text-left font-medium p-1" style={{color}}>{name}</th>{[metric(metrics,'recall'),metric(metrics,'precision'),metrics.accuracy].map((value,index)=><td key={index} className="p-1">{percent(value)}</td>)}</tr>)}</tbody></table>
    <svg viewBox="0 0 240 155" role="img" aria-label="Raw decision model versus final classifier calibration" className="w-full max-w-xs">
      <path d="M30 10V125H230 M30 125L230 10" fill="none" stroke="currentColor" opacity=".25"/>
      {outputs.map(({name,metrics,color})=><polyline key={name} points={metrics.calibration.bins.filter(bin=>bin.count&&bin.mean_confidence!=null&&bin.accuracy!=null).map(bin=>`${30+200*bin.mean_confidence!},${125-115*bin.accuracy!}`).join(' ')} fill="none" stroke={color} strokeWidth="2"><title>{name}</title></polyline>)}
      <text x="100" y="149" fontSize="10" fill="currentColor">Confidence →</text>
    </svg>
    {outputs.map(({name,metrics,color})=><p key={name} className="text-xs" style={{color}}>{name}: ECE {percent(metrics.calibration.ece)} · Brier {metrics.calibration.brier?.toFixed(3)??'—'} · {metrics.calibration.count} probability vectors</p>)}
    <p className="text-xs text-muted-foreground">Lower ECE and Brier are better. Diagonal: perfect calibration. Small samples are noisy.</p>
    {comparison.missing_paired_probability_count?<p className="text-xs text-muted-foreground">{comparison.missing_paired_probability_count} matched items lack one or both probability vectors; both calibration curves exclude them.</p>:null}
    {comparison.missing_raw_count?<p className="text-xs text-muted-foreground">{comparison.missing_raw_count} older reviewed item(s) lack a raw output and are excluded from both sides.</p>:null}
  </div>
}

export function PlaybackModelComparison(){
  const [snapshots,setSnapshots]=useState<Record<string,{comparison:DecisionModelComparison;classes?:ClassRole[]}>>({})
  useEffect(()=>{
    const update=(event:Event)=>setSnapshots((event as CustomEvent<typeof snapshots>).detail)
    window.addEventListener('flywheel-model-comparison-position',update)
    return ()=>window.removeEventListener('flywheel-model-comparison-position',update)
  },[])
  return <details className="disclosure"><summary>Raw decision model vs final classifier at this cycle</summary>{Object.keys(snapshots).length?Object.entries(snapshots).map(([id,snapshot])=><section key={id} className="space-y-2"><h3 className="text-sm font-semibold">{id}</h3><ModelComparison {...snapshot}/></section>):<p className="text-xs text-muted-foreground">No matched comparison snapshot recorded at this point.</p>}</details>
}
