import {useCallback,useEffect,useRef,useState,type ReactNode} from 'react'
import {Activity, History, Plus, RefreshCw, Undo2} from 'lucide-react'
import {Button} from '@/components/ui/button'
import {Card,CardContent,CardHeader,CardTitle} from '@/components/ui/card'
import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import {LabelCard as LegacyLabelCard,type CurrentItem} from './LabelCard'
import {graphql,subscribeEvents,type TraceEvent} from './graphql'
import {Catalog} from './Catalog'
import {AppDrawer} from './AppDrawer'
import {ScorecardCatalog} from './ScorecardCatalog'
import {MultiLabelCard,type BatchItem} from './MultiLabelCard'
import {ClassifierMetrics,type MetricClassifier} from './ClassifierMetrics'
import {SubmissionFeedback,type LabelSubmission} from './SubmissionFeedback'
import {ScorecardVersions} from './ScorecardVersions'
import {ReplayControls} from './ReplayControls'
import {PendingCommandIds} from './commandIdentity'
import {ExchangeDetail} from './ExchangeDetail'
import {CyclotronBrand} from './CyclotronBrand'
import {MatchedComparison,MatchedComparisonResult,type ComparisonResult} from './MatchedComparison'
import {ComparisonResume} from './ComparisonResume'
import {FeedbackControls} from './FeedbackControls'
import {OptimizationStatus,latestOptimizationActivity} from './OptimizationStatus'
import {MobileNavigation} from './MobileNavigation'
import {navigationItems} from './navigation'
export {TraceDetail} from './TraceDetail'

type Run={labelingAccess?:{allowed:boolean;reason:string|null};id:string;name:string;mode:string;status:string;createdAt:string;config:{input_mode?:string;scorecard_id?:string;selection_policy?:{primary:string;secondary?:string};max_requests?:number;backfill_count?:number;classifiers?:MetricClassifier[]};counts:{cycles:number;predictions:number;labels:number;optimizations:number}}
type Job={id:string;kind:string;status:string;result:Record<string,unknown>|null}
const RUNS='{runs{id name mode status createdAt config counts} capabilities{liveEnabled itemCount}}'
const DETAILS='query($id:ID!){run(runId:$id){id name mode status createdAt config counts labelingAccess} currentItem(runId:$id) jobs(runId:$id){id kind status result}}'
const locationState=()=>new URLSearchParams(window.location.hash.slice(1))
const routeSection=()=>{const value=locationState().get('section');return value==='classifiers'||value==='scorecards'?'scorecards':value==='items'?'items':'optimizations'}
function LabelCard(props:{current:CurrentItem;busy:boolean;readOnly?:boolean;onSubmit:(kind:string,payload:Record<string,unknown>)=>void;classifiers?:MetricClassifier[];events?:TraceEvent[];footer?:ReactNode}){
  if('classifiers' in props.current.prediction){
    const current=props.current as unknown as BatchItem
    return <MultiLabelCard {...props} current={current} names={Object.fromEntries(Object.entries(current.prediction.classifiers).map(([id,result])=>[id,(result as {name?:string}).name??id]))} />
  }
  return <><LegacyLabelCard {...props} busy={props.busy||props.readOnly===true}/>{props.footer}</>
}

function RunWorkspaceSkeleton(){
  return <main data-testid="run-workspace-skeleton" aria-busy="true" aria-label="Loading run" className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 p-4">
    <div className="flex items-center justify-between gap-3"><div className="space-y-2"><div className="h-7 w-52 animate-pulse rounded-md bg-muted" /><div className="h-3 w-80 max-w-[75vw] animate-pulse rounded bg-muted" /></div><div className="flex gap-2"><div className="h-9 w-20 animate-pulse rounded-lg bg-muted" /><div className="h-9 w-24 animate-pulse rounded-lg bg-muted" /></div></div>
    <div className="h-8 w-36 animate-pulse rounded-lg bg-muted" />
    <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(300px,420px)]"><section className="min-h-0 rounded-xl border border-border bg-card p-5"><div className="h-5 w-48 animate-pulse rounded bg-muted" /><div className="mt-5 space-y-3"><div className="h-4 w-full animate-pulse rounded bg-muted" /><div className="h-4 w-11/12 animate-pulse rounded bg-muted" /><div className="h-4 w-4/5 animate-pulse rounded bg-muted" /></div><div className="mt-8 h-40 animate-pulse rounded-lg bg-muted/70" /></section><aside className="hidden rounded-xl border border-border bg-card p-5 lg:block"><div className="h-4 w-32 animate-pulse rounded bg-muted" /><div className="mt-5 space-y-3"><div className="h-16 animate-pulse rounded-lg bg-muted/70" /><div className="h-16 animate-pulse rounded-lg bg-muted/70" /><div className="h-16 animate-pulse rounded-lg bg-muted/70" /></div></aside></div>
  </main>
}


