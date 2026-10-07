export type EvaluationSupport={count?:number;f1?:number|null;accuracy_interval_95?:number[]|null;accuracy_interval_method?:string;per_class?:Record<string,{count?:number;predicted_count?:number;f1?:number|null}>}
const percent=(value:number|null|undefined)=>value==null?'not recorded':`${(value*100).toFixed(1)}%`

export function EvaluationDetails({metrics,classes}:{metrics:EvaluationSupport;classes?:{label:string}[]}){
  const interval=metrics.accuracy_interval_95
  const support=metrics.per_class??{}
  const configured=classes?.map(row=>row.label)??[]
  const labels=[...configured,...Object.keys(support).filter(label=>!configured.includes(label))]
  const rows=labels.map(label=>[label,support[label]??{}] as const)
  return <details className="disclosure"><summary>F1, sample support and uncertainty</summary><div className="space-y-2 text-xs">
    <p>{metrics.count??'—'} scored items · F1: {percent(metrics.f1)}</p>
    <p>Accuracy interval: {interval?.length===2?`${percent(interval[0])}–${percent(interval[1])}`:'not recorded'}</p>
    {metrics.accuracy_interval_method?<p className="text-muted-foreground">{metrics.accuracy_interval_method}</p>:null}
    <p className="text-muted-foreground">Small or imbalanced samples can be misleading. This interval does not establish that an optimization caused an improvement.</p>
    {metrics.per_class?<table className="w-full text-left tabular-nums"><thead><tr>{['Class','Reviewed','Predicted','F1'].map(label=><th key={label} scope="col" className="p-1">{label}</th>)}</tr></thead><tbody>{rows.map(([label,row])=><tr key={label}><th scope="row" className="p-1 font-medium">{label}</th><td className="p-1">{row.count??'—'}</td><td className="p-1">{row.predicted_count??'—'}</td><td className="p-1">{percent(row.f1)}</td></tr>)}</tbody></table>:null}
  </div></details>
}
