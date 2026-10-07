import {Card,CardContent} from '@/components/ui/card'
import type {TraceEvent} from './graphql'
import {ReliabilityCurve,type CalibrationCurve} from './ReliabilityCurve'
import {ModelComparison,type DecisionModelComparison} from './ModelComparison'

export type MetricClassifier={id:string;name:string;config?:{classes?:{label:string;role?:string}[]}}
type Metrics={count:number;accuracy:number|null;per_class:Record<string,{recall:number|null;precision:number|null}>;calibration?:CalibrationCurve;decision_model_comparison?:DecisionModelComparison}
const percent=(value:number|null|undefined)=>value==null?'—':`${(value*100).toFixed(1)}%`

export function ClassifierMetrics({classifiers,events,compact=false,embedded=false}:{classifiers:MetricClassifier[];events:TraceEvent[];compact?:boolean;embedded?:boolean}){
  const latest=new Map<string,Metrics>()
  for(const event of events)if(event.payload.kind==='cycle-metrics')latest.set(String(event.payload.classifier_id??'legacy'),event.payload.metrics as Metrics)
  return <section aria-label="Running classifier metrics" data-compact={compact} data-embedded={embedded} className="classifier-metrics shrink-0 space-y-2">
    <div className="grid gap-3 md:grid-cols-3">{classifiers.map(classifier=>{
      const metrics=latest.get(classifier.id)
      const positive=classifier.config?.classes?.find(row=>row.role==='positive')?.label
      const macro=(key:'recall'|'precision')=>{
        const values=Object.values(metrics?.per_class??{}).map(row=>row[key])
        return values.length&&values.every(value=>value!=null)?values.reduce<number>((sum,value)=>sum+(value??0),0)/values.length:null
      }
      const recall=positive?metrics?.per_class[positive]?.recall:macro('recall')
      const precision=positive?metrics?.per_class[positive]?.precision:macro('precision')
      return <Card key={classifier.id} role="region" aria-label={`${classifier.name} metrics`} aria-live="polite"><CardContent className="space-y-2">
        {!embedded?<h2 className="text-sm font-medium">{classifier.name}</h2>:null}
        <dl className="grid grid-cols-3 gap-2">{[['Recall',recall],['Precision',precision],['Accuracy',metrics?.accuracy]].map(([name,value])=><div key={String(name)}><dt className="text-xs text-muted-foreground">{name}</dt><dd className="mt-1 font-semibold tabular-nums">{percent(value as number|null|undefined)}</dd>{compact?<div role="meter" aria-label={`${classifier.name} ${name}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value==null?undefined:Math.round(Number(value)*100)} aria-valuetext={percent(value as number|null|undefined)} className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className={`h-full rounded-full metric-${String(name).toLowerCase()}`} style={{width:`${Number(value??0)*100}%`}} /></div>:null}</div>)}</dl>
        {!compact?<p className="text-xs text-muted-foreground">{metrics?.count??0} reviewed in latest 200 · {positive?`positive: ${positive}`:'macro recall / precision'}</p>:null}
        <details className="disclosure"><summary>Confidence calibration</summary><ReliabilityCurve curve={metrics?.calibration}/></details>
        <details className="disclosure"><summary>Raw decision model vs final classifier</summary><ModelComparison comparison={metrics?.decision_model_comparison} positive={positive}/></details>
      </CardContent></Card>
    })}</div>
    {!compact?<p className="text-xs text-muted-foreground">Each classifier uses its most recent 200 human-labeled items, or all available if fewer. Predictions were made before your votes. Not a held-out evaluation.</p>:null}
  </section>
}
