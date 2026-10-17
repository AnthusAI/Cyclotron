import {useEffect,useState} from 'react'
import type {HeldOutClient,HeldOutInterval,HeldOutMetrics,HeldOutOverview,HeldOutReport,HeldOutSetSummary,HeldOutVerdict} from '../heldOutReport'

const percent=(value:number|null|undefined)=>value==null?'Not measured':`${Math.round(value*100)}%`
const day=(value:string|null|undefined)=>{
  if(!value)return ''
  const date=new Date(value)
  return Number.isNaN(date.getTime())?value:date.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'})
}
const plural=(count:number,word:string)=>`${count} ${word}${count===1?'':'s'}`

/** Every verdict has a mark and words; colour only repeats them. */
const VERDICTS:Record<HeldOutVerdict,{mark:string;words:string}>={
  improved:{mark:'▲',words:'Clear improvement'},
  worse:{mark:'▼',words:'Clearly worse'},
  'no-clear-change':{mark:'●',words:'No clear change'},
  'too-few-labels':{mark:'○',words:'Too few labels to claim a change'},
  'nothing-learned-yet':{mark:'–',words:'Nothing learned yet'},
}

function labelCountsText(counts:Record<string,number>){
  return Object.entries(counts).map(([label,count])=>`${count} ${label}`).join(', ')
}

function pValueText(pValue:number){
  return pValue<.001?'p < 0.001':`p = ${pValue.toFixed(3)}`
}

function changePointsText(points:number|null){
  if(points==null)return null
  return `${points>0?'+':''}${points.toFixed(1)} points`
}

function intervalText(interval:HeldOutInterval|null|undefined,format:(value:number)=>string){
  return interval?`95% interval ${format(interval.low)} to ${format(interval.high)}`:null
}

function metricRows(report:HeldOutReport){
  const positive=report.positiveLabel
  const rows:{name:string;pick:(metrics:HeldOutMetrics)=>number|null}[]=[
    {name:report.headline.name,pick:metrics=>metrics.balancedAccuracy},
    {name:'Accuracy',pick:metrics=>metrics.accuracy},
    {name:positive?`Precision (${positive})`:'Precision (average)',pick:metrics=>metrics.precision},
    {name:positive?`Recall (${positive})`:'Recall (average)',pick:metrics=>metrics.recall},
  ]
  for(const label of Object.keys(report.heldOutSet.scoredLabelCounts))
    rows.push({name:`Agrees on ${label}`,pick:metrics=>metrics.recallByLabel[label]??null})
  return rows
}

export type HeldOutReportViewProps={
  report:HeldOutReport|null
  /** The frozen sets, newest last; shown when there is no report yet. */
  sets?:HeldOutSetSummary[]
  /** The classifier's display name for the title; defaults to its id. */
  classifierName?:string
  /** Shown as "Freeze held-out set"; freezing makes no model call. */
  onFreeze?:()=>void
  /** Shown as "Evaluate" with the request bound; omit it where live calls are not allowed. */
  onEvaluate?:()=>void
  requestBound?:number|null
  busy?:boolean
}

/**
 * The before-and-after measurement on a frozen held-out set, rendered from a cyclotron-held-out-report/v1 document.
 * Data only unless onFreeze or onEvaluate is given. Styles: styles/components.css.
 */
