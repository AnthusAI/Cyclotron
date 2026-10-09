import {type ReactNode} from 'react'
import {Button} from '@/components/ui/button'
import {Card,CardHeader,CardTitle,CardContent} from '@/components/ui/card'
import {ClassifierMetrics,type MetricClassifier} from './ClassifierMetrics'
import type {TraceEvent} from './graphql'
import {useLabelDraft} from './labelDraft'
import {LabelingReview} from './components/labeling-review'

export type BatchItem={item:{id:string;values:Record<string,unknown>};prediction:{presentation_id:string;recorded_labels?:{classifier_id:string;label:string;comment:string}[];classifiers:Record<string,{label:string;confidence:number;classes:string[];probabilities?:Record<string,number>}>}}

export function MultiLabelCard({current,names,busy,readOnly=false,onSubmit,classifiers=[],events=[],footer}:{current:BatchItem;names:Record<string,string>;busy:boolean;readOnly?:boolean;onSubmit:(kind:string,payload:Record<string,unknown>)=>void;classifiers?:MetricClassifier[];events?:TraceEvent[];footer?:ReactNode}){
  const [{labels,comments},setDraft]=useLabelDraft(current.prediction.presentation_id)
  const setLabels=(update:(previous:Record<string,string>)=>Record<string,string>)=>setDraft(previous=>({...previous,labels:update(previous.labels)}))
  const setComments=(update:(previous:Record<string,string>)=>Record<string,string>)=>setDraft(previous=>({...previous,comments:update(previous.comments)}))
  const {item,prediction}=current
  const outputs=new Map(Object.entries(prediction.classifiers))
  // Serialized response objects have no display-order contract. Use the pinned
  // cyclotron order, retaining unconfigured outputs for legacy presentations.
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
    <fieldset disabled={busy||readOnly} aria-busy={busy} aria-label="Classifier feedback" tabIndex={0} className="classifier-feedback-strip">{orderedOutputs.map(([id,result])=><LabelingReview key={id} targetId={id} classifierName={names[id]??id} prediction={result} selectedLabel={labels[id]} explanationDraft={comments[id]??''} readOnly={readOnly||recorded.has(id)} recordedLabel={recorded.get(id)?.label} recordedExplanation={recorded.get(id)?.comment} onSelectedLabelChange={label=>setLabels(previous=>({...previous,[id]:label}))} onExplanationDraftChange={comment=>setComments(previous=>({...previous,[id]:comment}))}>
      <ClassifierMetrics compact embedded classifiers={[classifiers.find(classifier=>classifier.id===id)??{id,name:names[id]??id}]} events={events} />
      </LabelingReview>)}</fieldset>
    <div className="labeling-footer flex shrink-0 items-center justify-between gap-3"><div className="min-w-0 flex-1">{footer}</div>
    <div role="group" aria-label="Review actions" className="flex shrink-0 justify-end gap-3"><Button className="min-h-12 min-w-28 px-6 text-base" title={recorded.size?'Complete missing historical labels before moving to new items':undefined} disabled={busy||readOnly||recorded.size>0} variant="ghost" onClick={()=>onSubmit('skip',{item_id:item.id})}>Skip item</Button><Button className="min-h-12 min-w-40 px-6 text-base" disabled={busy||readOnly||!choices.length||missingHistorical} onClick={()=>onSubmit('label',{item_id:item.id,presentation_id:prediction.presentation_id,labels:choices})}>{busy?'Submitting…':'Submit feedback'}</Button></div>
    </div>
  </CardContent></Card>
}
