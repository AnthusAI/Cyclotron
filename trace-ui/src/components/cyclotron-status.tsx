import type {CyclotronStatus,ReviewRateState} from '../cyclotronStatus'

export type CyclotronStatusViewProps={
  status:CyclotronStatus
  /** A one-line strip for a page header, or a card with the detail. */
  variant?:'strip'|'card'
  /** Shown in the card as "Change review rate"; the application opens its own form. */
  onOverride?:()=>void
}

const percent=(value:number|null|undefined)=>value==null?null:`${Math.round(value*100)}%`
const day=(value:string|null|undefined)=>{
  if(!value)return null
  const date=new Date(value)
  return Number.isNaN(date.getTime())?value:date.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'})
}

/** Every state has a mark and a word; colour only repeats them. */
const STATES:Record<ReviewRateState,{mark:string;word:string}>={
  full:{mark:'●',word:'Full review'},
  onboarding:{mark:'○',word:'Onboarding'},
  tapering:{mark:'▼',word:'Tapering'},
  steady:{mark:'■',word:'Steady'},
  raised:{mark:'▲',word:'Raised to full review'},
  manual:{mark:'✎',word:'Manual rate'},
}

function versionText(status:CyclotronStatus){
  const {version,refits}=status.cyclotron
  return `Version ${version}${refits?` (${refits} refit${refits===1?'':'s'})`:''}`
}

function calibrationText(status:CyclotronStatus){
  const says=percent(status.calibration.saysSure),right=percent(status.calibration.isRight)
  return says&&right?`Says ${says}, right ${right}`:'Calibration not measured yet'
}

function rateText(status:CyclotronStatus){
  const rate=status.reviewRate
  return rate.state==='full'||rate.rate===1?'Reviewing every decision':`Reviewing ${percent(rate.rate)} of confident decisions`
}

function changeText(status:CyclotronStatus){
  const change=status.lastChange
  if(!change)return null
  const when=day(change.at)
  const suffix=when?` · ${when}`:''
  if(change.kind==='promoted')return `${change.fromVersion!=null?`Version ${change.fromVersion} → ${change.toVersion}`:`Version ${change.toVersion}`}: ${change.summary}${suffix}`
  if(change.kind==='refit')return `Refit on ${change.labels??'new'} labels, still version ${change.toVersion} (minor)${suffix}`
  if(change.kind==='dropped')return `Candidate dropped, still version ${change.toVersion}: ${change.summary}${suffix}`
  return `Definition changed to version ${change.toVersion}: ${change.summary}${suffix}`
}

function Metric({name,value,definition,testId}:{name:string;value:number|null;definition:string;testId:string}){
  const shown=percent(value)
  return <div className="cyclotron-status-metric" data-testid={testId}>
    <dt>{name}</dt>
    <dd className="cyclotron-status-value">{shown??'Not measured yet'}</dd>
    <dd className="cyclotron-status-definition">{definition}</dd>
    {shown?<dd className="cyclotron-status-bar" aria-hidden="true"><span style={{width:shown}} /></dd>:null}
  </div>
}

/**
 * What a cyclotron is doing now, rendered from a cyclotron-status/v1 snapshot.
 * Data only: the application fetches the snapshot. Styles: styles/shared.css.
 */
export function CyclotronStatusView({status,variant='strip',onOverride}:CyclotronStatusViewProps){
  const rate=status.reviewRate
  const state=STATES[rate.state]
  const agrees=percent(status.alignment.accuracy)
  const override=rate.override
  if(variant==='strip')return <section className="cyclotron-status" data-variant="strip" aria-label={`${status.cyclotron.id} status`}>
    <span>{versionText(status)}</span>
    <span>{agrees?`Agrees ${agrees}`:'Agreement not measured yet'}</span>
    <span>{calibrationText(status)}</span>
    <span data-review-state={rate.state}><span aria-hidden="true">{state.mark} </span>{state.word}: {rateText(status).toLowerCase()}</span>
    {status.pending.decisionsAwaitingReview?<span>{status.pending.decisionsAwaitingReview} awaiting review</span>:null}
  </section>

  const positive=status.alignment.positiveLabel
  const change=changeText(status)
  return <section className="cyclotron-status" data-variant="card" aria-label={`${status.cyclotron.id} status`}>
    <header className="cyclotron-status-header">
      <h3>{status.cyclotron.classifier}</h3>
      <p>{versionText(status)} · as of {day(status.asOf)}</p>
    </header>
    <dl className="cyclotron-status-metrics">
      <Metric testId="metric-recall" name="Recall" value={status.alignment.recall}
        definition={positive?`Of the items that should be a yes (${positive}), how many it found.`
          :'Of the items in each class, how many it found, averaged across classes.'} />
      <Metric testId="metric-precision" name="Precision" value={status.alignment.precision}
        definition={positive?`Of the yeses it gave (${positive}), how many were right.`
          :'Of the decisions it gave in each class, how many were right, averaged across classes.'} />
      <Metric testId="metric-accuracy" name="Accuracy" value={status.alignment.accuracy}
        definition="Of all items, how many it got right." />
    </dl>
    <p className="cyclotron-status-note">{status.alignment.labels} reviews in the last {status.alignment.window}, {status.alignment.measuredOn}.</p>
    <section className="cyclotron-status-section" aria-label="Calibration">
      <h4>Confidence</h4>
      <p>{calibrationText(status)}{status.calibration.gapPoints!=null?` · off by ${status.calibration.gapPoints} point${status.calibration.gapPoints===1?'':'s'}`:''}</p>
    </section>
    <section className="cyclotron-status-section" aria-label="Review rate" data-review-state={rate.state}>
      <h4><span aria-hidden="true">{state.mark} </span>{state.word}: {rateText(status).toLowerCase()}</h4>
      <p>{rate.reason}</p>
      {override?<p className="cyclotron-status-override">Manual rate {percent(override.rate)} set by {override.setBy}{override.expiresAt?` until ${day(override.expiresAt)}`:', no expiry'}.</p>:null}
      {rate.nextStep?<p className="cyclotron-status-note">Next: {rate.nextStep}</p>:null}
      {rate.expectedReviewsPerWeek!=null?<p className="cyclotron-status-note">About {Math.round(rate.expectedReviewsPerWeek)} reviews a week at this rate.</p>:null}
      {onOverride?<button type="button" className="cyclotron-review-secondary" onClick={onOverride}>Change review rate</button>:null}
    </section>
    {change?<section className="cyclotron-status-section" aria-label="Last change" data-change={status.lastChange?.kind}>
      <h4>Last change</h4>
      <p>{change}</p>
    </section>:null}
    <p className="cyclotron-status-note">
      {status.pending.decisionsAwaitingReview} decision{status.pending.decisionsAwaitingReview===1?'':'s'} awaiting review
      {status.pending.staleSince?` · reviews since ${day(status.pending.staleSince)} are not yet learned`:''}
    </p>
  </section>
}
