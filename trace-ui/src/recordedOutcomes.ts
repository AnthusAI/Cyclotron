import {metricRates,type ClassRole,type RateMetrics} from './metricRates'
import type {DecisionModelComparison} from './ModelComparison'

export type OutcomePoint={cycle:number|null;count:number;probabilityCount:number;recall:number|null;precision:number|null;accuracy:number|null;ece:number|null;brier:number|null;rawFinal?:DecisionModelComparison}
export type OutcomeWindow={id:string;classes:ClassRole[];first:OutcomePoint;latest:OutcomePoint}
const finite=(value:unknown):number|null=>typeof value==='number'&&Number.isFinite(value)?value:null
const object=(value:unknown):Record<string,unknown>|null=>value!=null&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:null
const pairedOutputs=(value:unknown):DecisionModelComparison|undefined=>{
  const comparison=object(value)
  if(!comparison||!(Number(comparison.count)>0))return undefined
  for(const key of ['raw','final']){
    const output=object(comparison[key]),calibration=object(output?.calibration)
    if(!output||!object(output.per_class)||!calibration||!Array.isArray(calibration.bins))return undefined
  }
  return comparison as unknown as DecisionModelComparison
}

/** Read recorded windows only. Neither recompute predictions nor infer missing metrics. */
export function recordedOutcomeWindows(events:Record<string,unknown>[],fallbackClasses:ClassRole[]=[]):OutcomeWindow[]{
  const windows=new Map<string,OutcomeWindow>()
  for(const event of events){
    if(event.kind!=='cycle-metrics')continue
    const metrics=object(event.metrics),count=finite(metrics?.count)
    if(!metrics||count==null||count<=0)continue
    const classes=Array.isArray(event.class_config)?event.class_config as ClassRole[]:fallbackClasses
    const calibration=object(metrics.calibration)
    const rates=metricRates({...metrics,per_class:object(metrics.per_class)??{}} as RateMetrics,classes.length?classes:undefined)
    const point:OutcomePoint={cycle:finite(event.cycle_number),count,probabilityCount:finite(calibration?.count)??0,...rates,
      accuracy:finite(metrics.accuracy),ece:finite(calibration?.ece),brier:finite(calibration?.brier),
      rawFinal:pairedOutputs(metrics.decision_model_comparison)}
    const id=typeof event.classifier_id==='string'?event.classifier_id:'Classifier'
    const prior=windows.get(id)
    windows.set(id,{id,classes,first:prior?.first??point,latest:point})
  }
  return [...windows.values()]
}