export function HeldOutReportView({report,sets=[],classifierName,onFreeze,onEvaluate,requestBound,busy=false}:HeldOutReportViewProps){
  const title=classifierName?.trim()||report?.cyclotron.classifier||'Classifier'
  const latestSet=sets.at(-1)
  const actions=onFreeze||onEvaluate?<div className="cyclotron-held-out-actions">
    {onFreeze?<button type="button" className="cyclotron-review-secondary" disabled={busy} onClick={onFreeze}>Freeze held-out set</button>:null}
    {onEvaluate&&latestSet?<button type="button" className="cyclotron-review-submit" disabled={busy} onClick={onEvaluate}>
      Evaluate{requestBound!=null?` (up to ${plural(requestBound,'model request')})`:''}</button>:null}
  </div>:null
  if(!report)return <section className="cyclotron-held-out-report" aria-label={`${title} before and after`}>
    <header className="cyclotron-held-out-header"><h3>{title}: before and after</h3></header>
    <p className="cyclotron-held-out-note">{latestSet
      ?`Held-out set frozen ${day(latestSet.frozenAt)} with ${plural(latestSet.items,'item')} (${labelCountsText(latestSet.labelCounts)}). Not evaluated yet.`
      :'No held-out set yet. Freeze one once reviewers have labeled some of the audit share.'}</p>
    {actions}
  </section>

  const verdict=VERDICTS[report.change.verdict]
  const set=report.heldOutSet
  const points=changePointsText(report.headline.changePoints)
  const pointsInterval=intervalText(report.headline.changeInterval,value=>`${value>0?'+':''}${value.toFixed(1)}`)
  const sharesInterval=(label:string,interval:HeldOutInterval|null|undefined)=>{
    const text=intervalText(interval,value=>percent(value))
    return text?`${label} ${text}`:null
  }
  const endpointIntervals=[sharesInterval('Before',report.headline.beforeInterval),sharesInterval('After',report.headline.afterInterval)]
    .filter(Boolean).join(' · ')
  return <section className="cyclotron-held-out-report" aria-label={`${title} before and after`} data-verdict={report.change.verdict}>
    <header className="cyclotron-held-out-header">
      <h3>{title}: before and after</h3>
      <p>Evaluated {day(report.evaluatedAt)} on {plural(set.scored,'held-out item')} ({labelCountsText(set.scoredLabelCounts)}), frozen {day(set.frozenAt)}</p>
    </header>
    <div className="cyclotron-held-out-headline" data-testid="cyclotron-held-out-headline">
      <p className="cyclotron-held-out-headline-name">{report.headline.name}</p>
      <p className="cyclotron-held-out-headline-value">{percent(report.headline.before)} → {percent(report.headline.after)}{points?<span className="cyclotron-held-out-points"> {points}</span>:null}</p>
      {pointsInterval?<p className="cyclotron-held-out-interval" data-testid="cyclotron-held-out-interval">Change {pointsInterval} points{endpointIntervals?` · ${endpointIntervals}`:''}</p>:null}
      <p className="cyclotron-held-out-verdict" data-verdict={report.change.verdict}><span aria-hidden="true">{verdict.mark} </span>{verdict.words}</p>
      <p className="cyclotron-held-out-note">{report.headline.definition}</p>
    </div>
    <table className="cyclotron-held-out-table">
      <thead><tr>
        <th scope="col">Measure</th>
        <th scope="col">Before · version {report.before.version}</th>
        <th scope="col">After · version {report.after.version}{report.after.refits?` (${plural(report.after.refits,'refit')})`:''}</th>
      </tr></thead>
      <tbody>{metricRows(report).map(row=><tr key={row.name}>
        <th scope="row">{row.name}</th>
        <td>{percent(row.pick(report.before.metrics))}</td>
        <td>{percent(row.pick(report.after.metrics))}</td>
      </tr>)}</tbody>
    </table>
    <p className="cyclotron-held-out-note">Before: {report.before.description} After: {report.after.description}</p>
    <p data-testid="cyclotron-held-out-pairs">Newly right on {report.change.gained} · newly wrong on {report.change.lost} · sign test {pValueText(report.change.pValue)} (item counts, unweighted)</p>
    <p>{report.change.summary}</p>
    <ul className="cyclotron-held-out-notes">
      <li>{set.selection}</li>
      {set.weighted?<li>Some items were reviewed at a reduced rate, so each counts with the inverse of its chance of review and the numbers stand for every audit decision.</li>:null}
      {set.excludedSinceFrozen?<li>{plural(set.excludedSinceFrozen,'frozen item')} later used for training {set.excludedSinceFrozen===1?'is':'are'} left out.</li>:null}
      {set.labelsChangedSinceFrozen?<li>{plural(set.labelsChangedSinceFrozen,'label')} corrected since freezing; scoring uses the frozen labels, so this count is informational.</li>:null}
      <li>This evaluation made {plural(report.cost.newRequests,'new model request')} and answered {report.cost.cachedRequests} from cache.</li>
    </ul>
    {actions}
  </section>
}

export type HeldOutPanelProps={
  client:HeldOutClient
  cyclotronId:string
  classifier?:string|null
  classifierName?:string
}

/** The held-out card with its own loading, freezing and evaluating, through the host's transport. */
export function HeldOutPanel({client,cyclotronId,classifier=null,classifierName}:HeldOutPanelProps){
  const [overview,setOverview]=useState<HeldOutOverview|null>(null)
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState<string|null>(null)
  const describe=(failure:unknown)=>failure instanceof Error?failure.message:String(failure)
  useEffect(()=>{
    let superseded=false
    client.overview(cyclotronId,classifier).then(
      loaded=>{if(!superseded)setOverview(loaded)},
      failure=>{if(!superseded)setError(describe(failure))})
    return ()=>{superseded=true}
  },[client,cyclotronId,classifier])
  const act=async(work:()=>Promise<unknown>)=>{
    setBusy(true);setError(null)
    try{await work();setOverview(await client.overview(cyclotronId,classifier))}
    catch(failure){setError(describe(failure))}
    finally{setBusy(false)}
  }
  if(!overview)return <section className="cyclotron-held-out-report" aria-busy={busy}>
    <p className="cyclotron-held-out-note">{error??'Loading the held-out measurement…'}</p>
  </section>
  const bound=overview.requestBound
  return <div className="cyclotron-held-out-panel">
    <HeldOutReportView report={overview.latestReport} sets={overview.sets} classifierName={classifierName} busy={busy}
      requestBound={bound}
      onFreeze={()=>void act(()=>client.freeze(cyclotronId,classifier))}
      onEvaluate={overview.allowLive&&bound!=null?()=>void act(()=>client.evaluate(cyclotronId,bound,classifier)):undefined} />
    {error?<p role="alert" className="cyclotron-held-out-error">{error}</p>:null}
  </div>
}
