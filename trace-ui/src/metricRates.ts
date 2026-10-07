export type ClassRole={label:string;role?:string}
export type RateMetrics={per_class:Record<string,{recall:number|null;precision:number|null}>;confusion_matrix?:Record<string,Record<string,number>>}
const validRate=(value:unknown):value is number=>typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=1

/** Never infer class polarity from its name or silently omit unsupported classes. */
export function metricRates(metrics:RateMetrics|undefined,config?:ClassRole[]){
  const classes:ClassRole[]=config??Object.keys(metrics?.per_class??{}).map(label=>({label}))
  const positiveLabels=classes.filter(row=>row.role==='positive').map(row=>row.label)
  if(!positiveLabels.length){
    const macro=(key:'recall'|'precision')=>{
      const values=classes.map(row=>metrics?.per_class[row.label]?.[key])
      return values.length&&values.every(validRate)?values.reduce((sum,value)=>sum+value,0)/values.length:null
    }
    return {recall:macro('recall'),precision:macro('precision'),positiveLabels,aggregation:'macro' as const}
  }
  const labels=classes.map(row=>row.label),matrix=metrics?.confusion_matrix
  const complete=matrix&&labels.every(a=>matrix[a]&&Object.keys(matrix[a]).length===labels.length&&labels.every(p=>typeof matrix[a][p]==='number'&&Number.isFinite(matrix[a][p])&&matrix[a][p]>=0))
  if(complete){
    const total=(actual:string[],predicted:string[])=>actual.reduce((sum,a)=>sum+predicted.reduce((subtotal,p)=>subtotal+matrix[a][p],0),0)
    const tp=total(positiveLabels,positiveLabels),actual=total(positiveLabels,labels),predicted=total(labels,positiveLabels)
    return {recall:actual?tp/actual:null,precision:predicted?tp/predicted:null,positiveLabels,aggregation:'positive-vs-rest' as const}
  }
  const single=positiveLabels.length===1?metrics?.per_class[positiveLabels[0]]:undefined
  return {recall:validRate(single?.recall)?single.recall:null,precision:validRate(single?.precision)?single.precision:null,positiveLabels,aggregation:'positive-vs-rest' as const}
}
