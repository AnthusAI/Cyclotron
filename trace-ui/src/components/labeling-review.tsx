import {type ReactNode,type Ref} from 'react'
import {buttonVariants} from './ui/button'
import {RadioChoices} from './ui/radio-choices'
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
  prediction?:LabelingReviewPrediction
  classes?:string[]
  requireExplanation?:boolean
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
  const values=prediction.classes.map(label=>({label,value:prediction.probabilities?.[label]??(label===prediction?.label?prediction.confidence:prediction.classes.length===2?1-prediction.confidence:0)}))
  const description=values.map(({label,value})=>`${label}: ${Math.round(value*100)}%`).join(', ')
  return <div className="prediction-confidence" role="meter" aria-label={`${name} prediction confidence`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(prediction!.confidence*100)} aria-valuetext={description} title={description}>
    {values.map(({label,value},index)=><span key={label} data-confidence-class={label} data-confidence-index={index%4} style={{flexGrow:value}} />)}
  </div>
}

/**
 * The shared, controlled label-and-explanation surface used by live review and
 * non-mutating recorded replays. It owns no draft state and never submits data.
 */
export function LabelingReview({targetId,classifierName,prediction,classes=[],requireExplanation=false,selectedLabel='',explanationDraft='',busy=false,readOnly=false,simulation=false,recordedLabel,recordedExplanation,onSelectedLabelChange,onExplanationDraftChange,explanationRef,children}:LabelingReviewProps){
  const selected=recordedLabel??selectedLabel
  const explanation=recordedExplanation??explanationDraft
  const locked=busy||readOnly
  return <section className="label-classifier space-y-2 rounded-lg border border-border p-3" data-testid={`labeling-review-${targetId}`} data-cyclotron-labeling-review="true" data-cyclotron-label-target={targetId} data-cyclotron-simulation={simulation?'true':'false'}>
    <h3 className="font-medium">{classifierName}</h3>
    <div className="prediction-choices"><RadioChoices ariaLabel={`${classifierName} classification`} className="prediction-buttons" itemClassName={`${buttonVariants({variant:'outline'})} label-choice min-h-12 flex-1 flex-col gap-0.5 px-3 py-2`} value={selected} disabled={locked} clearable onChange={value=>onSelectedLabelChange?.(value)}
      choices={(prediction?.classes??classes).map((label,index)=>{
        const predicted=label===prediction?.label
        const isSelected=selected===label
        return {value:label,ariaLabel:`${classifierName}: ${label}${predicted?`, predicted ${Math.round(prediction!.confidence*100)}%`:''}`,
          attrs:{'data-cyclotron-label-option':label,'data-predicted':String(predicted),'data-agreement':isSelected?(prediction?(predicted?'correct':'incorrect'):'selected'):'unselected'},
          content:<><span className="flex items-center gap-1.5 text-base font-semibold"><span aria-hidden="true" className="confidence-key" data-confidence-index={index%4} />{label}</span>{predicted?<span className="text-xs">Predicted · {Math.round(prediction!.confidence*100)}%</span>:<span className="text-xs">{isSelected?'Your label':'Choose'}</span>}</>}
      })} />{prediction?<ConfidenceBar name={classifierName} prediction={prediction} />:null}</div>
    {recordedLabel?<p className="text-xs text-muted-foreground">Recorded label: {recordedLabel} · replayed after prediction</p>:null}
    <Label htmlFor={`explanation-${targetId}`} className="block text-sm font-semibold">Explanation for {classifierName}</Label>
    <p id={`explanation-hint-${targetId}`} className="text-sm text-muted-foreground">Why? A sentence or two helps it learn what you want.{requireExplanation?' (required)':''}</p>
    <textarea ref={explanationRef} aria-describedby={`explanation-hint-${targetId}`} id={`explanation-${targetId}`} readOnly={readOnly} disabled={busy} rows={4} required={requireExplanation} placeholder="Explanations are what it learns from. Say what made this the right answer." className="block min-h-28 w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm" value={explanation} onChange={event=>onExplanationDraftChange?.(event.target.value)} data-cyclotron-explanation-target={targetId} />
    {children}
  </section>
}
