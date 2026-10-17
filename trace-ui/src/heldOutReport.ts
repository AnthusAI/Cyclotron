/**
 * cyclotron-held-out-report/v1: the classifier as its store was created versus the classifier active now,
 * both scored on one frozen held-out set. Mirrors src/decision_flywheel/schemas/cyclotron-held-out-report.v1.schema.json.
 * A Python spec checks that every schema field is named here.
 */
export type HeldOutVerdict='improved'|'worse'|'no-clear-change'|'too-few-labels'|'nothing-learned-yet'
export type HeldOutOutcome='gained'|'lost'|'bothRight'|'bothWrong'

/** Each item weighted by its inverse selection propensity. Precision and recall are for the positive label, or macro averages without one. */
export type HeldOutMetrics={
  balancedAccuracy:number|null
  accuracy:number|null
  precision:number|null
  recall:number|null
  recallByLabel:Record<string,number|null>
}

export type HeldOutEndpoint={version:number;refits:number;fingerprint:string;description:string;metrics:HeldOutMetrics}

export type HeldOutSetSummary={
  id:string
  frozenAt:string
  items:number
  labelCounts:Record<string,number>
  /** True when some item was reviewed with a selection probability below one. */
  weighted:boolean
  selection:string
}

export type HeldOutReport={
  schema:'cyclotron-held-out-report/v1'
  cyclotron:{id:string;classifier:string}
  evaluatedAt:string
  positiveLabel:string|null
  heldOutSet:HeldOutSetSummary&{
    scored:number
    scoredLabelCounts:Record<string,number>
    /** Frozen items since used for training, so not scored. */
    excludedSinceFrozen:number
    /** Frozen items whose label was later corrected; the frozen label is still used. */
    labelsChangedSinceFrozen:number
  }
  headline:{metric:'balancedAccuracy';name:string;definition:string;before:number|null;after:number|null;changePoints:number|null}
  before:HeldOutEndpoint
  after:HeldOutEndpoint
  change:{
    gained:number
    lost:number
    bothRight:number
    bothWrong:number
    /** Exact two-sided sign test on gained versus lost. */
    pValue:number
    significanceLevel:number
    minimumPerLabel:number
    verdict:HeldOutVerdict
    summary:string
  }
  cost:{newRequests:number;cachedRequests:number;promptTokens:number;outputTokens:number}
  items:{itemId:string;label:string;weight:number;before:string;after:string;outcome:HeldOutOutcome}[]
}

/** What heldOutOverview returns: the frozen sets, the latest report and what an evaluation may cost. */
export type HeldOutOverview={
  cyclotronId:string
  classifier:string|null
  sets:HeldOutSetSummary[]
  latestReport:HeldOutReport|null
  reportHistory:{evaluatedAt:string;setId:string;afterVersion:number;headline:HeldOutReport['headline'];verdict:HeldOutVerdict}[]
  /** The most new decision-model requests evaluating the latest set can make; null before a set is frozen. */
  requestBound:number|null
  allowLive:boolean
}

/** Narrow unknown JSON (for example a stored report) to a v1 report. */
export function isHeldOutReport(value:unknown):value is HeldOutReport{
  const report=value as Partial<HeldOutReport>|null
  return Boolean(report&&report.schema==='cyclotron-held-out-report/v1'&&report.headline&&report.before
    &&report.after&&report.change&&report.heldOutSet)
}

export type HeldOutGraphQL=<T>(query:string,variables:Record<string,unknown>)=>Promise<T>

export type HeldOutClient={
  overview:(cyclotronId:string,classifier?:string|null)=>Promise<HeldOutOverview>
  freeze:(cyclotronId:string,classifier?:string|null)=>Promise<HeldOutSetSummary>
  evaluate:(cyclotronId:string,confirmedRequestBound:number,classifier?:string|null)=>Promise<HeldOutReport>
}

/** Bind the held-out card to an application's authenticated GraphQL transport; the lab and embedded hosts serve the same two operations. */
export function createHeldOutClient(request:HeldOutGraphQL):HeldOutClient{
  const command='mutation($action:String!,$payload:JSON!){heldOutCommand(action:$action,payload:$payload)}'
  return {
    overview:async(cyclotronId,classifier=null)=>(await request<{heldOutOverview:HeldOutOverview}>(
      'query($id:ID!,$classifier:String){heldOutOverview(cyclotronId:$id,classifier:$classifier)}',
      {id:cyclotronId,classifier})).heldOutOverview,
    freeze:async(cyclotronId,classifier=null)=>(await request<{heldOutCommand:HeldOutSetSummary}>(command,
      {action:'freeze',payload:{cyclotronId,classifier}})).heldOutCommand,
    evaluate:async(cyclotronId,confirmedRequestBound,classifier=null)=>(await request<{heldOutCommand:HeldOutReport}>(command,
      {action:'evaluate',payload:{cyclotronId,classifier,confirmedRequestBound}})).heldOutCommand,
  }
}
