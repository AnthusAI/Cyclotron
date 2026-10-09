// Quoted verbatim in docs/embedding.md; a spec keeps the two identical and renders it.
// example:start
import {ReviewControl,type ReviewControlReview,type ReviewReason} from '../components/review-control'
import {CyclotronStatusView} from '../components/cyclotron-status'
import type {CyclotronStatus} from '../cyclotronStatus'
import type {CyclotronDecision} from '../cyclotronSdk'
// In an application: import ... from 'cyclotron/components/review-control', and so on,
// plus `import 'cyclotron/styles/components.css'` once.

const reasons:ReviewReason[]=[
  {code:'out_of_scope',text:'Out of scope'},
  {code:'policy_exclusion',text:'Policy exclusion'},
  {code:'duplicate',text:'Duplicate',noLabel:true},
]

/** One pending item: the decision the cyclotron made and the reviewer's controls. */
export function PendingItem({itemId,decision,onReview}:{itemId:string;decision:CyclotronDecision;onReview:(review:ReviewControlReview)=>void}){
  const [classifier,result]=Object.entries(decision.classifiers)[0]
  return <ReviewControl
    itemId={itemId}
    decision={{decisionId:decision.decisionId,label:result.label,confidence:result.confidence,
               classes:['include','exclude'],version:result.version}}
    question={classifier==='relevant'?'Is this relevant to the publication?':classifier}
    reviewReason={decision.review.selected?decision.review.detail:undefined}
    positiveLabel="include"
    reasons={reasons}
    onReview={onReview} />
}

/** The page header strip and the detail card. */
export function CyclotronHeader({status,onOverride}:{status:CyclotronStatus;onOverride:()=>void}){
  return <>
    <CyclotronStatusView status={status} />
    <details><summary>What the cyclotron is doing</summary>
      <CyclotronStatusView status={status} variant="card" onOverride={onOverride} />
    </details>
  </>
}
// example:end
