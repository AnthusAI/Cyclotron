import {Card,CardContent} from './components/ui/card'
import {ReliabilityCurve,type CalibrationCurve} from './ReliabilityCurve'
import {ModelComparison,type DecisionModelComparison} from './ModelComparison'
import {metricRates,type RateMetrics} from './metricRates'

export type MetricClassifier={id:string;name:string;config?:{classes?:{label:string;role?:string}[]}}
/** Minimal recorded event shape so the monitor can be embedded outside the app. */
export type MetricEvent={payload:Record<string,unknown>;[key:string]:unknown}
type Metrics=RateMetrics&{count:number;accuracy:number|null;calibration?:CalibrationCurve;decision_model_comparison?:DecisionModelComparison}
const percent=(value:number|null|undefined)=>value==null?'—':`${(value*100).toFixed(1)}%`

export function ClassifierMetrics({classifiers,events,compact=false,embedded=false}:{classifiers:MetricClassifier[];events:MetricEvent[];compact?:boolean;embedded?:boolean}){
  const latest=new Map<string,Metrics>()
  for(const event of events)if(event.payload.kind==='cycle-metrics')latest.set(String(event.payload.classifier_id??'legacy'),event.payload.metrics as Metrics)
  return <section aria-label="Running classifier metrics" data-compact={compact} data-embedded={embedded} className="classifier-metrics shrink-0 space-y-2">
    <div className="grid gap-3 md:grid-cols-3">{classifiers.map(classifier=>{
      const metrics=latest.get(classifier.id)
      const {recall,precision,positiveLabels}=metricRates(metrics,classifier.config?.classes)
      return <Card key={classifier.id} role="region" aria-label={`${classifier.name} metrics`} aria-live="polite"><CardContent className="space-y-2">
        {!embedded?<h2 className="text-sm font-medium">{classifier.name}</h2>:null}
        <dl className="grid grid-cols-3 gap-2">{[['Recall',recall],['Precision',precision],['Accuracy',metrics?.accuracy]].map(([name,value])=><div key={String(name)}><dt className="text-xs text-muted-foreground">{name}</dt><dd className="mt-1 font-semibold tabular-nums">{percent(value as number|null|undefined)}</dd>{compact?<div role="meter" aria-label={`${classifier.name} ${name}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value==null?undefined:Math.round(Number(value)*100)} aria-valuetext={percent(value as number|null|undefined)} className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className={`h-full rounded-full metric-${String(name).toLowerCase()}`} style={{width:`${Number(value??0)*100}%`}} /></div>:null}</div>)}</dl>
        {!compact?<p className="text-xs text-muted-foreground">{metrics?.count??0} reviewed in latest 200 · {positiveLabels.length?`positive: ${positiveLabels.join(', ')}`:'macro recall / precision'}</p>:null}
        <details className="disclosure"><summary>Confidence calibration</summary><ReliabilityCurve curve={metrics?.calibration}/></details>
        <details className="disclosure"><summary>Raw decision model vs final classifier</summary><ModelComparison comparison={metrics?.decision_model_comparison} classes={classifier.config?.classes}/></details>
      </CardContent></Card>
    })}</div>
    {!compact?<p className="text-xs text-muted-foreground">Each classifier uses its most recent 200 human-labeled items, or all available if fewer. Predictions were made before your votes. Not a held-out evaluation.</p>:null}
  </section>
}
