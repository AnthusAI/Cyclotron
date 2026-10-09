import {type ReactNode,type Ref} from 'react'
import {Button} from './ui/button'
import {Label} from './ui/label'

export type LabelingReviewPrediction={
  label:string
  confidence:number
  classes:string[]
  probabilities?:Record<string,number>
}

export type LabelingReviewProps={
  /** Stable key used by replay cursors and label automation. */
  targetId:string
  classifierName:string
  prediction:LabelingReviewPrediction
  selectedLabel?:string
  explanationDraft?:string
  busy?:boolean
  readOnly?:boolean
  /** Marks a non-mutating, recorded replay rather than a live review form. */
  simulation?:boolean
  recordedLabel?:string
  recordedExplanation?:string
  onSelectedLabelChange?:(label:string)=>void
  onExplanationDraftChange?:(value:string)=>void
  explanationRef?:Ref<HTMLTextAreaElement>
  children?:ReactNode
}

function ConfidenceBar({name,prediction}:{name:string;prediction:LabelingReviewPrediction}){
  const values=prediction.classes.map(label=>({label,value:prediction.probabilities?.[label]??(label===prediction.label?prediction.confidence:prediction.classes.length===2?1-prediction.confidence:0)}))
  const description=values.map(({label,value})=>`${label}: ${Math.round(value*100)}%`).join(', ')
  return <div className="prediction-confidence" role="meter" aria-label={`${name} prediction confidence`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(prediction.confidence*100)} aria-valuetext={description} title={description}>
    {values.map(({label,value},index)=><span key={label} data-confidence-class={label} data-confidence-index={index%4} style={{flexGrow:value}} />)}
  </div>
}

/**
 * The shared, controlled label-and-explanation surface used by live review and
 * non-mutating recorded replays. It owns no draft state and never submits data.
 */
export function LabelingReview({targetId,classifierName,prediction,selectedLabel='',explanationDraft='',busy=false,readOnly=false,simulation=false,recordedLabel,recordedExplanation,onSelectedLabelChange,onExplanationDraftChange,explanationRef,children}:LabelingReviewProps){
  const selected=recordedLabel??selectedLabel
  const explanation=recordedExplanation??explanationDraft
  const locked=busy||readOnly
  return <section className="label-classifier space-y-2 rounded-lg border border-border p-3" data-testid={`labeling-review-${targetId}`} data-cyclotron-labeling-review="true" data-cyclotron-label-target={targetId} data-cyclotron-simulation={simulation?'true':'false'}>
    <h3 className="font-medium">{classifierName}</h3>
    <div className="prediction-choices"><div role="group" aria-label={`${classifierName} classification`} className="prediction-buttons">{prediction.classes.map((label,index)=>{
      const predicted=label===prediction.label
      const isSelected=selected===label
      return <Button key={label} variant="outline" disabled={locked} className="label-choice min-h-12 flex-1 flex-col gap-0.5 px-3 py-2" aria-label={`${classifierName}: ${label}${predicted?`, predicted ${Math.round(prediction.confidence*100)}%`:''}`} aria-pressed={isSelected} data-cyclotron-label-option={label} data-predicted={predicted} data-agreement={isSelected?(predicted?'correct':'incorrect'):'unselected'} onClick={()=>onSelectedLabelChange?.(selectedLabel===label?'':label)}>
        <span className="flex items-center gap-1.5 text-base font-semibold"><span aria-hidden="true" className="confidence-key" data-confidence-index={index%4} />{label}</span>{predicted?<span className="text-xs">Predicted · {Math.round(prediction.confidence*100)}%</span>:<span className="text-xs">{isSelected?'Your label':'Choose'}</span>}
      </Button>
    })}</div><ConfidenceBar name={classifierName} prediction={prediction} /></div>
    {recordedLabel?<p className="text-xs text-muted-foreground">Recorded label: {recordedLabel} · replayed after prediction</p>:null}
    <Label htmlFor={`explanation-${targetId}`} className="text-xs text-muted-foreground">Explanation for {classifierName}</Label>
    <textarea ref={explanationRef} id={`explanation-${targetId}`} readOnly={readOnly} disabled={busy} rows={2} placeholder="Optional explanation" className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm" value={explanation} onChange={event=>onExplanationDraftChange?.(event.target.value)} data-cyclotron-explanation-target={targetId} />
    {children}
  </section>
}
