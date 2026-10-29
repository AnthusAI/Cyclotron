/** The lab's read-only decisions API for a standing run (src/decision_flywheel/lab_decisions.py). */

export type ReviewRouting={selected:boolean|null;reason:string|null;propensity:number|null;detail:string|null}
export type OutputDecision={publication:string|null;label:string|null;confidence:number|null;probabilities:Record<string,number>|null;version:number|null;review:ReviewRouting;humanLabel?:string|null}
export type DecisionRow={position:number;jobId:string;decisionId:string;decidedAt:string;item:{id:string;revision:number|null;fingerprint:string|null;title?:string;url?:string;source?:string};outputs:Record<string,OutputDecision>}
export type DecisionsPage={runId:string;decisions:DecisionRow[];examined:{from:number;to:number};next:string|null;total:number}
export type StandingOutput={id:string;name:string;question:string|null;classes:string[]}
export type StandingRun={runId:string;name:string;publications:{id:string;outputs:string[]}[];outputs:StandingOutput[]}
export type Health={runId:string;generatedAt:string;decisions:{lastHour:number;lastDay:number};secondsBetweenDecisions:{window:string;samples:number;median:number|null;p90:number|null};queue:{pending:number;running:number};failed:{lastDay:number};latestPosition:number}
export type TraceRecord=Record<string,unknown>&{event_id?:number;kind?:string;created_at?:string}
export type ItemOutput=OutputDecision&{name:string;model:string|null;composition:Record<string,unknown>|null;prediction:TraceRecord|null;exchange:string|null}
export type LabelRecord={output:string;classifierRevision:number;itemRevision:number;label:string;comment:string;createdAt:string;retracted:boolean;requestId:string;source:{kind:'newsroom';publication:string|null;site:string|null;editor:string|null;asserted:boolean;principal:string|null}|{kind:'lab'}}
export type Revision={revision:number;fingerprint:string;occurredAt:string|null;importedAt:string|null;values:Record<string,unknown>;provenance:null|{kind:string;principal?:string;session?:string;personId?:string;requestId:string;jobId:string}}
export type ItemDetail={runId:string;item:{id:string;revision:number|null;values:Record<string,unknown>};revisions:Revision[];decision:{jobId:string;position:number;decisionId:string;decidedAt:string;itemRevision:number|null;outputs:Record<string,ItemOutput>};exchanges:Record<string,{request:TraceRecord|null;response:TraceRecord|null;outputs:string[]}>;labels:LabelRecord[]}

export type DecisionFilters={output:string;label:string;minConfidence:string;maxConfidence:string;needsReview:''|'true'|'false';labeled:''|'true'|'false';source:string;since:string;until:string}
export const NO_FILTERS:DecisionFilters={output:'',label:'',minConfidence:'',maxConfidence:'',needsReview:'',labeled:'',source:'',since:'',until:''}

export class LabRequestError extends Error{
  readonly status:number
  constructor(message:string,status:number){super(message);this.status=status}
}

/** The account the lab reads: an explicit ``?account=`` on the page, else the session's selected account. */
export async function labAccount():Promise<string>{
  const pinned=new URLSearchParams(window.location.search).get('account')
  if(pinned)return pinned
  const response=await fetch('/api/session',{credentials:'same-origin'})
  if(!response.ok)throw new LabRequestError('Sign in to the lab to see decisions.',response.status)
  const session=await response.json() as {authenticated?:boolean;selectedAccountId?:string|null}
  if(!session.authenticated)throw new LabRequestError('Sign in to the lab to see decisions.',401)
  if(!session.selectedAccountId)throw new LabRequestError('Choose an account to see its decisions.',403)
  return session.selectedAccountId
}

/** Only filled-in filters are sent; time inputs (local) are sent as ISO instants. */
export function filterQuery(filters:DecisionFilters):Record<string,string>{
  const query:Record<string,string>={}
  for(const [key,value] of Object.entries(filters)){
    if(!value)continue
    if(key==='since'||key==='until'){const at=new Date(value);if(!Number.isNaN(at.getTime()))query[key]=at.toISOString();continue}
    if(key==='minConfidence'||key==='maxConfidence'){query[key]=String(Number(value)/100);continue}
    query[key]=value
  }
  return query
}

export async function labGet<T>(account:string,path:string,query:Record<string,string>={}):Promise<T>{
  const search=new URLSearchParams(query).toString()
  const response=await fetch(`/api/lab/${path}${search?`?${search}`:''}`,{credentials:'same-origin',headers:{'X-Cyclotron-Account':account,Accept:'application/json'}})
  if(response.status===401)throw new LabRequestError('Sign in to the lab to see decisions.',401)
  if(!response.ok){
    let detail='The lab could not load this view.'
    try{const body=await response.json() as {detail?:unknown};if(typeof body.detail==='string')detail=body.detail}catch{/* keep the generic message */}
    throw new LabRequestError(detail,response.status)
  }
  return await response.json() as T
}

export const percent=(value:number|null|undefined)=>value==null?'—':`${Math.round(value*100)}%`
export const seconds=(value:number|null|undefined)=>value==null?'—':value<10?`${value.toFixed(1)} s`:`${Math.round(value)} s`
export function when(value:string|null|undefined){
  if(!value)return '—'
  const at=new Date(value)
  return Number.isNaN(at.getTime())?value:at.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'})
}
/** Whether an output still waits for a person: routed to review, and no human label yet. */
export const needsReview=(output:OutputDecision)=>Boolean(output.review.selected)&&!output.humanLabel
