import {type ReactNode} from 'react'
import {Button} from '@/components/ui/button'
import {Card,CardHeader,CardTitle,CardContent} from '@/components/ui/card'
import {Label} from '@/components/ui/label'
import {ClassifierMetrics,type MetricClassifier} from './ClassifierMetrics'
import type {TraceEvent} from './graphql'
import {useLabelDraft} from './labelDraft'

export type BatchItem={item:{id:string;values:Record<string,unknown>};prediction:{presentation_id:string;recorded_labels?:{classifier_id:string;label:string;comment:string}[];classifiers:Record<string,{label:string;confidence:number;classes:string[];probabilities?:Record<string,number>}>}}

function ConfidenceBar({name,result}:{name:string;result:BatchItem['prediction']['classifiers'][string]}){
  const values=result.classes.map(label=>({label,value:result.probabilities?.[label]??(label===result.label?result.confidence:result.classes.length===2?1-result.confidence:0)}))
  const description=values.map(({label,value})=>`${label}: ${Math.round(value*100)}%`).join(', ')
  return <div className="prediction-confidence" role="meter" aria-label={`${name} prediction confidence`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(result.confidence*100)} aria-valuetext={description} title={description}>
    {values.map(({label,value},index)=><span key={label} data-confidence-class={label} data-confidence-index={index%4} style={{flexGrow:value}} />)}
  </div>
}

export function MultiLabelCard({current,names,busy,onSubmit,classifiers=[],events=[],footer}:{current:BatchItem;names:Record<string,string>;busy:boolean;onSubmit:(kind:string,payload:Record<string,unknown>)=>void;classifiers?:MetricClassifier[];events?:TraceEvent[];footer?:ReactNode}){
  const [{labels,comments},setDraft]=useLabelDraft(current.prediction.presentation_id)
  const setLabels=(update:(previous:Record<string,string>)=>Record<string,string>)=>setDraft(previous=>({...previous,labels:update(previous.labels)}))
  const setComments=(update:(previous:Record<string,string>)=>Record<string,string>)=>setDraft(previous=>({...previous,comments:update(previous.comments)}))
  const {item,prediction}=current
  const outputs=new Map(Object.entries(prediction.classifiers))
  // Serialized response objects have no display-order contract. Use the pinned
  // scorecard order, retaining unconfigured outputs for legacy presentations.
  const orderedIds=[...new Set([...classifiers.map(classifier=>classifier.id),...outputs.keys()])].filter(id=>outputs.has(id))
  const orderedOutputs=orderedIds.map(id=>[id,outputs.get(id)!] as const)
  const recorded=new Map(prediction.recorded_labels?.map(row=>[row.classifier_id,row]))
  const choices=orderedIds.filter(id=>labels[id]).map(id=>({classifier_id:id,label:labels[id],comment:comments[id]??''}))
  const missingHistorical=recorded.size>0&&orderedIds.some(id=>!recorded.has(id)&&!labels[id])
  return <Card className="labeling-card"><CardHeader><CardTitle>{String(item.values.title??item.id)}</CardTitle></CardHeader><CardContent className="labeling-card-content space-y-5">
    <div className="labeling-reading">
    <p className="labeling-article whitespace-pre-wrap text-lg leading-relaxed">{String(item.values.abstract??item.values.text??'')}</p>
    <p className="text-xs text-muted-foreground">{String(item.values.submitted_at??'')} · {String(item.values.authors??'')} · {Array.isArray(item.values.categories)?item.values.categories.join(', '):''}</p>
    </div>
    <fieldset disabled={busy} aria-busy={busy} aria-label="Classifier feedback" tabIndex={0} className="classifier-feedback-strip">{orderedOutputs.map(([id,result])=><section key={id} className="label-classifier space-y-2 rounded-lg border border-border p-3"><h3 className="font-medium">{names[id]??id}</h3>
      <div className="prediction-choices"><div role="group" aria-label={`${names[id]??id} classification`} className="prediction-buttons">{result.classes.map((label,index)=>{
        const predicted=label===result.label,selected=(recorded.get(id)?.label??labels[id])===label
        return <Button key={label} variant="outline" disabled={busy||recorded.has(id)} className="label-choice min-h-12 flex-1 flex-col gap-0.5 px-3 py-2" aria-label={`${names[id]??id}: ${label}${predicted?`, predicted ${Math.round(result.confidence*100)}%`:''}`} aria-pressed={selected} data-predicted={predicted} data-agreement={selected?(predicted?'correct':'incorrect'):'unselected'} onClick={()=>setLabels(previous=>({...previous,[id]:previous[id]===label?'':label}))}>
          <span className="flex items-center gap-1.5 text-base font-semibold"><span aria-hidden="true" className="confidence-key" data-confidence-index={index%4} />{label}</span>{predicted?<span className="text-xs">Predicted · {Math.round(result.confidence*100)}%</span>:<span className="text-xs">{selected?'Your label':'Choose'}</span>}
        </Button>
      })}</div><ConfidenceBar name={names[id]??id} result={result} /></div>
      {recorded.has(id)?<p className="text-xs text-muted-foreground">Recorded label: {recorded.get(id)?.label} · replayed after prediction</p>:null}
      <Label htmlFor={`explanation-${id}`} className="text-xs text-muted-foreground">Explanation for {names[id]??id}</Label><textarea id={`explanation-${id}`} readOnly={recorded.has(id)} rows={2} placeholder="Optional explanation" className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm" value={recorded.get(id)?.comment??comments[id]??''} onChange={event=>setComments(previous=>({...previous,[id]:event.target.value}))} />
      <ClassifierMetrics compact embedded classifiers={[classifiers.find(classifier=>classifier.id===id)??{id,name:names[id]??id}]} events={events} />
      </section>)}</fieldset>
    <div className="labeling-footer flex shrink-0 items-center justify-between gap-3"><div className="min-w-0 flex-1">{footer}</div>
    <div role="group" aria-label="Review actions" className="flex shrink-0 justify-end gap-3"><Button className="min-h-12 min-w-28 px-6 text-base" title={recorded.size?'Complete missing historical labels before moving to new items':undefined} disabled={busy||recorded.size>0} variant="ghost" onClick={()=>onSubmit('skip',{item_id:item.id})}>Skip item</Button><Button className="min-h-12 min-w-40 px-6 text-base" disabled={busy||!choices.length||missingHistorical} onClick={()=>onSubmit('label',{item_id:item.id,presentation_id:prediction.presentation_id,labels:choices})}>{busy?'Submitting…':'Submit feedback'}</Button></div>
    </div>
  </CardContent></Card>
}