export function RunTimeline({runId,revision,classifierId}:{runId:string;revision:number|string;classifierId?:string}){
  const [loading,setLoading]=useState(true)
  const url=`/runs/${runId}/timeline?revision=${encodeURIComponent(revision)}${classifierId?`&classifier_id=${encodeURIComponent(classifierId)}`:''}`
  useEffect(()=>setLoading(true),[url])
  return <div className="relative flex min-h-0 flex-1">
    <iframe key={url} title="Run timeline and event inspector" src={url} onLoad={()=>setLoading(false)} className="min-h-0 w-full flex-1 rounded-xl border border-border" />
    {loading?<div role="status" className="absolute inset-0 flex items-center justify-center rounded-xl bg-background text-sm text-muted-foreground">Loading timeline…</div>:null}
  </div>
}

export function WebApp(){
  const [commandIds]=useState(()=>new PendingCommandIds())
  const [section,setSection]=useState<'scorecards'|'items'|'optimizations'>(routeSection)
  const [runs,setRuns]=useState<Run[]>([]),[selected,setSelected]=useState(()=>locationState().get('run')??''),[run,setRun]=useState<Run|null>(null)
  const [current,setCurrent]=useState<CurrentItem|null>(null),[jobs,setJobs]=useState<Job[]>([]),[events,setEvents]=useState<TraceEvent[]>([])
  const [view,setView]=useState<'timeline'|'label'>(()=>locationState().get('view')==='label'?'label':'timeline'),[detail,setDetail]=useState<TraceEvent|null>(null),[revision,setRevision]=useState(0)
  const [failure,setFailure]=useState({message:'',runId:'',section:''})
  const error=failure.runId===selected&&failure.section===section?failure.message:''
  const setError=useCallback((message:string)=>setFailure({message,runId:selected,section}),[selected,section])
  const [stream,setStream]=useState('Connecting'),[sending,setSending]=useState(false)
  const [submission,setSubmission]=useState<LabelSubmission|null>(null)
  const [historyOpen,setHistoryOpen]=useState(false),[activityOpen,setActivityOpen]=useState(false)
  const [comparisonOpen,setComparisonOpen]=useState(false)
  const [historySearch,setHistorySearch]=useState(''),[historyScorecard,setHistoryScorecard]=useState('')
  const [activityFilter,setActivityFilter]=useState('all')
  const [capabilities,setCapabilities]=useState({liveEnabled:false,itemCount:0})
  const [creating,setCreating]=useState(false),[name,setName]=useState(''),[objective,setObjective]=useState('inherit'),[confirmed,setConfirmed]=useState(false)
  const [maxRequests,setMaxRequests]=useState(500),[maxOptimizer,setMaxOptimizer]=useState(1000),[token,setToken]=useState('')
  const [catalog,setCatalog]=useState<{classifiers:{id:string;name:string}[];itemLists:{id:string;name:string;count:number}[];scorecardDefinitions?:{id:string;name:string;revision:number}[]}>({classifiers:[],itemLists:[]})
  const [classifierIds,setClassifierIds]=useState<string[]>([]),[itemList,setItemList]=useState('')
  const [sessionScorecard,setSessionScorecard]=useState('')
  const [sessionMode,setSessionMode]=useState<'interactive'|'replay'>('interactive'),[replaySource,setReplaySource]=useState('')
  const [timelineClassifier,setTimelineClassifier]=useState(()=>locationState().get('classifier')??'')
  const timelineClassifiers=run?.id===selected?run.config.classifiers:undefined
  const invalidTimelineClassifier=!!timelineClassifiers&&!!timelineClassifier&&!timelineClassifiers.some(classifier=>classifier.id===timelineClassifier)
  const resolvedTimelineClassifier=timelineClassifiers?.find(classifier=>classifier.id===timelineClassifier)?.id??timelineClassifiers?.[0]?.id??''
  const firstRoute=useRef(true)
  useEffect(()=>{
    const restore=()=>{const route=locationState();setSection(routeSection());setSelected(route.get('run')??'');setView(route.get('view')==='label'?'label':'timeline');setTimelineClassifier(route.get('classifier')??'')}
    window.addEventListener('popstate',restore);window.addEventListener('hashchange',restore)
    return()=>{window.removeEventListener('popstate',restore);window.removeEventListener('hashchange',restore)}
  },[])
  useEffect(()=>{if(creating||historyOpen)graphql<typeof catalog>('{classifiers itemLists scorecardDefinitions}').then(value=>{
    setCatalog(value)
    setSessionScorecard(previous=>previous||value.scorecardDefinitions?.[0]?.id||'')
    setItemList(previous=>previous||value.itemLists[0]?.id||'')
    setClassifierIds(previous=>previous.length?previous:value.classifiers.slice(0,1).map(classifier=>classifier.id))
  }).catch(e=>setError(e.message))},[creating,historyOpen,setError])
  const refresh=useCallback(async()=>{
    const result=await graphql<{runs:Run[];capabilities:{liveEnabled:boolean;itemCount:number}}>(RUNS)
    setRuns(result.runs);setCapabilities(result.capabilities)
    setSelected(previous=>result.runs.some(run=>run.id===previous)?previous:
      (result.runs.find(run=>run.counts.optimizations>0)??result.runs[0])?.id??'')
  },[])
  useEffect(()=>{
    const route=new URLSearchParams(window.location.hash.slice(1))
    if(invalidTimelineClassifier)setTimelineClassifier('')
    route.set('run',selected);route.set('view',view);route.set('section',section)
    if(timelineClassifier&&!invalidTimelineClassifier)route.set('classifier',timelineClassifier)
    else route.delete('classifier')
    const hash=`#${route}`
    if((selected||section!=='optimizations')&&window.location.hash!==hash){
      if(firstRoute.current||invalidTimelineClassifier)window.history.replaceState(null,'',hash)
      else window.history.pushState(null,'',hash)
    }
    firstRoute.current=false
  },[selected,view,section,timelineClassifier,invalidTimelineClassifier])
  useEffect(()=>{refresh().catch(e=>setError(e.message))},[refresh,setError])
  useEffect(()=>{
    if(!selected||section!=='optimizations'){setRun(null);return}
    let cancelled=false,unsubscribe:(()=>void)|undefined
    setRun(null);setEvents([]);setDetail(null);setCurrent(null);setJobs([]);setStream('Connecting')
    const load=async()=>{
      if(view==='timeline'){
        const result=await graphql<{run:{eventCursor:number}}>('query($id:ID!){run(runId:$id){eventCursor}}',{id:selected})
        if(!cancelled)unsubscribe=subscribeEvents(selected,result.run?.eventCursor??0,event=>setEvents(previous=>[...previous,event]),setStream)
        return
      }
      let cursor=0
      const collected:TraceEvent[]=[]
      while(!cancelled){
        const result=await graphql<{events:TraceEvent[]}>('query($id:ID!,$after:Int!){events(runId:$id,after:$after){sequence sourceId payload}}',{id:selected,after:cursor})
        if(!result.events.length)break
        collected.push(...result.events);cursor=result.events.at(-1)!.sequence
      }
      if(cancelled)return
      setEvents(collected)
      unsubscribe=subscribeEvents(selected,cursor,event=>setEvents(previous=>[...previous,event]),setStream)
    }
    load().catch(e=>{if(!cancelled)setError(e.message)})
    const state=async()=>{
      try{const result=await graphql<{run:Run;currentItem:CurrentItem|null;jobs:Job[]}>(DETAILS,{id:selected})
        if(!cancelled){setRun(result.run);setCurrent(result.currentItem);setJobs(result.jobs)}}catch(e){if(!cancelled)setError((e as Error).message)}
    }
    state();const timer=setInterval(state,1000)
    return ()=>{cancelled=true;unsubscribe?.();clearInterval(timer)}
  },[selected,section,view,setError])
  const submittedJob=jobs.find(job=>job.id===submission?.jobId)
  const awaitingLabels=submission?.runId===selected&&!submission.unconfirmed&&
    (!submittedJob||['pending','running'].includes(submittedJob.status))
  const busy=sending||awaitingLabels||jobs.some(job=>['pending','running'].includes(job.status))
  const labelingDisabled=busy||run?.labelingAccess?.allowed===false
  const submit=async(kind:string,payload:Record<string,unknown>={})=>{
    if(run?.labelingAccess?.allowed===false){setError(run.labelingAccess.reason??'This run is read-only.');return null}
    setSending(true);setError('')
    if(kind==='label')setSubmission({runId:selected,count:Array.isArray(payload.labels)?payload.labels.length:1})
    else setSubmission(null)
    try{const identity=commandIds.identity(selected,kind,payload)
      const result=await graphql<{submitCommand:Job}>('mutation($id:ID!,$request:String!,$kind:String!,$payload:JSON!){submitCommand(runId:$id,requestId:$request,kind:$kind,payload:$payload){id kind status result}}',
      {id:selected,request:identity,kind,payload})
      if(typeof result.submitCommand?.id!=='string')throw new Error('Command acknowledgement unavailable')
      commandIds.acknowledge(identity)
      setJobs(previous=>[result.submitCommand,...previous])
      if(kind==='label')setSubmission(previous=>previous?{...previous,jobId:result.submitCommand.id}:null)
      if(kind==='skip')setCurrent(null)
      return result.submitCommand
    }catch(e){setError((e as Error).message);if(kind==='label')setSubmission(previous=>previous?{...previous,unconfirmed:true}:null);return null}finally{setSending(false)}
  }
  const resumeFeedback=async(jobId:string)=>{
    setSending(true);setError('')
    try{const result=await graphql<{resumeFeedbackCommand:Job}>('mutation($id:ID!,$job:ID!){resumeFeedbackCommand(runId:$id,jobId:$job){id kind status result}}',{id:selected,job:jobId})
      setJobs(previous=>[result.resumeFeedbackCommand,...previous.filter(job=>job.id!==jobId)])
      return result.resumeFeedbackCommand
    }catch(e){setError((e as Error).message);return null}finally{setSending(false)}
  }
  const create=async()=>{
    setSending(true);setError('')
    const selection_policy=objective==='recall'?{primary:'recall',secondary:'accuracy',positive_class:'include'}:{primary:objective,positive_class:'include'}
    try{const result=await graphql<{createRun?:Run;createReplay?:Run}>(sessionMode==='replay'?'mutation($name:String!,$config:JSON!,$confirmed:Boolean!,$source:ID!){createReplay(name:$name,sourceRunId:$source,config:$config,confirmed:$confirmed){id name mode status createdAt config}}':'mutation($name:String!,$config:JSON!,$confirmed:Boolean!){createRun(name:$name,mode:"live",config:$config,confirmed:$confirmed){id name mode status createdAt config}}',
      {name,confirmed,source:replaySource,config:{...(objective==='inherit'?{}:{selection_policy}),max_requests:maxRequests,max_optimizer_calls:maxOptimizer,...(itemList?{...(sessionScorecard?{scorecard_id:sessionScorecard}:{classifier_ids:classifierIds}),item_list_id:itemList}:{})}})
      const created=result.createReplay??result.createRun!
      await refresh();setSelected(created.id);setCreating(false);setView(sessionMode==='replay'?'timeline':'label')
    }catch(e){setError((e as Error).message)}finally{setSending(false)}
  }
  const recent=events.filter(event=>latestOptimizationActivity([event])||['decision-request','decision-response','human-feedback','cycle-metrics'].includes(String(event.payload.kind))).filter(event=>activityFilter==='all'||activityFilter==='optimizer'&&String(event.payload.kind).startsWith('optimizer-')||activityFilter==='decision'&&String(event.payload.kind).startsWith('decision-')).slice(-40).reverse()
  return <div data-labeling-view={view==='label'&&section==='optimizations'} className="app-shell bg-background text-foreground">
<header data-testid="main-application-header" className="app-header flex items-center justify-between gap-3 border-b border-border bg-background px-5 py-3"><CyclotronBrand /><nav aria-label="Main navigation" className="hidden gap-1 sm:flex">{navigationItems.map(item=><Button key={item.id} variant={section===item.id?'secondary':'ghost'} onClick={()=>setSection(item.id)}>{item.label}</Button>)}</nav><MobileNavigation section={section} onNavigate={setSection}/></header>
    {section!=='optimizations'?(section==='scorecards'?<ScorecardCatalog/>:<Catalog section="items" />):
    <div className="flex min-h-0 flex-1">
      <AppDrawer title="Run history" open={historyOpen} onOpenChange={setHistoryOpen}><div className="flex items-center justify-between"><p className="flex items-center gap-2 text-sm font-semibold"><History className="size-4" />Run history</p><Button variant="ghost" size="icon" aria-label="Refresh run history" onClick={()=>refresh().catch(e=>setError(e.message))}><RefreshCw /></Button></div><Button variant="outline" disabled={!capabilities.liveEnabled} onClick={()=>{setCreating(!creating);setHistoryOpen(false)}}><Plus />New run</Button>
        <Label htmlFor="history-search">Search run history</Label><Input id="history-search" value={historySearch} onChange={e=>setHistorySearch(e.target.value)} placeholder="Run name"/>
        <Label htmlFor="history-scorecard">Scorecard filter</Label><NativeSelect id="history-scorecard" value={historyScorecard} onChange={e=>setHistoryScorecard(e.target.value)}><NativeSelectOption value="">All scorecards</NativeSelectOption>{catalog.scorecardDefinitions?.map(card=><NativeSelectOption key={card.id} value={card.id}>{card.name}</NativeSelectOption>)}</NativeSelect>
        {runs.filter(item=>item.name.toLowerCase().includes(historySearch.toLowerCase())&&(!historyScorecard||item.config.scorecard_id===historyScorecard)).map(item=><button key={item.id} aria-current={selected===item.id?'true':undefined} onClick={()=>{setHistoryOpen(false);setSelected(item.id);setView(item.mode==='live'&&item.config.input_mode!=='replay'?'label':'timeline');setRevision(0)}} className={`rounded-lg border p-3 text-left ${selected===item.id?'border-primary bg-accent':'border-transparent hover:bg-accent/50'}`}><p className="text-sm font-medium">{item.name}</p><p className="mt-1 text-xs text-muted-foreground">{item.mode==='live'&&item.config.input_mode!=='replay'?'Interactive':'Replay'} · {item.counts.cycles} cycles</p><p className="mt-1 text-xs text-muted-foreground">{item.counts.labels} labels · {item.counts.optimizations} optimizer calls</p><p className="mt-1 text-xs text-muted-foreground">{new Date(item.createdAt).toLocaleDateString()}</p></button>)}
        {!runs.length?<p className="text-xs leading-relaxed text-muted-foreground">No runs yet. Import a recording through the API or create a live run.</p>:null}
      </AppDrawer>
      <main className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 p-4">
        {timelineClassifiers?.length?<div className="flex items-center gap-3"><Label htmlFor="timeline-classifier">Inspect classifier</Label><NativeSelect id="timeline-classifier" value={resolvedTimelineClassifier} onChange={event=>setTimelineClassifier(event.target.value)}>{timelineClassifiers.map(classifier=><NativeSelectOption key={classifier.id} value={classifier.id}>{classifier.name}</NativeSelectOption>)}</NativeSelect><p className="text-xs text-muted-foreground">Each classifier has its own learning history.</p></div>:null}
        <AppDrawer title="New optimization run" side="right" open={creating} onOpenChange={setCreating}><Card><CardHeader><CardTitle className="text-base">Classifiers and items for this session</CardTitle></CardHeader><CardContent className="space-y-3"><Label htmlFor="session-mode">Session type</Label><NativeSelect id="session-mode" value={sessionMode} onChange={event=>setSessionMode(event.target.value as typeof sessionMode)}><NativeSelectOption value="interactive">Interactive labeling</NativeSelectOption><NativeSelectOption value="replay">Replay existing feedback</NativeSelectOption></NativeSelect>{sessionMode==='replay'?<><Label htmlFor="replay-source">Source run</Label><NativeSelect id="replay-source" value={replaySource} onChange={event=>setReplaySource(event.target.value)}><NativeSelectOption value="">Choose source feedback</NativeSelectOption>{runs.filter(row=>row.config.classifiers?.length&&row.counts.labels>0).map(row=><NativeSelectOption key={row.id} value={row.id}>{row.name}</NativeSelectOption>)}</NativeSelect></>:null}<Label htmlFor="session-scorecard">Scorecard</Label><NativeSelect id="session-scorecard" value={sessionScorecard} onChange={event=>setSessionScorecard(event.target.value)}><NativeSelectOption value="">Independent classifiers (legacy)</NativeSelectOption>{catalog.scorecardDefinitions?.map(card=><NativeSelectOption key={card.id} value={card.id}>{card.name} · revision {card.revision}</NativeSelectOption>)}</NativeSelect><Label htmlFor="session-list">Item list</Label><NativeSelect id="session-list" value={itemList} onChange={event=>setItemList(event.target.value)}><NativeSelectOption value="">Legacy arXiv session</NativeSelectOption>{catalog.itemLists.map(list=><NativeSelectOption key={list.id} value={list.id}>{list.name} · {list.count} items</NativeSelectOption>)}</NativeSelect>{itemList&&!sessionScorecard?<fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">Classifiers (shared request)</legend>{catalog.classifiers.map(classifier=><Label key={classifier.id} className="flex gap-2"><input type="checkbox" checked={classifierIds.includes(classifier.id)} onChange={event=>setClassifierIds(previous=>event.target.checked?[...previous,classifier.id]:previous.filter(id=>id!==classifier.id))} />{classifier.name}</Label>)}{!classifierIds.length?<p className="text-sm text-muted-foreground">Select at least one classifier before creating the session.</p>:null}</fieldset>:null}</CardContent></Card>
        <Card className="gap-2"><CardHeader><CardTitle className="text-base">{sessionMode==='replay'?'Start a fresh replay':'Start an interactive run'}</CardTitle></CardHeader><CardContent className="flex flex-wrap items-end gap-3"><div className="space-y-1"><Label htmlFor="run-name">Name</Label><Input id="run-name" value={name} onChange={event=>setName(event.target.value)} placeholder="Recall / accuracy experiment" /></div><div className="space-y-1"><Label htmlFor="run-objective">Objective</Label><NativeSelect id="run-objective" value={objective} onChange={event=>setObjective(event.target.value)}><NativeSelectOption value="inherit">Scorecard / application defaults</NativeSelectOption><NativeSelectOption value="f1">F1 (configured positive class)</NativeSelectOption><NativeSelectOption value="recall">Recall / accuracy guard</NativeSelectOption><NativeSelectOption value="accuracy">Accuracy</NativeSelectOption></NativeSelect></div><div className="w-28 space-y-1"><Label htmlFor="max-requests">Request limit</Label><Input id="max-requests" type="number" min={1} value={maxRequests} onChange={event=>setMaxRequests(Number(event.target.value))} /></div><div className="w-28 space-y-1"><Label htmlFor="max-optimizer">Optimizer limit</Label><Input id="max-optimizer" type="number" min={1} value={maxOptimizer} onChange={event=>setMaxOptimizer(Number(event.target.value))} /></div><Label className="flex items-center gap-2"><input type="checkbox" checked={confirmed} onChange={event=>setConfirmed(event.target.checked)} />Authorize paid calls within these limits</Label><Button disabled={sending||!confirmed||(sessionMode==='replay'&&(!replaySource||!sessionScorecard))||!name.trim()||(itemList?(!sessionScorecard&&!classifierIds.length):capabilities.itemCount===0)} onClick={create}>Create run</Button></CardContent></Card></AppDrawer>
        {error?<div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm">{error}</div>:null}
        {error==='Authentication required'?<form className="flex gap-2" onSubmit={async event=>{event.preventDefault();const response=await fetch('/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});setToken('');if(response.ok){setError('');refresh()}else setError('Authentication required')}}><Input type="password" aria-label="Workspace access token" value={token} onChange={event=>setToken(event.target.value)} /><Button type="submit">Connect</Button></form>:null}
        {run?<><div className="flex flex-wrap items-center justify-between gap-3"><div><h1 className="text-xl font-semibold">{run.name}</h1><p className="mt-1 text-xs text-muted-foreground">{run.counts.cycles} cycles · {run.counts.labels} labels · {run.counts.optimizations} optimizer calls · <span role="status">{busy?'Working':run.status}</span> · stream {stream}</p></div><div className="flex gap-2"><Button variant={view==='timeline'?'secondary':'ghost'} onClick={()=>setView('timeline')}>Timeline</Button><Button variant={view==='label'?'secondary':'ghost'} disabled={run.mode!=='live'} onClick={()=>setView('label')}>Label items</Button><Button variant="outline" onClick={()=>{setHistoryScorecard(run.config.scorecard_id??'');setHistoryOpen(true)}}><History/>Run history</Button>{view==='label'?<Button variant="outline" onClick={()=>setActivityOpen(true)}><Activity/>Optimizer activity</Button>:null}</div></div>
        {run.config.input_mode==='replay'?<ReplayControls key={run.id} busy={busy} readOnly={run.labelingAccess?.allowed===false} finished={jobs[0]?.result?.finished===true} failed={jobs[0]?.status==='failed'||jobs[0]?.status==='interrupted'} onAdvance={()=>submit('replay-next')} />:null}
        <ScorecardVersions runId={run.id} busy={busy} onSelect={id=>{setSelected(id);setView('label')}} />
        {run.labelingAccess?.allowed===false?<p role="status" className="shrink-0 rounded-md border bg-muted px-3 py-2 text-sm">{run.labelingAccess.reason??'This run is read-only.'}</p>:null}
        {run.config.input_mode==='comparison'?<ComparisonResume key={`${run.id}:${jobs[0]?.id}`} runId={run.id} ceiling={run.config.max_requests??400} busy={busy} enabled={capabilities.liveEnabled} status={jobs[0]?.status??run.status} />:null}
        <div className="shrink-0"><Button variant="outline" size="sm" disabled={!capabilities.liveEnabled||busy} onClick={()=>setComparisonOpen(true)}>Compare runs</Button></div>
        {comparisonOpen?<MatchedComparison runs={runs.filter(row=>row.config.classifiers?.length)} initialRunId={run.id} onClose={()=>setComparisonOpen(false)} onCreated={id=>{setComparisonOpen(false);setSelected(id);setView('timeline');void refresh().catch(e=>setError(e.message))}} />:null}
        {view==='label'&&!run.config.classifiers?.length?<ClassifierMetrics classifiers={[{id:'legacy',name:'Classifier',config:{classes:[{label:'include',role:'positive'}]}}]} events={events} compact />:null}
        {view==='label'?<OptimizationStatus events={events} classifiers={run.config.classifiers??[]} jobs={jobs} onInspect={event=>{setDetail(event);setActivityOpen(true)}}/>:null}
        {view==='label'&&run.config.classifiers?.length?<FeedbackControls key={run.id} runId={run.id} classifiers={run.config.classifiers} events={events} jobs={jobs} busy={labelingDisabled} replay={run.config.input_mode==='replay'} onSubmit={submit} onResume={resumeFeedback}/>:null}
          {run.config.input_mode==='comparison'?<MatchedComparisonResult key={run.id} runId={run.id} status={jobs[0]?.status??run.status} result={jobs.find(job=>job.kind==='matched-evaluate'&&job.status==='completed')?.result as ComparisonResult|undefined} />:view==='timeline'?<div className="flex min-h-0 flex-1 flex-col gap-2"><div className="flex items-center justify-between"><p className="text-xs text-muted-foreground">Click a cycle or event to inspect its request, response, and configuration.</p><Button variant="outline" size="sm" onClick={()=>setRevision(events.at(-1)?.sequence??0)}><RefreshCw />Load latest events</Button></div><RunTimeline key={selected} runId={selected} revision={run.config.input_mode==='replay'?(jobs.find(job=>job.kind==='replay-next'&&job.status==='completed')?.id??revision):revision} classifierId={resolvedTimelineClassifier||undefined} /></div>:
          <div data-labeling-content className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-y-auto lg:grid-cols-[minmax(0,1fr)_minmax(300px,420px)]"><section className="space-y-3"><div className="flex flex-wrap gap-2">{run.status!=='completed'||current?<Button disabled={labelingDisabled} onClick={()=>submit('prepare')}>{current?'Refresh prediction':'Prepare next item'}</Button>:null}{!run.config.classifiers?.length?<Button variant="outline" disabled={labelingDisabled} onClick={()=>submit('undo')}><Undo2 />Undo last review</Button>:null}</div>{current?<LabelCard key={current.prediction.presentation_id} current={current} busy={busy} readOnly={run.labelingAccess?.allowed===false} onSubmit={submit} classifiers={run.config.classifiers} events={events} footer={submission?.runId===selected?<SubmissionFeedback submission={submission} jobs={jobs} compact />:run.config.backfill_count && run.counts.cycles<=run.config.backfill_count?<p className="text-xs text-muted-foreground">Historical item {run.counts.cycles} of {run.config.backfill_count} · Provide missing labels</p>:null} />:<Card><CardContent className="space-y-3">{run.status==='completed'?<><h2 className="text-lg font-semibold">Session complete</h2><p className="text-sm text-muted-foreground">All items in this run have been reviewed. Inspect the timeline or start another run from history.</p></>:<p className="text-sm text-muted-foreground">{busy?'Processing this cycle. Events stream on the right.':'Prepare an item to get its prediction before voting.'}</p>}{submission?.runId===selected?<SubmissionFeedback submission={submission} jobs={jobs}/>:null}</CardContent></Card>}{jobs[0]?.status==='failed'||jobs[0]?.status==='interrupted'?<p role="alert" className="text-sm text-destructive">Last command {jobs[0].status}. State retained; no automatic paid retry.</p>:null}</section><AppDrawer title="Optimizer activity" side="right" open={activityOpen} onOpenChange={setActivityOpen}><div className="flex items-center gap-2 text-sm font-semibold"><Activity className="size-4" />Live engine activity</div><Label htmlFor="activity-type">Activity type</Label><NativeSelect id="activity-type" value={activityFilter} onChange={e=>{setActivityFilter(e.target.value);setDetail(null)}}><NativeSelectOption value="all">All activity</NativeSelectOption><NativeSelectOption value="optimizer">Optimizer calls</NativeSelectOption><NativeSelectOption value="decision">Decision calls</NativeSelectOption></NativeSelect>{!detail?recent.map(event=><button key={event.sequence} onClick={()=>setDetail(event)} className="flex w-full items-center justify-between rounded-lg border border-border bg-card px-3 py-2 text-left text-xs"><span className="capitalize">{String(event.payload.kind).replaceAll('-',' ')}</span><span className="font-mono text-muted-foreground">{String(event.payload.cycle_number??'')}</span></button>):null}{detail?<><Button variant="outline" onClick={()=>setDetail(null)}>Back to activity</Button><ExchangeDetail event={detail} events={events} /></>:<p className="text-xs text-muted-foreground">Select a streamed model request or response to inspect its exact content.</p>}</AppDrawer></div>}
        </>:selected?<RunWorkspaceSkeleton/>:<div className="flex flex-1 items-center justify-center"><div className="max-w-sm text-center"><h1 className="text-xl font-semibold">A history you can inspect</h1><p className="mt-2 text-sm leading-relaxed text-muted-foreground">Choose a recorded run, or start a new labeling session. Every optimization exchange is stored by the API.</p></div></div>}
      </main>
    </div>}
  </div>
}
