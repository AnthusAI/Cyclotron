import {useId,useState} from 'react'
import {ThumbsDown,ThumbsUp} from 'lucide-react'

/** The decision a reviewer is checking. */
export type ReviewControlDecision={
  decisionId:string
  label:string
  /** How sure the cyclotron is, from 0 to 1; null when unknown. */
  confidence:number|null
  classes:string[]
  version?:number
}

/** An application's reason code. `noLabel` reasons close the review without teaching (for example "duplicate"). */
export type ReviewReason={code:string;text:string;noLabel?:boolean}

/** What the reviewer submitted; `label` is null for a review without a label. */
export type ReviewControlReview={
  itemId:string
  decisionId:string
  label:string|null
  reasonCode:string|null
  explanation:string|null
}

export type ReviewControlProps={
  itemId:string
  decision:ReviewControlDecision
  /** The decision question, shown as the heading. */
  question?:string
  /** Why this decision was sent to review, in words. */
  reviewReason?:string
  /** Thumbs for two classes (the default when there are two), or one button per class. */
  mode?:'thumbs'|'labels'
  /** The thumbs-up class; defaults to the first class. */
  positiveLabel?:string
  /** Reason codes; in thumbs mode a thumbs-down review needs one. */
  reasons?:ReviewReason[]
  /** A recorded review: shown read-only, with Undo when `onUndo` is given. */
  value?:{label:string|null;reasonCode?:string|null;explanation?:string|null}
  disabled?:boolean
  compact?:boolean
  onReview?:(review:ReviewControlReview)=>void
  onUndo?:(target:{itemId:string;decisionId:string})=>void
}

const percent=(value:number)=>`${Math.round(value*100)}%`

/**
 * A data-only review control: it shows the decision and its confidence, takes
 * a label, a reason code and an explanation, and reports them through
 * `onReview`. It never calls a server. Styles: `cyclotron/styles/shared.css`.
 */
export function ReviewControl({itemId,decision,question,reviewReason,mode,positiveLabel,reasons=[],value,disabled=false,compact=false,onReview,onUndo}:ReviewControlProps){
  const id=useId()
  const thumbs=(mode??(decision.classes.length===2?'thumbs':'labels'))==='thumbs'
  const positive=positiveLabel??decision.classes[0]
  const negative=decision.classes.find(label=>label!==positive)??decision.classes[1]
  // The draft belongs to one decision; a new decision starts empty.
  const empty={decisionId:decision.decisionId,choice:null as string|null,reasonCode:'',explanation:''}
  const [stored,setDraft]=useState(empty)
  const draft=stored.decisionId===decision.decisionId?stored:empty
  const update=(change:Partial<typeof empty>)=>setDraft({...draft,...change})
  const {choice,reasonCode,explanation}=draft
  const setChoice=(value:string|null)=>update({choice:value})
  const setReasonCode=(value:string)=>update({reasonCode:value})
  const setExplanation=(value:string)=>update({explanation:value})
  if(thumbs&&decision.classes.length!==2)throw new Error('thumbs mode needs exactly two classes')

  const reason=reasons.find(row=>row.code===reasonCode)
  const needsReason=thumbs&&choice===negative&&reasons.length>0
  const ready=choice!==null&&(!needsReason||Boolean(reason))
  const label=reason?.noLabel?null:choice
  const sure=decision.confidence==null?'confidence unknown':`${percent(decision.confidence)} sure`
  const submit=()=>{
    if(!ready||disabled)return
    onReview?.({itemId,decisionId:decision.decisionId,label,reasonCode:reason?.code??null,explanation:explanation.trim()||null})
  }

  if(value){
    const recordedReason=reasons.find(row=>row.code===value.reasonCode)
    return <section className="cyclotron-review" data-compact={compact} data-recorded="true" aria-label={question??'Review'}>
      {question?<h3 className="cyclotron-review-question">{question}</h3>:null}
      <p className="cyclotron-review-decision">Decision: <strong>{decision.label}</strong> · {sure}</p>
      <p className="cyclotron-review-recorded">
        <span aria-hidden="true">{value.label==null?'○':value.label===decision.label?'✓':'✗'} </span>
        {value.label==null?'Reviewed without a label':value.label===decision.label?`Agreed: ${value.label}`:`Correction: ${value.label}`}
        {recordedReason?` · ${recordedReason.text}`:value.reasonCode?` · ${value.reasonCode}`:''}
      </p>
      {value.explanation?<p className="cyclotron-review-explanation-text">“{value.explanation}”</p>:null}
      {onUndo?<button type="button" className="cyclotron-review-secondary" disabled={disabled} onClick={()=>onUndo({itemId,decisionId:decision.decisionId})}>Undo review</button>:null}
    </section>
  }

  const choices=thumbs
    ?[{label:positive,text:'Yes',icon:<ThumbsUp aria-hidden="true" className="cyclotron-review-icon" />},
      {label:negative,text:'No',icon:<ThumbsDown aria-hidden="true" className="cyclotron-review-icon" />}]
    :decision.classes.map(label=>({label,text:label,icon:null}))
  return <section className="cyclotron-review" data-compact={compact} data-mode={thumbs?'thumbs':'labels'} aria-label={question??'Review'}>
    {question?<h3 className="cyclotron-review-question">{question}</h3>:null}
    <p className="cyclotron-review-decision">Decision: <strong>{decision.label}</strong> · {sure}{decision.version!=null?` · version ${decision.version}`:''}</p>
    {reviewReason?<p className="cyclotron-review-why">Why you are seeing this: {reviewReason}</p>:null}
    <div role="group" aria-label="Your label" className="cyclotron-review-choices">
      {choices.map(row=>{
        const pressed=choice===row.label
        return <button key={row.label} type="button" className="cyclotron-review-choice" aria-pressed={pressed} disabled={disabled}
          data-cyclotron-label-option={row.label} data-agreement={pressed?(row.label===decision.label?'agreement':'correction'):'none'}
          aria-label={thumbs?`${row.text}: ${row.label}`:row.label}
          onClick={()=>setChoice(pressed?null:row.label)}>
          {row.icon}<span>{thumbs?`${row.text} · ${row.label}`:row.label}</span>
          {pressed?<span className="cyclotron-review-mark" aria-hidden="true">{row.label===decision.label?' ✓':' ✗'}</span>:null}
        </button>
      })}
    </div>
    {choice!==null&&choice!==decision.label?<p className="cyclotron-review-note">This is a correction: the decision was {decision.label}.</p>:null}
    {reasons.length&&(!thumbs||choice===negative)?<label className="cyclotron-review-field" htmlFor={`${id}-reason`}>
      <span>Reason{needsReason?'':' (optional)'}</span>
      <select id={`${id}-reason`} value={reasonCode} disabled={disabled} onChange={event=>setReasonCode(event.target.value)}>
        <option value="">Choose a reason</option>
        {reasons.map(row=><option key={row.code} value={row.code}>{row.text}{row.noLabel?' (no label)':''}</option>)}
      </select>
    </label>:null}
    <label className="cyclotron-review-field" htmlFor={`${id}-explanation`}>
      <span>Explanation (optional)</span>
      <textarea id={`${id}-explanation`} rows={compact?1:2} value={explanation} disabled={disabled}
        placeholder="Why? The cyclotron learns from this." onChange={event=>setExplanation(event.target.value)} />
    </label>
    <div className="cyclotron-review-actions">
      <button type="button" className="cyclotron-review-submit" disabled={disabled||!ready} onClick={submit}>Submit review</button>
    </div>
  </section>
}
